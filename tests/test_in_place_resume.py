from __future__ import annotations

import json
from pathlib import Path

import pytest
from harbor.publisher.packager import Packager

from workbuddy_bench.runner.in_place_resume import (
    DEFAULT_RETRYABLE_EXCEPTIONS,
    ResumeError,
    bootstrap_info,
    build_resume_plan,
    emit_bootstrap_shell,
    is_retryable_exception,
    planned_counts,
    run_in_place_resume,
    validate_prebuilt_contract,
)
from workbuddy_bench.runner.sharded_eval import load_tasks


TASK_NAME = "task-one"
TAG = "2026-08-28"
INSTANCE_ID = "job-one-123-456"


def _write_task(tasks_dir: Path, *, tag: str = TAG) -> Path:
    (tasks_dir.parent / "dataset.toml").write_text('[dataset]\nid = "dataset"\n')
    task_dir = tasks_dir / TASK_NAME
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM scratch\n")
    (task_dir / "task.toml").write_text(
        "[metadata]\n"
        'difficulty = "easy"\n'
        "[environment]\n"
        f'docker_image = "dataset/{TASK_NAME}:{tag}"\n'
    )
    return task_dir


def _planned_trial(
    *,
    mode: str = "direct",
    proxy_url: str = "",
    task_digest: str | None = None,
) -> dict:
    connection: dict[str, str] = {"mode": mode}
    if proxy_url:
        connection["proxy_url"] = proxy_url
        connection["model_route"] = "model-route"
    trial = {
        "task": {
            "name": TASK_NAME,
            "path": (
                f".workspace/tmp/staged/{INSTANCE_ID}/dataset/tasks/{TASK_NAME}"
            ),
        },
        "agent": {
            "kwargs": {
                "instance_id": INSTANCE_ID,
                "connection": connection,
            }
        },
    }
    if task_digest is not None:
        trial["task"]["digest"] = task_digest
    return trial


def _write_job(
    tmp_path: Path,
    *,
    n_attempts: int = 2,
    force_build: bool = False,
    mode: str = "direct",
    proxy_url: str = "",
) -> Path:
    job_dir = tmp_path / "results" / "job-one" / "experiment"
    job_dir.mkdir(parents=True)
    config = {
        "jobs_dir": str(job_dir.parent),
        "n_attempts": n_attempts,
        "environment": {"force_build": force_build},
        "datasets": [
            {
                "path": (
                    f".workspace/tmp/staged/{INSTANCE_ID}/dataset/tasks"
                )
            }
        ],
    }
    task_dir = tmp_path / "tasks" / TASK_NAME
    task_digest = None
    if task_dir.is_dir():
        content_hash, _ = Packager.compute_content_hash(task_dir)
        task_digest = f"sha256:{content_hash}"
    lock = {
        "trials": [
            _planned_trial(
                mode=mode,
                proxy_url=proxy_url,
                task_digest=task_digest,
            )
            for _ in range(n_attempts)
        ]
    }
    (job_dir / "config.json").write_text(json.dumps(config))
    (job_dir / "lock.json").write_text(json.dumps(lock))
    return job_dir


def _write_trial(
    job_dir: Path,
    trial_id: str,
    *,
    checksum: str,
    reward: int | float | None = 1,
    exception_type: str = "",
) -> Path:
    trial_dir = job_dir / f"{TASK_NAME}__{trial_id}"
    trial_dir.mkdir()
    task_path = f".workspace/tmp/staged/{INSTANCE_ID}/dataset/tasks/{TASK_NAME}"
    (trial_dir / "config.json").write_text(
        json.dumps({"task": {"path": task_path}})
    )
    result: dict[str, object] = {
        "task_id": {"path": task_path},
        "task_checksum": checksum,
    }
    if reward is not None:
        result["verifier_result"] = {"rewards": {"reward": reward}}
    if exception_type:
        result["exception_info"] = {"exception_type": exception_type}
    (trial_dir / "result.json").write_text(json.dumps(result))
    return trial_dir


def _plan_for_job(job_dir: Path, tasks_dir: Path, **kwargs):
    lock = json.loads((job_dir / "lock.json").read_text())
    return build_resume_plan(
        job_dir,
        planned_by_task=planned_counts(lock),
        current_tasks=load_tasks(tasks_dir),
        **kwargs,
    )


