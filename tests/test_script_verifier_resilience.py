"""Unit tests for single-try script verifier resilience (GitHub issue #6).

The tests use a small synthetic verifier that mirrors the vulnerable pattern:
a single ``try`` block records checks via ``safe_record`` with unguarded setup
statements between them, and ``finally: write_reward()`` reports the score.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from workbuddy_bench.judge.runners.rule.script_verifier_resilience import (
    declared_check_count,
    is_vulnerable_script_verifier,
    reconcile_reward_payload,
    transform_script,
)

# A "model" whose render() crashes when cache_size == 0 — the same defect shape
# as the issue's minimal reproduction (an unhandled edge case that raises in an
# unguarded setup statement).
SYNTHETIC_VERIFIER = '''\
import json
import os
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOG_DIR", "/tmp"))
RESULTS = []

def record(name, passed, detail=""):
    RESULTS.append({"name": name, "passed": bool(passed), "detail": str(detail)})

def safe_record(name, check):
    try:
        record(name, check())
    except Exception as exc:
        record(name, False, f"{type(exc).__name__}: {exc}")

def write_reward():
    passed = sum(1 for item in RESULTS if item["passed"])
    total = len(RESULTS) or 1
    reward = {
        "overall": passed / total,
        "test_pass_rate": passed / total,
        "tests_passed": passed,
        "tests_total": total,
        "test_status": "pass" if passed == total else "no_pass",
        "tests": RESULTS,
    }
    (LOG_DIR / "reward.json").write_text(json.dumps(reward, indent=2, ensure_ascii=False))
    print(json.dumps(reward, indent=2, ensure_ascii=False))

class Model:
    def __init__(self, crash_on_zero=False):
        self.crash_on_zero = crash_on_zero
        self.calls = 0
    def render(self, size):
        self.calls += 1
        if size == 0 and self.crash_on_zero:
            raise KeyError("popitem(): dictionary is empty")
        return "ok"

try:
    model = Model()
    model.render(8)
    safe_record("renders template", lambda: model.render(8) == "ok")
    model.render(8)
    safe_record("caches repeated render", lambda: model.calls == 3)
    model = Model(crash_on_zero=True)
    model.render(0)
    safe_record("handles cache_size zero", lambda: model.render(0) == "ok")
    safe_record("later check still runs", lambda: True)
finally:
    write_reward()
'''


def run_verifier(source: str, log_dir: Path) -> dict:
    verifier = log_dir / "verifier.py"
    verifier.write_text(source)
    env = dict(os.environ)
    env["LOG_DIR"] = str(log_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The verifier legitimately exits non-zero when the (defective) submission
    # crashes an unguarded setup statement; the harness runs it with `|| true`.
    subprocess.run(
        [sys.executable, str(verifier)], cwd=str(log_dir), env=env, check=False
    )
    return json.loads((log_dir / "reward.json").read_text())


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


def test_detection_and_count():
    assert is_vulnerable_script_verifier(SYNTHETIC_VERIFIER)
    assert declared_check_count(SYNTHETIC_VERIFIER) == 4


def test_original_verifier_inflates_score(workdir: Path):
    """The unfixed pattern: an unguarded setup crash truncates the run and the
    shrinking denominator yields a perfect score for a broken submission."""
    reward = run_verifier(SYNTHETIC_VERIFIER, workdir)
    # The crash before "handles cache_size zero" aborts the try block; only the
    # two passing checks before it are recorded: 2/2 == 1.0 (the inflation).
    assert reward["tests_total"] == 2
    assert reward["overall"] == 1.0


def test_transform_makes_checks_independent(workdir: Path):
    """Direction 1: after transformation every check still runs, so the check
    whose setup crashed is recorded as a failure instead of aborting the run."""
    transformed = transform_script(SYNTHETIC_VERIFIER)
    assert transformed is not None
    reward = run_verifier(transformed, workdir)
    assert reward["tests_total"] == 4
    assert reward["tests_passed"] == 3
    assert reward["overall"] == pytest.approx(0.75)
    names = [entry["name"] for entry in reward["tests"]]
    assert "handles cache_size zero" in names
    assert "later check still runs" in names


def test_transform_preserves_correct_submission(workdir: Path):
    """A correct submission is unaffected: no setup crash, all checks pass."""
    transformed = transform_script(SYNTHETIC_VERIFIER)
    assert transformed is not None
    # Make the model correct (no crash on size zero).
    correct = transformed.replace("crash_on_zero=True", "crash_on_zero=False")
    reward = run_verifier(correct, workdir)
    assert reward["tests_total"] == 4
    assert reward["overall"] == 1.0


def test_transform_skips_non_vulnerable_script():
    source = "x = 1\nprint(x)\n"
    assert is_vulnerable_script_verifier(source) is False
    assert transform_script(source) is None


def test_reconcile_fixed_denominator():
    """Direction 2: even when the verifier was not transformed, a truncated
    payload is scored against the declared check count, not the recorded one."""
    truncated = {
        "overall": 1.0,
        "test_pass_rate": 1.0,
        "tests_passed": 2,
        "tests_total": 2,
        "test_status": "pass",
        "tests": [
            {"name": "c1", "passed": True, "detail": ""},
            {"name": "c2", "passed": True, "detail": ""},
        ],
    }
    reconciled = reconcile_reward_payload(truncated, declared=4)
    assert reconciled["tests_total"] == 4
    assert reconciled["overall"] == pytest.approx(0.5)
    assert reconciled["test_status"] == "no_pass"
    # The missing checks are surfaced as failures.
    assert sum(1 for entry in reconciled["tests"] if entry["passed"]) == 2

    # No truncation -> untouched.
    full = dict(truncated)
    full["tests"].extend(
        [
            {"name": "c3", "passed": True, "detail": ""},
            {"name": "c4", "passed": True, "detail": ""},
        ]
    )
    full["tests_total"] = 4
    full["tests_passed"] = 4
    assert reconcile_reward_payload(full, 4)["tests_total"] == 4

    # declared == 0 -> untouched (never over-count).
    assert reconcile_reward_payload(truncated, 0)["tests_total"] == 2


# A verifier that records checks inside a ``with`` block (like the python_port
# tasks): the setup for a check crashes mid-block and must not abort the rest.
SYNTHETIC_WITH_VERIFIER = '''\
import json
import os
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOG_DIR", "/tmp"))
RESULTS = []

def record(name, passed, detail=""):
    RESULTS.append({"name": name, "passed": bool(passed), "detail": str(detail)})

def safe_record(name, check):
    try:
        record(name, check())
    except Exception as exc:
        record(name, False, f"{type(exc).__name__}: {exc}")

def write_reward():
    passed = sum(1 for item in RESULTS if item["passed"])
    total = len(RESULTS) or 1
    reward = {"overall": passed / total, "test_pass_rate": passed / total,
              "tests_passed": passed, "tests_total": total,
              "test_status": "pass" if passed == total else "no_pass", "tests": RESULTS}
    (LOG_DIR / "reward.json").write_text(json.dumps(reward))

class Ctx:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False

class Model:
    def __init__(self, crash=False):
        self.crash = crash
    def work(self, value):
        if self.crash and value == "boom":
            raise ValueError("boom")
        return "ok"

try:
    with Ctx():
        model = Model()
        model.work("a")
        safe_record("works in with", lambda: model.work("b") == "ok")
        model = Model(crash=True)
        model.work("boom")
        safe_record("after crash in with", lambda: model.work("c") == "ok")
        safe_record("later check runs", lambda: True)
finally:
    write_reward()
'''


def test_transform_contains_with_block_crash(workdir: Path):
    """A crash inside a ``with`` block is contained: the check after the crash
    fails, later checks still run, and the denominator is the full total."""
    transformed = transform_script(SYNTHETIC_WITH_VERIFIER)
    assert transformed is not None
    assert declared_check_count(SYNTHETIC_WITH_VERIFIER) == 3
    reward = run_verifier(transformed, workdir)
    assert reward["tests_total"] == 3
    assert reward["tests_passed"] == 2
    assert reward["overall"] == pytest.approx(2 / 3)


# A verifier that records checks inside a ``for`` loop (like the testing-*
# mutation verifiers): a crash in one iteration must not abort the loop, and the
# denominator must be the exact number of checks (base + per-iteration).
SYNTHETIC_LOOP_VERIFIER = '''\
import json
import os
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOG_DIR", "/tmp"))
RESULTS = []

def record(name, passed, detail=""):
    RESULTS.append({"name": name, "passed": bool(passed), "detail": str(detail)})

def safe_record(name, check):
    try:
        record(name, check())
    except Exception as exc:
        record(name, False, f"{type(exc).__name__}: {exc}")

def write_reward():
    passed = sum(1 for item in RESULTS if item["passed"])
    total = len(RESULTS) or 1
    reward = {"overall": passed / total, "test_pass_rate": passed / total,
              "tests_passed": passed, "tests_total": total,
              "test_status": "pass" if passed == total else "no_pass", "tests": RESULTS}
    (LOG_DIR / "reward.json").write_text(json.dumps(reward))

class Model:
    def __init__(self, crash_on=None):
        self.crash_on = crash_on
    def work(self, value):
        if value == self.crash_on:
            raise ValueError(f"crash on {value}")
        return "ok"

MUTATIONS = [1, 2, 3]

try:
    safe_record("base check", lambda: True)
    for mutation in MUTATIONS:
        result = Model(crash_on=2).work(mutation)
        safe_record(f"mutation {mutation}", lambda: True)
finally:
    write_reward()
'''


def test_transform_loop_crash_and_exact_denominator(workdir: Path):
    """A crash inside a loop iteration is contained: that iteration's check
    fails, the loop continues, and the denominator is the exact total (base + 3
    mutations), not a shrunk count."""
    transformed = transform_script(SYNTHETIC_LOOP_VERIFIER)
    assert transformed is not None
    # Exact denominator: 1 base + 3 loop checks.
    assert declared_check_count(SYNTHETIC_LOOP_VERIFIER) == 4
    reward = run_verifier(transformed, workdir)
    assert reward["tests_total"] == 4
    assert reward["tests_passed"] == 3
    assert reward["overall"] == pytest.approx(0.75)
    names = [entry["name"] for entry in reward["tests"]]
    assert "mutation 3" in names  # the loop completed past the crash
