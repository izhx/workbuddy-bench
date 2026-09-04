"""Regression tests for the record_full_io proxy-log split.

Covers the two failure modes that left runs without ``agent/requests.jsonl``
despite an intact run-level log: trials living outside the default
``results/<job_slug>`` layout, and the split being skipped when a run exits
non-zero after evaluation already finished.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from workbuddy_bench.runner.split_proxy_log import split_proxy_log

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTANCE_ID = "myjob-4242-1700000000"


def _manifest(tmp_path: Path, **overrides) -> Path:
    data = {
        "instance_id": INSTANCE_ID,
        "job_slug": "myjob",
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
    src.write_text("".join(json.dumps(r) + "\n" for r in records))
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


def test_run_sh_splits_from_exit_trap_on_nonzero_eval() -> None:
    """The split must survive a non-zero exit after evaluation finished.

    Harbor's summary printer can raise after every trial completed; under
    ``set -e`` that aborted run.sh before the old end-of-script split call, so
    fully-intact logs were never fanned out. Drive the real functions out of
    scripts/run.sh to prove the exit path now covers it.
    """
    run_sh = REPO_ROOT / "scripts" / "run.sh"
    body = run_sh.read_text()

    def _extract(name: str) -> str:
        start = body.index(f"{name}() {{")
        depth = 0
        for i in range(start, len(body)):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    return body[start:i + 1]
        raise AssertionError(f"unbalanced braces extracting {name}")

    script = "\n".join([
        "set -e",
        "SPLIT_PROXY_LOG_DONE=0; CLEANUP_DONE=0",
        "USE_LOCAL_PROXY=1; DRY_RUN=0; PROXY_LOG_DIR=''",
        "RESUME_IN_PLACE_PATH=''; JOB_CONFIG_RUNTIME=''",
        "MANIFEST_PATH=\"$1\"",
        # Stub the splitter invocation: this test asserts the shell control flow,
        # not the Python (covered by the cases above).
        "python3() { echo SPLIT-RAN; }",
        _extract("split_proxy_log_now"),
        _extract("cleanup_instance"),
        "trap 'rc=$?; trap - EXIT; cleanup_instance \"$rc\"; exit \"$rc\"' EXIT",
        "false",  # harbor run exiting 1 after evaluation completed
        "echo UNREACHABLE",
    ])

    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(REPO_ROOT / "pyproject.toml")],
        capture_output=True, text=True,
    )

    assert "SPLIT-RAN" in proc.stdout, proc.stdout + proc.stderr
    assert "UNREACHABLE" not in proc.stdout
    # The eval failure is still reported; the split does not mask it.
    assert proc.returncode == 1


def test_run_sh_split_runs_once_on_success() -> None:
    """Inline call + EXIT trap must not double-append records."""
    run_sh = REPO_ROOT / "scripts" / "run.sh"
    body = run_sh.read_text()

    def _extract(name: str) -> str:
        start = body.index(f"{name}() {{")
        depth = 0
        for i in range(start, len(body)):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    return body[start:i + 1]
        raise AssertionError(f"unbalanced braces extracting {name}")

    script = "\n".join([
        "set -e",
        "SPLIT_PROXY_LOG_DONE=0; CLEANUP_DONE=0",
        "USE_LOCAL_PROXY=1; DRY_RUN=0; PROXY_LOG_DIR=''",
        "RESUME_IN_PLACE_PATH=''; JOB_CONFIG_RUNTIME=''",
        "MANIFEST_PATH=\"$1\"",
        "python3() { echo SPLIT-RAN; }",
        _extract("split_proxy_log_now"),
        _extract("cleanup_instance"),
        "trap 'rc=$?; trap - EXIT; cleanup_instance \"$rc\"; exit \"$rc\"' EXIT",
        "split_proxy_log_now",  # happy-path inline call
    ])

    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(REPO_ROOT / "pyproject.toml")],
        capture_output=True, text=True,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("SPLIT-RAN") == 1, proc.stdout


def test_run_sh_skips_split_on_dry_run() -> None:
    run_sh = REPO_ROOT / "scripts" / "run.sh"
    body = run_sh.read_text()
    start = body.index("split_proxy_log_now() {")
    depth = 0
    for i in range(start, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                fn = body[start:i + 1]
                break

    script = "\n".join([
        "set -e",
        "SPLIT_PROXY_LOG_DONE=0",
        "USE_LOCAL_PROXY=1; DRY_RUN=1; PROXY_LOG_DIR=''",
        "RESUME_IN_PLACE_PATH=''; JOB_CONFIG_RUNTIME=''",
        "MANIFEST_PATH=\"$1\"",
        "python3() { echo SPLIT-RAN; }",
        fn,
        "split_proxy_log_now",
        "echo DONE",
    ])
    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(REPO_ROOT / "pyproject.toml")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SPLIT-RAN" not in proc.stdout
    assert "DONE" in proc.stdout
