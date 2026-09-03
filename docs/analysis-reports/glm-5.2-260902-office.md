# Office 评测运行错误分析

> 状态：单次运行的分析记录，不是生效的配置方案
> 分析日期：2026-08-28，补跑与原地恢复结果更新至 2026-09-01
> 数据来源：`results/glm-5.2.cc.office/2026-08-28__11-27-07`
> （该目录已被原地恢复改写；改动前的原始基线留底于同名 `.pre-inplace` 目录）
> 适用范围：glm-5.2 + claude-code/2.1.187 在 `datasets/wb-bench-office-v1.0/tasks` 上的 50 任务 × 3 trial 运行

一句话结论：本次运行的 15 个失败全部由基础设施与运行时上限造成，不反映模型能力。基线上报
0.7036，两轮原地恢复补齐全部无效 trial 后为 **0.7749**，官方分数 0.7947，仍差 2.0 个百分点。
主要缺口是失控 thinking 撞上 claude code 对非 Claude 模型的 32k 输出默认上限——该上限可通过
`max_output_tokens` 配置，但本次运行及其补跑均未生效。

## 运行概况

| 项 | 值 |
|---|---|
| 任务数 / trial 数 | 50 / 150 |
| 运行时长 | 3h 44m |
| task 镜像 | 全部复用 tag `2026-08-26`，构建 0 次 |
| overall / reward 均分 | 0.7279（n=145） |
| test_pass_rate | 0.7370 |
| llm_judge_component | 0.7138（n=138） |
| errored trial | 15 |
| job 级重试次数 | 0 |
| 成本 | $848.56（输入 156.3M / 输出 2.43M token） |

`n_trials=145` 与 `n_errored_trials=15` 不构成矛盾：145 是进入算分的 trial 数，15 个错误单独计数，两者存在交集（见下文"算分口径"）。

## 15 个 errored trial 分类

按根因分四类。其中 B、C 两类属于运行环境故障，与模型能力无关。

| 类别 | 异常类型 | 数量 | 根因 |
|---|---|---:|---|
| A1 | `UnknownApiError` | 7 | 单次回复超出 32000 输出 token 上限 |
| A2 | `UnknownApiError` | 3 | 网关返回裸 400 |
| B | `RuntimeError` × 2、`OSError` × 1 | 3 | 磁盘写满 |
| C | `NonZeroAgentExitCodeError` | 2 | 容器内用户创建 / 软链失败 |

### A1 输出 token 超限（7 个）

报错文本：

```text
API Error: Claude's response exceeded the 32000 output token maximum.
```

harness 上报的模型参数为 `maxOutputTokens=32000`、`contextWindow=200000`。单次回复撞上输出上限后 cc 进程以 exit 1 结束。

| trial | num_turns | 结束时 input_tokens |
|---|---:|---:|
| `device-incident-attribution-L5-0__sUexfPa` | 13 | 171,118 |
| `device-incident-attribution-L5-0__tdK2Vwk` | 13 | 169,210 |
| `execution-closeout-reconcile-L4__5qwycfj` | 11 | 229,917 |
| `execution-closeout-reconcile-L4__MBij8Kr` | 9 | 219,600 |
| `execution-closeout-reconcile-L4__wectVQU` | 8 | 215,641 |
| `trade-pnl-positions-L4-007__3P9jekL` | 9 | 134,662 |
| `trade-pnl-positions-L4-007__Td6RsMF` | 10 | 125,392 |

`execution-closeout-reconcile-L4` 的 3 个 trial 全部命中该类错误，该任务没有产生任何有效分。

**触发机制：失控的 thinking，不是外部输入过大。** 逐步看 `device-incident-attribution-L5-0__sUexfPa`
的 20 步轨迹，前 15 步是正常的探索与读文件，`reasoning_content` 只有几百字符；从第 16 步开始
连续四步各产出约 10 万字符的思考，第 20 步即报输出超限：

| step | reasoning_content 字符数 | 开头 |
|---:|---:|---|
| 16 | 102,948 | Let me carefully analyze this. I have a complex evidence package… |
| 17 | 110,066 | Let me organize my understanding of all the data so far… |
| 18 | 104,416 | Let me continue the analysis. I have all the input data now… |
| 19 | 108,304 | Let me organize my analysis. I have all the data now… |
| 20 | — | `API Error: … exceeded the 32000 output token maximum` |

