from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from workbuddy_bench.agents.cbc_agent import CbcAgent
from workbuddy_bench.agents.cc_agent import CcAgent
from workbuddy_bench.judge.runtime.harbor import merged_verifier_env
from workbuddy_bench.runner.runtime_proxy import (
    RUNTIME_PROXY_URL_ENV,
    runtime_proxy_url,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_proxy_url_overrides_recorded_provenance() -> None:
    assert runtime_proxy_url(
        "http://host.docker.internal:3456",
        environ={RUNTIME_PROXY_URL_ENV: "http://host.docker.internal:3457"},
    ) == "http://host.docker.internal:3457"


def test_runtime_proxy_url_falls_back_to_recorded_value() -> None:
    assert runtime_proxy_url(
        "http://host.docker.internal:3456",
        environ={},
    ) == "http://host.docker.internal:3456"


@pytest.mark.parametrize("agent_class", [CcAgent, CbcAgent])
def test_local_proxy_agents_use_runtime_endpoint(monkeypatch, tmp_path, agent_class) -> None:
    monkeypatch.setenv(
        RUNTIME_PROXY_URL_ENV,
        "http://host.docker.internal:3457",
    )
    kwargs = {
        "model_name": "model-route",
        "connection": {
            "mode": "local_proxy",
            "proxy_url": "http://host.docker.internal:3456",
            "model_route": "model-route",
        },
        "settings_preset": {"permissions": {"deny": []}},
    }
    if agent_class is CbcAgent:
        kwargs["models_preset"] = {"vendor": "openai"}

    agent = agent_class(tmp_path / "trial" / "agent", **kwargs)

    assert agent._effective_proxy_url() == "http://host.docker.internal:3457"


def test_composite_verifier_rebinds_existing_llm_route(monkeypatch) -> None:
    monkeypatch.setenv(
        RUNTIME_PROXY_URL_ENV,
        "http://host.docker.internal:3457",
    )
    verifier = SimpleNamespace(
        task=SimpleNamespace(
            config=SimpleNamespace(
                verifier=SimpleNamespace(env={}),
            )
        ),
        verifier_env=None,
        override_env={
            "WORKBUDDY_VERIFIER_LLM_BASE_URL": "http://host.docker.internal:3456/v1",
            "WORKBUDDY_VERIFIER_LLM_MODEL": "judge-route",
        },
    )

    env = merged_verifier_env(verifier)

    assert env["WORKBUDDY_VERIFIER_LLM_BASE_URL"] == (
        "http://host.docker.internal:3457/v1"
    )
    assert env["WORKBUDDY_VERIFIER_LLM_MODEL"] == "judge-route"


def test_composite_verifier_does_not_add_an_llm_route(monkeypatch) -> None:
    monkeypatch.setenv(
        RUNTIME_PROXY_URL_ENV,
        "http://host.docker.internal:3457",
    )
    verifier = SimpleNamespace(
        task=SimpleNamespace(
            config=SimpleNamespace(
                verifier=SimpleNamespace(env={}),
            )
        ),
        verifier_env=None,
        override_env={},
    )

    assert "WORKBUDDY_VERIFIER_LLM_BASE_URL" not in merged_verifier_env(verifier)


def test_run_sh_rebinds_in_place_proxy_without_rewriting_recorded_config() -> None:
    script = (REPO_ROOT / "scripts" / "run.sh").read_text()

    assert 'export PROXY_PORT="$RESUME_PROXY_PORT"' not in script
    assert 'export PROXY_HOST="$RESUME_PROXY_HOST"' not in script
    assert 'export WORKBUDDY_RUNTIME_PROXY_URL="$PROXY_HOST_URL"' in script
    assert "in-place resume cannot switch to a different URL" not in script
