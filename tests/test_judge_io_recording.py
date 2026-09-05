"""Judge attribution through real routing, HTTP handling, logging and splitting."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from workbuddy_bench.judge.core.routing import verifier_llm_route_from_mapping
from workbuddy_bench.judge.runtime.harbor import merged_verifier_env
from workbuddy_bench.proxy import main as proxy
from workbuddy_bench.proxy.config import load_config_from_yaml
from workbuddy_bench.proxy.interceptors.logger import proxy_log_filename
from workbuddy_bench.proxy.pipeline import Pipeline
from workbuddy_bench.runner.judge_routing import verifier_side_llm_env
from workbuddy_bench.runner.proxy_config import build_proxy_config
from workbuddy_bench.runner.split_proxy_log import split_proxy_log


PREFIX = "WORKBUDDY_VERIFIER_LLM_"
JUDGE_ROUTE = "judge-model"
MODEL_CONFIG = {"model": {"name": "backend-model", "backend_url_env": "TEST_JUDGE_URL",
                          "backend_key_env": "TEST_JUDGE_KEY"}}


def _manifest(instance_id="run-one", *, mode="in_container", recording=True, enabled=True):
    return {
        "instance_id": instance_id, "job_slug": "job",
        "model_connection": "local_proxy", "record_full_io": recording,
        "model_route": f"{instance_id}__agent-model", "model_protocols": ["openai"],
        "connection": {"proxy_url": "http://proxy"},
        "llm_judge": {
            "enabled": enabled, "mode": mode, "model_slug": JUDGE_ROUTE,
            "model": "backend-model", "api_base_env": "TEST_JUDGE_URL",
            "api_key_env": "TEST_JUDGE_KEY", "params": {"extra_body": {"top_k": 7}},
        },
    }


def _proxy_config(manifest, log_dir):
    return build_proxy_config(
        manifest=manifest, model_config=MODEL_CONFIG, port=3456, log_dir=str(log_dir),
        max_concurrent=4, backend_timeout=2, backend_retries=0, retry_base_delay=0,
        default_experiment="job", default_harness="test",
    )


def _verifier(trial, env):
    return SimpleNamespace(
        task=SimpleNamespace(config=SimpleNamespace(verifier=SimpleNamespace(env={}))),
        trial_paths=SimpleNamespace(trial_dir=trial), override_env=env, verifier_env=None,
    )


def _response(content):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("recording", [True, False])
def test_judge_round_trip_and_run_isolation(tmp_path, monkeypatch, recording):
    """Only the upstream HTTP transport is fake; exercise the real proxy pipeline."""
    monkeypatch.setenv("TEST_JUDGE_URL", "http://upstream/v1")
    monkeypatch.setenv("TEST_JUDGE_KEY", "backend-secret")
    monkeypatch.delenv("WORKBUDDY_RUNTIME_PROXY_URL", raising=False)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    public_log = log_dir / "proxy_requests.jsonl"
    public_log.write_text('{"id":"old-public-judge"}\n')

    async def run():
        for instance_id in ("run-one", "run-two"):
            manifest = _manifest(instance_id, recording=recording)
            state = tmp_path / instance_id
            state.mkdir()
            manifest_path = state / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            trials = [state / "experiment" / f"same-task__{suffix}" for suffix in ("one", "two")]
            for trial in trials:
                trial.mkdir(parents=True)
                (trial / "config.json").write_text(json.dumps({
                    "agent": {"kwargs": {"instance_id": instance_id}},
                }))
            config_path = state / "proxy.yaml"
            config_path.write_text(yaml.safe_dump(_proxy_config(manifest, log_dir)))
            config = load_config_from_yaml(config_path)
            assert config.routes[JUDGE_ROUTE].instance_id == (instance_id if recording else "")
            pipeline = Pipeline(config)
            monkeypatch.setattr(proxy, "_config", config)
            monkeypatch.setattr(proxy, "_pipeline", pipeline)
            upstream_requests = []

            async def upstream(request):
                upstream_requests.append(request)
                await asyncio.sleep(0)
                return _response('{"verdict":"pass","score":1.0}')

            try:
                async with (
                    httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as upstream_client,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://proxy") as client,
                ):
                    monkeypatch.setattr(pipeline.sender, "_get_client", lambda backend: upstream_client)
                    agent_route = manifest["model_route"]
                    response = await client.post("/v1/chat/completions", json={
                        "model": agent_route, "messages": [{"role": "user", "content": "agent-before"}],
                    }, headers={"Authorization": f"Bearer {trials[0].name}::{agent_route}"})
                    response.raise_for_status()

                    async def judge(trial):
                        env = merged_verifier_env(_verifier(trial, verifier_side_llm_env(manifest)))
                        route = verifier_llm_route_from_mapping(env)
                        response = await client.post(route.base_url + "/chat/completions", json={
                            "model": route.model, "messages": [{"role": "user", "content": trial.name}],
                        }, headers={"Authorization": f"Bearer {route.api_key}"})
                        response.raise_for_status()
                    await asyncio.gather(*[judge(trial) for trial in trials])
            finally:
                await pipeline.close()

            assert len(upstream_requests) == 3
            assert all(req.headers["Authorization"] == "Bearer backend-secret" for req in upstream_requests)
            bodies = [json.loads(req.content) for req in upstream_requests]
            assert all(body["model"] == "backend-model" for body in bodies)
            assert all(body["top_k"] == 7 for body in bodies[1:])
            source = log_dir / proxy_log_filename(instance_id)
            original = source.read_bytes() if recording else b""
            split_proxy_log(manifest_path, log_dir, job_root=state / "experiment")
            if recording:
                assert not source.exists()
                for index, trial in enumerate(trials):
                    records = _records(trial / "verifier/requests.jsonl")
                    assert len(records) == 1
                    assert all(record["instance_id"] == instance_id for record in records)
                    assert all(record["trial_id"] == trial.name for record in records)
                    assert all(record["route"] == JUDGE_ROUTE for record in records)
                    assert records[-1]["response"]["body"]["choices"]
                    assert all(record["request"]["body"]["messages"] for record in records)
                    agent_file = trial / "agent/requests.jsonl"
                    assert agent_file.exists() == (index == 0)
                    if index == 0:
                        assert [r["route"] for r in _records(agent_file)] == [agent_route]
                # A retry of an already-committed source must not duplicate either purpose.
                source.write_bytes(original)
                split_proxy_log(manifest_path, log_dir, job_root=state / "experiment")
                assert all(len(_records(trial / "verifier/requests.jsonl")) == 1 for trial in trials)
                assert len(_records(trials[0] / "agent/requests.jsonl")) == 1
            else:
                assert not source.exists()
                assert not list(state.rglob("requests.jsonl"))
            assert public_log.read_text() == '{"id":"old-public-judge"}\n'
    asyncio.run(run())


@pytest.mark.parametrize("runtime_marker", [False, True])
def test_verifier_tokens_refresh_without_mutating_saved_env(tmp_path, monkeypatch, runtime_marker):
    env = verifier_side_llm_env(_manifest(mode="in_container"))
    if runtime_marker:
        # Reconstruct an old config; run.sh supplies the marker only at runtime.
        monkeypatch.setenv(PREFIX + "PROXY_ROUTE", env.pop(PREFIX + "PROXY_ROUTE"))
    monkeypatch.setenv("WORKBUDDY_RUNTIME_PROXY_URL", "http://current-proxy:4567")
    env[PREFIX + "API_KEY"] = f"old-trial::{JUDGE_ROUTE}"
    original = dict(env)
    verifier = _verifier(tmp_path / "task__current", env)
    merged = merged_verifier_env(verifier)
    assert merged[PREFIX + "API_KEY"] == f"task__current::{JUDGE_ROUTE}"
    assert merged[PREFIX + "BASE_URL"] == "http://current-proxy:4567/v1"
    verifier.override_env = merged
    assert merged_verifier_env(verifier)[PREFIX + "API_KEY"] == merged[PREFIX + "API_KEY"]
    assert env == original


@pytest.mark.parametrize("mode,enabled", [("in_container", True), ("host_side", True), ("in_container", False)])
def test_run_sh_exports_current_verifier_route(tmp_path, mode, enabled):
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/run.sh").read_text()
    clear = script[script.index("unset WORKBUDDY_RUNTIME_PROXY_URL"):script.index("wait_for_proxy_route()")]
    export = script[script.index("# Old immutable resume configs"):script.index("# ── Run evaluation")]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest(mode=mode, enabled=enabled)))
    result = subprocess.run(
        ["bash", "-ec", clear + export + '\nprintenv WORKBUDDY_VERIFIER_LLM_PROXY_ROUTE'],
        cwd=root, text=True, capture_output=True, timeout=10,
        env={**os.environ, "USE_LOCAL_PROXY": "1", "MANIFEST_PATH": str(manifest),
             "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
             PREFIX + "PROXY_ROUTE": "stale-inherited-route"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (JUDGE_ROUTE if mode == "in_container" and enabled else "")


@pytest.mark.parametrize("marker", ["", JUDGE_ROUTE])
def test_verifier_does_not_rewrite_real_api_key(tmp_path, monkeypatch, marker):
    monkeypatch.delenv(PREFIX + "PROXY_ROUTE", raising=False)
    env = {PREFIX + "BASE_URL": "https://direct/v1", PREFIX + "MODEL": JUDGE_ROUTE,
           PREFIX + "API_KEY": "real-api-key", PREFIX + "PROXY_ROUTE": marker}
    assert merged_verifier_env(_verifier(tmp_path / "task__one", env))[PREFIX + "API_KEY"] == "real-api-key"


def test_disabled_judge_has_no_route(tmp_path):
    manifest = _manifest(mode="in_container", enabled=False)
    assert verifier_side_llm_env(manifest) == {}
    config = _proxy_config(manifest, tmp_path)
    assert [route["slug"] for route in config["proxy"]["routes"]] == [manifest["model_route"]]


@pytest.mark.parametrize("case", ["unknown", "missing", "malformed", "disabled", "ambiguous", "legacy-judge", "foreign", "host-side", "default-mode"])
def test_split_preserves_unknown_purpose_or_owner(tmp_path, case):
    manifest = _manifest()
    trial = tmp_path / "experiment/task__one"
    trial.mkdir(parents=True)
    record = {"id": "request", "instance_id": manifest["instance_id"],
              "trial_id": trial.name, "route": JUDGE_ROUTE}
    if case == "unknown":
        record["route"] = "unregistered-route"
    elif case == "missing":
        record.pop("route")
    elif case == "malformed":
        record["route"] = {}
    elif case == "disabled":
        manifest["llm_judge"]["enabled"] = False
    elif case == "ambiguous":
        manifest["model_route"] = JUDGE_ROUTE
        with pytest.raises(ValueError, match="distinct agent and judge routes"):
            _proxy_config(manifest, tmp_path)
    elif case == "legacy-judge":
        record.pop("instance_id")
    elif case in {"host-side", "default-mode"}:
        if case == "host-side":
            manifest["llm_judge"]["mode"] = "host_side"
        else:
            manifest["llm_judge"].pop("mode")
        config = _proxy_config(manifest, tmp_path)
        judge_route = next(route for route in config["proxy"]["routes"] if route["slug"] == JUDGE_ROUTE)
        assert not judge_route.get("instance_id")
        assert verifier_side_llm_env(manifest) == {}
    else:
        record["instance_id"] = "other-run"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    source = log_dir / proxy_log_filename(manifest["instance_id"])
    source.write_text(json.dumps(record) + "\n")
    original = source.read_bytes()
    split_proxy_log(path, log_dir, job_root=trial.parent)
    assert source.read_bytes() == original
    assert not list(trial.rglob("requests.jsonl"))
