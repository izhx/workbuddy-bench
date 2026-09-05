# record_full_io 的 LLM judge 日志归档设计与实现

状态：已实现 `scripts/run.sh` 的 `in_container` judge 请求归档。日期：2026-09-05。
验证范围：本地回归测试覆盖真实代理处理、日志写入和拆分，上游 HTTP 使用模拟响应；未运行 Docker 数据集实测。

## 目标与范围

沿用现有“按运行记录、结束后按 trial 拆分”的流程，补齐 LLM judge 请求的运行归属、trial 归属和独立归档。

适用配置为 `model_connection: local_proxy`、`record_full_io: true`，judge 模式为 `in_container`，由数据集 verifier 执行。

`host_side` post judge 不纳入本次改动。

最终输出约定：

```text
results/<job>/<experiment>/<trial>/
├── agent/requests.jsonl       # agent 请求和响应
└── verifier/requests.jsonl    # judge 请求和响应
```

上面是默认目录示例；实际归档根目录继续按现有显式 job 目录、runtime config 和默认路径的优先级解析。文件在有对应记录且收尾拆分成功后生成。

## 已有基础

- `proxy_log_filename(instance_id)` 位于 [`proxy/interceptors/logger.py`](../src/workbuddy_bench/proxy/interceptors/logger.py)，splitter 直接复用；旧文件名查找和源文件锁位于 [`runner/split_proxy_log.py`](../src/workbuddy_bench/runner/split_proxy_log.py)。
- splitter 已将目标选择集中到 `_record_destination()`，按目标文件路径暂存记录，并通过 `_commit_trial_records()`、`_atomic_output()` 共用去重和原子写入逻辑。agent 和 judge 共用这套写入逻辑。
- `_trial_roots()` 已覆盖实验目录及其对应的 `.attempt-history/<round>/<trial>`，agent 和 judge 归档共用此逻辑；归属不明或重名且无法唯一定位的记录继续保留在源文件中。

## 修复前的问题

1. judge 路由启用了日志拦截器，但没有 `instance_id`，请求进入公共的 `scripts/logs/proxy/proxy_requests.jsonl`。该目录可由 `PROXY_LOG_DIR` 覆盖。
2. verifier 侧 judge 调用没有在代理 token 中携带 trial 标识。
3. splitter 只查找当前运行的实例日志及旧版文件名，输出固定为 `<trial>/agent/requests.jsonl`。

修复前，judge 请求虽然被记录，但没有进入当前运行的日志拆分与结果归档流程。

## 数据流与标识

`instance_id` 表示一次运行，`trial_id` 使用实际 trial 目录名，不能用 task 名替代，因为同一 task 可以有多个 attempt。

```text
judge 路由携带 instance_id
    + 请求 token 携带 <trial_id>::<judge_route>
    ↓
现有 proxy/logger 记录 instance_id、trial_id、route
    ↓
agent 与 judge 写入同一份运行日志
    ↓
停止代理后，按 trial_id 和请求用途拆分
    ↓
agent/requests.jsonl 或 verifier/requests.jsonl
```

运行日志继续使用 logger 中 `proxy_log_filename(instance_id)` 的现有哈希命名规则。agent 和 judge 的用途通过 manifest 中的各自路由识别。

## 实现

### 1. judge 路由补运行标识

[`runner/proxy_config.py`](../src/workbuddy_bench/runner/proxy_config.py) 的 `_judge_route()` 在开启 `record_full_io` 且 judge 模式为 `in_container` 时，为 judge 路由设置与 agent 相同的 `instance_id`。

[`proxy/interceptors/logger.py`](../src/workbuddy_bench/proxy/interceptors/logger.py) 直接复用现有字段和文件选择。独立注册到共享代理的 judge 路由不设置运行标识。录制时若 agent 与 judge 路由相同，配置生成直接报错；正常 manifest 会给 agent 路由加实例前缀，不受此检查影响。

### 2. verifier 侧 judge 补 trial 标识

[`judge/runtime/harbor.py`](../src/workbuddy_bench/judge/runtime/harbor.py) 的 `merged_verifier_env()`：

- 从 `verifier.trial_paths.trial_dir.name` 获取当前 trial 标识。
- 对已确认的代理 judge 路由，将 `WORKBUDDY_VERIFIER_LLM_API_KEY` 组装为 `<trial_id>::<judge_route>`。
- 重复合并环境变量时保证前缀只出现一次，并使用当前 trial 标识。
- [`runner/judge_routing.py`](../src/workbuddy_bench/runner/judge_routing.py) 显式设置 `WORKBUDDY_VERIFIER_LLM_PROXY_ROUTE`，仅当模型和 API key 的路由部分均匹配该标记时添加前缀，不改写直连模型的真实 API key。

