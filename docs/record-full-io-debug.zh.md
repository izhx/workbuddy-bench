# `record_full_io` 不生成 `agent/requests.jsonl` 排查指南

本文用于排查通过 `wbb-runner` 启动新实验或执行 `in-place resume` 时，Job 已设置：

```yaml
model_connection: local_proxy
record_full_io: true
```

但 trial 目录下没有 `agent/requests.jsonl` 的问题。以下命令均在仓库根目录执行。

## 1. 先确认当前行为

`record_full_io` 不是由 agent 直接写入 trial 目录，而是分两步完成：

1. local proxy 在运行期间写入运行级原始日志：
   `scripts/logs/proxy/<instance_id>.jsonl`。
2. `scripts/run.sh` 在整个评测和 judge 正常结束后调用 `split_proxy_log`，再按
   `trial_id` 写入 `<trial>/agent/requests.jsonl`。

因此以下情况暂时没有 `agent/requests.jsonl` 是正常的：

- `wbb-runner` 处于默认 `handoff` 模式，刚通过启动就绪检查，实验仍在运行；
- 正在执行实际的 `in-place resume`，但尚未完成；
- 只执行了 `--dry-run`。dry-run 不启动代理、不调用模型，也不拆分日志。

只有在正式运行命令已经结束后，才能用 `agent/requests.jsonl` 是否存在判断功能是否完整生效。

## 2. 锁定本次正式运行

从 `wbb-runner` 的 handoff 信息或 `wbb-runner-state.json` 找到本次正式运行的 operator log，
然后查看其中所有 manifest 路径：

```bash
OPERATOR_LOG=/absolute/path/to/results/wbb-run-....log
rg -n '^Instance ID:|^Manifest:|^Job-private proxy started|^Model: .*local_proxy|^finished shard=|^In-place resume complete|split-proxy-log|ERROR:' "$OPERATOR_LOG"
```

`wbb-runner` 会先执行 dry-run，再启动正式命令，因此日志或 handoff 信息中可能出现多个 manifest。
应选择正式运行命令打印的 manifest，而不是前置 dry-run 的 manifest：

```bash
MANIFEST=/absolute/path/to/scripts/logs/instances/<instance-dir>/manifest.json

python3 - "$MANIFEST" <<'PY'
import json
import sys

m = json.load(open(sys.argv[1]))
for key in ("instance_id", "job_slug", "model_connection", "record_full_io", "model_route"):
    print(f"{key}={m.get(key)!r}")
print(f"proxy_url={(m.get('connection') or {}).get('proxy_url')!r}")
PY
```

正确的正式运行 manifest 应满足：

- `model_connection='local_proxy'`
- `record_full_io=True`
- `instance_id` 非空
- `model_route` 通常为 `<instance_id>__<model-slug>`，即 instance-specific route
- `proxy_url` 非空

如果 manifest 中 `record_full_io=False`，说明正式运行解析的 Job 不是预期配置。检查实际 Job slug、
分支、commit、工作区改动和正式命令，不要只检查另一份同名 YAML。

## 3. 判断运行是否已经到达拆分步骤

查看 operator log 的结尾：

```bash
tail -n 100 "$OPERATOR_LOG"
rg -n 'split-proxy-log|In-place resume complete|finished shard=.*exit_code|extra-attempt budget|Harbor in-place resume failed|ERROR:' "$OPERATOR_LOG"
```

按输出判断：

| 输出或状态 | 含义 |
|---|---|
| runner/Harbor 仍在运行 | 尚未执行拆分，暂时没有 `agent/requests.jsonl` 属于预期行为 |
| `In-place resume complete`，随后出现 `split-proxy-log` | 已到达拆分阶段，继续按其消息检查路径或归属问题 |
| shard `exit_code` 非 0 | `set -e` 使 `run.sh` 提前退出，拆分不会执行 |
| resume 预算不足或 Harbor resume 失败 | resume 返回非 0，拆分不会执行 |
| 日志中完全没有 `split-proxy-log` | 正式命令仍未结束，或在拆分前退出 |
| `no proxy log at ...` | 代理没有写日志，或拆分器查错了日志目录 |
| `no trial dirs under ...` | 拆分器查错了结果目录 |

当前 `scripts/run.sh` 只在主流程末尾调用拆分器，拆分不在 EXIT/cleanup trap 中。因此运行失败、
被终止、shard 非零退出或 resume 预算耗尽时，即使代理已经留下原始 JSONL，也不会生成
`agent/requests.jsonl`。

## 4. 检查运行级原始日志

先从 manifest 读取准确的 instance id，再查找文件：

```bash
INSTANCE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["instance_id"])' "$MANIFEST")"

find scripts/logs/proxy -maxdepth 1 -type f \
  \( -name "$INSTANCE_ID.jsonl" -o -name "proxy-$INSTANCE_ID.log" \) \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %s bytes %p\n'
```

再检查代理启动日志：

```bash
rg -n 'Proxy starting|Pipeline ready|Logging to|Failed to write log' \
  "scripts/logs/proxy/proxy-$INSTANCE_ID.log"
```

判断方式：

- 存在 `<instance_id>.jsonl`：`record_full_io` 已经让代理记录成功；问题在拆分未执行、
  结果目录错误或 trial 无法归属。
- 代理日志显示 `log_dir=(disabled)`：代理启动时 logging 被关闭。
- 显示有效 `log_dir`，但完全没有原始 JSONL：检查是否真的有请求经过该代理、请求流是否正常结束，
  以及日志目录权限。
- 原始 JSONL 只会在一次请求/流产生完整响应后写入；仅完成代理健康检查不会创建它。

如果服务器设置了自定义 `PROXY_LOG_DIR`，同时检查该目录：

