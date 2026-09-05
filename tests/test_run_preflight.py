from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from workbuddy_bench.runner.resolve_manifest import _model_connection


@pytest.mark.parametrize(
    ("job", "shared_proxy", "expected"),
    [
        ({}, False, "local_proxy"),
        ({"model_connection": "local_proxy", "record_full_io": True}, False, "local_proxy"),
        ({"model_connection": "direct", "record_full_io": False}, True, "direct"),
        ({"model_connection": "local_proxy", "record_full_io": False}, True, "local_proxy"),
    ],
)
def test_supported_recording_configuration(job: dict, shared_proxy: bool, expected: str) -> None:
    assert _model_connection(job, Path("job.yaml"), shared_proxy=shared_proxy) == expected


@pytest.mark.parametrize(
    ("connection", "shared_proxy", "message"),
    [
        ("direct", False, "requires model_connection: local_proxy"),
        ("local_proxy", True, "incompatible with SHARED_PROXY=1"),
    ],
)
def test_resolver_cli_rejects_recording_before_model_loading_or_staging(
    tmp_path: Path, connection: str, shared_proxy: bool, message: str
) -> None:
    job = tmp_path / "job.yaml"
    job.write_text(yaml.safe_dump({
        "model": "unavailable-model", "harness": "unavailable-harness",
        "model_connection": connection, "record_full_io": True,
    }))
    state = tmp_path / "state"
    command = [
        sys.executable, "-m", "workbuddy_bench.runner.resolve_manifest",
        "--job-config", str(job), "--model-config", str(tmp_path / "missing-model.yaml"),
        "--instance-id", "test", "--instance-dir", str(state),
    ]
    if shared_proxy:
        command.append("--shared-proxy")
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert not state.exists()


def test_run_sh_validates_recording_during_manifest_resolution() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts/run.sh").read_text()
    resolution = script.index("python3 -m workbuddy_bench.runner.resolve_manifest")
    shared_flag = script.index("RESOLVE_FLAGS+=(--shared-proxy)")
    ordinary_dry_run = script.index('if [ "$DRY_RUN" = "1" ]')
    in_place_dry_run = script.index('[ "$DRY_RUN" != "1" ] || exit 0')
    model_validation = script.index("workbuddy_bench.runner.validate_model")
    shared_health = script.index('/health"')
    assert shared_flag < resolution < min(
        ordinary_dry_run, in_place_dry_run, model_validation, shared_health
    )
    assert '"${RESOLVE_FLAGS[@]}"' in script[resolution:ordinary_dry_run]
