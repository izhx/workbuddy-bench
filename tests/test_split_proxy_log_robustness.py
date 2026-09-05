from __future__ import annotations

import fcntl
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from workbuddy_bench.proxy.config import BackendConfig, ProxyMode, RouteConfig
from workbuddy_bench.proxy.interceptors import RequestContext, ResponseContext
from workbuddy_bench.proxy.interceptors.logger import LogInterceptor, proxy_log_filename
from workbuddy_bench.runner import split_proxy_log as splitter
from workbuddy_bench.runner.in_place_resume import InvalidTrial, archive_invalid_trials
from workbuddy_bench.runner.split_proxy_log import (
    legacy_proxy_log_filename,
    proxy_log_candidates,
    proxy_log_source_lock_filename,
)


def _manifest(tmp_path: Path, instance_id: str = "current-instance") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "job_slug": "job",
                "model_route": f"{instance_id}__model",
                "model_connection": "local_proxy",
                "record_full_io": True,
            }
        )
    )
    return path


def _trial(run_dir: Path, name: str, instance_id: str | None) -> Path:
    trial = run_dir / name
    trial.mkdir(parents=True)
    if instance_id is not None:
        (trial / "config.json").write_text(
            json.dumps({"agent": {"kwargs": {"instance_id": instance_id}}})
        )
    return trial


def _write_source(log_dir: Path, instance_id: str, records: list[dict], *, legacy=False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        legacy_proxy_log_filename(instance_id) if legacy else proxy_log_filename(instance_id)
    )
    source = log_dir / filename
    owned_records = []
    for record in records:
        owned = dict(record)
        owned.setdefault("route", f"{instance_id}__model")
        if not legacy:
            owned.setdefault("instance_id", instance_id)
        owned_records.append(owned)
    source.write_text("".join(json.dumps(record) + "\n" for record in owned_records))
    return source


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_logger_and_splitter_share_collision_resistant_filename(tmp_path: Path) -> None:
    first = "job:a"
    second = "job_a"
    assert proxy_log_filename(first) != proxy_log_filename(second)

    logger = LogInterceptor(str(tmp_path))
    handle = logger._file_for(first)
    assert handle is not None
    handle.write("{}\n")
    logger.close()

    assert (tmp_path / proxy_log_filename(first)).is_file()
    assert not (tmp_path / legacy_proxy_log_filename(first)).exists()

    route = RouteConfig(
        slug=f"{first}__model",
        mode=ProxyMode.PASSTHROUGH,
        backend=BackendConfig(url="https://example.invalid"),
        instance_id=first,
    )
    record = logger._build_record(
        RequestContext(route=route, client_body={}, parsed_body={}), ResponseContext()
    )
    assert record["instance_id"] == first


def test_splitter_falls_back_to_legacy_sanitized_filename(tmp_path: Path) -> None:
    instance_id = "job:legacy"
    trial = _trial(tmp_path / "experiment", "task__one", instance_id)
    log_dir = tmp_path / "logs"
    source = _write_source(
        log_dir,
        instance_id,
        [{"id": "request-1", "trial_id": trial.name}],
        legacy=True,
    )

    assert splitter.split_proxy_log(
        _manifest(tmp_path, instance_id), log_dir, job_root=tmp_path / "experiment"
    ) == 0

    assert not source.exists()
    assert [record["id"] for record in _records(trial / "agent" / "requests.jsonl")] == [
        "request-1"
    ]


def test_splitter_falls_back_to_legacy_raw_legal_filename(tmp_path: Path) -> None:
    instance_id = "job:raw"
    trial = _trial(tmp_path / "experiment", "task__one", instance_id)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    source = log_dir / f"{instance_id}.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "request-1",
                "trial_id": trial.name,
                "route": f"{instance_id}__model",
            }
        )
        + "\n"
    )

    assert splitter.split_proxy_log(
        _manifest(tmp_path, instance_id), log_dir, job_root=tmp_path / "experiment"
    ) == 0

    assert not source.exists()
    assert [record["id"] for record in _records(trial / "agent" / "requests.jsonl")] == [
        "request-1"
    ]