`trade-pnl-positions-L4-007__3P9jekL` 形态完全一致（第 14–17 步各 84k–93k 字符）。从开头措辞
（"organize my understanding of all the data **so far**"、"pick up **mid-thought**"）可以看出模型
在反复重启同一段推理，每轮都把上下文里的表格数据重新捋一遍，10 万字符里大部分是重复内容。
thinking 计入输出配额，所以单步就能撞满 32k；`total_completion_tokens` 只有 934，因为 thinking
不计入该统计项，看这个字段会完全错过问题。13.4MB 的 `cc-output.txt` 也是这么来的。

排除过的假设：视觉输入。`wb-bench-office-v1.0` 数据集中图片文件数为 0，10 个失败 trial 的
trajectory 里 `"type": "image"`、`media_type`、`base64` 出现次数均为 0。Office 全部是
xlsx/csv/docx 表格任务，agent 用 pandas/openpyxl 读取，不存在图片进上下文的路径，禁用图片
读取对该子集是空操作。

**上限本身可配置，但本次运行未生效。** `cc_agent.py:270` 会把 `model_params.max_output_tokens`
翻成容器内的 `CLAUDE_CODE_MAX_OUTPUT_TOKENS`（`resolve_manifest.py:486` 同路径）。失败 trial 的
`config.json` 里 `agent.env` 只有 6 个键，不含该变量，`kwargs.model_params` 也只有
`{thinking_enabled, extra_body.reasoning_effort}`——即运行时用的是 claude code 对非 Claude 模型的
32k 默认值。`configs/models/glm-5.2.yaml` 后来在提交 `122d6aa`（Fix claude code 32k limit for
non-claude models）中把 `max_output_tokens` 设为 131072 并附注释「在 claude code 实验会遇到默认
32k 限制」，但那是本次运行之后的事。

### A2 网关裸 400（3 个）

报错文本只有一行，网关未返回细节：

```text
API Error: 400 Backend returned 400
```

| trial | num_turns | 结束时 input_tokens |
|---|---:|---:|
| `effective-control-state-L5-036__FDepo52` | 21 | 127,724 |
| `json-screener-summary-L4-004__BPKZ72i` | 11 | 52,025 |
| `portfolio-valuation-limits-L4-00__PBDnpsn` | 11 | 102,409 |

三者失败时的 input_tokens 距 200000 上下文上限都有较大余量，因此不是上下文溢出。需要查网关侧日志才能进一步定位。

### B 磁盘写满（3 个）

运行末段宿主 root overlay 文件系统占用达到 100%（剩余 15M）。

- `procurement-reconcile-L3-015__UVFfxzT`、`subscription-credit-reconcile-L4__NdGLx7R`：`docker compose up` 创建容器时写 `.tmp-config.v2.json` 失败，`Error response from daemon: ... no space left on device`，被记为 `RuntimeError`。
- `stock-fund-return-compare-L3-010__kkQrnZT`：`[Errno 28] No space left on device: '/tmp/tmp8782vbeb'`，被记为 `OSError`。

运行结束时 Docker 占用为镜像 231GB、构建缓存 39GB、容器 30GB。补跑前必须先清理空间。

### C 容器内用户创建 / 软链失败（2 个）

- `cloudagent-sdk-doc-validation-re__n2JbCFo`：`useradd: cannot open /etc/passwd`（exit 1）。
- `crypto-backtest-chain-L4-002__tyjRgv5`：claude 可执行文件软链步骤 exit 228。

这两个高度疑似 B 类的次生现象——`/etc/passwd` 打不开通常也是写入失败所致，但错误文本未直接给出 `no space left`，因此单独归类。

## reward = 0 的模式

先说结论：**没有 API 超时反复重试的现象**。

- job 级 `n_retries = 0`。
- 整个运行日志中 429 / rate-limit / timeout 字样仅出现 1 处。
- 单个 trial 的 `agent/cc-output.txt` 体积达 14MB，但内容是 6.8 万条 `"subtype":"thinking_tokens"` 流式事件（`reasoning_effort=high` 所致），不是重试刷出来的。以 `device-incident-attribution-L5-0__sUexfPa` 为例：thinking 事件 69,147 条，`"is_error":true` 仅 1 条。

真正的模式在算分口径上。11 个 reward=0 的 trial 中，只有 2 个是模型真实失败：

