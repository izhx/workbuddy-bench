# Cleanup lifecycle for scripts/run.sh.
#
# This file is sourced by the runner.  It deliberately owns only process
# finalization: the parent shell still supplies the live run state through
# PROXY_PID / PROXY_CONFIG / MANIFEST_PATH and related variables.

RUN_CLEANUP_DONE=0
RUN_FOREGROUND_PID=""
RUN_FOREGROUND_PGID=""
RUN_FOREGROUND_GROUP_VERIFIED=0

_run_cleanup_pid_is_live() {
    local pid="${1:-}"
    case "$pid" in
        ''|*[!0-9]*|0|1) return 1 ;;
    esac
    kill -0 -- "$pid" 2>/dev/null || return 1

    # A zombie has already closed its proxy log descriptors.  Treat it as
    # stopped even if kill -0 still succeeds until the parent reaps it.
    local state
    state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
    # If state inspection fails after kill -0 succeeded, assume it is still
    # live.  A false negative here would let the splitter touch an active log.
    [ -n "$state" ] || return 0
    case "$state" in
        Z*) return 1 ;;
    esac
    return 0
}

_run_cleanup_proxy_is_owned() {
    local pid="${1:-}" config="${2:-}"
    [ "${SHARED_PROXY:-0}" != "1" ] || return 1
    [ -n "$config" ] || return 1
    [ "$config" != "${SHARED_CONFIG:-}" ] || return 1
    _run_cleanup_pid_is_live "$pid" || return 1

    # Match the exact --config argv rather than a substring of a rendered ps
    # command.  PROXY_CONFIG is job-private and was passed verbatim at launch.
    local arg previous="" saw_proxy_module=0 saw_config=0
    while IFS= read -r -d '' arg; do
        if [ "$previous" = "-m" ] && [ "$arg" = "workbuddy_bench.proxy" ]; then
            saw_proxy_module=1
        elif [ "$previous" = "--config" ] && [ "$arg" = "$config" ]; then
            saw_config=1
        fi
        case "$arg" in
            --config=*)
                [ "${arg#--config=}" = "$config" ] && saw_config=1
                ;;
        esac
        previous="$arg"
    done < "/proc/$pid/cmdline"
    [ "$saw_proxy_module" = "1" ] && [ "$saw_config" = "1" ]
}

_run_cleanup_wait_while() {
    local attempts="$1" interval="${RUN_CLEANUP_WAIT_INTERVAL_SEC:-0.1}"
    shift
    case "$attempts" in
        ''|*[!0-9]*) attempts=1 ;;
    esac

    while "$@"; do
        [ "$attempts" -gt 0 ] || return 1
        sleep "$interval"
        attempts=$((attempts - 1))
    done
    return 0
}

_run_cleanup_wait_for_exit() {
    _run_cleanup_wait_while "${2:-1}" _run_cleanup_pid_is_live "$1" || return 1
    wait "$1" 2>/dev/null || true
}

_run_cleanup_clear_foreground() {
    RUN_FOREGROUND_PID=""
    RUN_FOREGROUND_PGID=""
    RUN_FOREGROUND_GROUP_VERIFIED=0
}

_run_cleanup_foreground_identity_matches() {
    local pid="${1:-}"
    case "$pid" in
        ''|*[!0-9]*|0|1) return 1 ;;
    esac

    local identity ppid pgid sid state
    identity="$(ps -p "$pid" -o ppid=,pgid=,sid=,stat= 2>/dev/null || true)"
    [ -n "$identity" ] || return 1
    read -r ppid pgid sid state <<< "$identity"
    [ "$ppid" = "$$" ] && [ "$pgid" = "$pid" ] && [ "$sid" = "$pid" ] || return 1
    case "$state" in
        Z*) return 1 ;;
        T*) return 0 ;;
    esac
    [ "${2:-}" != "stopped" ]
}

_run_cleanup_wait_for_foreground_identity() {
    local pid="$1" attempts="${RUN_FOREGROUND_IDENTITY_WAIT_ATTEMPTS:-50}"
    local interval="${RUN_FOREGROUND_IDENTITY_WAIT_INTERVAL_SEC:-0.01}"
    case "$attempts" in
        ''|*[!0-9]*) attempts=50 ;;
    esac

    while [ "$attempts" -gt 0 ]; do
        _run_cleanup_foreground_identity_matches "$pid" stopped && return 0
        _run_cleanup_pid_is_live "$pid" || return 1
        sleep "$interval"
        attempts=$((attempts - 1))
    done
    return 1
}