```bash
env | rg '^(SHARED_PROXY|PROXY_LOG_DIR)='
rg -n '^(SHARED_PROXY|PROXY_LOG_DIR)=' .env 2>/dev/null
find /actual/proxy/log/dir -maxdepth 1 -name "$INSTANCE_ID.jsonl" -ls
```

## 5. 检查 `SHARED_PROXY=1`

这是当前代码中一个确定的 `record_full_io` 失效路径。

```bash
env | rg '^SHARED_PROXY='
rg -n '^SHARED_PROXY=' .env 2>/dev/null
rg -n 'log_enabled:|shared:' scripts/logs/proxy/shared-proxy.yaml 2>/dev/null
rg -n 'Proxy starting|Pipeline ready|Logging to' scripts/logs/proxy/shared-proxy.log 2>/dev/null
```

当前共享代理的初始化配置硬编码 `log_enabled: false`。Job 的 `record_full_io: true` 虽然会生成
带 instance id 的 route，但合并共享配置时只更新 route，保留共享代理已有的顶层
`log_enabled`。`POST /admin/reload` 也只重载 route，不会创建启动时被省略的日志拦截器。

因此，如果看到下面任一证据，就能确认是共享代理缺陷：

```text
log_enabled: false
Proxy starting: ... log_dir=(disabled)
```

临时排障时最简单的验证方法，是在新实验中不设置 `SHARED_PROXY=1`，让 `run.sh` 启动作业私有代理。
不要只修改 `shared-proxy.yaml` 后调用 `/admin/reload`；`log_enabled` 是启动期配置，修改后必须重启
代理，而且简单全局开启会记录所有包含 `log` interceptor 的共享 route，可能产生大量日志。

## 6. 检查自定义目录

当前拆分器默认写死查找：

```text
原始日志：<repo>/scripts/logs/proxy/<instance_id>.jsonl
结果目录：<repo>/results/<job_slug>/...
```

但运行端支持 `PROXY_LOG_DIR`、Job `jobs_dir` 和 `jobs_dir_suffix`。如果使用了这些设置，代理和
Harbor 会写入自定义位置，而 `run.sh` 调用拆分器时没有把实际位置传进去。

检查 Job：

```bash
JOB_SLUG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["job_slug"])' "$MANIFEST")"
rg -n '^(jobs_dir|jobs_dir_suffix):' "configs/jobs/$JOB_SLUG.yaml"
rg -n '^jobs_dir:' ".workspace/data/generated/jobs/$JOB_SLUG.yaml" 2>/dev/null
```

如果 operator log 显示：

```text
[split-proxy-log] no proxy log at <repo>/scripts/logs/proxy/...
```

但原始文件实际位于自定义 `PROXY_LOG_DIR`，就是日志目录没有传递。

如果显示：

```text
[split-proxy-log] no trial dirs under <repo>/results/<job_slug>
```

但 Harbor trial 位于自定义 `jobs_dir`，就是结果目录推导错误。

## 7. 结论判定顺序

建议按下面顺序判断：

1. 正式 runner 是否仍在运行；如果是，等待整个 `scripts/run.sh` 结束后再检查 trial 文件。
2. 正式 manifest 是否为 `local_proxy + record_full_io=True`。
3. 是否启用了 `SHARED_PROXY=1`；若启用并显示 logging disabled，问题已经定位。
4. 默认或自定义 proxy log 目录是否存在 `<instance_id>.jsonl`。
5. operator log 是否执行过 `split-proxy-log`，以及主命令是否非零退出。
6. 实际 Harbor `jobs_dir` 是否等于拆分器假设的 `results/<job_slug>`。

## 8. 已确认的代码问题与建议修复

当前至少有三个需要修复的点：

1. 把日志拆分纳入安全的退出清理路径，使正常结束、非零退出和中断后都能处理已经完整写入的记录。
2. 将实际 `PROXY_LOG_DIR` 和 Harbor runtime `jobs_dir` 显式传给拆分器，不再写死默认路径。
3. 重新设计共享代理的 logging 开关：共享代理启动时注册 logger，再按 route 决定是否包含
   `log` interceptor；或者对 `record_full_io: true + SHARED_PROXY=1` 明确 fail-fast。

当前仓库没有覆盖上述共享代理、失败退出和自定义路径组合的 `record_full_io` 回归测试。

相关代码位置：

- Job 配置解析和 instance-specific route：`src/workbuddy_bench/runner/resolve_manifest.py:967-1024`
- 代理 `log_enabled` 和 route `instance_id`：`src/workbuddy_bench/runner/proxy_config.py:145-220`
- 共享代理默认关闭 logging：`scripts/proxy/proxy-shared.sh:74-86`
- 共享配置只合并 routes：`src/workbuddy_bench/runner/proxy_config.py:423-480`
- 热重载不更新 logging：`src/workbuddy_bench/proxy/main.py:157-193`
- 主流程结束后才拆分：`scripts/run.sh:760-837`
- 拆分器的默认路径假设：`src/workbuddy_bench/runner/split_proxy_log.py:65-96`

## 9. 手动补拆分（会修改日志产物）

只有在确认使用默认 proxy log 路径和默认 `results/<job_slug>` 结果路径后，才可执行：

```bash
uv run python -m workbuddy_bench.runner.split_proxy_log --manifest "$MANIFEST"
```

该命令会向 trial 的 `agent/requests.jsonl` 追加记录；如果所有记录都成功归属，还会删除运行级
`<instance_id>.jsonl`。重复执行可能追加重复内容，因此执行前应备份原始 JSONL，并先确认目标
trial 下不存在已经拆分过的相同记录。自定义 `jobs_dir` 当前无法通过该命令行接口正确指定，不能直接
用这条命令处理。