| trial | 情况 |
|---|---|
| `news-event-multifile-extract-L4__g8Swcbc` | 43 turns 正常收尾，无 API 错误，verifier 判 0 |
| `priority-sync-notification-pipel__ShgKaEb` | 58 turns 正常收尾，`is_error` 计数为 0，verifier 判 0 |

其余 9 个是 A 类 API 崩溃的 trial 被记为 0.0（另有 1 个记为 0.0067）。B、C 两类共 5 个 trial 的 reward 为 `None`，未进入均值。

### 算分口径的影响

A 类 trial 并未跑完，却以 0 分进入了均值分母：

| 口径 | 均分 | n |
|---|---:|---:|
| 含 API 崩溃零分（harness 上报值） | 0.7279 | 145 |
| 剔除 10 个 API 崩溃 trial | 0.7817 | 135 |

差 5.4 个百分点。解读该运行成绩时应同时给出两个口径。

## 错误从哪里报出

全部由 **Harbor 框架**（`harbor` 0.18.0）抛出，wbb 自身代码没有定义这些异常类。wbb 只提供 agent 适配层 `src/workbuddy_bench/agents/cc_agent.py`，它继承 `BaseInstalledAgent` 且未覆写任何错误分类逻辑。

链路分三层，路径均相对于 `.workspace/controller-venv/lib/python3.12/site-packages/`：

1. **抛出** — `harbor/agents/installed/base.py:444`，`BaseInstalledAgent._exec()`。命令包装成 `set -o pipefail; <cmd>` 在容器内执行，返回码非 0 即 `raise self._classify_exec_error(command, result)`。
2. **分类** — 同文件 `_classify_exec_error()`（:380）。纯正则匹配 stdout+stderr 拼接串，按 `ERROR_PATTERNS`（:226）声明顺序取首个命中项，全不命中则退回基类。
3. **捕获落盘** — `harbor/trial/trial.py:360` 的 `except Exception as exc` 兜住整个 trial，`_record_exception()`（:392）写成 `ExceptionInfo`，由 `_finalize()` 序列化进每个 trial 目录下的 `result.json`。

异常继承关系：

```text
RuntimeError
└── NonZeroAgentExitCodeError
    └── ApiError
        ├── ApiRateLimitError
        ├── ApiUsageLimitError
        ├── ApiInternalServerError
        ├── ApiOverloadedError
        ├── ApiConnectionClosedError
        └── UnknownApiError      ← 兜底
```

`ERROR_PATTERNS` 现有条目（按匹配优先级）：

| 正则 | 异常类型 |
|---|---|
| `rate.?limit` / `too many requests` | `ApiRateLimitError` |
| `specified API usage limits` / `Quota exceeded.` | `ApiUsageLimitError` |
| `API Error: 500 Internal server error` | `ApiInternalServerError` |
| `API Error: Overloaded` | `ApiOverloadedError` |
| `API Error: Connection closed mid-response` | `ApiConnectionClosedError` |
| `API Error` | `UnknownApiError` |
| `SSL_ERROR_SYSCALL` / `SSL_connect` / `Could not resolve host` / `Connection refused` / `Connection timed out` / `curl: \(\d+\)` | `NetworkConnectionError` |

## wbb 能捕捉到，但分类偏钝

15 个错误全部完整落盘，类型、消息、traceback 都在 trial 的 `result.json` 里，捕捉能力没有缺失。问题在分类粒度：

**一、A1 和 A2 全部由兜底规则命中。** 正则表中没有输出超限和裸 400 的条目，`API Error: Claude's response exceeded the 32000 output token maximum` 与 `API Error: 400 Backend returned 400` 都只能匹配到最后那条泛化的 `API Error`。于是同一个 `UnknownApiError` 底下混着两种性质完全不同的故障：一个该调 `max_output_tokens` 或约束回复长度，另一个是网关侧问题。仅凭异常类型无法区分，本次是靠翻 `agent/cc-output.txt` 才分开的。

**二、只做文本匹配，未利用结构化事件。** 基类注释明确写了 `Override for non-regex classification (e.g. structured event parsing)`，而 `cc_agent.py` 没有覆写。cc 的 stream-json 输出本身带 `"subtype"`、`"is_error"`、`"result"` 等结构化字段，目前未被使用。副作用是正则搜的是整个 stdout，而该输出动辄 14MB，任务正文中只要出现 "rate limit" 等字样就可能误判成 `ApiRateLimitError`。