Office、Web 的 judge 都消费这套环境变量，在统一入口完成注入。`run.sh` 也从本次 manifest 导出该标记，供没有保存标记的旧配置在恢复时使用；不修改已保存的 Harbor 配置。

### 3. splitter 按请求用途分流

[`runner/split_proxy_log.py`](../src/workbuddy_bench/runner/split_proxy_log.py)：

- 在 `split_proxy_log()` 中从 manifest 读取 agent 路由和已启用的 `in_container` judge 路由，将用途识别所需信息传给 `_record_destination()`。
- `_record_destination()` 对 agent 请求返回 `<trial>/agent/requests.jsonl`，对 judge 请求返回 `<trial>/verifier/requests.jsonl`；路由缺失、未知或在旧 manifest 中同时对应两种用途时返回 `None`。
- `_split_locked()` 已按目标路径分组，无需改成 `(trial_id, 用途)`，也无需为 judge 新建一套暂存、锁或提交逻辑。
- 两类目标文件继续复用 `_commit_trial_records()`、`_atomic_output()` 的去重、原子写入、权限保留和失败后重跑机制。
- 继续先校验运行归属，再匹配 trial 和用途；无法确认归属或用途的记录保留在源文件中。
- 继续支持现有旧版实例日志的兼容识别，避免把无法识别用途的记录默认当作 agent 请求。

### 4. 收尾流程与文档

[`scripts/lib/run_cleanup.sh`](../scripts/lib/run_cleanup.sh) 已按“停止评测进程组、停止私有代理、执行 splitter”的顺序收尾。无法确认进程组或代理已停止时保留源日志；verifier 侧 judge 共用现有收尾入口。

录制配置检查已并入 [`runner/resolve_manifest.py`](../src/workbuddy_bench/runner/resolve_manifest.py)；`run.sh` 在 `SHARED_PROXY=1` 时传入 `--shared-proxy`，在数据集 staging 前拒绝不支持的录制组合，无需另设 preflight 模块。普通及分片运行的 runtime YAML 位于本次 `INSTANCE_STATE_DIR/jobs/`，原地恢复则显式传入实验目录；judge 归档沿用这些路径，不重新读取公共 generated 配置。

[`docs/usage.zh.md`](usage.zh.md) 说明了两个输出路径、生成时机，以及未能归档的记录保留位置。

## 测试与验收

回归测试位于 `tests/test_judge_io_recording.py`、`tests/test_split_proxy_log_robustness.py`、`tests/test_runtime_proxy.py` 和 `tests/test_run_cleanup.py`；代理集成测试替换上游 HTTP transport，清理测试使用真实进程模拟最后一次写入。

覆盖以下行为：

1. `in_container` judge 请求带正确的运行和 trial 标识，能够完成记录与拆分。
2. 同一运行包含多个 trial，且 agent、judge 请求交错时，归档内容不串 trial、不混用途。
3. 同一 trial 的 agent 和 judge 日志可以同时生成；仅有 judge 请求时也能正常归档。
4. 重复执行 splitter 不重复写入已归档记录。
5. 某个目标文件提交失败后，源记录仍可用于恢复；重跑不会重复写入此前成功提交的目标。
6. 不同运行使用同一个 judge 模型时，日志按 `instance_id` 隔离。
7. 原地恢复使用当前 trial 标识，环境变量重复合并不重复添加 token 前缀。
8. 关闭录制、禁用 judge、直连请求的原有行为得到保留；未知归属或用途的记录留在源文件。
9. 收尾拆分发生在私有代理退出后，包含 verifier 侧 judge 的最后一批请求。
10. 原地恢复多轮归档后，带有运行及 trial 标识的 agent、judge 记录仍能进入对应 attempt-history trial 的各自文件，不扫描其他实验的归档目录。

## 兼容边界

### 旧的公共日志

已有 `proxy_requests.jsonl` 中的 judge 记录通常缺少 `instance_id` 和 `trial_id`，不能可靠恢复到具体运行和 trial。保留原文件，不依据时间、模型名或 task 名猜测归属，也不直接把公共文件加入会替换或删除源文件的自动拆分流程。

本方案补齐后续运行的完整归档；历史公共日志的恢复需要另行确认是否存在足够的归属证据。