def test_bootstrap_extracts_staged_instance_and_proxy(tmp_path: Path) -> None:
    job_dir = _write_job(
        tmp_path,
        mode="local_proxy",
        proxy_url="http://host.docker.internal:4567",
    )

    info = bootstrap_info(job_dir)

    assert info.instance_id == INSTANCE_ID
    assert info.dataset_path == f".workspace/tmp/staged/{INSTANCE_ID}/dataset/tasks"
    assert info.proxy_mode == "local_proxy"
    assert info.proxy_host == "host.docker.internal"
    assert info.proxy_port == 4567
    assert info.model_route == "model-route"
    assert info.planned_tasks == (TASK_NAME,)

    shell = emit_bootstrap_shell(info)
    assert "RESUME_RECORDED_PROXY_URL=http://host.docker.internal:4567" in shell
    assert "RESUME_PROXY_HOST=" not in shell
    assert "RESUME_PROXY_PORT=" not in shell


def test_plan_keeps_zero_reward_and_replaces_missing_reward(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path)
    current = load_tasks(tasks_dir)
    checksum = str(current[TASK_NAME]["checksum"])
    zero_trial = _write_trial(job_dir, "zero", checksum=checksum, reward=0)
    missing_reward = _write_trial(
        job_dir, "missing-reward", checksum=checksum, reward=None
    )
    lock = json.loads((job_dir / "lock.json").read_text())

    plan = build_resume_plan(
        job_dir,
        planned_by_task=planned_counts(lock),
        current_tasks=current,
    )

    assert plan.valid_total == 1
    assert plan.attempts_needed == 1
    assert zero_trial not in [item.path for item in plan.invalid_trials]
    assert [(item.path, item.reason) for item in plan.invalid_trials] == [
        (missing_reward, "missing_reward")
    ]