**三、分类做了，但没有接重试。** `ApiRateLimitError` 的 docstring 说明该独立类型的用途是配合 `harbor run --max-retries 3 --retry-include ApiRateLimitError`。本次 job 的 `n_retries=0`，`configs/jobs/glm-5.2.cc.office.yaml` 中也没有任何 retry 配置。即 harbor 具备按异常类型重试的能力，wbb 当前未启用，因此本可重试的瞬时故障（A2 的 3 个裸 400、满盘期间的 B/C 类失败）都直接计为 trial 失败。

## 可选改进方向

按性价比排序，均未实施：

1. **确认 `max_output_tokens` 真正进入运行时。** 配置项与注入链路都已存在
   （`configs/models/glm-5.2.yaml` → `resolve_manifest.py:486` → 容器内
   `CLAUDE_CODE_MAX_OUTPUT_TOKENS`），提交 `122d6aa` 也已把值设为 131072，但本次运行与两轮
   补跑的 trial `config.json` 中都查不到该变量。新跑之前应先 dump 一个 trial 的 `agent.env`
   核对，这是投入产出比最高的一项——A1 独占 7/15 的失败。
2. **给 `CcAgent` 增加 `ERROR_PATTERNS` 覆盖**，把输出超限和 `API Error: \d{3}` 拆成独立类型。改动最小，不影响既有结果。
3. **覆写 `_classify_exec_error()`**，改用 stream-json 事件判定。更准确，且能消除大输出下的正则误判。
4. **为 job 增加 `max_retries` + `retry_include` 白名单。** 需注意不要与 `src/workbuddy_bench/proxy/sender.py:202` 已有的 429/502/503/504 重试叠成嵌套重试——`sender.py` 中关于固定间隔重试的注释正是在警告这一点。
5. **放宽输出上限后同步复核墙钟预算。** 上限一放开，压力会从 token 转到时间：本轮已经出现
   `AgentTimeoutError`（3600 秒跑满）。两个上限需要一起调，只调一个会把失败从一类换成另一类。


## 补跑结果（2026-08-28）

针对上述 15 个失败 trial 做了一轮补跑。磁盘瓶颈此时已消失（root 挂载变更为 66T，可用 62T），无需清理。

补跑通过三个 job 完成，按每个任务的失败次数分组——Harbor 的 `n_attempts` 是 job 级而非 trial 级，不分组就会把已成功的 trial 一并重跑：

| job | n_attempts | 任务数 | trial 数 | 结果目录 |
|---|---:|---:|---:|---|
| `glm-5.2.cc.office.retry1` | 1 | 8 | 8 | `2026-08-28__19-21-30` |
| `glm-5.2.cc.office.retry2` | 2 | 2 | 4 | `2026-08-28__20-35-42` |
| `glm-5.2.cc.office.retry3` | 3 | 1 | 3 | `2026-08-28__21-39-19` |

合计 15 个 trial，与目标数一致，未多跑也未漏跑。三个 job 均以 tag `2026-08-26` 复用预构建镜像（preflight 8/2/1 全部 ready，构建 0 次），且刻意不使用 `RESUME_JOB`（resume 是任务级，会把这些部分打分的任务判定为已完成而静默跳过）。

**补回 10 个有效分，5 个仍然失败：**

| 任务 | 补跑 reward | 原失败类型 |
|---|---:|---|
| `stock-fund-return-compare-L3-010` | 0.9438 | B 类 |
| `trade-pnl-positions-L4-007` | 0.8482 | A1 类（1/2 成功） |
| `subscription-credit-reconcile-L4-024` | 0.8139 | B 类 |
| `portfolio-valuation-limits-L4-009` | 0.8078 | A2 类 |
| `json-screener-summary-L4-004` | 0.7967 | A2 类 |
| `execution-closeout-reconcile-L4-003-successor` | 0.7754 | A1 类（1/3 成功） |
| `procurement-reconcile-L3-015` | 0.7376 | B 类 |
| `cloudagent-sdk-doc-validation-report` | 0.7200 | C 类 |
| `crypto-backtest-chain-L4-002` | 0.6755 | C 类 |
| `effective-control-state-L5-036` | 0.4990 | A2 类 |

