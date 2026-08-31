"""Trial-validity tests for crashed in-place resume attempts.

The regression these cover: Harbor writes a graded ``result.json`` even when the
agent never really ran, e.g. when the model provider API returned an error or
`docker compose` failed. Those trials carry a non-null reward (usually 0.0) that
grades an empty workspace, so keeping them silently biases scores downward.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workbuddy_bench.runner.in_place_resume import (
    DEFAULT_RETRYABLE_EXCEPTIONS,
    ResumeError,
    build_resume_plan,
    is_retryable_exception,
)

CHECKSUM = "abc123"
TASK = "some-task"


def write_trial(
    job_dir: Path,
    trial_name: str,
    *,
    reward: object = 0.0,
    exception_type: str | None = None,
    checksum: str = CHECKSUM,
    task_name: str = TASK,
) -> Path:
    trial_dir = job_dir / trial_name
    trial_dir.mkdir(parents=True)
    (trial_dir / "config.json").write_text(json.dumps({"task": {"path": f"tasks/{task_name}"}}))
    result: dict[str, object] = {
        "task_id": {"path": f"tasks/{task_name}"},
        "task_checksum": checksum,
        "verifier_result": {"rewards": {"reward": reward}},
    }
    if exception_type:
        result["exception_info"] = {
            "exception_type": exception_type,
            "exception_message": "boom",
        }
    (trial_dir / "result.json").write_text(json.dumps(result))
    return trial_dir


def plan_for(job_dir: Path, *, planned: int = 1, **kwargs):
    return build_resume_plan(
        job_dir,
        planned_by_task={TASK: planned},
        current_tasks={TASK: {"checksum": CHECKSUM}},
        **kwargs,
    )


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
def test_crashed_trial_with_reward_is_retried(tmp_path: Path, exception_type: str) -> None:
    write_trial(tmp_path, f"{TASK}__abc", reward=0.0, exception_type=exception_type)
    plan = plan_for(tmp_path)
    assert plan.valid_total == 0
    assert plan.attempts_needed == 1
    assert [item.reason for item in plan.invalid_trials] == [f"crashed:{exception_type}"]


def test_crashed_trial_with_positive_reward_is_still_retried(tmp_path: Path) -> None:
    # A truncated run can still score partial credit; that score is not usable.
    write_trial(tmp_path, f"{TASK}__abc", reward=0.7, exception_type="UnknownApiError")
    plan = plan_for(tmp_path)
    assert plan.valid_total == 0
    assert plan.attempts_needed == 1


def test_clean_trial_with_zero_reward_is_kept(tmp_path: Path) -> None:
    write_trial(tmp_path, f"{TASK}__abc", reward=0.0, exception_type=None)
    plan = plan_for(tmp_path)
    assert plan.valid_total == 1
    assert plan.attempts_needed == 0
    assert plan.invalid_trials == ()


def test_agent_timeout_is_a_real_outcome_and_kept(tmp_path: Path) -> None:
    write_trial(tmp_path, f"{TASK}__abc", reward=0.0, exception_type="AgentTimeoutError")
    plan = plan_for(tmp_path)
    assert plan.valid_total == 1
    assert plan.invalid_trials == ()


def test_cancelled_keeps_its_own_reason(tmp_path: Path) -> None:
    write_trial(tmp_path, f"{TASK}__abc", reward=None, exception_type="CancelledError")
    plan = plan_for(tmp_path)
    assert [item.reason for item in plan.invalid_trials] == ["cancelled"]


def test_missing_reward_without_exception_still_retried(tmp_path: Path) -> None:
    write_trial(tmp_path, f"{TASK}__abc", reward=None, exception_type=None)
    plan = plan_for(tmp_path)
    assert [item.reason for item in plan.invalid_trials] == ["missing_reward"]


def test_keep_exceptions_opts_out_of_retrying_api_crashes(tmp_path: Path) -> None:
    write_trial(tmp_path, f"{TASK}__abc", reward=0.0, exception_type="UnknownApiError")
    plan = plan_for(tmp_path, keep_exceptions=frozenset({"UnknownApiError"}))
    assert plan.valid_total == 1
    assert plan.invalid_trials == ()


def test_extra_retry_exception_is_honoured(tmp_path: Path) -> None:
    write_trial(tmp_path, f"{TASK}__abc", reward=0.0, exception_type="WeirdCustomError")
    kept = plan_for(tmp_path)
    assert kept.valid_total == 1
    retried = plan_for(
        tmp_path,
        retry_exceptions=DEFAULT_RETRYABLE_EXCEPTIONS | frozenset({"WeirdCustomError"}),
    )
    assert retried.valid_total == 0
    assert [item.reason for item in retried.invalid_trials] == ["crashed:WeirdCustomError"]


def test_legacy_policy_keeps_crashed_trials_with_reward(tmp_path: Path) -> None:
    # --no-retry-crashed narrows the policy to CancelledError only.
    write_trial(tmp_path, f"{TASK}__abc", reward=0.0, exception_type="UnknownApiError")
    plan = plan_for(tmp_path, retry_exceptions=frozenset({"CancelledError"}))
    assert plan.valid_total == 1
    assert plan.invalid_trials == ()


def test_mixed_job_counts_only_clean_trials(tmp_path: Path) -> None:
    write_trial(tmp_path, f"{TASK}__ok", reward=1.0, exception_type=None)
    write_trial(tmp_path, f"{TASK}__api", reward=0.0, exception_type="UnknownApiError")
    write_trial(tmp_path, f"{TASK}__none", reward=None, exception_type=None)
    plan = plan_for(tmp_path, planned=3)
    assert plan.valid_total == 1
    assert plan.attempts_needed == 2
    assert sorted(item.reason for item in plan.invalid_trials) == [
        "crashed:UnknownApiError",
        "missing_reward",
    ]


def test_checksum_drift_on_crashed_trial_still_aborts(tmp_path: Path) -> None:
    write_trial(
        tmp_path,
        f"{TASK}__abc",
        reward=0.0,
        exception_type="UnknownApiError",
        checksum="stale",
    )
    with pytest.raises(ResumeError, match="checksum"):
        plan_for(tmp_path)


def test_unknown_api_subclasses_are_retried_by_suffix() -> None:
    assert is_retryable_exception("SomeNewProviderApiError")
    assert not is_retryable_exception(None)
    assert not is_retryable_exception("AgentTimeoutError")
