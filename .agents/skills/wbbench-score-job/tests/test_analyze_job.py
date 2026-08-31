from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_job.py"
SPEC = importlib.util.spec_from_file_location("wbbench_score_job", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class AnalyzeJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_run(self, subset: str, attempts: int = 1) -> tuple[Path, str]:
        task = f"fixture-{subset}"
        run = self.root / f"job-{subset}" / "2026-01-01__00-00-00"
        run.mkdir(parents=True)
        write_json(
            run / "config.json",
            {
                "n_attempts": attempts,
                "datasets": [{"path": f"/staged/wb-bench-{subset}-v1.0/tasks"}],
                "agents": [{"model_name": "fixture-model", "import_path": "fixture:Agent"}],
            },
        )
        write_json(
            run / "lock.json",
            {
                "trials": [
                    {
                        "task": {
                            "name": task,
                            "path": f"/staged/wb-bench-{subset}-v1.0/tasks/{task}",
                        }
                    }
                ]
            },
        )
        write_json(
            run / "result.json",
            {"id": f"run-{subset}", "finished_at": "2026-01-01T00:01:00Z", "stats": {}},
        )
        (run / "job.log").write_text("fixture\n", encoding="utf-8")
        return run, task

    def add_trial(
        self,
        run: Path,
        task: str,
        suffix: str,
        harbor_reward: float | None,
        score_json: dict | None = None,
        exception: dict | None = None,
        tokens: int = 1,
    ) -> Path:
        trial = run / f"{task}__{suffix}"
        trial.mkdir()
        result = {
            "task_name": f"workbuddy/{task}",
            "agent_result": {
                "n_input_tokens": tokens,
                "n_cache_tokens": 0,
                "n_output_tokens": 0,
            },
            "exception_info": exception,
            "step_results": None,
        }
        if harbor_reward is not None:
            result["verifier_result"] = {"rewards": {"reward": harbor_reward}}
        write_json(trial / "result.json", result)
        (trial / "trial.log").write_text("fixture\n", encoding="utf-8")
        if score_json is not None:
            write_json(trial / "verifier" / "score.json", score_json)
        return trial

    def test_all_four_subset_score_contracts(self) -> None:
        fixtures = {
            "office": (0.6, {"overall": 0.6, "test_pass_rate": 0.4, "llm_judge_component_score": 0.8}),
            "web": (0.7, {"reward": 0.7}),
            "code": (0.5, {"overall": 0.5, "test_pass_rate": 0.5, "tests_passed": 1, "tests_total": 2}),
            "sec": (0.25, None),
        }
        for subset, (reward, score_json) in fixtures.items():
            with self.subTest(subset=subset):
                run, task = self.make_run(subset)
                trial = self.add_trial(run, task, "a", reward, score_json)
                if subset == "sec":
                    (trial / "verifier").mkdir()
                    (trial / "verifier" / "reward.txt").write_text("0.25\n", encoding="utf-8")
                # The primary user-facing input is often the parent Harbor job
                # directory; a single contained run must resolve unambiguously.
                result = MODULE.analyze(run.parent)
                self.assertEqual(result["dataset"]["subset"], subset)
                self.assertAlmostEqual(result["score"]["reward"], reward)
                self.assertEqual(result["validity"]["status"], "complete")

    def test_code_rebases_a_shrunken_pure_count_denominator(self) -> None:
        run, task = self.make_run("code", attempts=2)
        self.add_trial(
            run,
            task,
            "a",
            0.5,
            {"overall": 0.5, "test_pass_rate": 0.5, "tests_passed": 2, "tests_total": 4},
        )
        self.add_trial(
            run,
            task,
            "b",
            0.5,
            {"overall": 0.5, "test_pass_rate": 0.5, "tests_passed": 1, "tests_total": 2},
        )
        result = MODULE.analyze(run)
        self.assertAlmostEqual(result["score"]["reward"], 0.375)
        sources = result["score"]["score_sources"]
        self.assertEqual(sources["verifier/score.json:tests_passed/max_tests_total"], 1)

    def test_sec_reports_api_zero_token_and_default_score_signals(self) -> None:
        run, task = self.make_run("sec")
        trial = self.add_trial(
            run,
            task,
            "a",
            0.25,
            exception={
                "exception_type": "UnknownApiError",
                "exception_message": "API Error: Unable to connect to API (ECONNRESET)",
            },
            tokens=0,
        )
        (trial / "verifier").mkdir()
        (trial / "verifier" / "reward.txt").write_text("0.25\n", encoding="utf-8")
        (trial / "verifier" / "test-stdout.txt").write_text(
            "ERROR: findings.json not found\n", encoding="utf-8"
        )
        result = MODULE.analyze(run)
        categories = result["anomalies"]["summary"]["by_category"]
        self.assertEqual(categories["api_failure"], 1)
        self.assertEqual(categories["zero_token_agent"], 1)
        self.assertEqual(categories["required_output_missing"], 1)
        self.assertEqual(categories["positive_score_on_failed_trial"], 1)
        self.assertEqual(categories["positive_score_with_missing_output"], 1)
        self.assertFalse(result["validity"]["unqualified_model_score_usable"])

    def test_output_inside_run_is_rejected(self) -> None:
        run, task = self.make_run("web")
        self.add_trial(run, task, "a", 1.0, {"reward": 1.0})
        result = MODULE.analyze(run)
        with self.assertRaises(MODULE.AnalysisError):
            MODULE.write_outputs(result, run / "report", "en", False)


if __name__ == "__main__":
    unittest.main()