def test_plan_ignores_report_directory(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    report_dir = job_dir / "report"
    report_dir.mkdir()
    (report_dir / "score-report.md").write_text("report\n")

    plan = _plan_for_job(job_dir, tasks_dir)

    assert plan.valid_total == 0
    assert plan.attempts_needed == 1
    assert plan.invalid_trials == ()


def test_plan_still_rejects_other_unrecognized_directory(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    (job_dir / "artifacts").mkdir()

    with pytest.raises(ResumeError, match="unrecognized directory"):
        _plan_for_job(job_dir, tasks_dir)


def test_plan_archives_trial_with_invalid_config_json(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    current = load_tasks(tasks_dir)
    checksum = str(current[TASK_NAME]["checksum"])
    trial_dir = _write_trial(job_dir, "bad-config", checksum=checksum, reward=0)
    (trial_dir / "config.json").write_text("{")
    lock = json.loads((job_dir / "lock.json").read_text())

    plan = build_resume_plan(
        job_dir,
        planned_by_task=planned_counts(lock),
        current_tasks=current,
    )

    assert plan.valid_total == 0
    assert plan.attempts_needed == 1
    assert [(item.path, item.reason) for item in plan.invalid_trials] == [
        (trial_dir, "invalid_trial_config")
    ]


def test_plan_retries_cancelled_trial_even_if_reward_exists(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    current = load_tasks(tasks_dir)
    checksum = str(current[TASK_NAME]["checksum"])
    trial_dir = _write_trial(
        job_dir,
        "cancelled",
        checksum=checksum,
        reward=0,
        exception_type="CancelledError",
    )
    lock = json.loads((job_dir / "lock.json").read_text())

    plan = build_resume_plan(
        job_dir,
        planned_by_task=planned_counts(lock),
        current_tasks=current,
    )

    assert [(item.path, item.reason) for item in plan.invalid_trials] == [
        (trial_dir, "cancelled")
    ]


def test_plan_rejects_checksum_drift_before_archiving(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    current = load_tasks(tasks_dir)
    _write_trial(job_dir, "stale", checksum="old-checksum", reward=0)
    lock = json.loads((job_dir / "lock.json").read_text())

    with pytest.raises(ResumeError, match="checksum does not match"):
        build_resume_plan(
            job_dir,
            planned_by_task=planned_counts(lock),
            current_tasks=current,
        )


def test_plan_rejects_checksum_drift_when_reward_is_missing(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    _write_trial(job_dir, "stale", checksum="old-checksum", reward=None)

    with pytest.raises(ResumeError, match="checksum does not match"):
        _plan_for_job(job_dir, tasks_dir)


@pytest.mark.parametrize(
    "exception_type",
    [
        "UnknownApiError",
        "ApiRateLimitError",
        "ApiOverloadedError",
        "ApiInternalServerError",
        "ApiConnectionClosedError",
        "ApiUsageLimitError",
        "NonZeroAgentExitCodeError",
        "NetworkConnectionError",
        "RuntimeError",
        "EnvironmentStartTimeoutError",
        "OSError",
    ],
)
def test_plan_retries_crashed_trial_with_reward(
    tmp_path: Path,
    exception_type: str,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    _write_trial(
        job_dir,
        "crashed",
        checksum=checksum,
        reward=0,
        exception_type=exception_type,
    )

    plan = _plan_for_job(job_dir, tasks_dir)

    assert plan.valid_total == 0
    assert plan.attempts_needed == 1
    assert [item.reason for item in plan.invalid_trials] == [
        f"crashed:{exception_type}"
    ]


def test_plan_retries_crashed_trial_with_positive_reward(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    _write_trial(
        job_dir,
        "crashed",
        checksum=checksum,
        reward=0.7,
        exception_type="UnknownApiError",
    )

    plan = _plan_for_job(job_dir, tasks_dir)

    assert plan.valid_total == 0
    assert plan.attempts_needed == 1


def test_plan_keeps_agent_timeout_as_real_outcome(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    _write_trial(
        job_dir,
        "timeout",
        checksum=checksum,
        reward=0,
        exception_type="AgentTimeoutError",
    )

    plan = _plan_for_job(job_dir, tasks_dir)

    assert plan.valid_total == 1
    assert plan.invalid_trials == ()


def test_plan_retries_result_without_task_checksum(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    trial_dir = _write_trial(job_dir, "no-checksum", checksum=checksum, reward=0)
    result = json.loads((trial_dir / "result.json").read_text())
    del result["task_checksum"]
    (trial_dir / "result.json").write_text(json.dumps(result))

    plan = _plan_for_job(job_dir, tasks_dir)

    assert [item.reason for item in plan.invalid_trials] == [
        "missing_task_checksum"
    ]


def test_plan_keep_exception_overrides_default_retry(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    _write_trial(
        job_dir,
        "api-error",
        checksum=checksum,
        reward=0,
        exception_type="UnknownApiError",
    )

    plan = _plan_for_job(
        job_dir,
        tasks_dir,
        keep_exceptions=frozenset({"UnknownApiError"}),
    )

    assert plan.valid_total == 1
    assert plan.invalid_trials == ()


def test_plan_honours_extra_retry_exception(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    _write_trial(
        job_dir,
        "custom-error",
        checksum=checksum,
        reward=0,
        exception_type="WeirdCustomError",
    )

    kept = _plan_for_job(job_dir, tasks_dir)
    retried = _plan_for_job(
        job_dir,
        tasks_dir,
        retry_exceptions=DEFAULT_RETRYABLE_EXCEPTIONS
        | frozenset({"WeirdCustomError"}),
    )

    assert kept.valid_total == 1
    assert retried.valid_total == 0
    assert [item.reason for item in retried.invalid_trials] == [
        "crashed:WeirdCustomError"
    ]


def test_plan_legacy_policy_keeps_api_crash(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    _write_trial(
        job_dir,
        "api-error",
        checksum=checksum,
        reward=0,
        exception_type="UnknownApiError",
    )

    plan = _plan_for_job(
        job_dir,
        tasks_dir,
        retry_exceptions=frozenset({"CancelledError"}),
    )

    assert plan.valid_total == 1
    assert plan.invalid_trials == ()


def test_plan_mixed_job_counts_only_clean_trials(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=3)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    _write_trial(job_dir, "ok", checksum=checksum, reward=1)
    _write_trial(
        job_dir,
        "api-error",
        checksum=checksum,
        reward=0,
        exception_type="UnknownApiError",
    )
    _write_trial(job_dir, "missing-reward", checksum=checksum, reward=None)

    plan = _plan_for_job(job_dir, tasks_dir)

    assert plan.valid_total == 1
    assert plan.attempts_needed == 2
    assert sorted(item.reason for item in plan.invalid_trials) == [
        "crashed:UnknownApiError",
        "missing_reward",
    ]


def test_unknown_api_subclasses_are_retried_by_suffix() -> None:
    assert is_retryable_exception("SomeNewProviderApiError")
    assert not is_retryable_exception(None)
    assert not is_retryable_exception("AgentTimeoutError")


def test_run_rejects_planned_digest_drift_before_archiving(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    incomplete_trial = job_dir / f"{TASK_NAME}__incomplete"
    incomplete_trial.mkdir()
    (incomplete_trial / "config.json").write_text(
        json.dumps({"task": {"path": f"tasks/{TASK_NAME}"}})
    )
    (task_dir / "environment" / "Dockerfile").write_text("FROM changed\n")
    calls: list[Path] = []

    with pytest.raises(ResumeError, match="digest does not match lock.json"):
        run_in_place_resume(
            job_dir,
            tasks_dir,
            task_image_tag=TAG,
            harbor_runner=lambda path: calls.append(path) or 0,
        )

    assert incomplete_trial.is_dir()
    assert calls == []
    assert not (job_dir.parent / f"{job_dir.name}.attempt-history").exists()


def test_run_archives_invalid_trial_and_fills_only_missing_slot(
    tmp_path: Path,
) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    valid_trial = _write_trial(job_dir, "valid", checksum=checksum, reward=0)
    invalid_trial = _write_trial(
        job_dir, "invalid", checksum=checksum, reward=None
    )
    report_dir = job_dir / "report"
    report_dir.mkdir()
    report_file = report_dir / "score-report.md"
    report_file.write_text("report\n")
    calls: list[Path] = []

    def fake_harbor(resume_dir: Path) -> int:
        calls.append(resume_dir)
        assert json.loads((resume_dir / "config.json").read_text())["job_name"] == "experiment"
        _write_trial(resume_dir, "replacement", checksum=checksum, reward=1)
        return 0

    rc = run_in_place_resume(
        job_dir,
        tasks_dir,
        task_image_tag=TAG,
        max_extra_attempts=1,
        harbor_runner=fake_harbor,
    )

    assert rc == 0
    assert calls == [job_dir.resolve()]
    assert (job_dir / "config.json.before-in-place-resume").is_file()
    assert valid_trial.is_dir()
    assert not invalid_trial.exists()
    assert report_file.read_text() == "report\n"
    history_root = job_dir.parent / f"{job_dir.name}.attempt-history"
    assert list(history_root.glob("*/task-one__invalid"))
    history = [
        json.loads(line)
        for line in (history_root / "attempt-history.jsonl").read_text().splitlines()
    ]
    assert history[0]["reason"] == "missing_reward"


def test_budget_failure_does_not_archive_or_launch(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    invalid_trial = _write_trial(
        job_dir, "invalid", checksum=checksum, reward=None
    )
    calls: list[Path] = []

    rc = run_in_place_resume(
        job_dir,
        tasks_dir,
        task_image_tag=TAG,
        max_extra_attempts=0,
        harbor_runner=lambda path: calls.append(path) or 0,
    )

    assert rc == 2
    assert invalid_trial.is_dir()
    assert calls == []


def test_dry_run_reports_insufficient_budget_without_archiving(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, n_attempts=1)
    checksum = str(load_tasks(tasks_dir)[TASK_NAME]["checksum"])
    invalid_trial = _write_trial(
        job_dir, "invalid", checksum=checksum, reward=None
    )

    rc = run_in_place_resume(
        job_dir,
        tasks_dir,
        task_image_tag=TAG,
        max_extra_attempts=0,
        dry_run=True,
    )

    assert rc == 2
    assert invalid_trial.is_dir()


def test_prebuilt_contract_rejects_force_build_true(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir)
    job_dir = _write_job(tmp_path, force_build=True)
    config = json.loads((job_dir / "config.json").read_text())
    lock = json.loads((job_dir / "lock.json").read_text())

    with pytest.raises(ResumeError, match="force_build=true"):
        validate_prebuilt_contract(
            config,
            lock,
            tasks_dir,
            task_image_tag=TAG,
        )


def test_prebuilt_contract_rejects_different_image_tag(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir, tag="2026-08-27")
    job_dir = _write_job(tmp_path)
    config = json.loads((job_dir / "config.json").read_text())
    lock = json.loads((job_dir / "lock.json").read_text())

    with pytest.raises(ResumeError, match="does not match expected"):
        validate_prebuilt_contract(
            config,
            lock,
            tasks_dir,
            task_image_tag=TAG,
        )
