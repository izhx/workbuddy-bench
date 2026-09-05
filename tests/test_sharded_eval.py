from __future__ import annotations

import json
from pathlib import Path

from workbuddy_bench.runner.sharded_eval import load_completed_tasks


TASK_NAME = "task-one"
TASK_CHECKSUM = "current-checksum"


def _tasks() -> dict[str, dict[str, object]]:
    return {TASK_NAME: {"checksum": TASK_CHECKSUM}}


def _write_result(
    job_dir: Path,
    trial_id: str,
    *,
    checksum: str = TASK_CHECKSUM,
    reward: float | int | None = 1,
) -> Path:
    trial_dir = job_dir / f"{TASK_NAME}__{trial_id}"
    trial_dir.mkdir(parents=True)
    result: dict[str, object] = {"task_checksum": checksum}
    if reward is not None:
        result["verifier_result"] = {"rewards": {"reward": reward}}
    (trial_dir / "result.json").write_text(json.dumps(result))
    return trial_dir


def test_load_completed_tasks_reuses_zero_reward(tmp_path: Path) -> None:
    job_dir = tmp_path / "resume"
    trial_dir = _write_result(job_dir, "zero", reward=0)

    completed, source, trial_dirs = load_completed_tasks(
        tmp_path, _tasks(), ["resume"]
    )

    assert completed == {TASK_NAME}
    assert source[TASK_NAME] == (str(job_dir.resolve()), 0)
    assert trial_dirs[TASK_NAME] == [trial_dir]


def test_load_completed_tasks_ignores_missing_reward(tmp_path: Path) -> None:
    _write_result(tmp_path / "resume", "missing", reward=None)

    completed, source, trial_dirs = load_completed_tasks(
        tmp_path, _tasks(), ["resume"]
    )

    assert completed == set()
    assert source == {}
    assert trial_dirs == {}


def test_load_completed_tasks_ignores_checksum_mismatch(tmp_path: Path) -> None:
    _write_result(tmp_path / "resume", "stale", checksum="old-checksum")

    completed, source, trial_dirs = load_completed_tasks(
        tmp_path, _tasks(), ["resume"]
    )

    assert completed == set()
    assert source == {}
    assert trial_dirs == {}


def test_load_completed_tasks_does_not_skip_with_too_few_attempts(
    tmp_path: Path,
) -> None:
    _write_result(tmp_path / "resume", "only-attempt")

    completed, source, trial_dirs = load_completed_tasks(
        tmp_path, _tasks(), ["resume"], n_attempts=2
    )

    assert completed == set()
    assert source == {}
    assert trial_dirs == {}


def test_load_completed_tasks_accumulates_attempts_across_resume_jobs(
    tmp_path: Path,
) -> None:
    first_trial = _write_result(tmp_path / "resume-one", "first", reward=0)
    second_trial = _write_result(tmp_path / "resume-two", "second", reward=1)

    completed, source, trial_dirs = load_completed_tasks(
        tmp_path,
        _tasks(),
        ["resume-one", "resume-two"],
        n_attempts=2,
    )

    assert completed == {TASK_NAME}
    assert source[TASK_NAME] == (str((tmp_path / "resume-one").resolve()), 0)
    assert trial_dirs[TASK_NAME] == [first_trial, second_trial]
