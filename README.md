<p align="center">
  <img src="docs/assets/wb-bench-banner-dark.png#gh-light-mode-only" style="height: 5em" alt="WorkBuddy Bench" />
  <img src="docs/assets/wb-bench-banner.png#gh-dark-mode-only" style="height: 5em" alt="WorkBuddy Bench" />
</p>

<p align="center">
  <a href="http://workbuddybench.com/"><img alt="Website" src="https://img.shields.io/badge/Website-workbuddybench.com-28B894"></a>
  <a href="https://huggingface.co/datasets/tencent/workbuddy-bench"><img alt="Dataset" src="https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-HuggingFace-FFD21E"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Tencent-blue"></a>
  <img alt="Tracks" src="https://img.shields.io/badge/tracks-Code%20%C2%B7%20Web%20%C2%B7%20Office%20%C2%B7%20Security-28B894">
</p>

<p align="center">
  <strong>English</strong> ·
  <a href="README.zh.md">简体中文</a>
</p>

---

## Overview

WorkBuddy Bench is a benchmark for evaluating coding agents on real-world work,
reverse-engineered from actual developer, PM, algo, QA, ops, and security tasks.
Given a task and a sandboxed workspace, an agent is asked to produce the correct
change (a patch, an artifact, a report) and is graded against a test suite.

The benchmark ships four subsets:

| Subset | Tasks | Domain | What the agent does |
|--------|-------|--------|---------------------|
| **Code** | 80 | repo-level SWE (Python) | Dropped into a real project at a baseline commit; locate and edit code across modules, pass the tests. 18 categories across developer, PM, algo, QA, and ops roles. |
| **Web** | 70 | front-end / GUI (HTML/CSS/JS) | Generate, modify, analyze, and QA front-end work: page interaction, data visualization, visual design, front-end project analysis, code testing, page implementation, document conversion. |
| **Office** | 50 | office data / file workflows | Operate over mixed-format files (xlsx / csv / pdf / docx) to produce the exactly-correct artifact. Difficulty comes from structure, relationships, state, and evidence chains, not row count. |
| **Security** | 60 | security / vuln (red + blue) | Find and safely reproduce real vulnerabilities, analyze malware, run security operations, probe agent attack surfaces, across 6 domains. |