> 取分口径：reward 必须从 `verifier/reward.json` 或 `verifier/score.json` 读取。
> `verifier/reward.txt` 存的是 `test_pass_rate` 而非 reward，两者常常不同（例如
> `cloudagent-sdk-doc-validation-report` 的 `reward.txt` 是 0.9，实际 reward 是 0.72），
> 且该文件在部分 trial 中缺失（基线 150 个 trial 里只有 131 个有 `reward.txt`，而
> `reward.json` 有 145 个）。

B、C 两类共 5 个环境故障 trial **全部补回**，印证了它们确实是磁盘写满的产物而非任务本身的问题。A2 类 3 个裸 400 也全部跑通，说明那是瞬时的网关故障。

仍然失败的 5 个 trial：

| trial | 异常类型 | 说明 |
|---|---|---|
| `device-incident-attribution-L5-037` ×2 | `UnknownApiError` | 均复现 32000 输出上限 |
| `trade-pnl-positions-L4-007` ×1 | `UnknownApiError` | 复现 32000 输出上限 |
| `execution-closeout-reconcile-L4-003-successor` ×1 | `UnknownApiError` | 复现 32000 输出上限 |
| `execution-closeout-reconcile-L4-003-successor` ×1 | `AgentTimeoutError` | 新的失败形态 |

A1 类（输出 token 上限）会复现，但**是概率性的、不是确定性的**。补跑未改任何配置（同一 tag
`2026-08-26` 的镜像、`maxOutputTokens=32000`、`reasoning_effort=high`），同样的任务重跑，
32k 失败从基线的 7 个降到 4 个：

| 任务 | 基线 | 补跑 |
|---|---|---|
| `device-incident-attribution-L5-037` | 2/2 撞 32k | 2/2 撞 32k |
| `trade-pnl-positions-L4-007` | 2/2 撞 32k | 1/2 撞 32k，1 个通过（0.8482） |
| `execution-closeout-reconcile-L4-003-successor` | 3/3 撞 32k | 1/3 撞 32k，1 个通过（0.7754），1 个超时 |

即模型这一次是否写出超过 32k 的单条回复带有随机性，多跑能捞回一部分分数。但各任务的
概率差异很大：`device-incident-attribution-L5-037` 两轮共 4 个 trial 全部撞墙，靠重试
捞不动；`execution-closeout-reconcile-L4-003-successor` 基线三个全废，补跑能出一个有效分。

因此重试只是缓解手段，根治仍需调整 `max_output_tokens` 或约束回复长度。另外需注意：靠反复
补跑刷上来的分数已不是单次评测口径，横向对比时应说明补跑轮次。

`AgentTimeoutError` 是本轮新出现的异常类型，不在原 15 个错误的分类中，共出现 2 次。其中 `json-screener-summary-L4-004__Q2K8oLW` 是一个值得注意的情形：同一个 trial 既记录了 `AgentTimeoutError`，又拿到了 0.7875 分——agent 侧超时，但 verifier 仍完成了打分。因此本轮 6 个异常对应的是 5 个无分 trial，异常数与无分 trial 数并不相等。

### 合并后的 Office 得分

把基线中 15 个 errored trial 替换为补跑结果（保留基线 135 个正常 trial + 15 个补跑 trial）后：

| 口径 | 分数 | n |
|---|---:|---:|
| 基线，harbor 上报（崩溃 trial 记 0） | 0.7036 | 150 |
| 基线，仅算有分 trial | 0.7279 | 145 |
| **合并后** | **0.7543** | **150** |

合并后比基线上报值高 5.1 个百分点。此时 150 个 trial 全部有分——仍然失败的 5 个 trial 在
`reward.json` 中记为明确的 0.0 而非缺失值，因此两种口径重合，不再存在分母歧义；50 个任务
每个都凑满 3 个 trial。

`execution-closeout-reconcile-L4-003-successor` 从基线的「零有效分」变为三个 trial
0.0 / 0.7754 / 0.0，任务均分 0.7754。

补跑中 LLM judge 工作正常（`llm_judge_component_score` 分布在 0.6–1.0），仅
`cloudagent-sdk-doc-validation-report` 一个任务为 0.0，属该任务的真实判分，不是基线里
那种 `judge_evidence` 权限问题导致的系统性缺失。

## 原地恢复补跑（2026-09-01）

上面那轮补跑是手工拆 job 做的。之后用 `--resume-in-place` 又在同一个实验目录上补了两轮，
两轮之间恰好跨越了判定逻辑的一次改动，对照结果说明了判定策略的影响。

