"""Resume one Harbor job directory until all planned trials have valid rewards.

This is deliberately different from sharded_eval's cross-job reuse. The
Harbor job's original trial plan remains authoritative. Completed trials whose
checksum still matches the prepared task and whose reward field is present
(including reward 0) stay in place. Incomplete or ineligible trial directories
are moved to a recoverable sibling history directory before Harbor fills the
vacated planned slots in the original job directory.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

from workbuddy_bench.runner.sharded_eval import load_tasks
from workbuddy_bench.runner.task_images import resolve_task_images


class ResumeError(RuntimeError):
    """Raised when an in-place resume cannot proceed safely."""


# Trials that crashed on the model provider side, the network, or the container
# runtime never produced a real agent attempt. Harbor still writes a graded
# ``result.json`` for them, so the verifier reward (usually 0.0) reflects an
# empty or truncated workspace rather than model behaviour. Keeping such a
# reward would bias scores downward, so these trials are archived and retried
# even though a non-null reward is present.
#
# Names are Harbor's ``type(exception).__name__`` values; see
# harbor.agents.installed.base, harbor.trial.errors and harbor.environments.base.
RETRYABLE_AGENT_EXCEPTIONS = frozenset(
    {
        "NonZeroAgentExitCodeError",
        "ApiError",
        "ApiRateLimitError",
        "ApiUsageLimitError",
        "ApiInternalServerError",
        "ApiOverloadedError",
        "ApiConnectionClosedError",
        "UnknownApiError",
        "NetworkConnectionError",
    }
)

RETRYABLE_INFRA_EXCEPTIONS = frozenset(
    {
        "CancelledError",
        # Bare RuntimeError is what Harbor raises for failed `docker compose`
        # environment operations.
        "RuntimeError",
        "OSError",
        "SandboxBuildFailedError",
        "HealthcheckError",
        "AgentSetupTimeoutError",
        "EnvironmentStartTimeoutError",
        "VerifierTimeoutError",
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
    }
)

DEFAULT_RETRYABLE_EXCEPTIONS = RETRYABLE_AGENT_EXCEPTIONS | RETRYABLE_INFRA_EXCEPTIONS

# Deliberately NOT retried: these are genuine evaluation outcomes, not crashes.
# ``AgentTimeoutError`` means the agent burned its own wall-clock budget, and
# the context/output length errors mean the model produced too much. Their
# rewards grade real behaviour.
NON_RETRYABLE_EXCEPTIONS = frozenset(
    {
        "AgentTimeoutError",
        "ContextLengthExceededError",
        "OutputLengthExceededError",
    }
)


def is_retryable_exception(
    exception_type: str | None,
    *,
    retry_types: frozenset[str] = DEFAULT_RETRYABLE_EXCEPTIONS,
    keep_types: frozenset[str] = frozenset(),
) -> bool:
    """Return True when a trial crashed instead of producing a real attempt."""

    if not exception_type:
        return False
    if exception_type in keep_types:
        return False
    if exception_type in retry_types:
        return True
    # Harbor may add further ApiError subclasses; treat unseen provider errors as
    # crashes rather than silently scoring them as agent failures. Gated on the
    # base class still being in the policy, so a caller that narrows the policy
    # (e.g. --no-retry-crashed) is not overridden by the heuristic.
    return "ApiError" in retry_types and exception_type.endswith("ApiError")


@dataclass(frozen=True)
class BootstrapInfo:
    job_dir: Path
    instance_id: str
    dataset_path: str
    proxy_mode: str
    proxy_url: str
    proxy_host: str
    proxy_port: int | None
    model_route: str
    planned_tasks: tuple[str, ...]


@dataclass(frozen=True)
class InvalidTrial:
    path: Path
    task_name: str
    reason: str


@dataclass(frozen=True)
class ResumePlan:
    planned_by_task: dict[str, int]
    valid_by_task: dict[str, int]
    invalid_trials: tuple[InvalidTrial, ...]

    @property
    def planned_total(self) -> int:
        return sum(self.planned_by_task.values())

    @property
    def valid_total(self) -> int:
        return sum(self.valid_by_task.values())

    @property
    def attempts_needed(self) -> int:
        return self.planned_total - self.valid_total


def _read_json(path: Path, *, label: str) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ResumeError(f"{label} not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResumeError(f"{label} is not readable JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ResumeError(f"{label} must contain a JSON object: {path}")
    return data


def _job_files(job_dir: Path) -> tuple[Path, dict, dict]:
    try:
        resolved = job_dir.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ResumeError(f"job directory does not exist: {job_dir}") from exc
    if not resolved.is_dir():
        raise ResumeError(f"job path is not a directory: {resolved}")
    config = _read_json(resolved / "config.json", label="Harbor job config")
    lock = _read_json(resolved / "lock.json", label="Harbor job lock")
    trials = lock.get("trials")
    if not isinstance(trials, list) or not trials:
        raise ResumeError(f"Harbor job lock has no planned trials: {resolved / 'lock.json'}")
    return resolved, config, lock


def _job_identity_needs_materializing(job_dir: Path, config: dict) -> bool:
    configured_root = Path(str(config.get("jobs_dir") or "jobs")).expanduser()
    if not configured_root.is_absolute():
        configured_root = Path.cwd() / configured_root
    if configured_root.resolve() != job_dir.parent.resolve():
        raise ResumeError(
            f"recorded jobs_dir {configured_root.resolve()} does not contain "
            f"the requested experiment {job_dir}"
        )
    recorded_name = config.get("job_name")
    if recorded_name and str(recorded_name) != job_dir.name:
        raise ResumeError(
            f"recorded job_name {recorded_name!r} does not match directory "
            f"name {job_dir.name!r}"
        )
    return not bool(recorded_name)


def materialize_job_identity(job_dir: Path, config: dict) -> bool:
    """Persist a missing job_name so Harbor 0.18 resumes the requested directory."""

    if not _job_identity_needs_materializing(job_dir, config):
        return False
    config_path = job_dir / "config.json"
    backup_path = job_dir / "config.json.before-in-place-resume"
    if not backup_path.exists():
        shutil.copy2(config_path, backup_path)
    config["job_name"] = job_dir.name
    config_path.write_text(json.dumps(config, indent=4, ensure_ascii=False) + "\n")
    print(
        f"Recorded missing Harbor job_name={job_dir.name!r} in {config_path} "
        f"(backup: {backup_path})"
    )
    return True


def _trial_task_name(trial: dict) -> str:
    task = trial.get("task") or {}
    name = task.get("name")
    if name:
        return str(name)
    path = task.get("path")
    return Path(str(path)).name if path else ""


def _connection_from_trial(trial: dict) -> dict:
    agent = trial.get("agent") or {}
    kwargs = agent.get("kwargs") or {}
    connection = kwargs.get("connection") or {}
    return connection if isinstance(connection, dict) else {}


def _dataset_paths_from_config(config: dict) -> list[str]:
    candidates: list[str] = []
    for entry in config.get("datasets") or []:
        if isinstance(entry, dict) and entry.get("path"):
            candidates.append(str(entry["path"]))
    for entry in config.get("tasks") or []:
        if isinstance(entry, dict) and entry.get("path"):
            candidates.append(str(entry["path"]))
    return candidates


def _stage_instance_from_config(config: dict) -> tuple[str, str]:
    candidates = _dataset_paths_from_config(config)
    if len(candidates) != 1:
        raise ResumeError(
            "lightweight in-place resume requires exactly one recorded dataset "
            f"path; found {candidates}"
        )
    marker = ".workspace/tmp/staged/"
    instances = {
        suffix.split("/", 1)[0]
        for path in candidates
        if marker in path
        for suffix in [path.split(marker, 1)[1]]
        if suffix.split("/", 1)[0]
    }
    if len(instances) == 1:
        return next(iter(instances)), candidates[0]
    if not instances:
        raise ResumeError(
            "job config does not reference a WorkBuddy staged dataset; "
            "this lightweight in-place resume cannot reconstruct it"
        )
    raise ResumeError(f"job config references multiple staged instances: {sorted(instances)}")


def bootstrap_info(job_dir: Path) -> BootstrapInfo:
    """Extract the old instance and proxy identity needed by run.sh."""

    resolved, config, lock = _job_files(job_dir)
    trials = [trial for trial in lock["trials"] if isinstance(trial, dict)]

    instance_ids = {
        str(((trial.get("agent") or {}).get("kwargs") or {}).get("instance_id"))
        for trial in trials
        if ((trial.get("agent") or {}).get("kwargs") or {}).get("instance_id")
    }
    staged_instance, dataset_path = _stage_instance_from_config(config)
    if len(instance_ids) > 1:
        raise ResumeError(f"planned trials contain multiple instance ids: {sorted(instance_ids)}")
    instance_id = next(iter(instance_ids), staged_instance)
    if instance_id != staged_instance:
        raise ResumeError(
            f"trial instance id {instance_id!r} does not match staged dataset "
            f"instance {staged_instance!r}"
        )

    connections = [_connection_from_trial(trial) for trial in trials]
    modes = {str(conn.get("mode") or "direct") for conn in connections}
    if len(modes) != 1:
        raise ResumeError(f"planned trials contain multiple connection modes: {sorted(modes)}")
    mode = next(iter(modes))
    if mode not in {"direct", "local_proxy"}:
        raise ResumeError(f"unsupported recorded connection mode: {mode!r}")

    proxy_urls = {str(conn.get("proxy_url")) for conn in connections if conn.get("proxy_url")}
    if len(proxy_urls) > 1:
        raise ResumeError(f"planned trials contain multiple proxy URLs: {sorted(proxy_urls)}")
    proxy_url = next(iter(proxy_urls), "")
    model_routes = {
        str(conn.get("model_route")) for conn in connections if conn.get("model_route")
    }
    if len(model_routes) > 1:
        raise ResumeError(
            f"planned trials contain multiple model routes: {sorted(model_routes)}"
        )
    model_route = next(iter(model_routes), "")
    proxy_host = ""
    proxy_port: int | None = None
    if mode == "local_proxy":
        if not proxy_url:
            raise ResumeError("local_proxy job has no recorded proxy_url in lock.json")
        parsed = urlparse(proxy_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ResumeError(f"unsupported recorded local proxy URL: {proxy_url!r}")
        proxy_host = parsed.hostname
        proxy_port = parsed.port or 80

    return BootstrapInfo(
        job_dir=resolved,
        instance_id=instance_id,
        dataset_path=dataset_path,
        proxy_mode=mode,
        proxy_url=proxy_url,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        model_route=model_route,
        planned_tasks=tuple(planned_counts(lock)),
    )


def emit_bootstrap_shell(info: BootstrapInfo) -> str:
    values = {
        "RESUME_IN_PLACE_PATH": str(info.job_dir),
        "RESUME_INSTANCE_ID": info.instance_id,
        "RESUME_DATASET_PATH": info.dataset_path,
        "RESUME_PROXY_MODE": info.proxy_mode,
        "RESUME_PROXY_URL": info.proxy_url,
        "RESUME_PROXY_HOST": info.proxy_host,
        "RESUME_PROXY_PORT": "" if info.proxy_port is None else str(info.proxy_port),
        "RESUME_MODEL_ROUTE": info.model_route,
        "RESUME_TASKS_JSON": json.dumps(info.planned_tasks),
    }
    return "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())


def planned_counts(lock: dict) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for raw in lock.get("trials") or []:
        if not isinstance(raw, dict):
            raise ResumeError("lock.json contains a non-object planned trial")
        task_name = _trial_task_name(raw)
        if not task_name:
            raise ResumeError("lock.json contains a planned trial without a task name")
        counts[task_name] += 1
    if not counts:
        raise ResumeError("lock.json contains no named planned trials")
    return dict(sorted(counts.items()))


def validate_prebuilt_contract(
    config: dict,
    lock: dict,
    tasks_dir: Path,
    *,
    task_image_tag: str,
) -> dict[str, dict[str, object]]:
    """Validate the minimum no-build contract and return current task checksums."""

    environment = config.get("environment") or {}
    if environment.get("force_build", False) is not False:
        raise ResumeError(
            "the recorded Harbor job has force_build=true; it cannot be resumed "
            "under the mandatory prebuilt-image policy"
        )
    force_build_trials = [
        index
        for index, trial in enumerate(lock.get("trials") or [])
        if isinstance(trial, dict)
        and ((trial.get("environment") or {}).get("force_build", False) is not False)
    ]
    if force_build_trials:
        raise ResumeError(
            "the recorded Harbor lock contains force_build=true trial(s): "
            f"{force_build_trials}"
        )

    planned = planned_counts(lock)
    current_tasks = load_tasks(tasks_dir, set(planned))
    missing = sorted(set(planned) - set(current_tasks))
    if missing:
        raise ResumeError(f"prepared staged dataset is missing planned tasks: {missing}")

    expected_images = {
        image.task_name: image.reference
        for image in resolve_task_images(
            tasks_dir,
            tag=task_image_tag,
            include_tasks=planned,
            compute_source_hash=False,
        )
    }
    image_errors: list[str] = []
    for task_name in planned:
        task_toml = tasks_dir / task_name / "task.toml"
        try:
            task_config = tomllib.loads(task_toml.read_text())
        except (FileNotFoundError, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ResumeError(f"cannot read prepared task config {task_toml}: {exc}") from exc
        docker_image = str((task_config.get("environment") or {}).get("docker_image") or "")
        if not docker_image:
            image_errors.append(f"{task_name}: missing environment.docker_image")
        elif docker_image != expected_images[task_name]:
            image_errors.append(
                f"{task_name}: prepared image {docker_image!r} does not match "
                f"expected {expected_images[task_name]!r}"
            )
    if image_errors:
        raise ResumeError("prebuilt task-image contract failed:\n  " + "\n  ".join(image_errors))
    return current_tasks


def _task_name_from_trial_dir(trial_dir: Path, result: dict | None) -> str:
    if result:
        task_id = result.get("task_id") or {}
        if isinstance(task_id, dict) and task_id.get("path"):
            return Path(str(task_id["path"])).name
        result_config = result.get("config") or {}
        task = (
            (result_config.get("task") or {})
            if isinstance(result_config, dict)
            else {}
        )
        if isinstance(task, dict) and task.get("path"):
            return Path(str(task["path"])).name
        raw_name = result.get("task_name")
        if raw_name:
            return str(raw_name).rsplit("/", 1)[-1]

    config_path = trial_dir / "config.json"
    if config_path.is_file():
        try:
            trial_config = json.loads(config_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if isinstance(trial_config, dict):
            task = trial_config.get("task") or {}
            if isinstance(task, dict) and task.get("path"):
                return Path(str(task["path"])).name
    if "__" in trial_dir.name:
        return trial_dir.name.split("__", 1)[0]
    return ""


def build_resume_plan(
    job_dir: Path,
    *,
    planned_by_task: dict[str, int],
    current_tasks: dict[str, dict[str, object]],
    retry_exceptions: frozenset[str] = DEFAULT_RETRYABLE_EXCEPTIONS,
    keep_exceptions: frozenset[str] = frozenset(),
) -> ResumePlan:
    valid: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    invalid: list[InvalidTrial] = []
    trial_dir_count = 0

    for trial_dir in sorted(path for path in job_dir.iterdir() if path.is_dir()):
        looks_like_trial = (
            "__" in trial_dir.name
            or (trial_dir / "config.json").exists()
            or (trial_dir / "result.json").exists()
        )
        if not looks_like_trial:
            raise ResumeError(
                f"unrecognized directory inside Harbor job; move it out before resume: {trial_dir}"
            )
        trial_dir_count += 1

        config_path = trial_dir / "config.json"
        result_path = trial_dir / "result.json"
        result: dict | None = None
        config_reason = ""
        result_reason = ""
        if not config_path.is_file():
            config_reason = "missing_trial_config"
        else:
            try:
                raw_config = json.loads(config_path.read_text())
                if not isinstance(raw_config, dict):
                    config_reason = "invalid_trial_config"
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                config_reason = "invalid_trial_config"

        if not result_path.is_file():
            result_reason = "missing_result"
        else:
            try:
                raw_result = json.loads(result_path.read_text())
                if isinstance(raw_result, dict):
                    result = raw_result
                else:
                    result_reason = "invalid_result_json"
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                result_reason = "invalid_result_json"

        task_name = _task_name_from_trial_dir(trial_dir, result)
        if not task_name:
            task_name = "<unknown>"
        elif task_name not in planned_by_task:
            raise ResumeError(
                f"trial {trial_dir.name} belongs to task {task_name!r}, which is not in lock.json"
            )
        if task_name != "<unknown>":
            seen[task_name] += 1

        reason = config_reason or result_reason
        if result is not None:
            exception_info = result.get("exception_info")
            exception_type = (
                exception_info.get("exception_type")
                if isinstance(exception_info, dict)
                else None
            )
            verifier_result = result.get("verifier_result")
            rewards = (
                verifier_result.get("rewards")
                if isinstance(verifier_result, dict)
                else None
            )
            reward = rewards.get("reward") if isinstance(rewards, dict) else None
            # Checksum drift is a dataset-wide problem, so it still aborts the
            # whole resume before any archiving, even for a trial that would be
            # retried anyway.
            if (
                task_name != "<unknown>"
                and reward is not None
                and result.get("task_checksum") != current_tasks[task_name]["checksum"]
            ):
                raise ResumeError(
                    f"trial {trial_dir.name} checksum does not match the prepared "
                    "task; in-place resume cannot change the original job config"
                )
            if not reason:
                if task_name == "<unknown>":
                    reason = "unknown_task"
                elif is_retryable_exception(
                    exception_type,
                    retry_types=retry_exceptions,
                    keep_types=keep_exceptions,
                ):
                    # A crashed trial is archived even when it carries a reward:
                    # that reward grades an empty or truncated workspace rather
                    # than model behaviour.
                    reason = (
                        "cancelled"
                        if exception_type == "CancelledError"
                        else f"crashed:{exception_type}"
                    )
                elif reward is None:
                    reason = "missing_reward"

        if reason:
            invalid.append(InvalidTrial(trial_dir, task_name, reason))
        else:
            valid[task_name] += 1

    overfull = {
        task_name: count
        for task_name, count in seen.items()
        if count > planned_by_task[task_name]
    }
    if overfull:
        raise ResumeError(f"job directory has more trial dirs than planned: {overfull}")
    if trial_dir_count > sum(planned_by_task.values()):
        raise ResumeError(
            f"job directory has {trial_dir_count} trial dirs but lock.json plans only "
            f"{sum(planned_by_task.values())}"
        )

    return ResumePlan(
        planned_by_task=planned_by_task,
        valid_by_task={name: valid.get(name, 0) for name in planned_by_task},
        invalid_trials=tuple(invalid),
    )


def _print_plan(plan: ResumePlan, *, used: int, budget: int) -> None:
    print(
        "In-place resume plan: "
        f"planned={plan.planned_total} valid={plan.valid_total} "
        f"invalid_to_archive={len(plan.invalid_trials)} "
        f"attempts_needed={plan.attempts_needed} "
        f"extra_attempts_used={used}/{budget}"
    )
    reason_counts = Counter(item.reason for item in plan.invalid_trials)
    if reason_counts:
        print(
            "  archive reasons: "
            + " ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
        )
    invalid_by_task: Counter[str] = Counter(item.task_name for item in plan.invalid_trials)
    for task_name, target in plan.planned_by_task.items():
        valid = plan.valid_by_task[task_name]
        if valid != target or invalid_by_task[task_name]:
            print(
                f"  {task_name}: valid={valid}/{target} "
                f"invalid={invalid_by_task[task_name]} "
                f"missing={target - valid - invalid_by_task[task_name]}"
            )
    for item in plan.invalid_trials:
        print(f"  archive {item.path.name}: {item.reason}")


def archive_invalid_trials(
    job_dir: Path,
    invalid_trials: Iterable[InvalidTrial],
    *,
    round_number: int,
) -> Path | None:
    items = list(invalid_trials)
    if not items:
        return None
    history_root = job_dir.parent / f"{job_dir.name}.attempt-history"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    round_dir = history_root / f"{timestamp}-round-{round_number}"
    round_dir.mkdir(parents=True, exist_ok=False)
    history_path = history_root / "attempt-history.jsonl"
    for item in items:
        destination = round_dir / item.path.name
        shutil.move(str(item.path), str(destination))
        record = {
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "round": round_number,
            "task_name": item.task_name,
            "trial_name": item.path.name,
            "reason": item.reason,
            "archived_to": str(destination),
        }
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Archived {len(items)} invalid trial(s) to {round_dir}")
    return round_dir


HarborRunner = Callable[[Path], int]


def _default_harbor_runner(job_dir: Path) -> int:
    completed = subprocess.run(
        ["harbor", "job", "resume", "--job-path", str(job_dir)],
        check=False,
    )
    return completed.returncode


def run_in_place_resume(
    job_dir: Path,
    tasks_dir: Path,
    *,
    task_image_tag: str,
    max_extra_attempts: int | None = None,
    dry_run: bool = False,
    retry_exceptions: frozenset[str] = DEFAULT_RETRYABLE_EXCEPTIONS,
    keep_exceptions: frozenset[str] = frozenset(),
    harbor_runner: HarborRunner = _default_harbor_runner,
) -> int:
    resolved, config, lock = _job_files(job_dir)
    planned = planned_counts(lock)
    current_tasks = validate_prebuilt_contract(
        config,
        lock,
        tasks_dir,
        task_image_tag=task_image_tag,
    )
    planned_total = sum(planned.values())
    needs_job_name = _job_identity_needs_materializing(resolved, config)
    budget = planned_total if max_extra_attempts is None else max_extra_attempts
    if budget < 0:
        raise ResumeError("max extra attempts must be >= 0")

    used = 0
    round_number = 0
    while True:
        plan = build_resume_plan(
            resolved,
            planned_by_task=planned,
            current_tasks=current_tasks,
            retry_exceptions=retry_exceptions,
            keep_exceptions=keep_exceptions,
        )
        _print_plan(plan, used=used, budget=budget)
        if needs_job_name:
            print(
                f"  config identity: job_name is missing and will be set to "
                f"{resolved.name!r} before Harbor runs"
            )
        if plan.attempts_needed == 0:
            print(
                f"In-place resume complete: valid={plan.valid_total}/{plan.planned_total} "
                f"extra_attempts_used={used}"
            )
            return 0
        remaining_budget = budget - used
        if plan.attempts_needed > remaining_budget:
            print(
                "ERROR: in-place resume needs "
                f"{plan.attempts_needed} attempt(s), but only {remaining_budget} "
                "remain in the extra-attempt budget.",
                file=sys.stderr,
            )
            return 2
        if dry_run:
            return 0

        round_number += 1
        if needs_job_name:
            materialize_job_identity(resolved, config)
            needs_job_name = False
        archive_invalid_trials(
            resolved,
            plan.invalid_trials,
            round_number=round_number,
        )
        returncode = harbor_runner(resolved)
        used += plan.attempts_needed
        if returncode != 0:
            print(
                f"ERROR: Harbor in-place resume failed with exit code {returncode}; "
                "archived trials were preserved in attempt history.",
                file=sys.stderr,
            )
            return returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser(
        "bootstrap", help="Read the old staged instance and proxy identity."
    )
    bootstrap.add_argument("--job-dir", type=Path, required=True)
    bootstrap.add_argument("--emit-shell", action="store_true")

    run = subparsers.add_parser(
        "run", help="Archive ineligible trials and resume the Harbor job in place."
    )
    run.add_argument("--job-dir", type=Path, required=True)
    run.add_argument("--tasks-dir", type=Path, required=True)
    run.add_argument("--task-image-tag", required=True)
    run.add_argument(
        "--max-extra-attempts",
        type=int,
        default=None,
        help="Maximum new trial attempts; default is the job's total planned trials.",
    )
    run.add_argument(
        "--retry-exception",
        action="append",
        default=[],
        metavar="TYPE",
        help=(
            "Additional Harbor exception_type to treat as a crash and retry even "
            "when the trial carries a reward. Repeatable."
        ),
    )
    run.add_argument(
        "--keep-exception",
        action="append",
        default=[],
        metavar="TYPE",
        help=(
            "Harbor exception_type to keep as a valid result instead of retrying "
            "it, e.g. --keep-exception UnknownApiError. Repeatable; wins over "
            "--retry-exception."
        ),
    )
    run.add_argument(
        "--no-retry-crashed",
        action="store_true",
        help=(
            "Only retry trials with a missing reward. Keeps API, network and "
            "container crashes that already carry a reward."
        ),
    )
    run.add_argument("--dry-run", action="store_true")
    return parser


def _resolve_exception_policy(args: argparse.Namespace) -> tuple[frozenset[str], frozenset[str]]:
    keep = frozenset(args.keep_exception or ())
    if args.no_retry_crashed:
        if args.retry_exception:
            raise ResumeError("--no-retry-crashed cannot be combined with --retry-exception")
        # CancelledError has no usable reward anyway; keep archiving it.
        return frozenset({"CancelledError"}), keep
    conflicting = sorted(keep & frozenset(args.retry_exception or ()))
    if conflicting:
        raise ResumeError(
            f"exception type(s) passed to both --retry-exception and --keep-exception: {conflicting}"
        )
    return DEFAULT_RETRYABLE_EXCEPTIONS | frozenset(args.retry_exception or ()), keep


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "bootstrap":
            info = bootstrap_info(args.job_dir)
            if args.emit_shell:
                print(emit_bootstrap_shell(info))
            else:
                print(
                    json.dumps(
                        {
                            "job_dir": str(info.job_dir),
                            "instance_id": info.instance_id,
                            "dataset_path": info.dataset_path,
                            "proxy_mode": info.proxy_mode,
                            "proxy_url": info.proxy_url,
                            "proxy_host": info.proxy_host,
                            "proxy_port": info.proxy_port,
                            "model_route": info.model_route,
                            "planned_tasks": info.planned_tasks,
                        },
                        indent=2,
                    )
                )
            return 0
        retry_exceptions, keep_exceptions = _resolve_exception_policy(args)
        return run_in_place_resume(
            args.job_dir,
            args.tasks_dir,
            task_image_tag=args.task_image_tag,
            max_extra_attempts=args.max_extra_attempts,
            dry_run=args.dry_run,
            retry_exceptions=retry_exceptions,
            keep_exceptions=keep_exceptions,
        )
    except ResumeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
