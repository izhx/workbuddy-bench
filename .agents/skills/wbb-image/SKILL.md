---
name: wbb-image
description: Prebuild and verify reusable local task images for WorkBuddy Bench. Use when resolving task image references, building all or selected task environments, checking local images against source-hash labels, or preparing a benchmark run to reuse images with an explicit matching tag.
---

# WBB Task Images

Manage reusable task images through the repository's existing CLI. Run every
command from the WorkBuddy Bench repository root. Do not change benchmark
manifests or source task TOML files.

## Establish the exact scope

1. Identify the dataset tasks path, for example
   `datasets/wb-bench-office-v1.0/tasks`.
2. Identify the exact task names. Omit `--include-task` only when the user wants
   every task under that path. Repeat it for multiple selected tasks.
3. Require an explicit tag for every command, even though the standalone CLI
   accepts `latest` by default. Valid tags are `latest` and real ISO dates in
   `YYYY-MM-DD` form.
4. Prefer a date tag for a stable local snapshot. `latest` is mutable, and
   Docker Compose may apply its normal registry pull behavior to that tag.

Resolve references and source hashes before building:

```bash
uv run python -m workbuddy_bench.runner.task_images list \
  <tasks-path> --tag <latest-or-YYYY-MM-DD>
```

For a selected task:

```bash
uv run python -m workbuddy_bench.runner.task_images list \
  <tasks-path> --tag <latest-or-YYYY-MM-DD> \
  --include-task <task-name>
```

Report the number of targets and their resolved references. References follow
`<dataset-id>/<normalized-task-name>:<tag>`.

## Prebuild images

Build only when the user asks to prebuild or repair images. Building mutates the
local Docker image store and the Dockerfile may access the network.

```bash
uv run python -m workbuddy_bench.runner.task_images build \
  <tasks-path> --tag <latest-or-YYYY-MM-DD> \
  --include-task <task-name>
```

Remove `--include-task` only for an explicitly requested all-task build. The
builder reuses an existing image whose labels match the current source. It can
replace a stale `latest` image. A stale existing date tag is treated as
immutable and fails; recommend a new date tag. Use `build --force` only when the
user explicitly authorizes replacing that exact dated image.

Never run broad Docker cleanup or pruning as part of this workflow.

## Verify local images

Use the read-only preflight for checks. Do not add `--build-missing` when the
request is only to inspect or verify.

```bash
uv run python -m workbuddy_bench.runner.task_images preflight \
  <tasks-path> --tag <latest-or-YYYY-MM-DD> \
  --include-task <task-name>
```

Preflight uses local `docker image inspect`; it does not pull an image. A
nonzero result means at least one selected reference is missing, stale, or
unmanaged. Report each failing reference and reason rather than silently
building it.

The builder labels each image with dataset id, task name, and a SHA-256 hash of
build-relevant files under the task's `environment/` directory. The hash
excludes `.git`, `__pycache__`, `.DS_Store`, and runtime-only
`docker-compose.yaml`. This detects a mismatch between current environment
sources and a normally built local image. It is not a cryptographic trust proof
against someone who can forge Docker labels, does not cover instructions or
tests outside `environment/`, and does not prove reproducibility when base
images or build inputs are mutable.

If Docker cannot be inspected, report the daemon or permission error. Do not
change Docker permissions as part of this skill.

## Hand off to a benchmark run

Prebuild and preflight the exact task selection with the same tag that the run
will use. Reuse also requires the job's existing Harbor setting:

```yaml
environment_override:
  force_build: false
```

Then pass the matching tag explicitly:

```bash
uv run ./scripts/run.sh --job <slug> \
  --task-image-tag <latest-or-YYYY-MM-DD>
```

`NO_FORCE_BUILD=1` is the one-run alternative to the YAML override. Do not
silently set either one unless the user asks to run with image reuse.

Keep these runtime rules explicit in the handoff:

- With no `--task-image-tag` and no `TASK_IMAGE_TAG`, image injection is
  disabled and the original benchmark behavior is preserved.
- An explicit tag injects `environment.docker_image` only into the staged task
  copy. It does not edit the source dataset or add image metadata to the
  benchmark manifest.
- With `force_build=true` and a Dockerfile, Harbor follows its build path rather
  than reusing the injected image.
- With `force_build=false`, a missing local image can trigger Compose's normal
  pull attempt and then fail. Harbor does not fall back to building the task
  Dockerfile.

Before launching a requested run, stop if preflight fails. Provide the exact
build command for the failed scope and tag; build only if the user also
authorized prebuilding.
