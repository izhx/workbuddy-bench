#!/usr/bin/env bash
# Run one or more WorkBuddy Bench job configs sequentially.
#
# Usage:
#   scripts/run-jobs.sh <job-slug> [job-slug ...]
#   scripts/run-jobs.sh --auto-build-harness-mount <job-slug> [job-slug ...]
#   scripts/run-jobs.sh --task-image-tag YYYY-MM-DD <job-slug> [job-slug ...]
#
# Environment variables such as NO_FORCE_BUILD, SHARDS, SHARD_CONCURRENCY,
# TASK_IMAGE_TAG, and PROXY_PORT are forwarded to scripts/run.sh.
# --auto-build-harness-mount is a convenience wrapper for
# AUTO_BUILD_HARNESS_MOUNT=1.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

AUTO_BUILD=0
TASK_IMAGE_TAG_ARG=""
JOBS=()

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --auto-build-harness-mount)
            AUTO_BUILD=1
            shift
            ;;
        --task-image-tag)
            [ "$#" -ge 2 ] || { echo "ERROR: --task-image-tag requires a value" >&2; exit 1; }
            TASK_IMAGE_TAG_ARG="$2"
            shift 2
            ;;
        --help|-h)
            usage 0
            ;;
        --*)
            echo "ERROR: unknown option: $1" >&2
            usage 1
            ;;
        *)
            JOBS+=("$1")
            shift
            ;;
    esac
done

if [ "${#JOBS[@]}" -eq 0 ]; then
    echo "ERROR: at least one job slug is required" >&2
    usage 1
fi

for job in "${JOBS[@]}"; do
    if [ ! -f "configs/jobs/${job}.yaml" ]; then
        echo "ERROR: job config not found: configs/jobs/${job}.yaml" >&2
        exit 1
    fi
done

for job in "${JOBS[@]}"; do
    echo
    echo "==================================================================="
    echo "==> Running job: $job"
    echo "==================================================================="
    run_args=(--job "$job")
    [ -n "$TASK_IMAGE_TAG_ARG" ] && run_args+=(--task-image-tag "$TASK_IMAGE_TAG_ARG")
    if [ "$AUTO_BUILD" = "1" ]; then
        AUTO_BUILD_HARNESS_MOUNT=1 uv run ./scripts/run.sh "${run_args[@]}"
    else
        uv run ./scripts/run.sh "${run_args[@]}"
    fi
    echo "==> Finished job: $job"
done

echo
echo "==> All requested jobs complete. Results under: $REPO_ROOT/results/"
