"""Regression tests for the record_full_io proxy-log split.

Covers the two failure modes that left runs without ``agent/requests.jsonl``
despite an intact run-level log: trials living outside the default
``results/<job_slug>`` layout, and the split being skipped when a run exits
non-zero after evaluation already finished.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from workbuddy_bench.runner.split_proxy_log import split_proxy_log

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTANCE_ID = "myjob-4242-1700000000"
MODEL_ROUTE = f"{INSTANCE_ID}__model"


def _manifest(tmp_path: Path, **overrides) -> Path:
    data = {
        "instance_id": INSTANCE_ID,
        "job_slug": "myjob",
        "model_route": MODEL_ROUTE,
        "model_connection": "local_proxy",
        "record_full_io": True,
    }
    data.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    return path


def _write_log(log_dir: Path, records: list[dict]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    src = log_dir / f"{INSTANCE_ID}.jsonl"
    owned_records = []
    for record in records:
        owned = dict(record)
        owned.setdefault("route", MODEL_ROUTE)
        owned_records.append(owned)
    src.write_text("".join(json.dumps(r) + "\n" for r in owned_records))
    return src


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_splits_into_custom_jobs_dir_from_runtime_config(tmp_path: Path) -> None:
    """A job using jobs_dir/jobs_dir_suffix must still get per-trial logs.

    The splitter reads the resolved ``jobs_dir`` back out of the generated
    runtime config; the default ``results/<job_slug>`` guess would find nothing.
    """
    exp = tmp_path / "custom-suffix" / "2026-01-01__00-00-00"
    (exp / "taskA__aaa").mkdir(parents=True)
    (exp / "taskB__bbb").mkdir(parents=True)
    log_dir = tmp_path / "proxylog"
    src = _write_log(log_dir, [
        {"seq": 1, "trial_id": "taskA__aaa"},
        {"seq": 2, "trial_id": "taskB__bbb"},
        {"seq": 3, "trial_id": "taskA__aaa"},
    ])
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(yaml.safe_dump({"jobs_dir": str(tmp_path / "custom-suffix")}))

    rc = split_proxy_log(_manifest(tmp_path), log_dir, runtime_config=runtime)

    assert rc == 0
    assert [r["seq"] for r in _lines(exp / "taskA__aaa" / "agent" / "requests.jsonl")] == [1, 3]
    assert [r["seq"] for r in _lines(exp / "taskB__bbb" / "agent" / "requests.jsonl")] == [2]
    # Everything was attributed, so the run-level log is consumed.
    assert not src.exists()


def test_job_dir_points_at_single_experiment_dir(tmp_path: Path) -> None:
    """In-place resume passes one experiment dir, not the jobs root."""
    exp = tmp_path / "results" / "myjob" / "2026-02-02__11-11-11"
    (exp / "taskC__ccc").mkdir(parents=True)
    log_dir = tmp_path / "proxylog"
    _write_log(log_dir, [{"seq": 1, "trial_id": "taskC__ccc"}])

    rc = split_proxy_log(_manifest(tmp_path), log_dir, job_root=exp)

    assert rc == 0
    assert [r["seq"] for r in _lines(exp / "taskC__ccc" / "agent" / "requests.jsonl")] == [1]


def test_unattributed_records_are_retained(tmp_path: Path) -> None:
    """Run-level-id and unknown-trial records stay in the source log."""
    exp = tmp_path / "results" / "myjob" / "2026-03-03__03-03-03"
    (exp / "taskD__ddd").mkdir(parents=True)
    log_dir = tmp_path / "proxylog"
    src = _write_log(log_dir, [
        {"seq": 1, "trial_id": "taskD__ddd"},
        {"seq": 2, "trial_id": INSTANCE_ID},     # never carried a trial
        {"seq": 3, "trial_id": "ghost__zzz"},    # no such trial dir
    ])

    rc = split_proxy_log(_manifest(tmp_path), log_dir, results_root=tmp_path / "results")

    assert rc == 0
    assert [r["seq"] for r in _lines(exp / "taskD__ddd" / "agent" / "requests.jsonl")] == [1]
    assert [r["seq"] for r in _lines(src)] == [2, 3]


def test_runtime_config_falls_back_to_default_layout(tmp_path: Path) -> None:
    """A missing/unreadable runtime config must not lose the default results path."""
    exp = tmp_path / "results" / "myjob" / "2026-04-04__04-04-04"
    (exp / "taskE__eee").mkdir(parents=True)
    log_dir = tmp_path / "proxylog"
    _write_log(log_dir, [{"seq": 1, "trial_id": "taskE__eee"}])

    rc = split_proxy_log(
        _manifest(tmp_path),
        log_dir,
        results_root=tmp_path / "results",
        runtime_config=tmp_path / "does-not-exist.yaml",
    )

    assert rc == 0
    assert [r["seq"] for r in _lines(exp / "taskE__eee" / "agent" / "requests.jsonl")] == [1]


def test_noop_when_record_full_io_off(tmp_path: Path) -> None:
    log_dir = tmp_path / "proxylog"
    src = _write_log(log_dir, [{"seq": 1, "trial_id": "taskF__fff"}])

    rc = split_proxy_log(_manifest(tmp_path, record_full_io=False), log_dir)

    assert rc == 0
    assert src.exists()  # left untouched


def test_run_sh_delegates_cleanup_instead_of_embedding_split_logic() -> None:
    run_sh = (REPO_ROOT / "scripts" / "run.sh").read_text()
    cleanup = (REPO_ROOT / "scripts" / "lib" / "run_cleanup.sh").read_text()

    assert 'source "$SCRIPT_DIR/lib/run_cleanup.sh"' in run_sh
    assert "register_run_cleanup" in run_sh
    assert "workbuddy_bench.runner.split_proxy_log" not in run_sh
    assert "workbuddy_bench.runner.split_proxy_log" in cleanup
