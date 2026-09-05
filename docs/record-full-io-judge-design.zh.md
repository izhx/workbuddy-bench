# record_full_io 的 LLM judge 日志归档设计

状态：judge 请求归档待实现；本文已按当前代码结构更新。日期：2026-09-05。

## 目标与范围

沿用现有“按运行记录、结束后按 trial 拆分”的流程，补齐 LLM judge 请求的运行归属、trial 归属和独立归档。

适用配置为 `model_connection: local_proxy`、`record_full_io: true`，覆盖 `scripts/run.sh` 自动执行的两种 judge 模式：

- `in_container`：由数据集 verifier 执行的 judge。
- `host_side`：评测结束后自动执行的 post-judge。

最终输出约定：

```text
results/<job>/<experiment>/<trial>/
├── agent/requests.jsonl       # agent 请求和响应
└── verifier/requests.jsonl    # judge 请求和响应
```

上面是默认目录示例；实际归档根目录继续按现有显式 job 目录、runtime config 和默认路径的优先级解析。文件在有对应记录且收尾拆分成功后生成。

## 已有基础

- `proxy_log_filename(instance_id)` 位于 [`proxy/interceptors/logger.py`](../src/workbuddy_bench/proxy/interceptors/logger.py)，splitter 直接复用；旧文件名查找和源文件锁位于 [`runner/split_proxy_log.py`](../src/workbuddy_bench/runner/split_proxy_log.py)。
- splitter 已将目标选择集中到 `_record_destination()`，按目标文件路径暂存记录，并通过 `_commit_trial_records()`、`_atomic_output()` 共用去重和原子写入逻辑。目前目标仍只有 `agent/requests.jsonl`。
- `_trial_roots()` 已覆盖实验目录及其对应的 `.attempt-history/<round>/<trial>`，后续 judge 归档可复用；归属不明或重名且无法唯一定位的记录继续保留在源文件中。

## 当前缺口

1. judge 路由启用了日志拦截器，但没有 `instance_id`，请求进入公共的 `scripts/logs/proxy/proxy_requests.jsonl`。该目录可由 `PROXY_LOG_DIR` 覆盖。
2. 两种 judge 调用默认都没有在代理 token 中携带 trial 标识。
3. splitter 只查找当前运行的实例日志及旧版文件名，输出固定为 `<trial>/agent/requests.jsonl`。

因此，judge 请求虽然被记录，但没有进入当前运行的日志拆分与结果归档流程。

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

## 代码改动

### 1. judge 路由补运行标识

修改 [`runner/proxy_config.py`](../src/workbuddy_bench/runner/proxy_config.py) 的 `_judge_route()`：开启 `record_full_io` 时，为本次运行的 judge 路由设置与 agent 相同的 `instance_id`。

现有 [`proxy/interceptors/logger.py`](../src/workbuddy_bench/proxy/interceptors/logger.py) 已支持上述字段和文件选择，可以复用。独立注册到共享代理的 judge 路由需要保持其单独的生命周期边界，不能无条件注入某个运行的标识。

### 2. verifier 侧 judge 补 trial 标识

修改 [`judge/runtime/harbor.py`](../src/workbuddy_bench/judge/runtime/harbor.py) 的 `merged_verifier_env()`：

- 从 `verifier.trial_paths.trial_dir.name` 获取当前 trial 标识。
- 对已确认的代理 judge 路由，将 `WORKBUDDY_VERIFIER_LLM_API_KEY` 组装为 `<trial_id>::<judge_route>`。
- 重复合并环境变量时保证前缀只出现一次，并使用当前 trial 标识。
- 只处理代理 token，不能改写直连模型的真实 API key；代理识别必要时由 [`runner/judge_routing.py`](../src/workbuddy_bench/runner/judge_routing.py) 显式传递标记。

Office、Web 的 judge 都消费这套环境变量，优先在统一入口完成注入。应覆盖恢复运行时从旧配置重建 verifier 环境变量的路径。

### 3. host-side judge 补 trial 标识

修改 [`scorer/llm_judge.py`](../src/workbuddy_bench/scorer/llm_judge.py)：

- `judge_task()` 将当前实际 trial 名传给 `call_llm()`。
- `call_llm()` 在 `via_proxy` 模式下使用 `<trial_id>::<judge_route>` 作为请求 token。
- token 在每次请求中构造，避免修改并发任务共用的 `JudgeBackend`。
- 请求重试、解析重试和矛盾结果重试均保留相同 trial 标识。