### 两版判定策略的差别

旧版（`a154450`）只看 reward 是否为空：`verifier_result.rewards.reward` 非 null 即算有效。
按这个规则，基线 150 个 trial 里只有 5 个无分的会被补，10 个带 `UnknownApiError`
但 verifier 给了 0.0 分的 trial 一律算完成，永远补不到。实测结果正是
`valid=150/150 extra_attempts_used=5`，得分 0.7036 → 0.7298。

新版（主仓 `dev/exp-skills`，`in_place_resume.py` 903 行）改为按异常类型判定，新增
`RETRYABLE_AGENT_EXCEPTIONS` 与 `RETRYABLE_INFRA_EXCEPTIONS` 两张白名单。前者含
`UnknownApiError`、`NonZeroAgentExitCodeError`、`NetworkConnectionError` 等，后者含
`RuntimeError`、`OSError`、`CancelledError` 等。代码注释给出的理由是：这类 trial 从未产生
真实的 agent 尝试，harbor 写下的 reward 反映的是空的或被截断的工作区而非模型行为，保留会把
分数系统性拉低，因此即使有分也要归档重跑。

值得注意的是 `AgentTimeoutError`、`ContextLengthExceededError`、
`OutputLengthExceededError` **被刻意排除**在白名单外——agent 烧完自己的墙钟预算、模型输出
过长，都属于真实的评测结果，分数应当保留。

### 实测

在已被旧版补过一轮的目录（150 个 trial 全部有分，其中 10 个带 `UnknownApiError`）上跑新版：

```
In-place resume plan: planned=150 valid=140 invalid_to_archive=10 attempts_needed=10
  archive reasons: crashed:UnknownApiError=10
```

10 个全部被识别，涉及 6 个任务：`execution-closeout-reconcile-L4-003-successor` 3/3 全废，
`device-incident-attribution-L5-037` 与 `trade-pnl-positions-L4-007` 各 2 个，
`effective-control-state-L5-036`、`json-screener-summary-L4-004`、
`portfolio-valuation-limits-L4-009` 各 1 个。进度条从 `140/150` 起步，已有的 140 个未被重跑。
补跑出来的 trial 中有几个自身又崩溃，in-place 动用额外配额继续替换，最终
`valid=150/150 extra_attempts_used=10`，耗时约 1 小时。

| 口径 | 分数 | n | 带异常 trial |
|---|---:|---:|---:|
| 原始基线，harbor 上报 | 0.7036 | 150 | 15 |
| 原始基线，仅算有分 trial | 0.7279 | 145 | 15 |
| 旧版原地恢复（补 5 个无分槽位） | 0.7298 | 150 | 10 |
| **新版原地恢复（再补 10 个崩溃 trial）** | **0.7749** | **150** | **1** |
| 官方分数（参考） | 0.7947 | — | — |

0.7749 与 harbor 自报的 `reward = 0.774936` 一致。150 个 trial 全部有分，两种算分口径重合。
`n_errored_trials` 15 → 1。距官方 0.7947 尚差 2.0 个百分点。

剩下那 1 个是 `execution-closeout-reconcile-L4__NVvEgrJ`，`AgentTimeoutError`，
跑满 3600 秒墙钟上限，reward 0.0。按上述策略它不该被补，会稳定留在目录里，不是遗漏。
它单独就压低总分约 0.5 个百分点，是与官方分差距的一部分来源。

本轮累计 `cost_usd = 1055.77`，input 194.0M tokens / output 2.80M。

### 恢复出的分数是真实波动，不是配置变更带来的

需要澄清一个容易误判的因果：原地恢复**沿用原实验 lock 中固定的配置**，不会捡起仓库里的最新
配置。逐字段比对归档失败 trial 与其替身的 `config.json`，两者的 `agent.env` 键集合、
`kwargs.model_params`、`settings_preset`、`CLAUDE_CODE_MAX_TURNS` 完全一致，替身同样**没有**
`CLAUDE_CODE_MAX_OUTPUT_TOKENS`。也就是说 `122d6aa` 对 `max_output_tokens` 的修复在本轮补跑中
并未生效，两批 trial 跑在同一个 32k 默认上限下。

