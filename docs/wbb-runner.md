# wbb-runner 简要使用说明

`wbb-runner` 用于使用预构建 task 镜像启动 WorkBuddy Bench 实验，并在需要时通过
`--resume-in-place` 补跑 API、网络、Docker、环境或结果不完整导致的无效 trial。

下面以 `$wbb-runner ...` 表示发给 Codex 的请求，不是 Shell 命令。

## 必填信息

启动前应提供：

- job slug；
- 明确的预构建镜像 tag，例如 `2026-08-31`；
- 运行模式：`handoff` 或 `managed`；
- 补跑总预算，即最多允许新增多少个 trial；
- 如果是恢复旧实验，还要提供准确的实验目录。

每个 task 必须配置为 3 次 attempt。未指定模式时默认使用 `handoff`。

## 两种模式

| 模式 | 行为 |
|---|---|
| `handoff` | 确认实验正常启动后立即返回；不定期检查，只有用户询问时才检查。 |
| `managed` | 持续等待实验结束，分析无效 trial，并在预算内执行原地补跑。 |

模式可以在实验运行过程中切换。切换到 `handoff` 只停止 Agent 监控，不会停止 Harbor、
proxy、容器或实验进程。

## 常用请求

以默认 handoff 模式启动：

```text
$wbb-runner 使用 handoff 模式运行 job <slug>，预构建镜像 tag 为 <tag>，补跑总预算为 30。
```

托管到完成：

```text
$wbb-runner 使用 managed 模式运行 job <slug>，预构建镜像 tag 为 <tag>，补跑总预算为 30。
```

从一个已有实验继续：

```text
$wbb-runner 使用 managed 模式继续实验 results/<job>/<experiment>，job 为 <slug>，镜像 tag 为 <tag>，补跑总预算为 12。
```

单次查看状态：

```text
$wbb-runner 检查 <instance-id>
```

`检查`只读取一次进程、日志和 Harbor 结果，不会启动补跑，也不会自动安排下一次检查。

继续处理已经结束的阶段：

```text
$wbb-runner 继续 <instance-id>
```

如果初始实验已经结束，`继续`会分析结果，并在已有授权和预算范围内启动需要的
in-place repair。

切换模式：

```text
$wbb-runner 将 <instance-id> 切换为 managed 模式
$wbb-runner 将 <instance-id> 切换为 handoff 模式，实验继续运行
```

## 什么算“正常启动”

Agent 会确认：

- dry-run、预构建镜像、模型、harness 和 proxy 检查通过；
- 运行进程仍然存活，没有立即报错退出；
- 已记录 instance ID、manifest 和 operator log；
- sharded 模式下所有 shard 已输出 PID 和日志；或者非 sharded 模式已经创建真实 Harbor
  实验目录、`config.json`、`lock.json` 和初始 trial。

不要求任意 task 已经成功完成；那属于进度监控，不属于启动确认。

## 状态与补跑

每个 workflow 会在 manifest 同目录保存 `wbb-runner-state.json`，记录当前模式、最后观察到的
阶段、日志、实验目录和补跑预算。该文件不是实时状态；每次检查仍会重新验证进程和结果文件。

原地补跑遵循以下规则：

- 只有 checksum 匹配、reward 非空且没有可重试崩溃异常的 trial 才有效；
- reward 为 `0` 也可以是有效结果，不会为了提高分数而重跑；
- 补跑前先执行 dry-run，并受总 attempt budget 限制；
- 预构建镜像 tag、task 内容和实验身份在补跑期间不能改变；
- `--resume-job` 是跨实验复用机制，不代替 `--resume-in-place` 修复。

完成条件是每个实验最终报告 `attempts_needed=0` 且 `valid=planned`，从而保证每个 task
都有 3 个有效 attempt。

详细行为见 [`wbb-runner` skill](../.agents/skills/wbb-runner/SKILL.md) 和
[`resume-in-place` 设计说明](resume-in-place.md)。
