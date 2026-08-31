---
name: wbb-runner
description: Run a WorkBuddy Bench job with prebuilt task images, then use the repository's native in-place resume policy to replace API, network, Docker, environment, or incomplete trials until every task has three valid attempts within a bounded retry budget. Use for managed run-and-repair workflows, not ordinary reporting or retrying low scores.
---

# WBB Run and Repair

Run from the WorkBuddy Bench repository root. This workflow spends model quota,
mutates existing Harbor experiments during repair, and requires prebuilt task
images.

## Establish the run contract

Before launching, establish:

- one exact job slug and one explicit task image tag;
- whether this is a new run or repair of exact existing experiment directories;
- a maximum total number of new repair attempts;
- any explicit `--retry-exception` or `--keep-exception` overrides.

Handle multiple jobs sequentially. The target is exactly three valid attempts
per task. Resolve the effective `n_attempts` before a new launch and stop if it
is not `3`; do not edit the job config without authorization. For an existing
experiment, verify that `lock.json` plans exactly three slots for every task.

A valid attempt has a matching task checksum, a non-null reward, and no
retryable Harbor crash exception. Reward `0` is valid when the attempt did not
crash. Never retry merely to obtain a higher score.

If the user asked only to inspect or plan, do not launch or resume. For an
authorized run-and-repair request, show the resolved job, tag, experiment paths,
exception overrides, and total repair-attempt bound before the first
quota-consuming command.

## Launch with prebuilt images

Read and follow [`../wbb-run-prebuilt/SKILL.md`](../wbb-run-prebuilt/SKILL.md)
for the initial dry-run, exact task selection, image preflight, launch
authorization, and operator logging. Never build missing images in this skill.

For a new run, record the Harbor job directories present before launch, the
printed instance id, manifest path, launch plan/log paths, and operator log.
After launch, identify each newly created real Harbor experiment. A real
experiment has `config.json`, `lock.json`, and direct trial directories. Do not
mistake `.launches/`, `resumed-trials/`, or `*.attempt-history/` for an
experiment, and do not select a directory merely because it has the newest
timestamp. Confirm its recorded instance identity when concurrent runs are
possible.

Repair multiple shard experiments sequentially, one exact directory per
in-place command. For an existing experiment, skip the initial launch and start
with the dry-run below.

Cross-experiment `--resume-job` is not the repair mechanism: it reuses trials
with matching checksum and non-null reward, but does not exclude every
reward-bearing crash handled by in-place resume. Use it only when explicitly
requested and the source trials have already been checked or repaired. If fewer
than three reusable attempts exist for a task, `sharded_eval` reruns that task
with the full job-level attempt count.

## Preview native in-place repair

Use the same exception-policy flags for preview and execution:

```bash
uv run ./scripts/run.sh \
  --job <job-slug> \
  --task-image-tag <tag> \
  --resume-in-place <exact-experiment-dir> \
  --max-extra-attempts <remaining-budget> \
  --dry-run
```

The native plan validates recorded identity, staged task contents, checksums,
prebuilt images, proxy/model identity, planned slots, existing results, and the
attempt budget before moving anything. Review `valid`, `attempts_needed`,
`archive reasons`, and every listed trial.

By default the runner retries its maintained API, network, agent-startup,
Docker/environment, verifier, cancellation, and missing-result failure types,
including crashes that already carry a reward. It keeps genuine evaluation
outcomes such as agent wall-clock timeout or model output-length failure when
they carry a reward.

- Add `--retry-exception <ExactHarborType>` for a newly confirmed crash type.
- Add `--keep-exception <ExactHarborType>` to override a default retry type.
- Do not use `--no-retry-crashed` unless the user explicitly requests the old,
  missing-reward-only behavior.

Type names are exact, case-sensitive `exception_info.exception_type` values.
`--keep-exception` does not make a missing or null reward valid.
When evidence comes from another server, first inspect a representative result
and relevant logs. If classification requires message or log regexes rather
than an exact type, stop and update the core in-place policy instead of moving
trials with an ad-hoc skill script.

An unrelated child directory such as `report/` blocks native resume. Report its
exact path and ask before moving it outside the experiment; never relocate or
delete unrelated output automatically.

## Execute and finish

Run the approved command without `--dry-run` and preserve combined output:

```bash
RUN_LOG="$(pwd)/results/wbb-runner-$(date -u +%Y-%m-%dT%H-%M-%SZ)-$$-<job>-resume.log"
printf 'job=%s\nexperiment=%s\ntag=%s\nlog=%s\n' \
  '<job-slug>' '<exact-experiment-dir>' '<tag>' "$RUN_LOG" | tee "$RUN_LOG"
set -o pipefail
uv run ./scripts/run.sh \
  --job <job-slug> \
  --task-image-tag <tag> \
  --resume-in-place <exact-experiment-dir> \
  --max-extra-attempts <remaining-budget> \
  2>&1 | tee -a "$RUN_LOG"
```

Include any approved exception overrides in both commands. One invocation
already archives invalid trials to sibling attempt history and repeatedly calls
Harbor until all planned slots are valid or its budget is exhausted; do not add
a second quarantine layer.

Track the reported `extra_attempts_used` against the workflow-wide bound. If a
command exhausts its local budget, inspect the new archive reasons before using
any remaining global budget. Stop rather than retrying blindly when the same API
or infrastructure cause is still present, an identity/checksum/image/proxy
check fails, no global budget remains, or the user interrupts.

Finish each experiment with the same dry-run and exception overrides. Claim
completion only when it reports `attempts_needed=0` and `valid=planned`; because
the lock plans three slots per task, this proves every task in that experiment
has three valid attempts. Do not change image tags, task contents, or experiment
identity between retries, and do not delete attempt history.

## Handoff

Report the job, image tag, exact experiment directories, operator/resume logs,
exception overrides, archive reason counts, attempts used, final valid/planned
counts, and either completion or the precise stop condition.
