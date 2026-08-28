---
name: wbb-run-prebuilt
description: Launch one or more WorkBuddy Bench jobs with already-prebuilt task images. Use when a run must reuse local task images under one explicit shared tag, with exact task-selection preflight, no task image builds, and human-readable command output saved under results.
---

# Run WBB with Prebuilt Images

Launch one or more job slugs from `configs/jobs/` using an explicit task image
tag. Work from the WorkBuddy Bench repository root.

## Required inputs

Require both of these before proceeding:

- One or more exact job slugs.
- One explicit tag shared by all requested jobs. Accept only `latest` or a real
  ISO date in `YYYY-MM-DD` form. Never infer or default the tag, and always pass
  it with `--task-image-tag` rather than relying on ambient `TASK_IMAGE_TAG`.

If different jobs need different tags, treat them as separate launches. Prefer
a date tag for stable local reuse; `latest` is mutable and Docker Compose may
apply its normal registry pull behavior to it.

## Resolve and preflight every job

Before the real launch, resolve each job independently with the required tag:

```bash
uv run ./scripts/run.sh --job <job-slug> \
  --task-image-tag <tag> --dry-run
```

Record the `Manifest: .../manifest.json` path printed by each dry-run. The
manifest provides that job's dataset path and concrete `selected_tasks`. An
empty `selected_tasks` means all tasks in the dataset.

Use those exact values for a read-only local image check:

```bash
uv run python -m workbuddy_bench.runner.task_images preflight \
  <manifest-dataset-path> --tag <tag> --manifest <manifest-path>
```

Repeat this for every job, even when jobs appear similar. Do not use
`--build-missing`: this skill launches with images that are already prebuilt.
Stop before any evaluation if a dry-run fails or any selected image is missing,
stale, or unmanaged. Report the affected job and exact image references. If the
user wants the images built, hand off to the `wbb-image` workflow rather than
silently building them.

The task-image preflight is separate from `run.sh --dry-run`; the latter resolves
configuration and task selection but does not validate reusable task images.

## Launch with reuse forced for this invocation

Launching an evaluation can consume model quota and substantial runtime. If the
user has not already explicitly asked to start the real evaluation, obtain
confirmation after all preflights pass.

For one job:

```bash
mkdir -p results
RUN_LOG="$(pwd)/results/wbb-run-$(date -u +%Y-%m-%dT%H-%M-%SZ)-$$-<job-slug>.log"
printf 'jobs=%s\ntask_image_tag=%s\nlog=%s\n' \
  '<job-slug>' '<tag>' "$RUN_LOG" | tee "$RUN_LOG"
set -o pipefail
NO_FORCE_BUILD=1 uv run ./scripts/run.sh \
  --job <job-slug> --task-image-tag <tag> \
  2>&1 | tee -a "$RUN_LOG"
```

For multiple jobs, preserve the requested order:

```bash
mkdir -p results
RUN_LOG="$(pwd)/results/wbb-run-$(date -u +%Y-%m-%dT%H-%M-%SZ)-$$-batch.log"
printf 'jobs=%s\ntask_image_tag=%s\nlog=%s\n' \
  '<job-a> <job-b>' '<tag>' "$RUN_LOG" | tee "$RUN_LOG"
set -o pipefail
NO_FORCE_BUILD=1 scripts/run-jobs.sh \
  --task-image-tag <tag> <job-a> <job-b> \
  2>&1 | tee -a "$RUN_LOG"
```

Run each complete block in one Bash invocation so `RUN_LOG` and `pipefail`
apply to the evaluation pipeline. The log is a regular file directly under
`results/`, rather than a directory that could be confused with a Harbor job.
`tee` keeps the output visible live while saving combined stdout and stderr;
`pipefail` preserves the evaluation command's nonzero status. The UTC timestamp
plus shell PID keeps concurrent launch logs distinct.

`NO_FORCE_BUILD=1` makes this launch use Harbor's `force_build=false` path
without editing the job YAML. The explicit tag causes `docker_image` references
to be injected only into staged task copies. Source datasets and benchmark
manifests remain unchanged.

`scripts/run-jobs.sh` runs jobs sequentially and stops on the first failure.
Report which jobs completed, which job failed, and which later jobs were not
started. Always print the resolved operator log path before launch and include
it in the handoff, whether the run succeeds or fails. Do not retry, rebuild
images, or change job configuration unless the user requests it.