_run_cleanup_foreground_group_is_live() {
    local pgid="${1:-}"
    case "$pgid" in
        ''|*[!0-9]*|0|1) return 1 ;;
    esac
    kill -0 -- "-$pgid" 2>/dev/null || return 1

    local rows member_pgid state
    if ! rows="$(ps -eo pgid=,stat= 2>/dev/null)"; then
        # The group-level signal probe succeeded. If process-table inspection is
        # unavailable, fail closed and continue treating it as live.
        return 0
    fi
    while read -r member_pgid state; do
        [ "$member_pgid" = "$pgid" ] || continue
        case "$state" in
            Z*) ;;
            *) return 0 ;;
        esac
    done <<< "$rows"
    return 1
}

_run_cleanup_foreground_group_is_owned() {
    local pid="${RUN_FOREGROUND_PID:-}" pgid="${RUN_FOREGROUND_PGID:-}"
    [ "${RUN_FOREGROUND_GROUP_VERIFIED:-0}" = "1" ] || return 1
    [ -n "$pid" ] && [ "$pgid" = "$pid" ] || return 1

    local identity ppid actual_pgid sid state
    identity="$(ps -p "$pid" -o ppid=,pgid=,sid=,stat= 2>/dev/null || true)"
    if [ -n "$identity" ]; then
        read -r ppid actual_pgid sid state <<< "$identity"
        [ "$ppid" = "$$" ] && [ "$actual_pgid" = "$pgid" ] && [ "$sid" = "$pgid" ]
        return $?
    fi

    # The group leader may already have been reaped while descendants are still
    # shutting down. Every remaining member must still belong to the verified
    # session before an escalation is allowed.
    local rows member_pgid member_sid member_state found=0
    rows="$(ps -eo pgid=,sid=,stat= 2>/dev/null || true)"
    [ -n "$rows" ] || return 1
    while read -r member_pgid member_sid member_state; do
        [ "$member_pgid" = "$pgid" ] || continue
        case "$member_state" in
            Z*) continue ;;
        esac
        [ "$member_sid" = "$pgid" ] || return 1
        found=1
    done <<< "$rows"
    [ "$found" = "1" ]
}

_run_cleanup_wait_for_foreground_group() {
    _run_cleanup_wait_while "${3:-1}" _run_cleanup_foreground_group_is_live "$2" || return 1
    wait "$1" 2>/dev/null || true
    _run_cleanup_clear_foreground
    return 0
}

_run_cleanup_signal_foreground_group() {
    local signal="$1"
    if ! _run_cleanup_foreground_group_is_owned; then
        _run_cleanup_foreground_group_is_live "$RUN_FOREGROUND_PGID" || return 0
        echo "WARNING: refusing to signal unverified foreground process group PGID=${RUN_FOREGROUND_PGID:-}; it may remain running." >&2
        return 1
    fi
    kill "-$signal" -- "-${RUN_FOREGROUND_PGID}" 2>/dev/null || true
    # Deliver pending termination even if interrupted during the startup handshake.
    [ "$signal" = "KILL" ] || kill -CONT -- "-${RUN_FOREGROUND_PGID}" 2>/dev/null || true
}

_run_cleanup_stop_foreground() {
    local signal="${1:-TERM}"
    local pid="${RUN_FOREGROUND_PID:-}" pgid="${RUN_FOREGROUND_PGID:-}"
    [ -n "$pid" ] || return 2
    case "$signal" in
        INT|TERM) ;;
        *) signal=TERM ;;
    esac

    case "$pid:$pgid" in
        *[!0-9:]*|0:*|1:*|*:0|*:1|:*)
            echo "WARNING: refusing invalid foreground PID/PGID=$pid/$pgid." >&2
            return 1
            ;;
    esac
    if [ "${RUN_FOREGROUND_GROUP_VERIFIED:-0}" != "1" ] \
        && _run_cleanup_foreground_identity_matches "$pid"; then
        RUN_FOREGROUND_GROUP_VERIFIED=1
    fi
    if ! _run_cleanup_foreground_group_is_live "$pgid"; then
        wait "$pid" 2>/dev/null || true
        _run_cleanup_clear_foreground
        return 0
    fi

    local signals=("$signal") next attempts
    [ "$signal" = "TERM" ] || signals+=(TERM)
    signals+=(KILL)
    for next in "${signals[@]}"; do
        if [ "$next" = "$signal" ]; then
            attempts="${RUN_CLEANUP_FOREGROUND_SIGNAL_WAIT_ATTEMPTS:-50}"
        elif [ "$next" = "TERM" ]; then
            attempts="${RUN_CLEANUP_FOREGROUND_TERM_WAIT_ATTEMPTS:-50}"
        else
            attempts="${RUN_CLEANUP_FOREGROUND_KILL_WAIT_ATTEMPTS:-20}"
        fi
        echo "Cleanup: forwarding $next to tracked foreground process group PGID=$pgid"
        _run_cleanup_signal_foreground_group "$next" || return 1
        _run_cleanup_wait_for_foreground_group "$pid" "$pgid" "$attempts" && return 0
    done

    echo "WARNING: foreground process group PGID=$pgid is still live after KILL." >&2
    return 1
}