因此 A1 类失败的恢复确属同配置下的概率性差异，而非上限放宽的结果。补跑后 18 个相关 trial
中命中 32k 报错的为 0 个，且步数从失败时的 18–20 步涨到 43–171 步——模型这次没有陷入那种
单步十万字符的重复推理，而是走完了正常的多步流程。

| 任务 | 补跑后三个 trial 的 reward |
|---|---|
| `device-incident-attribution-L5-037` | 0.7389 / 0.3417 / 0.5145 |
| `effective-control-state-L5-036` | 0.4588 / 0.5840 / 0.5659 |
| `execution-closeout-reconcile-L4-003-successor` | 0.8107 / 0.7774 / 0.0000（超时） |
| `json-screener-summary-L4-004` | 0.8196 / 0.8889 / 0.7896 |
| `portfolio-valuation-limits-L4-009` | 0.8358 / 0.8498 / 0.8508 |
| `trade-pnl-positions-L4-007` | 0.8659 / 0.8909 / 0.9327 |

推论：若把 `max_output_tokens: 131072` 真正带进运行时，A1 这类失败应当进一步减少，但这需要
一次全新运行来验证——原地恢复做不到，它按设计冻结原配置。同时要注意放宽输出上限会把压力
转移到墙钟：`execution-closeout-reconcile-L4-003-successor` 这次已经出现 `AgentTimeoutError`，
模型能一直想下去时更容易撞 3600 秒而不是撞 token 墙。


### 一个由分类偏钝导致的偏差

前文「分类偏钝」一节指出 A1 的输出超限被兜底正则吞成了 `UnknownApiError`。这个缺陷在新版
判定下产生了实际后果：那 4 个 32k 超限的 trial 本应被识别为 `OutputLengthExceededError`
并按策略保留分数，却因为异常类型是 `UnknownApiError` 而被当作 API 崩溃归档重跑了。

也就是说，判定策略本身是对的，但它依赖的异常类型不准。要让策略按设计意图工作，需要先落实
「可选改进方向」中的第 2 或第 3 项，让输出超限有自己的异常类型。在那之前，
`--keep-exception` 无法用来豁免这批 trial——它们和真正的 API 崩溃共用同一个类型名。

不过这里有一层反转值得记下：策略把 `OutputLengthExceededError` 列为「真实评测结果、保留分数」，
前提是那个上限就是模型该有的上限。而本次的 32k 并非 glm-5.2 的真实能力边界，只是
`max_output_tokens` 未生效时 claude code 对非 Claude 模型的默认值。在这种配置缺陷下，把它们
当崩溃重跑反倒更接近本意。真正的修法是让配置生效，而不是去调判定策略——否则等配置修好之后，
豁免规则又会变成错的。


### 目录布局

原地恢复会就地改写实验目录，被替换的 trial 移入归档目录：

- `results/glm-5.2.cc.office/2026-08-28__11-27-07` — 现役，150/150 有效，0.7749
- `results/glm-5.2.cc.office/2026-08-28__11-27-07.pre-inplace` — 原始基线留底（575M），未被改动
- `results/glm-5.2.cc.office/2026-08-28__11-27-07.attempt-history` — 两轮归档
  （`…03-17-01…-round-1` 5 个、`…02-16-44…-round-1` 10 个），`attempt-history.jsonl` 共 15 行

原地恢复不创建新实验目录，也不支持从多个实验累计继承。若要保留改动前的状态，需自行复制或
改名留底——本次即先把原基线改名为 `.pre-inplace`。同一目录上的并发恢复由
`<experiment>.resume.lock` 配合 `flock` 排他，锁在进程退出时释放，遗留的空锁文件无影响。

## 补跑注意事项

- B、C 两类共 5 个 trial 属于环境故障，补跑前必须先清理磁盘空间。宿主机为共享环境，清理 Docker 资源不要使用 `prune`。
- 这类环境失败不要走 `RESUME_JOB` 路径；`--resume-in-place` 可以处理（`RuntimeError`、`OSError` 都在基础设施白名单里）。
- A1 的 7 个若不调整输出 token 上限或约束回复长度，补跑仍会复现。
- A2 的 3 个建议先查网关日志定位后再决定是否补跑。
- 靠反复补跑刷上来的分数不再是单次评测口径。横向对比时应说明补跑轮次与判定策略版本——
  0.7298 与 0.7749 出自同一个目录，差别仅在于判定 trial 有效性的规则。