This repository is the benchmark's **evaluation framework**. It drops an agent
CLI (a *harness*) into a local Docker sandbox, runs it against a batch of tasks,
captures what happened (patches, trajectories, test results, efficiency), and
scores the run. It is built on top of
[Harbor](https://github.com/harbor-framework/harbor).

## Quick start

The easiest way to run WorkBuddy Bench is to let a coding agent drive the whole
flow for you. This repo ships a **`wbbench-run-setup` skill** (under
[`.agents/skills/`](.agents/skills/), also linked at `.claude/skills/`) that takes
you end-to-end — from an unconfigured checkout to a running, analyzed evaluation:
datasets, environment, model config, `.env` credentials, the job file, launching
the run, and finally the analysis report. Open the repo in a skill-aware agent
(Claude Code, CodeBuddy Code, …), invoke the skill (e.g. `/wbbench-run-setup`) or
simply ask it to *set up a WorkBuddy Bench run*, and it guides you one step at a
time.

Prefer to configure things by hand — or want to see what the skill is doing under
the hood? The **Setup** and **Usage** sections below walk through the same steps
manually.

## Setup

Every task runs in Docker for reproducibility. You need Python ≥ 3.12,
[uv](https://docs.astral.sh/uv/), and Docker.

```bash
uv sync            # install dependencies
cp .env.example .env
```

The task datasets are **not** in this repo. They live on HuggingFace at
[`tencent/workbuddy-bench`](https://huggingface.co/datasets/tencent/workbuddy-bench)
as one archive per subset. Download the subsets you want into `datasets/`:

```bash
./scripts/dataset/fetch-dataset.sh code   # or: web / office / sec / all
```

This fetches, checksum-verifies, and extracts each subset to
`datasets/wb-bench-<subset>-v1.0/`. See [`datasets/README.md`](datasets/README.md)
for the archive list, sizes, and the `WB_BENCH_HF_REPO` override.

Point the framework at a model. Copy the template and fill it in:

```bash
cp configs/models/_template.model.yaml configs/models/<provider>/<slug>.yaml
```

```yaml
model:
  name: Hy3                         # the model id your backend expects
  protocols: [openai]               # openai or anthropic
  backend_url_env: MODEL_BASE_URL   # the .env var holding the base URL
  backend_key_env: MODEL_API_KEY    # the .env var holding the API key
```

`backend_url_env` and `backend_key_env` name the env vars; put the actual URL and
key in `.env`, where secrets stay out of version control.

## Usage

A **job** is one evaluation run: a model, a harness, and a dataset brought
together. Copy the template and fill it in:

```bash
cp configs/jobs/_template.job.yaml configs/jobs/<slug>.yaml
```

```yaml
model: <provider>/<slug>                  # a model under configs/models/
harness: codebuddy-code/<version>         # the agent CLI to evaluate
dataset: datasets/wb-bench-code-v1.0/tasks
```

Then run it:

```bash
uv run ./scripts/run.sh --help                  # list available jobs
uv run ./scripts/run.sh --job <slug> --dry-run  # preview what will run, then exit
uv run ./scripts/run.sh --job <slug>            # run it
```

To resume from completed trials in an earlier Harbor experiment directory, keep
the same job config and pass the old result directory:

```bash
uv run ./scripts/run.sh \
  --job <slug> \
  --resume-job results/<job>/<experiment-dir>
```

`--resume-job` accepts either a repository-relative path or an absolute path and
can be repeated when the earlier run produced more than one experiment
directory:

```bash
uv run ./scripts/run.sh \
  --job <slug> \
  --resume-job results/<job>/<experiment-dir-1> \
  --resume-job results/<job>/<experiment-dir-2>
```

Any resume request uses the sharded runner, with `--shards 1` when `SHARDS` is
unset. A previous trial is reusable only when its `result.json` contains the
`verifier_result.rewards.reward` field and its task checksum matches the current
task; a reward of `0` is still a completed result. A task is skipped only after
the resume directories collectively provide at least the job's configured
`n_attempts`. If they provide fewer attempts, the entire task runs again rather
than filling only the difference.

Reusable trial directories are linked under the current result root's
`resumed-trials/`. Remaining tasks are written to new Harbor timestamp
directories, and the old experiment directories are not modified. Invalid
resume paths fail before run setup. `--dry-run` retains its manifest-only
behavior and does not calculate how many trials can be reused.

Task sandboxes can use explicitly enabled reusable local images named
`<dataset-id>/<task>:<tag>` (for example,
`wb-bench-code-v1.0/<task>:2026-08-27`). Reuse has no default tag: first build
the images, then run with the same explicit tag and `force_build=false`:

```bash
uv run python -m workbuddy_bench.runner.task_images build \
  datasets/wb-bench-code-v1.0/tasks --tag 2026-08-27

# Reuse is opt-in: set environment_override.force_build=false in the job.
uv run ./scripts/run.sh --job <slug> --task-image-tag 2026-08-27
```

Without `--task-image-tag` (or `TASK_IMAGE_TAG`), the runner does not inject
`environment.docker_image` and preserves the original benchmark behavior. With
an explicit tag, it injects only into the staged dataset copy; checked-in task
files remain unchanged. Harbor's existing `force_build` switch still decides
whether to build or reuse. The runner never builds missing task images
automatically. See
[`configs/README.md`](configs/README.md) for the full policy.

Results land under `results/`. The full set of job and model fields is documented
in [`configs/README.md`](configs/README.md).

## Citation & license

Licensed under the Tencent license. See [`LICENSE`](LICENSE).

If you find WorkBuddy Bench helpful, please cite it:

```bibtex
@misc{workbuddybench2026,
    title  = {WorkBuddy Bench: Evaluating Coding Agents on Real Role-Played Work},
    author = {Tencent Youtu Lab and Keen Security Lab and WorkBuddy and Yunding Security Lab},
    year   = {2026},
    note   = {https://github.com/Tencent/workbuddy-bench}
}
```