run_tracked_foreground() {
    [ "$#" -gt 0 ] || {
        echo "ERROR: run_tracked_foreground requires a command." >&2
        return 2
    }
    [ -z "${RUN_FOREGROUND_PID:-}" ] || {
        echo "ERROR: a tracked foreground command is already running (PID=$RUN_FOREGROUND_PID)." >&2
        return 2
    }
    command -v setsid >/dev/null 2>&1 || {
        echo "ERROR: tracked foreground execution requires the setsid command." >&2
        return 127
    }

    # Bash defers traps while synchronously waiting for an external command. Run
    # the evaluation in its own session and wait through the interruptible shell
    # builtin instead. Reset inherited signal dispositions before exec: async
    # commands otherwise inherit SIGINT ignored when job control is disabled.
    (
        trap - INT TERM
        # Pause before exec so even a command that immediately exits after
        # spawning children cannot outrun the parent's group ownership check.
        exec setsid --wait -- bash -c 'kill -STOP "$$"; exec "$@"' bash "$@"
    ) &
    RUN_FOREGROUND_PID=$! RUN_FOREGROUND_PGID=$! RUN_FOREGROUND_GROUP_VERIFIED=0

    if _run_cleanup_wait_for_foreground_identity "$RUN_FOREGROUND_PID"; then
        RUN_FOREGROUND_GROUP_VERIFIED=1
        kill -CONT -- "$RUN_FOREGROUND_PID" 2>/dev/null || true
    elif _run_cleanup_pid_is_live "$RUN_FOREGROUND_PID"; then
        echo "ERROR: tracked foreground PID=$RUN_FOREGROUND_PID did not establish its private process group." >&2
        local failed_pid="$RUN_FOREGROUND_PID"
        kill -TERM -- "$failed_pid" 2>/dev/null || true
        kill -CONT -- "$failed_pid" 2>/dev/null || true
        _run_cleanup_wait_for_exit "$failed_pid" "${RUN_CLEANUP_FOREGROUND_KILL_WAIT_ATTEMPTS:-20}" || true
        _run_cleanup_clear_foreground
        return 125
    fi

    local child_rc
    if wait "$RUN_FOREGROUND_PID"; then
        child_rc=0
    else
        child_rc=$?
    fi
    # A shard launcher can fail while already-started shards survive. Keep its
    # verified group until all members are stopped, even after reaping the leader.
    if ! _run_cleanup_stop_foreground TERM; then
        [ "$child_rc" -ne 0 ] || child_rc=125
    fi
    return "$child_rc"
}

_run_cleanup_stop_private_proxy() {
    local pid="${PROXY_PID:-}" config="${PROXY_CONFIG:-}"
    [ -n "$pid" ] || return 2
    case "$pid" in
        *[!0-9]*|0|1)
            echo "WARNING: refusing invalid private proxy PID=$pid; leaving proxy log unsplit." >&2
            return 1
            ;;
    esac

    # A proxy that already exited has closed/flushed its log and is safe to
    # post-process.  A live PID must still match this run's private config.
    if ! _run_cleanup_pid_is_live "$pid"; then
        return 0
    fi
    if ! _run_cleanup_proxy_is_owned "$pid" "$config"; then
        _run_cleanup_pid_is_live "$pid" || return 0
        echo "WARNING: refusing to stop unverified proxy PID=$pid; leaving proxy log unsplit." >&2
        return 1
    fi

    echo "Cleanup: stopping job-private proxy PID=$pid"
    kill -TERM -- "$pid" 2>/dev/null || true
    if _run_cleanup_wait_for_exit "$pid" "${RUN_CLEANUP_TERM_WAIT_ATTEMPTS:-50}"; then
        return 0
    fi

    # PID reuse or a changed command line after TERM means ownership is no
    # longer proven.  Never escalate to KILL in that state.
    if ! _run_cleanup_proxy_is_owned "$pid" "$config"; then
        _run_cleanup_pid_is_live "$pid" || return 0
        echo "WARNING: proxy PID=$pid did not exit and ownership can no longer be verified; not sending KILL." >&2
        return 1
    fi

    echo "WARNING: proxy PID=$pid did not exit after TERM; sending KILL." >&2
    kill -KILL -- "$pid" 2>/dev/null || true
    if _run_cleanup_wait_for_exit "$pid" "${RUN_CLEANUP_KILL_WAIT_ATTEMPTS:-20}"; then
        return 0
    fi

    echo "WARNING: proxy PID=$pid is still live after KILL; leaving proxy log unsplit." >&2
    return 1
}

