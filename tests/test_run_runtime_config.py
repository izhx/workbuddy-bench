"""Exercise run.sh's eval branches without starting Docker or a model backend."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from workbuddy_bench.runner.split_proxy_log import split_proxy_log

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("shards", [1, 2])
def test_relaunch_keeps_original_runtime_and_split_destination(tmp_path: Path, shards: int) -> None:
    script = (REPO_ROOT / "scripts/run.sh").read_text()
    start = script.index("# ── Run evaluation")
    eval_phase = script[script.index("\n", start) + 1:]
    shell = '''
set -e
RESUME_IN_PLACE=
RESUME_JOBS=()
HAS_TASK_SELECTION=0
DISABLE_VERIFICATION=1
prepare_effective_tasks() { :; }
run_tracked_foreground() {
    if [ "$1" != harbor ]; then "$@" --dry-run; fi
}
''' + eval_phase + '\nprintf "%s" "$JOB_CONFIG_RUNTIME" > "$INSTANCE_STATE_DIR/runtime-path"\n'
    tasks = tmp_path / "tasks"
    task = tasks / "fixture-task"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[metadata]\ndifficulty = "easy"\n')
    (task / "instruction.md").write_text("Fixture task; never executed.\n")
    job_path = tmp_path / "job.yaml"
    job = yaml.safe_load((REPO_ROOT / "configs/jobs/glm-5.2.cc.office.yaml").read_text())
    runtimes = []
    manifests = []
    original = b""
    for number in (1, 2):
        state = tmp_path / f"instance-{number}"
        state.mkdir()
        manifest = state / "manifest.json"
        manifest.write_text(json.dumps({
            "instance_id": f"instance-{number}", "job_slug": "job",
            "dataset": str(tasks), "harness_backend": "local",
            "model_connection": "local_proxy", "record_full_io": True,
            "model_route": f"instance-{number}__model", "backend_model_name": "glm-5.2",
            "connection": {"effective": "local_proxy", "proxy_url": "http://127.0.0.1:3456"},
        }))
        job["jobs_dir"] = str(tmp_path / f"results-{number}")
        job_path.write_text(yaml.safe_dump(job))
        result = subprocess.run(
            ["bash", "-c", shell], cwd=REPO_ROOT, text=True, capture_output=True, timeout=10,
            env={**os.environ, "REPO_ROOT": str(REPO_ROOT), "JOB_CONFIG": str(job_path),
                 "INSTANCE_STATE_DIR": str(state), "MANIFEST_PATH": str(manifest),
                 "EFFECTIVE_TASKS_DIR": str(tasks), "SHARDS": str(shards),
                 "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        runtime = Path((state / "runtime-path").read_text())
        assert runtime.is_relative_to(state)
        runtimes.append(runtime)
        manifests.append(manifest)
        if number == 1:
            original = runtime.read_bytes()
    assert runtimes[0] != runtimes[1]
    assert runtimes[0].read_bytes() == original

    trial = tmp_path / "results-1" / "experiment" / "task__one"
    trial.mkdir(parents=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    source = log_dir / "instance-1.jsonl"
    source.write_text(json.dumps({
        "id": "request", "trial_id": trial.name, "route": "instance-1__model",
    }) + "\n")
    split_proxy_log(manifests[0], log_dir, runtime_config=runtimes[0])
    assert (trial / "agent/requests.jsonl").is_file()
    assert not source.exists()