def test_splitter_consumes_canonical_and_legacy_sources_together(tmp_path: Path) -> None:
    instance_id = "job:migrated"
    trial = _trial(tmp_path / "experiment", "task__one", instance_id)
    log_dir = tmp_path / "logs"
    canonical = _write_source(
        log_dir,
        instance_id,
        [{"id": "request-new", "trial_id": trial.name}],
    )
    legacy = _write_source(
        log_dir,
        instance_id,
        [
            {"id": "request-old", "trial_id": trial.name},
            {"id": "request-new", "trial_id": trial.name},
        ],
        legacy=True,
    )

    assert splitter.split_proxy_log(
        _manifest(tmp_path, instance_id), log_dir, job_root=tmp_path / "experiment"
    ) == 0

    assert not canonical.exists()
    assert not legacy.exists()
    assert [
        record["id"] for record in _records(trial / "agent" / "requests.jsonl")
    ] == ["request-new", "request-old"]


def test_very_long_instance_id_does_not_block_canonical_source(tmp_path: Path) -> None:
    instance_id = "long-instance-" + ("x" * 1000)
    trial = _trial(tmp_path / "experiment", "task__one", instance_id)
    log_dir = tmp_path / "logs"
    canonical = _write_source(
        log_dir,
        instance_id,
        [{"id": "request-long", "trial_id": trial.name}],
    )

    candidates = proxy_log_candidates(log_dir, instance_id)
    assert candidates == [canonical]
    assert splitter.split_proxy_log(
        _manifest(tmp_path, instance_id), log_dir, job_root=tmp_path / "experiment"
    ) == 0

    assert not canonical.exists()
    assert [record["id"] for record in _records(trial / "agent" / "requests.jsonl")] == [
        "request-long"
    ]