_run_cleanup_split_proxy_log() {
    [ "${DRY_RUN:-0}" != "1" ] || return 0
    [ "${USE_LOCAL_PROXY:-}" = "1" ] || return 0
    [ -n "${MANIFEST_PATH:-}" ] && [ -f "$MANIFEST_PATH" ] || return 0

    local split_cmd=(python3 -m workbuddy_bench.runner.split_proxy_log
        --manifest "$MANIFEST_PATH")
    [ -n "${PROXY_LOG_DIR:-}" ] && split_cmd+=(--log-dir "$PROXY_LOG_DIR")
    if [ -n "${RESUME_IN_PLACE_PATH:-}" ]; then
        split_cmd+=(--job-dir "$RESUME_IN_PLACE_PATH")
    elif [ -n "${JOB_CONFIG_RUNTIME:-}" ]; then
        split_cmd+=(--runtime-config "$JOB_CONFIG_RUNTIME")
    fi

    "${split_cmd[@]}" || {
        echo "WARNING: proxy-log split failed (non-fatal)." >&2
        return 1
    }
}

run_cleanup_instance() {
    local original_rc="${1:-$?}" signal="${2:-}" foreground_stopped=1
    [ "${RUN_CLEANUP_DONE:-0}" = "1" ] && return "$original_rc"
    RUN_CLEANUP_DONE=1
    set +e

    # Stop the active evaluation process group before its private proxy. This is
    # normally populated only while run_tracked_foreground is waiting; keeping
    # the ordering here prevents surviving shard children from issuing requests
    # after the proxy log has been finalized.
    if [ -n "${RUN_FOREGROUND_PID:-}" ]; then
        _run_cleanup_stop_foreground "${signal:-TERM}" || foreground_stopped=0
    fi

    # No PID means this run never started a private proxy (direct/shared/dry
    # run or a preflight failure), so there is no owned writer to finalize.
    if [ "${DRY_RUN:-0}" != "1" ] && [ -n "${PROXY_PID:-}" ]; then
        if _run_cleanup_stop_private_proxy && [ "$foreground_stopped" = "1" ]; then
            # The destructive splitter may replace/unlink the run-level log.
            # Invoke it only after the sole owned writer has fully exited.
            _run_cleanup_split_proxy_log || true
        fi
    fi

    if [ "$foreground_stopped" != "1" ]; then
        echo "WARNING: foreground remains live; preserving staged tasks and unsplit logs." >&2
        return "$original_rc"
    fi

    # Remove this run's throwaway staged dataset copy.  Keep the target scoped
    # to one instance and reject path separators in a caller-supplied id.
    if [ -n "${INSTANCE_ID:-}" ] && [ -n "${REPO_ROOT:-}" ]; then
        case "$INSTANCE_ID" in
            .|..|*/*)
                echo "WARNING: refusing staged cleanup for unsafe instance id: $INSTANCE_ID" >&2
                ;;
            *)
                rm -rf -- "$REPO_ROOT/.workspace/tmp/staged/$INSTANCE_ID" 2>/dev/null || true
                ;;
        esac
    fi
    return "$original_rc"
}

_run_cleanup_dispatch() {
    local rc="$1" signal="${2:-}"
    trap - EXIT INT TERM
    run_cleanup_instance "$rc" "$signal"
    exit "$rc"
}

register_run_cleanup() {
    trap '_run_cleanup_dispatch "$?"' EXIT
    trap '_run_cleanup_dispatch 130 INT' INT
    trap '_run_cleanup_dispatch 143 TERM' TERM
}
