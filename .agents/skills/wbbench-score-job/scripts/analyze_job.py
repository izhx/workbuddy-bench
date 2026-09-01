#!/usr/bin/env python3
"""Score and audit one WorkBuddy Bench Harbor run.

The script is intentionally standalone (stdlib only). It understands the
canonical score contracts for Office, Web, Code, and SEC and keeps the Harbor
input tree read-only.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import tomllib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_REPORT_DIRNAME = "report-wbb"
SUBSETS: dict[str, dict[str, Any]] = {
    "office": {
        "dataset_id": "wb-bench-office-v1.0",
        "display_name": "WorkBuddyBench-Office",
        "full_task_count": 50,
        "score_contract": "composite_score_json",
    },
    "web": {
        "dataset_id": "wb-bench-web-v1.0",
        "display_name": "WB-Bench-Web",
        "full_task_count": 70,
        "score_contract": "composite_score_json",
    },
    "code": {
        "dataset_id": "wb-bench-code-v1.0",
        "display_name": "WB-Bench-Code",
        "full_task_count": 80,
        "score_contract": "composite_score_json",
    },
    "sec": {
        "dataset_id": "wb-bench-sec-v1.0",
        "display_name": "WB-Bench-SEC",
        "full_task_count": 60,
        "score_contract": "task_native_harbor_result",
    },
}

DATASET_RE = re.compile(r"wb-bench-(office|web|code|sec)(?:-v\d+(?:\.\d+)*)?", re.I)
OUTPUT_MISSING_RE = re.compile(
    r"(?:findings\.json|report\.jsonl|poc\.(?:json|py)|score\.json|reward\.(?:txt|json))"
    r"[^\n]{0,100}(?:not found|missing|no such file)|"
    r"(?:not found|missing|no such file)[^\n]{0,100}"
    r"(?:findings\.json|report\.jsonl|poc\.(?:json|py)|score\.json|reward\.(?:txt|json))",
    re.I,
)
API_RE = re.compile(
    r"UnknownApiError|Api(?:Timeout|UsageLimit|RateLimit|Connection)?Error|"
    r"API Error|ECONNRESET|unable to connect to API|429 too many requests",
    re.I,
)
API_SIGNATURE_RE = re.compile(
    r"API Error[^\n]{0,220}|ECONNRESET[^\n]{0,220}|"
    r"unable to connect to API[^\n]{0,220}|429 too many requests[^\n]{0,220}",
    re.I,
)


class AnalysisError(RuntimeError):
    """User-facing input or artifact error."""


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "file not found"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _read_text_limited(path: Path, limit: int = 1_000_000) -> str:
    try:
        with path.open("rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return ""
    if len(data) > limit:
        data = data[:limit]
    return data.decode("utf-8", errors="replace")


def _unit_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return None
    return number


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _nested(mapping: Any, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _trial_dirs(run_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in run_dir.iterdir()
        if path.is_dir()
        and "__" in path.name
        and ((path / "result.json").exists() or (path / "trial.log").exists())
    )


def _looks_like_run(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / "trial.log").is_file():
        return False
    try:
        trials = _trial_dirs(path)
    except OSError:
        return False
    return bool(trials) and any((path / name).is_file() for name in ("config.json", "lock.json", "job.log"))


def resolve_run_dir(input_dir: Path) -> Path:
    path = input_dir.expanduser().resolve()
    if not path.is_dir():
        raise AnalysisError(f"input is not a directory: {path}")
    if (path / "trial.log").is_file():
        raise AnalysisError(f"input is a trial directory, not a run: {path}")
    if _looks_like_run(path):
        return path
    candidates = sorted(child for child in path.iterdir() if _looks_like_run(child))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AnalysisError(f"no Harbor run found directly under: {path}")
    rendered = "\n  ".join(str(item) for item in candidates)
    raise AnalysisError(
        f"multiple Harbor runs found; pass one exact run directory:\n  {rendered}"
    )


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def detect_subset(run_dir: Path, run_config: Any, lock: Any) -> str:
    matches: set[str] = set()
    for value in (run_config, lock):
        for text in _walk_strings(value):
            matches.update(match.lower() for match in DATASET_RE.findall(text))
    if not matches:
        for match in DATASET_RE.findall(str(run_dir)):
            matches.add(match.lower())
    if len(matches) != 1:
        detail = ", ".join(sorted(matches)) or "none"
        raise AnalysisError(f"could not identify exactly one WB-Bench subset (found: {detail})")
    return next(iter(matches))


def _repo_root_from_script() -> Path:
    # <repo>/.agents/skills/wbbench-score-job/scripts/analyze_job.py
    return Path(__file__).resolve().parents[4]


def resolve_dataset_root(subset: str, supplied: Path | None) -> Path | None:
    dataset_id = SUBSETS[subset]["dataset_id"]
    candidates: list[Path] = []
    if supplied is not None:
        base = supplied.expanduser().resolve()
        candidates.extend((base, base / dataset_id))
    candidates.extend(
        (
            _repo_root_from_script() / "datasets" / dataset_id,
            Path.cwd() / "datasets" / dataset_id,
        )
    )
    for candidate in candidates:
        if (candidate / "dataset.toml").is_file() and (candidate / "tasks").is_dir():
            return candidate.resolve()
    if supplied is not None:
        raise AnalysisError(
            f"--dataset-root does not resolve to {dataset_id}: {supplied}"
        )
    return None


def _task_name_from_lock_entry(entry: Any) -> str | None:
    task = entry.get("task") if isinstance(entry, dict) else None
    if not isinstance(task, dict):
        return None
    name = task.get("name")
    if isinstance(name, str) and name:
        return name.rsplit("/", 1)[-1]
    path = task.get("path")
    return Path(path).name if isinstance(path, str) and path else None


def expected_tasks_from_lock(lock: Any) -> list[str]:
    entries = lock.get("trials") if isinstance(lock, dict) else None
    if not isinstance(entries, list):
        return []
    names = {_task_name_from_lock_entry(entry) for entry in entries}
    return sorted(name for name in names if name)


def expected_tasks_from_dataset(dataset_root: Path | None) -> list[str]:
    if dataset_root is None:
        return []
    return sorted(path.name for path in (dataset_root / "tasks").iterdir() if path.is_dir())


def load_task_metadata(dataset_root: Path | None, tasks: Iterable[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if dataset_root is None:
        return output
    for task in tasks:
        path = dataset_root / "tasks" / task / "task.toml"
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(metadata, dict):
            output[task] = {
                "category": metadata.get("category"),
                "difficulty": metadata.get("difficulty"),
            }
    return output


def _task_name_from_result(result: Any, trial_dir: Path) -> str:
    if isinstance(result, dict):
        task_name = result.get("task_name")
        if isinstance(task_name, str) and task_name:
            return task_name.rsplit("/", 1)[-1]
        for value in (
            _nested(result, "task_id", "path"),
            _nested(result, "config", "task", "path"),
        ):
            if isinstance(value, str) and value:
                return Path(value).name
    return trial_dir.name.rsplit("__", 1)[0]


def _score_from_score_json(payload: Any) -> tuple[float | None, str, int | None, int | None, str | None, bool]:
    if not isinstance(payload, dict):
        return None, "", None, None, None, False
    score = None
    source = ""
    for key in ("reward", "overall", "test_pass_rate"):
        score = _unit_float(payload.get(key))
        if score is not None:
            source = f"verifier/score.json:{key}"
            break
    passed = _nonnegative_int(payload.get("tests_passed"))
    total = _nonnegative_int(payload.get("tests_total"))
    if score is None and passed is not None and total:
        score = min(passed / total, 1.0)
        source = "verifier/score.json:tests_passed/tests_total"
    status = payload.get("test_status") if isinstance(payload.get("test_status"), str) else None
    pure_count = False
    if score is not None and passed is not None and total and "llm_judge_component_score" not in payload:
        overall = _unit_float(payload.get("overall"))
        test_rate = _unit_float(payload.get("test_pass_rate"))
        not_composite = overall is None or test_rate is None or abs(overall - test_rate) <= 1e-9
        pure_count = not_composite and abs(score - min(passed / total, 1.0)) < 5e-4
    return score, source, passed, total, status, pure_count


def _harbor_reward(result: Any) -> float | None:
    return _unit_float(_nested(result, "verifier_result", "rewards", "reward"))


def _native_artifact_reward(trial_dir: Path) -> tuple[float | None, str | None]:
    verifier_dirs = [
        trial_dir / "verifier",
        *sorted(trial_dir.glob("steps/*/verifier")),
    ]
    for verifier in verifier_dirs:
        reward_txt = verifier / "reward.txt"
        if reward_txt.is_file():
            text = _read_text_limited(reward_txt, 4096).strip()
            value = _unit_float(text.splitlines()[0] if text else None)
            if value is not None:
                return value, str(reward_txt)
        for name in ("reward.json", "rewards.json"):
            path = verifier / name
            if not path.is_file():
                continue
            payload, _ = _read_json(path)
            if isinstance(payload, dict):
                for key in ("reward", "overall", "test_pass_rate"):
                    value = _unit_float(payload.get(key))
                    if value is not None:
                        return value, str(path)
            else:
                value = _unit_float(payload)
                if value is not None:
                    return value, str(path)
    return None, None


def _excerpt(text: str, pattern: re.Pattern[str] | None = None, limit: int = 320) -> str:
    compact = " ".join(text.split())
    if pattern is not None:
        match = pattern.search(compact)
        if match:
            start = max(0, match.start() - 60)
            compact = compact[start : start + limit]
    return compact[:limit]


def classify_exception(exception: Any) -> tuple[str, str]:
    if not isinstance(exception, dict):
        return "", ""
    exc_type = str(exception.get("exception_type") or "")
    message = str(exception.get("exception_message") or "")
    text = f"{exc_type} {message}"
    lower = text.lower()
    if API_RE.search(text):
        return "api_failure", "critical"
    if "environmentstarttimeout" in lower or ("environment" in lower and "timed out" in lower):
        return "environment_start_failure", "critical"
    if "docker compose" in lower or "buildkit" in lower or "image build" in lower:
        return "docker_build_failure", "critical"
    if "verifier" in lower or "rewardfile" in lower or "reward file" in lower:
        return "verifier_failure", "critical"
    if "network" in lower or "connectionerror" in lower:
        return "network_failure", "critical"
    if "agenttimeout" in lower or ("agent" in lower and "timed out" in lower):
        return "agent_timeout", "warning"
    if "cancel" in lower:
        return "cancelled", "critical"
    if "timeout" in lower or "timed out" in lower:
        return "timeout", "warning"
    return "runtime_exception", "warning"


def _exception_records(result: Any) -> list[tuple[str, Any]]:
    if not isinstance(result, dict):
        return []
    records: list[tuple[str, Any]] = []
    if isinstance(result.get("exception_info"), dict):
        records.append(("result.json:exception_info", result["exception_info"]))
    steps = result.get("step_results")
    if isinstance(steps, list):
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("exception_info"), dict):
                continue
            name = step.get("step_name") or index
            records.append((f"result.json:step_results[{name}].exception_info", step["exception_info"]))
    return records


def _agent_token_total(result: Any) -> int | None:
    records: list[Any] = []
    if isinstance(result, dict):
        records.append(result.get("agent_result"))
        if isinstance(result.get("step_results"), list):
            records.extend(
                step.get("agent_result") for step in result["step_results"] if isinstance(step, dict)
            )
    numeric_seen = False
    total = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
            value = _nonnegative_int(record.get(key))
            if value is not None:
                numeric_seen = True
                total += value
    return total if numeric_seen else None


def _add_anomaly(
    anomalies: list[dict[str, Any]],
    *,
    severity: str,
    category: str,
    message: str,
    trial: str | None = None,
    task: str | None = None,
    evidence: str | None = None,
) -> None:
    item: dict[str, Any] = {
        "severity": severity,
        "category": category,
        "message": message,
    }
    if trial:
        item["trial"] = trial
    if task:
        item["task"] = task
    if evidence:
        item["evidence"] = evidence
    anomalies.append(item)


def _scan_sec_missing_output(trial_dir: Path) -> tuple[str | None, Path | None]:
    candidates = list((trial_dir / "verifier").glob("*.txt"))
    candidates += list((trial_dir / "verifier").glob("*.log"))
    candidates += list(trial_dir.glob("steps/*/verifier/*.txt"))
    candidates += list(trial_dir.glob("steps/*/verifier/*.log"))
    for path in sorted(set(candidates)):
        text = _read_text_limited(path)
        match = OUTPUT_MISSING_RE.search(text)
        if match:
            return _excerpt(text[match.start() : match.start() + 400]), path
    return None, None


def analyze_trial(trial_dir: Path, run_dir: Path, subset: str, anomalies: list[dict[str, Any]]) -> dict[str, Any]:
    result_path = trial_dir / "result.json"
    result, result_error = _read_json(result_path)
    task = _task_name_from_result(result, trial_dir)
    if result_error:
        _add_anomaly(
            anomalies,
            severity="critical",
            category="missing_or_malformed_result",
            message=result_error,
            trial=trial_dir.name,
            task=task,
            evidence=_relative(result_path, run_dir),
        )

    exception_categories: set[str] = set()
    exception_count = 0
    for location, exception in _exception_records(result):
        category, severity = classify_exception(exception)
        if not category:
            continue
        exception_categories.add(category)
        exception_count += 1
        exc_type = str(exception.get("exception_type") or "unknown")
        message = str(exception.get("exception_message") or "")
        if category == "api_failure":
            signature = API_SIGNATURE_RE.search(message)
            detail = _excerpt(signature.group(0)) if signature else "API request failed; inspect the recorded exception"
        else:
            detail = _excerpt(message)
        _add_anomaly(
            anomalies,
            severity=severity,
            category=category,
            message=f"{exc_type}: {detail}",
            trial=trial_dir.name,
            task=task,
            evidence=location,
        )

    harbor_reward = _harbor_reward(result)
    score = None
    score_source = ""
    tests_passed = tests_total = None
    test_status = None
    pure_count_ratio = False
    artifact_reward = None
    artifact_path = None

    if subset == "sec":
        score = harbor_reward
        if score is not None:
            score_source = "result.json:verifier_result.rewards.reward"
        else:
            _add_anomaly(
                anomalies,
                severity="critical",
                category="missing_canonical_score",
                message="SEC trial has no numeric verifier_result.rewards.reward; scored as 0",
                trial=trial_dir.name,
                task=task,
                evidence=_relative(result_path, run_dir),
            )
        artifact_reward, raw_artifact_path = _native_artifact_reward(trial_dir)
        artifact_path = _relative(Path(raw_artifact_path), run_dir) if raw_artifact_path else None
        if score is not None and artifact_reward is not None and abs(score - artifact_reward) > 5e-4:
            _add_anomaly(
                anomalies,
                severity="critical",
                category="score_disagreement",
                message=f"Harbor reward {score:.4f} != task-native reward {artifact_reward:.4f}",
                trial=trial_dir.name,
                task=task,
                evidence=artifact_path,
            )
    else:
        score_path = trial_dir / "verifier" / "score.json"
        payload, score_error = _read_json(score_path)
        if score_error:
            _add_anomaly(
                anomalies,
                severity="critical",
                category="missing_or_malformed_score_artifact",
                message=f"{score_error}; scored as 0",
                trial=trial_dir.name,
                task=task,
                evidence=_relative(score_path, run_dir),
            )
        else:
            score, score_source, tests_passed, tests_total, test_status, pure_count_ratio = _score_from_score_json(payload)
            if score is None:
                _add_anomaly(
                    anomalies,
                    severity="critical",
                    category="missing_canonical_score",
                    message="score.json has no usable numeric score; scored as 0",
                    trial=trial_dir.name,
                    task=task,
                    evidence=_relative(score_path, run_dir),
                )
        if test_status == "build_error":
            score = None
            _add_anomaly(
                anomalies,
                severity="critical",
                category="verifier_build_error",
                message="score.json declares test_status=build_error; scored as 0",
                trial=trial_dir.name,
                task=task,
                evidence=_relative(score_path, run_dir),
            )
        if score is not None and harbor_reward is not None and abs(score - harbor_reward) > 5e-4:
            _add_anomaly(
                anomalies,
                severity="critical",
                category="score_disagreement",
                message=f"canonical score.json {score:.4f} != Harbor reward {harbor_reward:.4f}",
                trial=trial_dir.name,
                task=task,
                evidence=_relative(score_path, run_dir),
            )

    effective_score = float(score) if score is not None else 0.0
    token_total = _agent_token_total(result)
    if exception_categories and effective_score > 0:
        _add_anomaly(
            anomalies,
            severity="critical",
            category="positive_score_on_failed_trial",
            message=f"reward={effective_score:.4f} despite runtime exception; inspect for default scoring",
            trial=trial_dir.name,
            task=task,
            evidence=_relative(result_path, run_dir),
        )
    if token_total == 0 and exception_categories:
        _add_anomaly(
            anomalies,
            severity="warning",
            category="zero_token_agent",
            message="agent recorded zero input/cache/output tokens on an exception trial",
            trial=trial_dir.name,
            task=task,
            evidence=_relative(result_path, run_dir),
        )
    if subset == "sec":
        missing_message, missing_path = _scan_sec_missing_output(trial_dir)
        if missing_message and missing_path:
            severity = "critical" if effective_score > 0 else "warning"
            _add_anomaly(
                anomalies,
                severity=severity,
                category="required_output_missing",
                message=missing_message,
                trial=trial_dir.name,
                task=task,
                evidence=_relative(missing_path, run_dir),
            )
            if effective_score > 0:
                _add_anomaly(
                    anomalies,
                    severity="critical",
                    category="positive_score_with_missing_output",
                    message=f"reward={effective_score:.4f} although required output is missing",
                    trial=trial_dir.name,
                    task=task,
                    evidence=_relative(missing_path, run_dir),
                )

    return {
        "trial": trial_dir.name,
        "task": task,
        "reward": effective_score,
        "has_canonical_score": score is not None,
        "score_source": score_source,
        "harbor_reward": harbor_reward,
        "native_artifact_reward": artifact_reward,
        "native_artifact_path": artifact_path,
        "tests_passed": tests_passed,
        "tests_total": tests_total,
        "test_status": test_status,
        "pure_count_ratio": pure_count_ratio,
        "token_total": token_total,
        "exception_categories": sorted(exception_categories),
        "exception_count": exception_count,
    }


def rebase_pure_count_attempts(records: list[dict[str, Any]]) -> None:
    eligible = [
        rec for rec in records
        if rec.get("pure_count_ratio") and _nonnegative_int(rec.get("tests_total"))
    ]
    if len(eligible) < 2:
        return
    max_total = max(int(rec["tests_total"]) for rec in eligible)
    if max_total <= 0:
        return
    for rec in eligible:
        passed = _nonnegative_int(rec.get("tests_passed")) or 0
        rebased = min(passed / max_total, 1.0)
        if abs(rebased - float(rec["reward"])) > 1e-9:
            rec["raw_reward"] = rec["reward"]
            rec["reward"] = rebased
            rec["score_source"] = "verifier/score.json:tests_passed/max_tests_total"


def _breakdown(per_task: dict[str, dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in per_task.values():
        value = record.get(field) or "unknown"
        groups[str(value)].append(record)
    output: dict[str, dict[str, Any]] = {}
    for key, records in sorted(groups.items()):
        output[key] = {
            "n_tasks": len(records),
            "reward": round(statistics.fmean(item["reward"] for item in records), 4),
            "coverage_adjusted_reward": round(
                statistics.fmean(item["coverage_adjusted_reward"] for item in records), 4
            ),
            "pass_rate": round(statistics.fmean(item["pass_rate"] for item in records), 4),
        }
    return output


def _anomaly_summary(anomalies: list[dict[str, Any]]) -> dict[str, Any]:
    by_severity = Counter(item["severity"] for item in anomalies)
    by_category = Counter(item["category"] for item in anomalies)
    affected_trials = {item.get("trial") for item in anomalies if item.get("trial")}
    affected_tasks = {item.get("task") for item in anomalies if item.get("task")}
    return {
        "n_items": len(anomalies),
        "by_severity": dict(sorted(by_severity.items())),
        "by_category": dict(sorted(by_category.items())),
        "n_affected_trials": len(affected_trials),
        "n_affected_tasks": len(affected_tasks),
    }


def _model_and_harness(config: Any) -> tuple[str | None, str | None]:
    agents = config.get("agents") if isinstance(config, dict) else None
    if not isinstance(agents, list) or not agents or not isinstance(agents[0], dict):
        return None, None
    agent = agents[0]
    model = agent.get("model_name") if isinstance(agent.get("model_name"), str) else None
    import_path = agent.get("import_path") if isinstance(agent.get("import_path"), str) else None
    harness = import_path.rsplit(":", 1)[-1] if import_path else None
    return model, harness


def _compact_harbor_stats(stats: Any) -> dict[str, Any] | None:
    if not isinstance(stats, dict):
        return None
    compact = {
        key: stats.get(key)
        for key in (
            "n_completed_trials",
            "n_errored_trials",
            "n_running_trials",
            "n_pending_trials",
            "n_cancelled_trials",
            "n_retries",
        )
        if key in stats
    }
    evals = stats.get("evals")
    if isinstance(evals, dict):
        compact["evals"] = {
            name: {
                key: payload.get(key)
                for key in ("n_trials", "n_errors", "metrics", "pass_at_k")
                if isinstance(payload, dict) and key in payload
            }
            for name, payload in evals.items()
        }
    return compact


def analyze(input_dir: Path, dataset_root_arg: Path | None = None) -> dict[str, Any]:
    run_dir = resolve_run_dir(input_dir)
    config, config_error = _read_json(run_dir / "config.json")
    lock, lock_error = _read_json(run_dir / "lock.json")
    run_result, run_result_error = _read_json(run_dir / "result.json")
    if config_error:
        raise AnalysisError(f"cannot read run config.json: {config_error}")
    subset = detect_subset(run_dir, config, lock)
    subset_info = SUBSETS[subset]
    dataset_root = resolve_dataset_root(subset, dataset_root_arg)

    anomalies: list[dict[str, Any]] = []
    if lock_error:
        _add_anomaly(
            anomalies,
            severity="warning",
            category="missing_or_malformed_lock",
            message=f"cannot obtain exact planned task set: {lock_error}",
            evidence="lock.json",
        )
    if run_result_error:
        _add_anomaly(
            anomalies,
            severity="critical",
            category="missing_or_malformed_run_result",
            message=run_result_error,
            evidence="result.json",
        )

    planned_tasks = expected_tasks_from_lock(lock)
    planned_source = "lock.json"
    if not planned_tasks:
        planned_tasks = expected_tasks_from_dataset(dataset_root)
        planned_source = "dataset_root" if planned_tasks else "discovered_trials"

    trial_dirs = _trial_dirs(run_dir)
    if not trial_dirs:
        raise AnalysisError(f"no direct Harbor trial directories found: {run_dir}")
    trial_records = [analyze_trial(path, run_dir, subset, anomalies) for path in trial_dirs]
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in trial_records:
        by_task[record["task"]].append(record)
    for records in by_task.values():
        records.sort(key=lambda item: item["trial"])
        if subset != "sec":
            rebase_pure_count_attempts(records)

    if not planned_tasks:
        planned_tasks = sorted(by_task)
    planned_set = set(planned_tasks)
    discovered_set = set(by_task)
    missing_tasks = sorted(planned_set - discovered_set)
    extra_tasks = sorted(discovered_set - planned_set)
    for task in missing_tasks:
        _add_anomaly(
            anomalies,
            severity="critical",
            category="missing_task",
            message="planned task produced no trial directory and is scored as 0",
            task=task,
            evidence="lock.json",
        )
    for task in extra_tasks:
        _add_anomaly(
            anomalies,
            severity="warning",
            category="unexpected_task",
            message="trial task is not in the lock plan and is excluded from the primary score",
            task=task,
        )

    expected_attempts = _nonnegative_int(config.get("n_attempts") if isinstance(config, dict) else None)
    if not expected_attempts:
        counts = [len(by_task[task]) for task in planned_tasks if by_task.get(task)]
        expected_attempts = Counter(counts).most_common(1)[0][0] if counts else 1

    metadata = load_task_metadata(dataset_root, planned_tasks)
    per_task: dict[str, dict[str, Any]] = {}
    covered_planned_slots = 0
    for task in planned_tasks:
        records = by_task.get(task, [])
        n_attempts = len(records)
        covered_planned_slots += min(n_attempts, expected_attempts)
        if n_attempts != expected_attempts:
            category = "missing_attempts" if n_attempts < expected_attempts else "extra_attempts"
            severity = "critical" if n_attempts < expected_attempts else "warning"
            _add_anomaly(
                anomalies,
                severity=severity,
                category=category,
                message=f"planned {expected_attempts} attempt(s), found {n_attempts}",
                task=task,
            )
        rewards = [float(record["reward"]) for record in records]
        full_passes = [1.0 if reward >= 1.0 else 0.0 for reward in rewards]
        reward = statistics.fmean(rewards) if rewards else 0.0
        pass_rate = statistics.fmean(full_passes) if full_passes else 0.0
        slot_denominator = max(expected_attempts, n_attempts)
        coverage_reward = sum(rewards) / slot_denominator if slot_denominator else 0.0
        coverage_pass = sum(full_passes) / slot_denominator if slot_denominator else 0.0
        meta = metadata.get(task, {})
        per_task[task] = {
            "category": meta.get("category"),
            "difficulty": meta.get("difficulty"),
            "n_attempts": n_attempts,
            "expected_attempts": expected_attempts,
            "reward": round(reward, 4),
            "coverage_adjusted_reward": round(coverage_reward, 4),
            "pass_rate": round(pass_rate, 4),
            "coverage_adjusted_pass_rate": round(coverage_pass, 4),
            "reward_min": round(min(rewards), 4) if rewards else 0.0,
            "reward_max": round(max(rewards), 4) if rewards else 0.0,
            "trials": records,
        }

    task_values = list(per_task.values())
    primary_reward = statistics.fmean(item["reward"] for item in task_values)
    primary_pass = statistics.fmean(item["pass_rate"] for item in task_values)
    coverage_reward = statistics.fmean(item["coverage_adjusted_reward"] for item in task_values)
    coverage_pass = statistics.fmean(item["coverage_adjusted_pass_rate"] for item in task_values)
    included_trials = [record for task in planned_tasks for record in by_task.get(task, [])]
    observed_trial_reward = statistics.fmean(record["reward"] for record in included_trials) if included_trials else 0.0

    run_stats = run_result.get("stats") if isinstance(run_result, dict) else None
    job_finished = bool(isinstance(run_result, dict) and run_result.get("finished_at"))
    if not job_finished:
        _add_anomaly(
            anomalies,
            severity="critical",
            category="unfinished_run",
            message="run result has no finished_at timestamp",
            evidence="result.json",
        )

    anomaly_summary = _anomaly_summary(anomalies)
    infrastructure_categories = {
        "api_failure",
        "docker_build_failure",
        "environment_start_failure",
        "network_failure",
        "verifier_failure",
        "verifier_build_error",
        "missing_or_malformed_result",
        "missing_or_malformed_run_result",
        "missing_or_malformed_score_artifact",
        "missing_canonical_score",
        "missing_task",
        "missing_attempts",
        "unfinished_run",
        "cancelled",
        "score_disagreement",
    }
    anomaly_categories = set(anomaly_summary["by_category"])
    has_infrastructure_failure = bool(anomaly_categories & infrastructure_categories)
    incomplete = bool(
        missing_tasks
        or any(len(by_task.get(task, [])) < expected_attempts for task in planned_tasks)
        or not job_finished
    )
    if incomplete:
        validity_status = "incomplete"
    elif has_infrastructure_failure:
        validity_status = "complete_with_runtime_failures"
    elif anomalies:
        validity_status = "complete_with_warnings"
    else:
        validity_status = "complete"

    model, harness = _model_and_harness(config)
    expected_slots = len(planned_tasks) * expected_attempts
    score_sources = Counter(record["score_source"] or "missing" for record in included_trials)
    output = {
        "schema_version": SCHEMA_VERSION,
        "input_dir": str(input_dir.expanduser().resolve()),
        "run_dir": str(run_dir),
        "dataset": {
            "subset": subset,
            "id": subset_info["dataset_id"],
            "display_name": subset_info["display_name"],
            "score_contract": subset_info["score_contract"],
            "full_release_task_count": subset_info["full_task_count"],
            "dataset_root": str(dataset_root) if dataset_root else None,
            "planned_task_source": planned_source,
        },
        "run": {
            "job_name": run_dir.parent.name,
            "run_name": run_dir.name,
            "run_id": run_result.get("id") if isinstance(run_result, dict) else None,
            "model": model,
            "harness": harness,
            "finished": job_finished,
            "planned_tasks": len(planned_tasks),
            "expected_attempts_per_task": expected_attempts,
            "expected_trial_slots": expected_slots,
            "discovered_trial_dirs": len(trial_records),
            "covered_planned_slots": covered_planned_slots,
            "coverage": round(covered_planned_slots / expected_slots, 4) if expected_slots else 0.0,
            "missing_tasks": missing_tasks,
            "unexpected_tasks": extra_tasks,
            "harbor_stats": _compact_harbor_stats(run_stats),
        },
        "score": {
            "reward": round(primary_reward, 4),
            "pass_rate": round(primary_pass, 4),
            "coverage_adjusted_reward": round(coverage_reward, 4),
            "coverage_adjusted_pass_rate": round(coverage_pass, 4),
            "observed_trial_reward": round(observed_trial_reward, 4),
            "n_tasks": len(per_task),
            "n_included_trials": len(included_trials),
            "n_trials_with_canonical_score": sum(bool(record["has_canonical_score"]) for record in included_trials),
            "score_sources": dict(sorted(score_sources.items())),
            "definitions": {
                "reward": "mean attempts within each planned task, then mean planned tasks",
                "pass_rate": "mean per-task fraction of attempts with final reward >= 1.0",
                "coverage_adjusted_reward": "task-balanced reward with missing planned attempt slots filled as 0",
                "observed_trial_reward": "flat mean over discovered included trials; diagnostic only",
            },
        },
        "breakdowns": {
            "category": _breakdown(per_task, "category"),
            "difficulty": _breakdown(per_task, "difficulty"),
        },
        "per_task": per_task,
        "anomalies": {
            "summary": anomaly_summary,
            "items": anomalies,
        },
        "validity": {
            "status": validity_status,
            "unqualified_model_score_usable": not has_infrastructure_failure and not incomplete,
            "has_infrastructure_failure": has_infrastructure_failure,
            "guidance": (
                "Report the numeric score only with anomaly and coverage caveats."
                if has_infrastructure_failure or incomplete
                else "No infrastructure or completeness blocker was detected."
            ),
        },
    }
    return output


def _format_breakdown(name: str, values: dict[str, Any], language: str) -> list[str]:
    header = (
        "| 分组 | 任务数 | reward | coverage-adjusted | pass_rate |"
        if language == "zh"
        else "| group | tasks | reward | coverage-adjusted | pass_rate |"
    )
    lines = [f"### {name}", "", header, "|---|---:|---:|---:|---:|"]
    for key, item in values.items():
        lines.append(
            f"| {key} | {item['n_tasks']} | {item['reward']:.4f} | "
            f"{item['coverage_adjusted_reward']:.4f} | {item['pass_rate']:.4f} |"
        )
    lines.append("")
    return lines


def render_markdown(data: dict[str, Any], language: str) -> str:
    dataset = data["dataset"]
    run = data["run"]
    score = data["score"]
    anomalies = data["anomalies"]
    validity = data["validity"]
    report = data.get("report") or {}
    if language == "en":
        lines = [
            f"# {dataset['display_name']} job score report",
            "",
            "## Result",
            "",
            f"- Run: `{data['run_dir']}`",
            f"- Report version: `{report.get('version')}`; generated at `{report.get('generated_at')}`",
            f"- Model / harness: `{run['model']}` / `{run['harness']}`",
            f"- Status: `{validity['status']}`; unqualified model score usable: `{str(validity['unqualified_model_score_usable']).lower()}`",
            f"- Coverage: {run['covered_planned_slots']}/{run['expected_trial_slots']} ({run['coverage']:.2%})",
            f"- reward: **{score['reward']:.4f}**",
            f"- pass_rate: **{score['pass_rate']:.4f}**",
            f"- coverage_adjusted_reward: **{score['coverage_adjusted_reward']:.4f}**",
            "",
            "## Anomalies",
            "",
            f"- Items: {anomalies['summary']['n_items']}; affected trials: {anomalies['summary']['n_affected_trials']}",
        ]
    else:
        lines = [
            f"# {dataset['display_name']} Job 算分与异常报告",
            "",
            "## 结果概览",
            "",
            f"- Run：`{data['run_dir']}`",
            f"- 报告版本：`{report.get('version')}`；生成时间：`{report.get('generated_at')}`",
            f"- 模型 / Harness：`{run['model']}` / `{run['harness']}`",
            f"- 有效性：`{validity['status']}`；可否无保留作为模型分数：`{str(validity['unqualified_model_score_usable']).lower()}`",
            f"- 覆盖率：{run['covered_planned_slots']}/{run['expected_trial_slots']}（{run['coverage']:.2%}）",
            f"- reward：**{score['reward']:.4f}**",
            f"- pass_rate：**{score['pass_rate']:.4f}**",
            f"- coverage_adjusted_reward：**{score['coverage_adjusted_reward']:.4f}**",
            "",
            "`reward` 是先按任务平均 attempts、再平均任务；`coverage_adjusted_reward` 仅用于观察缺失 attempt 被补零后的影响。",
            "",
            "## 异常汇总",
            "",
            f"- 异常条目：{anomalies['summary']['n_items']}；受影响 trials：{anomalies['summary']['n_affected_trials']}；受影响任务：{anomalies['summary']['n_affected_tasks']}",
        ]
    for category, count in anomalies["summary"]["by_category"].items():
        lines.append(f"- `{category}`：{count}")
    lines.extend(["", "### 异常样例" if language == "zh" else "### Anomaly samples", ""])
    if anomalies["items"]:
        for item in anomalies["items"][:30]:
            target = item.get("trial") or item.get("task") or "run"
            evidence = f"；证据 `{item['evidence']}`" if language == "zh" and item.get("evidence") else ""
            if language == "en" and item.get("evidence"):
                evidence = f"; evidence `{item['evidence']}`"
            lines.append(
                f"- [{item['severity']}] `{item['category']}` `{target}`：{item['message']}{evidence}"
                if language == "zh"
                else f"- [{item['severity']}] `{item['category']}` `{target}`: {item['message']}{evidence}"
            )
        if len(anomalies["items"]) > 30:
            lines.append(
                f"- 其余 {len(anomalies['items']) - 30} 条见 `score-analysis.json`。"
                if language == "zh"
                else f"- See `score-analysis.json` for {len(anomalies['items']) - 30} more items."
            )
    else:
        lines.append("- 未检测到异常。" if language == "zh" else "- No anomalies detected.")
    lines.append("")

    if language == "zh":
        lines.extend(_format_breakdown("按 category", data["breakdowns"]["category"], language))
        lines.extend(_format_breakdown("按 difficulty", data["breakdowns"]["difficulty"], language))
        heading = "## 任务得分（最低 10 项）"
    else:
        # Keep table field names canonical even in an English report.
        lines.extend(_format_breakdown("By category", data["breakdowns"]["category"], language))
        lines.extend(_format_breakdown("By difficulty", data["breakdowns"]["difficulty"], language))
        heading = "## Lowest-scoring tasks (up to 10)"
    lines.extend([heading, "", "| task | attempts | reward | coverage-adjusted | pass_rate |", "|---|---:|---:|---:|---:|"])
    ranked = sorted(data["per_task"].items(), key=lambda item: (item[1]["reward"], item[0]))[:10]
    for task, item in ranked:
        lines.append(
            f"| `{task}` | {item['n_attempts']}/{item['expected_attempts']} | "
            f"{item['reward']:.4f} | {item['coverage_adjusted_reward']:.4f} | {item['pass_rate']:.4f} |"
        )
    interpretation = (
        "该分数必须连同异常与覆盖率限制一起报告。"
        if language == "zh" and not validity["unqualified_model_score_usable"]
        else "未发现会阻止直接解释模型分数的基础设施或完整性问题。"
        if language == "zh"
        else validity["guidance"]
    )
    lines.extend(["", "## 解释" if language == "zh" else "## Interpretation", "", interpretation, ""])
    if dataset["subset"] == "sec":
        lines.append(
            "SEC 使用 task-native scoring；非零 reward 仍需结合 API/runtime 异常、token 和必需输出缺失判断。"
            if language == "zh"
            else "SEC uses task-native scoring; a positive reward still requires API/runtime, token, and required-output checks."
        )
        lines.append("")
    return "\n".join(lines)


def write_outputs(data: dict[str, Any], output_dir: Path, language: str, force: bool) -> tuple[Path, Path]:
    target = output_dir.expanduser().resolve()
    json_path = target / "score-analysis.json"
    report_path = target / "score-report.md"
    existing = [path for path in (json_path, report_path) if path.exists()]
    if existing and not force:
        raise AnalysisError(
            "output exists; choose another directory or pass --force: "
            + ", ".join(str(path) for path in existing)
        )
    target.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report_path.write_text(render_markdown(data, language), encoding="utf-8")
    return json_path, report_path


def report_version(report_time: datetime) -> str:
    if report_time.tzinfo is None:
        report_time = report_time.replace(tzinfo=timezone.utc)
    return report_time.astimezone(timezone.utc).strftime("%Y-%m-%d__%H-%M-%SZ")


def default_output_dir(data: dict[str, Any], version: str) -> Path:
    return (
        Path(data["run_dir"]).resolve()
        / DEFAULT_REPORT_DIRNAME
        / version
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calculate scores and audit anomalies for one WB-Bench Harbor job/run."
    )
    parser.add_argument("job_or_run_dir", type=Path)
    parser.add_argument("--dataset-root", type=Path, help="Dataset directory or parent datasets directory.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Output directory; defaults to "
            "<RUN_DIR>/report-wbb/<UTC-report-time>."
        ),
    )
    output.add_argument(
        "--stdout",
        action="store_true",
        help="Emit the full JSON to stdout instead of writing report files.",
    )
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--force", action="store_true", help="Overwrite the two known output files.")
    args = parser.parse_args(argv)
    try:
        data = analyze(args.job_or_run_dir, args.dataset_root)
        if args.stdout:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            generated_at = datetime.now(timezone.utc).replace(microsecond=0)
            version = report_version(generated_at)
            output_dir = args.output_dir or default_output_dir(data, version)
            data["report"] = {
                "version": version,
                "generated_at": generated_at.isoformat(),
                "output_dir": str(output_dir.expanduser().resolve()),
            }
            json_path, report_path = write_outputs(data, output_dir, args.language, args.force)
            print(f"analysis_json={json_path}")
            print(f"report_md={report_path}")
            print(f"reward={data['score']['reward']:.4f}")
            print(f"status={data['validity']['status']}")
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