@pytest.mark.parametrize("concurrent", [False, True])
def test_colliding_legacy_source_is_partitioned_by_record_owner(
    tmp_path: Path, concurrent: bool
) -> None:
    first_id = "job:a"
    second_id = "job_a"
    assert legacy_proxy_log_filename(first_id) == legacy_proxy_log_filename(second_id)

    first_trial = _trial(tmp_path / "first-experiment", "task-a__one", first_id)
    second_trial = _trial(tmp_path / "second-experiment", "task-b__one", second_id)
    first_manifest = _manifest(tmp_path / "first-state", first_id)
    second_manifest = _manifest(tmp_path / "second-state", second_id)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    source = log_dir / legacy_proxy_log_filename(first_id)
    source.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                {
                    "id": "request-first",
                    "trial_id": first_trial.name,
                    "route": f"{first_id}__model",
                },
                {
                    "id": "request-second",
                    "trial_id": second_trial.name,
                    "route": f"{second_id}__model",
                },
            ]
        )
    )

    first_legacy = proxy_log_candidates(log_dir, first_id)[1]
    second_legacy = proxy_log_candidates(log_dir, second_id)[1]
    assert first_legacy == second_legacy == source
    assert proxy_log_source_lock_filename(first_legacy) == proxy_log_source_lock_filename(
        second_legacy
    )

    invocations = [
        (first_manifest, first_trial.parent),
        (second_manifest, second_trial.parent),
    ]
    if concurrent:
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "workbuddy_bench.runner.split_proxy_log",
                    "--manifest",
                    str(manifest),
                    "--log-dir",
                    str(log_dir),
                    "--job-dir",
                    str(experiment),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for manifest, experiment in invocations
        ]
        results = [process.communicate(timeout=10) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
    else:
        for manifest, experiment in invocations:
            assert splitter.split_proxy_log(
                manifest, log_dir, job_root=experiment
            ) == 0

    assert not source.exists()
    assert [
        record["id"] for record in _records(first_trial / "agent" / "requests.jsonl")
    ] == ["request-first"]
    assert [
        record["id"] for record in _records(second_trial / "agent" / "requests.jsonl")
    ] == ["request-second"]


def test_foreign_and_unverifiable_records_stay_in_canonical_source(tmp_path: Path) -> None:
    instance_id = "current-instance"
    trial = _trial(tmp_path / "experiment", "task__one", instance_id)
    log_dir = tmp_path / "logs"
    source = _write_source(
        log_dir,
        instance_id,
        [
            {"id": "owned", "trial_id": trial.name},
            {
                "id": "foreign",
                "trial_id": trial.name,
                "instance_id": "other-instance",
            },
            {"id": "missing", "trial_id": trial.name, "instance_id": None, "route": ""},
        ],
    )

    assert splitter.split_proxy_log(
        _manifest(tmp_path, instance_id), log_dir, job_root=trial.parent
    ) == 0

    assert [record["id"] for record in _records(trial / "agent" / "requests.jsonl")] == [
        "owned"
    ]
    assert [record["id"] for record in _records(source)] == ["foreign", "missing"]


def test_duplicate_trial_name_selects_only_matching_instance(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    current = _trial(
        jobs_root / "2026-01-01__00-00-00", "task__same", "current-instance"
    )
    other = _trial(
        jobs_root / "2026-12-01__00-00-00", "task__same", "other-instance"
    )
    log_dir = tmp_path / "logs"
    source = _write_source(
        log_dir,
        "current-instance",
        [{"id": "request-1", "trial_id": "task__same"}],
    )

    assert splitter.split_proxy_log(
        _manifest(tmp_path), log_dir, job_root=jobs_root
    ) == 0

    assert not source.exists()
    assert (current / "agent" / "requests.jsonl").is_file()
    assert not (other / "agent" / "requests.jsonl").exists()


@pytest.mark.parametrize("from_jobs_root", [False, True])
@pytest.mark.parametrize("has_active_trial", [False, True])
def test_split_includes_archived_resume_rounds(
    tmp_path: Path, from_jobs_root: bool, has_active_trial: bool
) -> None:
    experiment = tmp_path / "jobs" / "2026-09-05__00-00-00"
    archived = []
    for number in (1, 2):
        trial = _trial(experiment, f"task__old{number}", "current-instance")
        round_dir = archive_invalid_trials(
            experiment, [InvalidTrial(trial, "task", "retryable")], round_number=number
        )
        assert round_dir is not None
        archived.append(round_dir / trial.name)
    active = _trial(experiment, "task__new", "current-instance") if has_active_trial else None
    trials = archived + ([active] if active else [])
    log_dir = tmp_path / "logs"
    source = _write_source(log_dir, "current-instance", [
        {"id": trial.name, "trial_id": trial.name} for trial in trials
    ])

    splitter.split_proxy_log(
        _manifest(tmp_path), log_dir,
        job_root=experiment.parent if from_jobs_root else experiment,
    )

    assert not source.exists()
    for trial in trials:
        assert [record["id"] for record in _records(trial / "agent/requests.jsonl")] == [trial.name]
        assert not (trial / "verifier/requests.jsonl").exists()


def test_exact_experiment_does_not_scan_sibling_history(tmp_path: Path) -> None:
    experiment = tmp_path / "current"
    active = _trial(experiment, "task__active", "current-instance")
    sibling = _trial(tmp_path / "other.attempt-history" / "round-1", "task__old", "current-instance")
    source = _write_source(tmp_path / "logs", "current-instance", [
        {"id": trial.name, "trial_id": trial.name} for trial in (active, sibling)
    ])
    splitter.split_proxy_log(_manifest(tmp_path), source.parent, job_root=experiment)
    assert (active / "agent/requests.jsonl").exists()
    assert not (sibling / "agent/requests.jsonl").exists()
    assert [record["id"] for record in _records(source)] == [sibling.name]


def test_job_dir_accepts_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trial = _trial(tmp_path / "experiment", "task__one", "current-instance")
    source = _write_source(tmp_path / "logs", "current-instance", [{"id": "one", "trial_id": trial.name}])
    manifest = _manifest(tmp_path)
    monkeypatch.chdir(trial.parent)
    splitter.split_proxy_log(manifest, source.parent, job_root=Path("."))
    assert (trial / "agent/requests.jsonl").exists()
    assert not source.exists()


def test_duplicate_active_and_archived_identity_stays_unattributed(tmp_path: Path) -> None:
    experiment = tmp_path / "experiment"
    active = _trial(experiment, "task__same", "current-instance")
    archived = _trial(tmp_path / "experiment.attempt-history" / "round-1", active.name, "current-instance")
    source = _write_source(tmp_path / "logs", "current-instance", [{"id": "ambiguous", "trial_id": active.name}])
    splitter.split_proxy_log(_manifest(tmp_path), source.parent, job_root=experiment)
    assert source.exists()
    assert not (active / "agent/requests.jsonl").exists()
    assert not (archived / "agent/requests.jsonl").exists()


def test_retry_preserves_legacy_record_multiplicity(tmp_path: Path) -> None:
    destination = tmp_path / "agent/requests.jsonl"
    spool = tmp_path / "spool.jsonl"
    spool.write_text('{"legacy": true}\n' * 2 + '{"id": "once"}\n' * 2)
    splitter._commit_trial_records(destination, spool)
    splitter._commit_trial_records(destination, spool)
    assert _records(destination) == [{"legacy": True}, {"legacy": True}, {"id": "once"}]


@pytest.mark.parametrize("identities", [("other-a", "other-b"), ("current-instance", "current-instance")])
def test_duplicate_trial_without_unique_match_stays_unattributed(
    tmp_path: Path, identities: tuple[str, str]
) -> None:
    jobs_root = tmp_path / "jobs"
    trials = [
        _trial(jobs_root / f"run-{index}", "task__same", identity)
        for index, identity in enumerate(identities)
    ]
    log_dir = tmp_path / "logs"
    source = _write_source(
        log_dir,
        "current-instance",
        [{"id": "request-1", "trial_id": "task__same"}],
    )

    assert splitter.split_proxy_log(
        _manifest(tmp_path), log_dir, job_root=jobs_root
    ) == 0

    assert source.is_file()
    assert [record["id"] for record in _records(source)] == ["request-1"]
    assert all(not (trial / "agent" / "requests.jsonl").exists() for trial in trials)


def test_partial_destination_failure_is_idempotent_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = tmp_path / "experiment"
    first = _trial(experiment, "a__one", "current-instance")
    second = _trial(experiment, "b__two", "current-instance")
    log_dir = tmp_path / "logs"
    source = _write_source(
        log_dir,
        "current-instance",
        [
            {"id": "request-a", "trial_id": first.name},
            {"id": "request-b", "trial_id": second.name},
        ],
    )

    original_commit = splitter._commit_trial_records
    calls = 0

    def fail_second(destination: Path, spool: Path) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated destination failure")
        return original_commit(destination, spool)

    monkeypatch.setattr(splitter, "_commit_trial_records", fail_second)
    with pytest.raises(OSError, match="simulated destination failure"):
        splitter.split_proxy_log(_manifest(tmp_path), log_dir, job_root=experiment)

    assert source.is_file()
    assert [record["id"] for record in _records(first / "agent" / "requests.jsonl")] == [
        "request-a"
    ]

    monkeypatch.setattr(splitter, "_commit_trial_records", original_commit)
    assert splitter.split_proxy_log(_manifest(tmp_path), log_dir, job_root=experiment) == 0

    assert not source.exists()
    assert [record["id"] for record in _records(first / "agent" / "requests.jsonl")] == [
        "request-a"
    ]
    assert [record["id"] for record in _records(second / "agent" / "requests.jsonl")] == [
        "request-b"
    ]


def test_source_append_during_split_is_detected_and_retry_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_id = "current-instance"
    experiment = tmp_path / "experiment"
    trial = _trial(experiment, "task__one", instance_id)
    log_dir = tmp_path / "logs"
    source = _write_source(
        log_dir,
        instance_id,
        [{"id": "request-before", "trial_id": trial.name}],
    )

    original_commit = splitter._commit_trial_records
    appended = False

    def append_after_commit(destination: Path, spool: Path) -> tuple[int, int]:
        nonlocal appended
        result = original_commit(destination, spool)
        if not appended:
            appended = True
            with source.open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "id": "request-during",
                            "instance_id": instance_id,
                            "trial_id": trial.name,
                            "route": f"{instance_id}__model",
                        }
                    )
                    + "\n"
                )
        return result

    monkeypatch.setattr(splitter, "_commit_trial_records", append_after_commit)
    with pytest.raises(RuntimeError, match="changed while splitting"):
        splitter.split_proxy_log(
            _manifest(tmp_path, instance_id), log_dir, job_root=experiment
        )

    destination = trial / "agent" / "requests.jsonl"
    assert [record["id"] for record in _records(destination)] == ["request-before"]
    assert [record["id"] for record in _records(source)] == [
        "request-before",
        "request-during",
    ]

    monkeypatch.setattr(splitter, "_commit_trial_records", original_commit)
    assert splitter.split_proxy_log(
        _manifest(tmp_path, instance_id), log_dir, job_root=experiment
    ) == 0
    assert not source.exists()
    assert [record["id"] for record in _records(destination)] == [
        "request-before",
        "request-during",
    ]


