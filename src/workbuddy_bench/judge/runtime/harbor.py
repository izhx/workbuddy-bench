"""Harbor-facing runtime helpers for dataset-native verifier profiles."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harbor.models.trial.paths import EnvironmentPaths

from workbuddy_bench.judge.core import (
    VERIFIER_LLM_ENV_PREFIX,
    ArtifactWriter,
    EvaluationContext,
    ScoreResult,
)
from workbuddy_bench.judge.runners.rule.script_verifier_resilience import (
    transform_verifier_file,
)
from workbuddy_bench.runner.model_endpoints import openai_api_base_url
from workbuddy_bench.runner.runtime_proxy import runtime_proxy_url


def merged_verifier_env(
    verifier: Any,
    base: Mapping[str, str] | None = None,
    *,
    prepend_pythonpath: str | None = None,
) -> dict[str, str]:
    """Merge task, verifier, and override env blocks in runtime precedence order."""

    env: dict[str, str] = {str(key): str(value) for key, value in (base or {}).items()}
    task_env = getattr(getattr(verifier.task, "config", None), "verifier", None)
    if task_env is not None:
        env.update({str(k): str(v) for k, v in getattr(task_env, "env", {}).items()})
    if getattr(verifier, "verifier_env", None):
        env.update({str(k): str(v) for k, v in verifier.verifier_env.items()})
    if getattr(verifier, "override_env", None):
        env.update({str(k): str(v) for k, v in verifier.override_env.items()})
    # ``harbor job resume`` reconstructs verifier env from the immutable old
    # config. Rebind only an already-configured verifier-side LLM route to the
    # current local proxy; do not turn programmatic verifiers into LLM judges.
    verifier_base_key = f"{VERIFIER_LLM_ENV_PREFIX}BASE_URL"
    if verifier_base_key in env:
        current_proxy_url = runtime_proxy_url()
        if current_proxy_url:
            env[verifier_base_key] = openai_api_base_url(current_proxy_url)
        # prepare_job marks proxy routes explicitly. run.sh also exports the
        # marker so resumes of old immutable configs get the current trial token.
        proxy_route_key = f"{VERIFIER_LLM_ENV_PREFIX}PROXY_ROUTE"
        proxy_route = env.get(proxy_route_key, os.environ.get(proxy_route_key, ""))
        api_key = f"{VERIFIER_LLM_ENV_PREFIX}API_KEY"
        if (
            proxy_route
            and env.get(f"{VERIFIER_LLM_ENV_PREFIX}MODEL") == proxy_route
            and env.get(api_key, "").rsplit("::", 1)[-1] == proxy_route
        ):
            env[api_key] = f"{verifier.trial_paths.trial_dir.name}::{proxy_route}"
    if prepend_pythonpath:
        env["PYTHONPATH"] = _prepend_path(prepend_pythonpath, env.get("PYTHONPATH"))
    return env


def _prepend_path(prefix: str, value: str | None) -> str:
    existing = str(value or "").strip()
    if not existing:
        return prefix
    parts = existing.split(":")
    if prefix in parts:
        return existing
    return f"{prefix}:{existing}"


@dataclass
class HarborAttemptRuntime:
    """Stable Harbor runtime contract shared by dataset profiles."""

    verifier: Any
    environment: Any
    tests_dir: str
    workspace: str
    container_verifier_dir: str
    host_verifier_dir: Path

    @classmethod
    def from_verifier(cls, verifier: Any) -> "HarborAttemptRuntime":
        environment = verifier.environment
        env_paths = EnvironmentPaths.for_os(environment.os)
        host_verifier_dir = verifier.trial_paths.verifier_dir
        host_verifier_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            verifier=verifier,
            environment=environment,
            tests_dir=str(env_paths.tests_dir),
            workspace=(env_paths.tests_dir.parent / "workspace").as_posix(),
            container_verifier_dir=str(env_paths.verifier_dir),
            host_verifier_dir=host_verifier_dir,
        )

    @property
    def task_id(self) -> str:
        return str(self.verifier.task.short_name)

    async def upload_tests(self) -> None:
        await self.environment.upload_dir(
            source_dir=self.verifier.task.paths.tests_dir,
            target_dir=self.tests_dir,
        )
        # If verifier.py is a single-try script, rewrite it so each check runs
        # independently and write_reward uses a fixed denominator, then overwrite
        # the uploaded copy. The dataset command still runs /tests/verifier.py.
        transformed = transform_verifier_file(self.verifier.task.paths.tests_dir / "verifier.py")
        if transformed is None:
            return
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(transformed)
            tmp_path = Path(tmp.name)
        try:
            await self.environment.upload_file(tmp_path, f"{self.tests_dir}/verifier.py")
        finally:
            tmp_path.unlink(missing_ok=True)

    async def upload_dir(self, *, source_dir: Path, target_dir: str) -> None:
        await self.environment.upload_dir(source_dir=source_dir, target_dir=target_dir)

    async def download_verifier_dir(
        self,
        *,
        source_dir: str | None = None,
        target_dir: Path | None = None,
    ) -> None:
        if getattr(self.environment.capabilities, "mounted", False):
            return
        await self.environment.download_dir(
            source_dir=source_dir or self.container_verifier_dir,
            target_dir=target_dir or self.host_verifier_dir,
        )

    def env(
        self,
        base: Mapping[str, str] | None = None,
        *,
        prepend_pythonpath: str | None = None,
    ) -> dict[str, str]:
        return merged_verifier_env(
            self.verifier,
            base,
            prepend_pythonpath=prepend_pythonpath,
        )

    def context(
        self,
        *,
        dataset_id: str,
        task_id: str | None = None,
        container_paths: Mapping[str, str] | None = None,
        host_paths: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationContext:
        return EvaluationContext(
            dataset_id=dataset_id,
            task_id=task_id or self.task_id,
            workspace=self.workspace,
            tests_dir=self.tests_dir,
            verifier_dir=self.container_verifier_dir,
            container_paths={str(k): str(v) for k, v in (container_paths or {}).items()},
            host_paths={str(k): str(v) for k, v in (host_paths or {}).items()},
            env={str(k): str(v) for k, v in (env or {}).items()},
            metadata=dict(metadata or {}),
        )

    def artifact_writer(self) -> ArtifactWriter:
        return ArtifactWriter(
            reward_json_path=self.verifier.trial_paths.reward_json_path,
            score_json_path=self.host_verifier_dir / "score.json",
        )

    def write_score(self, score: ScoreResult) -> None:
        self.artifact_writer().write(score)