### 4. splitter 按请求用途分流

修改 [`runner/split_proxy_log.py`](../src/workbuddy_bench/runner/split_proxy_log.py)：

- 在 `split_proxy_log()` 中从 manifest 读取 agent 路由和 judge 路由，将用途识别所需信息传给 `_record_destination()`。
- 扩展 `_record_destination()`：agent 请求返回 `<trial>/agent/requests.jsonl`，judge 请求返回 `<trial>/verifier/requests.jsonl`，无法识别用途时返回 `None`。
- `_split_locked()` 已按目标路径分组，无需改成 `(trial_id, 用途)`，也无需为 judge 新建一套暂存、锁或提交逻辑。
- 两类目标文件继续复用 `_commit_trial_records()`、`_atomic_output()` 的去重、原子写入、权限保留和失败后重跑机制。
- 继续先校验运行归属，再匹配 trial 和用途；无法确认归属或用途的记录保留在源文件中。
- 继续支持现有旧版实例日志的兼容识别，避免把无法识别用途的记录默认当作 agent 请求。

### 5. 收尾流程与文档

[`scripts/lib/run_cleanup.sh`](../scripts/lib/run_cleanup.sh) 已按“停止评测进程组、停止私有代理、执行 splitter”的顺序收尾。无法确认进程组或代理已停止时保留源日志；自动 host-side judge 在代理关闭前运行，继续复用该入口。

录制配置检查已并入 [`runner/resolve_manifest.py`](../src/workbuddy_bench/runner/resolve_manifest.py)；`run.sh` 在 `SHARED_PROXY=1` 时传入 `--shared-proxy`，在数据集 staging 前拒绝不支持的录制组合，无需另设 preflight 模块。普通及分片运行的 runtime YAML 位于本次 `INSTANCE_STATE_DIR/jobs/`，原地恢复则显式传入实验目录；judge 归档应沿用这些路径，不重新读取公共 generated 配置。

更新 [`docs/usage.zh.md`](usage.zh.md)，说明两个输出路径、生成时机，以及未能归档的记录保留位置。

## 测试与验收

在 `tests/test_split_proxy_log.py`、`tests/test_split_proxy_log_robustness.py`、`tests/test_runtime_proxy.py` 和 `tests/test_run_cleanup.py` 中补充覆盖；路由构造和 host-side 请求测试可集中新增到 `tests/test_judge_io_recording.py`。

需要验证：

1. 两种 judge 模式的请求都带正确的运行和 trial 标识，能够完成记录与拆分。
2. 同一运行包含多个 trial，且 agent、judge 请求交错时，归档内容不串 trial、不混用途。
3. 同一 trial 的 agent 和 judge 日志可以同时生成；仅有 judge 请求时也能正常归档。
4. 请求重试不丢记录；重复执行 splitter 不重复写入已归档记录。
5. 某个目标文件提交失败后，源记录仍可用于恢复；重跑不会重复写入此前成功提交的目标。
6. 不同运行使用同一个 judge 模型时，日志按 `instance_id` 隔离。
7. 原地恢复使用当前 trial 标识，环境变量重复合并不重复添加 token 前缀。
8. 关闭录制、禁用 judge、直连请求的原有行为得到保留；未知归属或用途的记录留在源文件。
9. 收尾拆分发生在私有代理退出后，包含自动 host-side judge 的最后一批请求。
10. 原地恢复多轮归档后，带有运行及 trial 标识的 agent、judge 记录仍能进入对应 attempt-history trial 的各自文件，不扫描其他实验的归档目录。

## 兼容边界

### 旧的公共日志

已有 `proxy_requests.jsonl` 中的 judge 记录通常缺少 `instance_id` 和 `trial_id`，不能可靠恢复到具体运行和 trial。保留原文件，不依据时间、模型名或 task 名猜测归属，也不直接把公共文件加入会替换或删除源文件的自动拆分流程。

本方案补齐后续运行的完整归档；历史公共日志的恢复需要另行确认是否存在足够的归属证据。

### 独立的 run-judge.sh

[`scripts/judge/run-judge.sh`](../scripts/judge/run-judge.sh) 使用共享代理，并可对多个已有结果目录执行 judge。这与 `scripts/run.sh` 的自动 host-side judge 是两个入口。

若该入口也要求自动归档，还需要单独设计本次 judge 调用的运行标识、各结果目录的归属映射，以及共享代理仍在写入时的安全日志收尾。上述四处核心改动不能视为已经覆盖这个入口。