def test_source_lock_uses_stable_exclusive_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[int] = []
    monkeypatch.setattr(splitter.fcntl, "flock", lambda _fd, operation: operations.append(operation))

    source = tmp_path / proxy_log_filename("current-instance")
    with splitter._source_locks([source]):
        assert (tmp_path / proxy_log_source_lock_filename(source)).is_file()

    assert operations == [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN]


def test_source_lock_times_out_without_mutating_source(tmp_path: Path) -> None:
    source = tmp_path / "shared-legacy.jsonl"
    source.write_text('{"sentinel": true}\n')
    lock_path = tmp_path / proxy_log_source_lock_filename(source)
    with lock_path.open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(TimeoutError, match="timed out"):
            with splitter._source_locks(
                [source], timeout_seconds=0, poll_seconds=0.001
            ):
                raise AssertionError("contended source lock must not be acquired")
    assert source.read_text() == '{"sentinel": true}\n'


@pytest.mark.parametrize(
    ("destination_mode", "expected_mode"),
    [(None, 0o640), (0o600, 0o600)],
)
def test_atomic_commit_preserves_append_style_permissions(
    tmp_path: Path, destination_mode: int | None, expected_mode: int
) -> None:
    spool = tmp_path / "spool.jsonl"
    spool.write_text('{"id":"new"}\n')
    spool.chmod(0o640)
    destination = tmp_path / "agent" / "requests.jsonl"
    if destination_mode is not None:
        destination.parent.mkdir()
        destination.write_text('{"id":"old"}\n')
        destination.chmod(destination_mode)

    splitter._commit_trial_records(destination, spool)

    assert stat.S_IMODE(destination.stat().st_mode) == expected_mode


def test_cli_returns_nonzero_for_split_failure(tmp_path: Path) -> None:
    manifest = tmp_path / "invalid.json"
    manifest.write_text("not-json")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "workbuddy_bench.runner.split_proxy_log",
            "--manifest",
            str(manifest),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "[split-proxy-log] ERROR:" in result.stderr
