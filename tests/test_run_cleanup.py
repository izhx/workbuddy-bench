"""Focused shell-lifecycle tests for scripts/lib/run_cleanup.sh."""

from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workbuddy_bench.proxy.interceptors.logger import proxy_log_filename


REPO_ROOT = Path(__file__).resolve().parents[1]
CLEANUP_LIB = REPO_ROOT / "scripts" / "lib" / "run_cleanup.sh"


def _run_bash(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift;\n' + script,
            "bash",
            str(CLEANUP_LIB),
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _proxy_cleanup_preamble(manifest: Path) -> str:
    return f"""
DRY_RUN=0
USE_LOCAL_PROXY=1
PROXY_PID=4242
PROXY_CONFIG=/tmp/job-private-proxy.yaml
PROXY_LOG_DIR=/tmp/proxy-logs
MANIFEST_PATH={manifest}
RESUME_IN_PLACE_PATH=/tmp/exact-experiment
JOB_CONFIG_RUNTIME=/tmp/runtime.yaml
INSTANCE_ID=
REPO_ROOT={REPO_ROOT}
ALIVE=1
_run_cleanup_pid_is_live() {{ [ "$ALIVE" = 1 ]; }}
_run_cleanup_proxy_is_owned() {{ return 0; }}
sleep() {{ :; }}
    """


def _pid_is_effectively_live(pid: int) -> bool:
    """Return false for an absent process or a zombie awaiting reaping."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return raw.rsplit(")", 1)[1].split()[0] != "Z"


def test_term_waits_for_proxy_exit_before_split_and_preserves_rc(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    script = _proxy_cleanup_preamble(manifest) + r"""
kill() {
    printf 'signal:%s:%s\n' "$1" "${@: -1}"
    [ "$1" != "-TERM" ] || ALIVE=0
}
python3() { printf 'split:%s\n' "$*"; }

run_cleanup_instance 17
rc=$?
printf 'return:%s\n' "$rc"
"""

    proc = _run_bash(script)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "return:17" in proc.stdout
    assert proc.stdout.count("split:") == 1
    assert proc.stdout.index("signal:-TERM:4242") < proc.stdout.index("split:")
    assert "--job-dir /tmp/exact-experiment" in proc.stdout
    assert "--runtime-config" not in proc.stdout


def test_real_writer_flushes_final_record_before_real_splitter(tmp_path: Path) -> None:
    """A still-open source fd must be closed before the splitter can unlink it."""

    instance_id = "cleanup-integration"
    trial_name = "task__one"
    log_dir = tmp_path / "logs"
    experiment = tmp_path / "experiment"
    trial = experiment / trial_name
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(
        json.dumps({"agent": {"kwargs": {"instance_id": instance_id}}})
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "job_slug": "job",
                "model_connection": "local_proxy",
                "record_full_io": True,
            }
        )
    )
    log_dir.mkdir()
    source = log_dir / proxy_log_filename(instance_id)
    source.write_text(
        json.dumps(
            {"id": "initial", "instance_id": instance_id, "trial_id": trial_name}
        )
        + "\n"
    )

    ready = tmp_path / "writer-ready"
    final_record = json.dumps(
        {"id": "on-term", "instance_id": instance_id, "trial_id": trial_name}
    )
    writer_script = r"""
log_path=$1
ready_path=$2
exec 3>>"$log_path"
trap 'printf "%s\n" "$FINAL_RECORD" >&3; exec 3>&-; exit 0' TERM
: > "$ready_path"
while :; do sleep 0.05; done
"""
    writer = subprocess.Popen(
        ["bash", "-c", writer_script, "writer", str(source), str(ready)],
        env={**os.environ, "FINAL_RECORD": final_record},
    )
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "writer did not become ready"

        proc = _run_bash(
            f"""
DRY_RUN=0
USE_LOCAL_PROXY=1
PROXY_PID={writer.pid}
PROXY_CONFIG=/tmp/fake-private-proxy.yaml
PROXY_LOG_DIR={shlex.quote(str(log_dir))}
MANIFEST_PATH={shlex.quote(str(manifest))}
RESUME_IN_PLACE_PATH={shlex.quote(str(experiment))}
INSTANCE_ID=
REPO_ROOT={shlex.quote(str(REPO_ROOT))}
PYTHONPATH={shlex.quote(str(REPO_ROOT / 'src'))}
PATH={shlex.quote(str(REPO_ROOT / '.venv' / 'bin'))}:$PATH
_run_cleanup_proxy_is_owned() {{ return 0; }}
run_cleanup_instance 0
"""
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        writer.wait(timeout=2)
        requests = [
            json.loads(line)
            for line in (trial / "agent" / "requests.jsonl").read_text().splitlines()
        ]
        assert [record["id"] for record in requests] == ["initial", "on-term"]
        assert not source.exists()
    finally:
        if writer.poll() is None:
            writer.terminate()
            try:
                writer.wait(timeout=2)
            except subprocess.TimeoutExpired:
                writer.kill()
                writer.wait(timeout=2)


def test_split_failure_does_not_replace_original_rc(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    script = _proxy_cleanup_preamble(manifest) + r"""
kill() { [ "$1" != "-TERM" ] || ALIVE=0; }
python3() { echo split-failed; return 42; }

run_cleanup_instance 23
rc=$?
printf 'return:%s\n' "$rc"
"""

    proc = _run_bash(script)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "split-failed" in proc.stdout
    assert "proxy-log split failed (non-fatal)" in proc.stderr
    assert "return:23" in proc.stdout


def test_stubborn_owned_proxy_gets_kill_then_splits(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    script = _proxy_cleanup_preamble(manifest) + r"""
RUN_CLEANUP_TERM_WAIT_ATTEMPTS=1
RUN_CLEANUP_KILL_WAIT_ATTEMPTS=1
kill() {
    printf 'signal:%s:%s\n' "$1" "${@: -1}"
    [ "$1" != "-KILL" ] || ALIVE=0
}
python3() { echo split-ran; }

run_cleanup_instance 0
"""

    proc = _run_bash(script)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "signal:-TERM:4242" in proc.stdout
    assert "signal:-KILL:4242" in proc.stdout
    assert proc.stdout.index("signal:-KILL:4242") < proc.stdout.index("split-ran")


def test_kill_is_refused_if_ownership_changes_after_term(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    script = _proxy_cleanup_preamble(manifest) + r"""
RUN_CLEANUP_TERM_WAIT_ATTEMPTS=1
OWNERSHIP_CHECKS=0
_run_cleanup_proxy_is_owned() {
    OWNERSHIP_CHECKS=$((OWNERSHIP_CHECKS + 1))
    [ "$OWNERSHIP_CHECKS" = 1 ]
}
kill() { printf 'signal:%s:%s\n' "$1" "${@: -1}"; }
python3() { echo split-ran; }

run_cleanup_instance 5
rc=$?
printf 'return:%s\n' "$rc"
"""

    proc = _run_bash(script)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "signal:-TERM:4242" in proc.stdout
    assert "signal:-KILL" not in proc.stdout
    assert "split-ran" not in proc.stdout
    assert "not sending KILL" in proc.stderr
    assert "return:5" in proc.stdout


@pytest.mark.parametrize(
    ("setup", "unexpected"),
    [
        ("DRY_RUN=1; PROXY_PID=", "split-ran"),
        ("DRY_RUN=0; PROXY_PID=", "split-ran"),
        ("DRY_RUN=0; PROXY_PID=4242; USE_LOCAL_PROXY=", "split-ran"),
    ],
)
def test_dry_run_and_no_proxy_paths_do_not_split(
    tmp_path: Path, setup: str, unexpected: str
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    script = f"""
DRY_RUN=0
USE_LOCAL_PROXY=1
PROXY_PID=
PROXY_CONFIG=/tmp/proxy.yaml
MANIFEST_PATH={manifest}
INSTANCE_ID=
REPO_ROOT={REPO_ROOT}
{setup}
_run_cleanup_pid_is_live() {{ return 1; }}
python3() {{ echo split-ran; }}
run_cleanup_instance 0
"""

    proc = _run_bash(script)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert unexpected not in proc.stdout


def test_invalid_proxy_pid_is_not_treated_as_a_stopped_owned_proxy(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    proc = _run_bash(
        f"""
DRY_RUN=0
USE_LOCAL_PROXY=1
PROXY_PID=not-a-pid
PROXY_CONFIG=/tmp/private.yaml
MANIFEST_PATH={manifest}
INSTANCE_ID=
REPO_ROOT={REPO_ROOT}
python3() {{ echo split-ran; }}
run_cleanup_instance 6
rc=$?
printf 'return:%s\n' "$rc"
"""
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "split-ran" not in proc.stdout
    assert "refusing invalid private proxy PID" in proc.stderr
    assert "return:6" in proc.stdout


def test_cleanup_runs_only_once(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    script = _proxy_cleanup_preamble(manifest) + r"""
kill() { [ "$1" != "-TERM" ] || ALIVE=0; }
python3() { echo split-ran; }

run_cleanup_instance 0
run_cleanup_instance 0
"""

    proc = _run_bash(script)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("split-ran") == 1


@pytest.mark.parametrize("instance_id", [".", "..", "nested/id"])
def test_staged_cleanup_rejects_unsafe_instance_ids(instance_id: str) -> None:
    proc = _run_bash(
        f"""
DRY_RUN=0
PROXY_PID=
INSTANCE_ID={instance_id}
REPO_ROOT={REPO_ROOT}
rm() {{ echo unsafe-rm; }}
run_cleanup_instance 0
"""
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unsafe-rm" not in proc.stdout
    assert "refusing staged cleanup for unsafe instance id" in proc.stderr


def test_proxy_ownership_requires_module_and_exact_private_config(tmp_path: Path) -> None:
    private_config = tmp_path / "private-proxy.yaml"
    script = f"""
SHARED_PROXY=0
SHARED_CONFIG={tmp_path / 'shared-proxy.yaml'}
_run_cleanup_proxy_is_owned "$$" {private_config}
printf 'owned:%s\n' "$?"
_run_cleanup_proxy_is_owned "$$" {tmp_path / 'other.yaml'}
printf 'other:%s\n' "$?"
"""

    # These extra argv are intentionally visible in /proc/$$/cmdline, matching
    # the shape of the real `python3 -m workbuddy_bench.proxy --config ...`.
    proc = _run_bash(
        script,
        "-m",
        "workbuddy_bench.proxy",
        "--config",
        str(private_config),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == ["owned:0", "other:1"]


def test_exit_trap_preserves_nonzero_status_and_runs_once() -> None:
    proc = _run_bash(
        r"""
run_cleanup_instance() { printf 'cleanup:%s\n' "$1"; return "$1"; }
register_run_cleanup
set -e
false
echo unreachable
"""
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == ["cleanup:1"]


@pytest.mark.parametrize(("signal", "expected_rc"), [("INT", 130), ("TERM", 143)])
def test_signal_traps_use_conventional_exit_codes(signal: str, expected_rc: int) -> None:
    proc = _run_bash(
        f"""
run_cleanup_instance() {{ printf 'cleanup:%s\\n' "$1"; return "$1"; }}
register_run_cleanup
kill -{signal} "$$"
echo unreachable
"""
    )

    assert proc.returncode == expected_rc, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == [f"cleanup:{expected_rc}"]


@pytest.mark.parametrize(("exit_code", "expected"), [(0, 0), (37, 37)])
def test_tracked_foreground_preserves_status_and_clears_pid(
    exit_code: int, expected: int
) -> None:
    proc = _run_bash(
        f"""
run_tracked_foreground bash -c 'exit {exit_code}'
rc=$?
printf 'rc:%s pid:%s pgid:%s verified:%s\n' \
    "$rc" "$RUN_FOREGROUND_PID" "$RUN_FOREGROUND_PGID" "$RUN_FOREGROUND_GROUP_VERIFIED"
"""
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"rc:{expected} pid: pgid: verified:0"


def test_tracked_foreground_failure_reaches_exit_trap_with_child_status() -> None:
    proc = _run_bash(
        r"""
run_cleanup_instance() { printf 'cleanup:%s\n' "$1"; return "$1"; }
register_run_cleanup
set -e
run_tracked_foreground bash -c 'exit 37'
echo unreachable
"""
    )

    assert proc.returncode == 37, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == ["cleanup:37"]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_tracked_foreground_reaps_group_after_leader_exits(
    tmp_path: Path, exit_code: int
) -> None:
    pids_file = tmp_path / "pids"
    child_code = """
import os, subprocess, sys
from pathlib import Path
child = subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')
os._exit(int(sys.argv[2]))
"""
    try:
        proc = _run_bash(
            r'''
run_tracked_foreground "$1" -c "$2" "$3" "$4"
rc=$?
printf 'rc:%s pid:%s pgid:%s\n' "$rc" "$RUN_FOREGROUND_PID" "$RUN_FOREGROUND_PGID"
''',
            sys.executable, child_code, str(pids_file), str(exit_code),
        )
        leader, descendant = map(int, pids_file.read_text().split())
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"rc:{exit_code} pid: pgid:" in proc.stdout
        assert not _pid_is_effectively_live(leader)
        assert not _pid_is_effectively_live(descendant)
    finally:
        if pids_file.exists():
            leader = int(pids_file.read_text().split()[0])
            try:
                os.killpg(leader, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_failed_foreground_cleanup_preserves_sources_and_staged_tasks() -> None:
    proc = _run_bash(r'''
RUN_FOREGROUND_PID=4242
PROXY_PID=4243
REPO_ROOT=/tmp/unused-cleanup-test
INSTANCE_ID=test
_run_cleanup_stop_foreground() { return 1; }
_run_cleanup_stop_private_proxy() { echo proxy-stopped; return 0; }
_run_cleanup_split_proxy_log() { echo unexpected-split; }
rm() { echo unexpected-remove; }
run_cleanup_instance 17
''')
    assert proc.returncode == 17
    assert proc.stdout.strip() == "proxy-stopped"
    assert "preserving staged tasks and unsplit logs" in proc.stderr


def test_signal_during_startup_stops_paused_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "paused-pid"
    executed = tmp_path / "must-not-execute"
    try:
        proc = _run_bash(r'''
register_run_cleanup
_run_cleanup_wait_for_foreground_identity() {
    for ((i=0; i<100; i++)); do
        if _run_cleanup_foreground_identity_matches "$1" stopped; then
            printf '%s' "$1" > "$PID_FILE"
            kill -TERM "$$"
        fi
        sleep 0.01
    done
    return 1
}
PID_FILE=$1
run_tracked_foreground touch "$2"
''', str(pid_file), str(executed))
        assert proc.returncode == 143, proc.stdout + proc.stderr
        assert pid_file.exists()
        assert not _pid_is_effectively_live(int(pid_file.read_text()))
        assert not executed.exists()
    finally:
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stubborn_owned_foreground_group_escalates_to_kill() -> None:
    proc = _run_bash(
        r"""
RUN_FOREGROUND_PID=4242
RUN_FOREGROUND_PGID=4242
RUN_FOREGROUND_GROUP_VERIFIED=1
RUN_CLEANUP_FOREGROUND_SIGNAL_WAIT_ATTEMPTS=1
RUN_CLEANUP_FOREGROUND_KILL_WAIT_ATTEMPTS=1
ALIVE=1
_run_cleanup_foreground_group_is_live() { [ "$ALIVE" = 1 ]; }
_run_cleanup_foreground_group_is_owned() { return 0; }
sleep() { :; }
wait() { :; }
kill() {
    printf 'signal:%s:%s\n' "$1" "${@: -1}"
    [ "$1" != "-KILL" ] || ALIVE=0
}
_run_cleanup_stop_foreground TERM
"""
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "signal:-TERM:-4242" in proc.stdout
    assert "signal:-KILL:-4242" in proc.stdout
    assert proc.stdout.index("signal:-TERM") < proc.stdout.index("signal:-KILL")


@pytest.mark.parametrize(
    ("signal_name", "signal_number", "expected_rc"),
    [("TERM", signal.SIGTERM, 143), ("INT", signal.SIGINT, 130)],
)
def test_shell_only_signal_stops_tracked_child_group_before_proxy_cleanup(
    tmp_path: Path,
    signal_name: str,
    signal_number: signal.Signals,
    expected_rc: int,
) -> None:
    ready = tmp_path / "child-pids"
    child_code = """
import os
import subprocess
import sys

grandchild = subprocess.Popen(["sleep", "30"])
with open(sys.argv[1], "w") as handle:
    handle.write(f"{os.getpid()} {grandchild.pid}\\n")
grandchild.wait()
"""
    shell_code = r'''
source "$1"
DRY_RUN=0
USE_LOCAL_PROXY=1
PROXY_PID=4242
INSTANCE_ID=test-signal-cleanup
REPO_ROOT=/tmp/not-a-real-run-root
RUN_CLEANUP_WAIT_INTERVAL_SEC=0.01
RUN_CLEANUP_FOREGROUND_SIGNAL_WAIT_ATTEMPTS=20
RUN_CLEANUP_FOREGROUND_TERM_WAIT_ATTEMPTS=20
RUN_CLEANUP_FOREGROUND_KILL_WAIT_ATTEMPTS=20
_run_cleanup_stop_private_proxy() { echo proxy-stopped; return 0; }
_run_cleanup_split_proxy_log() { echo proxy-split; return 0; }
rm() { echo staged-cleaned; }
register_run_cleanup
run_tracked_foreground "$2" -c "$3" "$4"
echo unreachable
'''

    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            shell_code,
            "bash",
            str(CLEANUP_LIB),
            sys.executable,
            child_code,
            str(ready),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    tracked_pids: list[int] = []
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "tracked child did not become ready"
        tracked_pids = [int(raw) for raw in ready.read_text().split()]
        child_pid, grandchild_pid = tracked_pids
        assert os.getpgid(child_pid) == child_pid
        assert os.getpgid(grandchild_pid) == child_pid

        # Target only run.sh's shell PID. The private child session will receive
        # the signal only if the registered trap forwards it correctly.
        os.kill(proc.pid, signal_number)
        stdout, stderr = proc.communicate(timeout=5)

        assert proc.returncode == expected_rc, stdout + stderr
        assert "unreachable" not in stdout
        assert f"forwarding {signal_name}" in stdout
        assert stdout.index(f"forwarding {signal_name}") < stdout.index("proxy-stopped")
        assert stdout.index("proxy-stopped") < stdout.index("proxy-split")
        assert "staged-cleaned" in stdout
        assert not _pid_is_effectively_live(child_pid)
        assert not _pid_is_effectively_live(grandchild_pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        if tracked_pids:
            child_pid = tracked_pids[0]
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_run_sh_bounds_instance_id_by_utf8_bytes() -> None:
    script = (REPO_ROOT / "scripts" / "run.sh").read_text()

    assert (
        'INSTANCE_ID_BYTES="$(printf \'%s\' "$INSTANCE_ID" | LC_ALL=C wc -c)"'
        in script
    )
    assert '[ "$INSTANCE_ID_BYTES" -gt 128 ]' in script


def test_run_sh_tracks_all_long_running_evaluation_entrypoints() -> None:
    script = (REPO_ROOT / "scripts" / "run.sh").read_text()

    assert 'run_tracked_foreground "${resume_cmd[@]}"' in script
    assert 'run_tracked_foreground "${cmd[@]}"' in script
    assert 'run_tracked_foreground "${harbor_cmd[@]}"' in script
    assert script.count(
        "run_tracked_foreground python3 -m workbuddy_bench.runner.run_post_judge"
    ) == 2
