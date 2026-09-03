# GLM-5.2 WB-Bench-Code 深入分析：分数从哪里来，缺口在哪里

> 分析日期：2026-09-02
> 运行目录：`results/glm-5.2.cc.code/2026-08-28__10-53-56`
> 模型：`glm-5.2`（`reasoning_effort: high`，`CLAUDE_CODE_MAX_TURNS: 256`）
> Harness：`claude-code/2.1.187`（`CcAgent`）
> 数据集：`wb-bench-code-v1.0`，80 任务 × 3 attempts = 240 trial

## 0. 结论先行

本次 code 子集 `reward = 0.7029`、`pass_rate = 0.3542`。对"为什么不是更高"这个问题，三条候选原因的归因如下：

| 候选原因 | 是否成立 | 量级 | 依据 |
|---|---|---|---|
| **API / 环境因素** | **基本不成立** | 计分 trial 中 0 个 API 错误 | 240 个 trial 的 `exception_info` 全为 `None`；stream-json 终态全 `success`；13 个基础设施失败已在补跑中被替换 |
| **verifier 对函数名/字段名要求过严** | **成立，且可量化** | 受影响子集约 +0.22 | 隐藏测试 + 精确符号/字段/错误码匹配；无 smoke test 的 script_verifier 任务 0.7275 vs 有 smoke test 的 0.9505 |
| **模型能力** | **部分成立** | 真实仓库 bug 是主要短板 | 22 个 `pytest_injected`（真实 OSS 仓库 bug）仅 0.5467；"加代码"倾向、大补丁反而低分 |

一句话：**0.7029 这个数字里，"环境噪声"贡献几乎为零；真正压低分数的是两股力量——一是合成任务上 verifier 的隐藏契约（模型要靠猜精确的函数名/字段名/错误码），二是真实开源仓库 bug 本身的 SWE 难度。前者是评测设计问题，后者才是模型能力问题。**

如果你拿到的"官方/预期分数"比 0.7029 高，最先要排查的不是模型，而是下面第 1 节的三个测量口径陷阱——其中 `verifier_user` 一项就能把同一份实验从 0.0 翻到 0.7029。

**对账官方 77.06**：见第 1.5 节。结论是官方 77.06 与本地 70.29 **大概率不矛盾**——差距落在"聚合口径"里（逐 attempt 均值 vs best-of-3），不需要用"模型更强"或"环境更好"来解释。

---

## 1. 分数和官方对不上，先查这三处测量口径

在怀疑模型或环境之前，WB-Bench-Code 有三个已知的口径陷阱会让同一批 trial 算出截然不同的分数。任何"对不上"都应先逐一排除。

### 1.1 `verifier_user` —— 能把 0.0 变成 0.7029 的一行配置

**这是最大的坑，已实测。** `configs/bench/wb-bench-code-v1.0.yaml` 必须显式设置 `verifier_user: "root"`。缺这一行时，code 继承 `_default.yaml` 的 `verifier_user: dev`，而数据集测试文件是 `640` 权限、目录 `750`、owner `1002:1002`——容器内 `dev` 既不是 owner 也不在 group 里，落到 other 位读不到 `/tests`。

危险之处在于**它不报错**：`pytest_injected` 的 verifier 用 `shutil.copy2` 复制注入测试时读不到源文件、又无异常处理，于是 pytest 跑的是未打补丁的 workspace，`collected 0 items`，而 `script_exit_code` 仍是 0。Harbor 把 trial 记成正常完成，表面看像"模型能力差"。

修复前后（同一 80 任务）：平均 reward **0.0 → 0.7029**，`build_error` 29 → 0。**排查信号**：出现大面积 0 分且 `script_exit_code=0` 时，先看这一行，不要先怀疑模型。

### 1.2 `reward` 不是 `pass_rate`

`scorer.metrics` 输出两个主指标，语义完全不同：

- `reward` = 每个 trial 取 verifier 报告分数（`build_error` 记 0），先任务内对 3 次取均值，再对任务取均值。**本次 0.7029。**
- `pass_rate` = 全部测试通过（分数 ≥ 1.0）的 trial 占比。**本次 0.3542。**

两者相差 0.35。如果外部数字是"全通过率"口径，就该和 0.3542 比，不是 0.7029。反之亦然。混用这两个数会得出完全错误的"对不上"结论。

### 1.3 `per_task` 键被截断到 32 字符

`metrics.json` 的 `per_task` 键截断到 32 字符（如 `api_contract-hard-validation_err` 对应真实目录 `api_contract-hard-validation_errors`）。任何按任务名 join 元数据的脚本，若不做唯一前缀解析，会静默丢任务、算出偏低的子集分。本报告所有按任务的聚合都做了唯一前缀解析并断言唯一命中。

### 1.4 追踪一个分数的完整链路

要把总分追到单条证据，链路是：

```
metrics.json (reward 0.7029)
  └─ per_task[task].reward                     # 任务级
       └─ per_task[task].attempts[i].reward    # trial 级，含 score_source
            └─ <trial>/verifier/score.json     # 判定结构 + judges[].metadata.stdout
                 └─ verifier.py 打印的 tests[] # 逐条断言 name/passed/detail  ← 命名严格度证据在此
                 └─ <trial>/verifier/test_output.txt  # pytest 型任务的 FAILED 明细
```

**关键提醒**：trial 目录里的 `verifier/reward.json` 是**精简版**（只有 `reward`/`tests_passed`/`tests_total`），逐条测试名只保留在 `score.json` 的 `judges[].metadata.stdout` 里。要做命名严格度分析必须从后者解析。

### 1.5 与官方 77.06 的对账：差距在聚合口径，不在能力

官方报告 GLM-5.2 在 code 上是 **77.06**，本地 `reward` 是 **70.29**。注意 77.06 **高于本地任何单次 attempt**（三轮分别 69.21 / 69.97 / 71.68），所以官方口径**不可能是"逐 attempt 取均值"**——那个口径天花板就是 71.68。把同一批 240 trial 换几种聚合口径重算：

| 聚合口径 | 数值 (×100) | 说明 |
|---|---:|---|
| 逐 attempt 均值（本地 `reward`） | **70.29** | 每任务对 3 次取均值，再对任务平均。把方差当损失 |
| median-of-3 | 71.01 | 每任务取 3 次中位数 |
| top-2 均值（去掉最差一次） | 75.36 | 每任务保留最好的 2 次 |
| **best-of-3**（每任务取最高） | **79.70** | pass@3 式，把方差当收益 |
| worst-of-3 | 60.15 | 每任务取最低 |

**77.06 落在 top-2(75.36) 与 best-of-3(79.70) 之间。** 我无法用本地这批 trial 精确重构出 77.06（不等于任何一个干净口径），但区间位置足以支撑结论：

- **官方大概率用了保留高分的聚合**（best-of-n / top-k / pass@k 式）。若官方每次 attempt 质量比本地略稳一点，best-of-3 或 top-2 落在 77.06 完全合理。这种情况下，70.29 与 77.06 是**同一批能力、不同口径**，本地偏低纯粹因为"逐 attempt 均值"是所有口径里最保守的一个。
- **也不能排除官方是另一次独立运行**、每次 attempt 本身更强。但由于本地环境净影响≈0（第 2 节：0 个 API 错误），"官方更高"**不可能是环境差异造成的**，只可能是采样运气或 agent 配置差异——仍然不是"模型在本地变弱了"。

**这里有一个更重要的信息**：本地逐 attempt 均值 70.29 与 best-of-3 79.70 之间的 **9.41 分全部来自任务内 3 次 attempt 的方差**。而方差最大的任务恰恰是本报告点名的"隐藏契约"受害者：

| 任务 | mean | max | 方差来源 |
|---|---:|---:|---|
| `refactor-hard-validation_error_paths` | 30.6 | 91.7 | 有一次丢失公开入口 `best_match` → 归零（见 §4.2） |
| `bug_fix-easy-a_crash_in_local` | 44.4 | 100 | 首次定位是否命中 |
| `tool_behavior-easy-diagnostics_and_observability` | 50.0 | 100 | 契约猜测 |
| `feature-medium-install_and_run_script` | 50.0 | 100 | 契约猜测 |
| `performance-hard-selector_cache` | 72.2 | 100 | 缓存实现不稳定 |

**含义**：如果官方是 best-of-n 口径，它从方差里"捡回"的分数里，有相当一部分捡的是这些**契约漏洞/命名运气**，而非稳定的真实能力。换句话说，77.06（若为 best-of-3 口径）会比 70.29 **更多地掩盖**第 3 节揭示的隐藏契约问题——多跑几次总有一次蒙对名字。评测方若想让分数更反映稳定能力，应优先修第 3 节的契约可见性，而不是提高 n。

**给对账者的操作建议**：确认官方的 (1) 聚合口径（mean@k / best@k / pass@k）、(2) attempts 数、(3) 是否同一 `verifier_user: root` 修复后的代码。三者对齐后即可精确对账；只要口径是 best-of-3，本地这批数据的对应值是 79.70，与 77.06 相差 2.64，属正常运行间波动。

---

## 2. API / 环境因素：计分结果里几乎为零

### 2.1 计分 trial 中的 LLM API 错误 = 0

三层独立检查一致：

| 检查层 | 方法 | 结果 |
|---|---|---|
| Harbor 结果 | 240 个 `result.json` 的 `exception_info` | 全部 `None` |
| 会话终态 | 240 个 `cc-output.txt` 末尾 `type:result` 记录 | 全部 `subtype:success`、`is_error:false`、`api_error_status:null` |
| 流内签名扫描 | 逐行解析 382MB stream-json，匹配 overloaded/rate_limit/5xx/连接重置/上下文超限 | **0 命中** |

计数陷阱：对 `cc-output.txt` 直接 `grep 429`/`503` 会在 238/239 个 trial "命中"，但那全是 UUID 与 token 计数里的数字子串，不是状态码。必须按 JSON 结构解析。

### 2.2 补跑前归档的 13 个失败，全是基础设施问题

原地补跑归档了 14 条记录到 `2026-08-28__10-53-56.attempt-history/`（首轮 13 + 补跑轮 1）：`CancelledError` 6、`NonZeroAgentExitCodeError`（Docker exec exit 128）3、`RuntimeError`（compose 起不来）2、`missing_result` 1、`AgentTimeoutError`（3600s 墙钟超时无 reward）1、`NonZeroAgentExitCodeError`（OOM exit 137）1。**没有一个是 LLM API 错误**，全是 Docker/进程层面，且已被有效 trial 替换。

### 2.3 两类不致崩溃、但污染环境的干扰（不影响归因）

工具级 `is_error` 中出现 ENOSPC 磁盘满（38 次 / 19 trial）与 WebFetch 域名受限（35 次 / 18 trial）。二者都不使 trial 崩溃，照常计分。ENOSPC 报错路径全部落在 `/tmp/claude-1000/.../tasks/*.output`（agent 后台命令输出），**不是工作区代码写入失败**。

**这里有一个容易误读的数字**：ENOSPC 组均值 0.6105 看似低于总体 0.7029，但同任务内配对（15 个任务同时有 ENOSPC trial 和干净 trial）后方向反转——ENOSPC 侧 0.6150 反而高于干净侧 0.5819。原因是磁盘满集中命中的是本来就低分、跑得久、写文件多的 `bug-fix`/`feature` trial（19 个里 11 个 bug-fix、2 个 feature），属选择效应而非因果。剔除全部 19 个 ENOSPC trial 后总体均值仅从 0.7029 变到 0.7108。**环境噪声不改变任何结论。**

---

## 3. verifier 的隐藏契约：合成任务失分的主因

这一节回答"是不是测试对函数名/字段名要求太高"——**答案是肯定的，且可量化**。

### 3.1 三种 verifier family，难度性质完全不同

| family | 任务数 | reward | pass_rate | 性质 |
|---|---:|---:|---:|---|
| `script_verifier` | 54 | 0.7647 | 0.4383 | **合成**契约满足：写代码满足一段隐藏的 Python 断言脚本 |
| `pytest_injected` | 22 | 0.5467 | 0.1970 | **真实 OSS 仓库** bug：注入特定隐藏测试选择器 |
| `repo_understanding` | 4 | 0.7265 | 0.0833 | 仓库理解型结构化产出 |
| 合计 | 80 | 0.7029 | 0.3542 | |

`script_verifier` 的失分机制和 `pytest_injected` 完全不同，必须分开看。本节讲前者（命名严格度），第 4 节讲后者（真实能力）。

### 3.2 关键证据：能否看到 smoke test，决定成败

**agent 的工作区里不含 verifier**（`test.sh` 是 `exit 64` 的桩，真实测试在评测时才注入）。因此 agent 要么能从工作区自带的 smoke test 推断契约，要么只能**猜**精确的函数名/字段名/错误码。

按"工作区是否自带测试文件"切分 `script_verifier` 任务：

| 分组 | 任务数 | 平均 reward |
|---|---:|---:|
| 工作区**有** smoke/测试文件 | 9 | **0.9505** |
| 工作区**无**任何测试文件 | 45 | **0.7275** |

**差 0.22。** 9 个有 smoke test 的任务几乎全满分：`support_sla` 1.0、`audit_event_export` 1.0、`day7_retention` 1.0、三个 `testing-*` 1.0、`schema_drift` 0.9487、`option_resolution` 0.9394（唯一例外 `seat_limit_policy` 0.6667）。而 24 个"每次尝试都失败某些断言"的任务里，**23 个不带任何测试文件**。

进一步佐证：`support_sla` 指令极其含糊（"帮我把响应时间、没及时回的数量和还没关的工单整理出来，最好能按团队看"，60 字符），但因为工作区有 `tests/test_smoke.py` 揭示了输出 schema，模型拿了满分。同样含糊的指令，没有 smoke test 时就大面积失分。**决定成败的不是指令清晰度，而是契约是否可见。**

### 3.3 典型案例：`api_contract-hard-markup_errors`（0.0556）

这是命名严格度最干净的案例。指令仅一句："把 parse/render/escape 的行为固定：正常嵌套能解析，未闭合和错配要抛带 code/position 的错误。"

**verifier 要求的精确契约**（agent 看不到）：

- 函数 `parse_markup(text)`，返回带 `.text`/`.style` 的 `Segment` 对象列表
- `Segment` 作为公开 dataclass
- `MarkupError(message, code, position)`，且 `.code == "unclosed_tag"` / `"unmatched_close"`
- `escape()`、`render_markup()`

**模型实际产出**（795 行补丁，附带自写单元测试）：

| verifier 要求 | 模型产出 | 匹配 |
|---|---|---|
| `render_markup` | `render_markup` | ✅（沿用 baseline 名） |
| `escape` | `escape` | ✅ |
| `MarkupError`（类存在） | `MarkupError` | ✅（但构造签名不同） |
| `parse_markup` | `parse` | ❌ 名字不对 |
| `Segment`（扁平 span） | `Text` / `Element`（AST 树） | ❌ 结构与名字都不对 |
| `.code=="unclosed_tag"` | 自定义错误码 | ❌ 字面值不对 |

模型在 step 13 的推理里**显式权衡并主动选错**了：

> "…do we even need a tree, or just a flat token/span list? Rich uses spans. But a tree (nested Elements) is the natural representation of nested markup and is easy to consume. Tree it is…"

它考虑过 verifier 想要的扁平 span 表示，出于"更干净的工程设计"选择了 AST 树。**没有任何测试能纠正它。**

后果被放大：11 条断言里连"plain render is unchanged"都失败——因为该断言是 `render_markup("hello")=="hello" and callable(parse_markup) and callable(escape)`，`parse_markup` 这个名字不存在导致 `callable(None)` 短路，**一个缺失的名字连带拖垮整组断言**。模型写的是**能工作、甚至更完善**的解析器（自带的 `ParseTests`/`ErrorContractTests` 都通过），只因契约名不对而得 0.0556。

### 3.4 命名严格度的系统性

对全部 54 个 `script_verifier` 任务扫描 verifier 断言中被硬编码、却未在指令中出现的标识符：

- **报表/导出型任务**（`data_reporting`/`product_policy`/`product_analytics`/`model_evaluation`/`data_quality`）的 verifier 要求精确的输出字典键：`avg_first_response_minutes`、`sla_misses`、`priority_counts`、`manual_review`、`conversion_lift`、`macro_f1`…这些键名**几乎都不在指令里**。有 smoke test 的能推出来（故满分），没有的只能猜。
- **API 契约型**（`markup_errors`/`token_errors`/`openapi_params`/`validation_errors`）要求精确的公开符号名和错误码字面值（`"bad_signature"`、`"expired"`、`build_parameters`、`unmatched_close`），同样未在指令中给出。

这不是模型"不会写"，而是评测把**可自由命名的实现细节**当成了硬性判分点，且对 agent 隐藏了唯一能揭示这些细节的测试。

---

## 4. 模型能力：真实仓库 bug 才是硬短板

命名问题之外，确有真实的能力短板，集中在 `pytest_injected` 一族。

### 4.1 真实 OSS 仓库 bug（22 任务，0.5467）

这些是从真实项目截取的 bug/feature：`asgiref`、`django`、`pytest`、`marshmallow`、`jsonschema`、`black`、`fastapi`、`itsdangerous`、`coveragepy`、`platformdirs`、`copier`、`sqlfluff`…属于 SWE-bench 式的真实难度。`pass_rate` 仅 0.1970——即便部分测试常过，全绿很难。这里的失分**是真实能力信号**：需要在陌生大仓库里精确定位、改对边界，隐藏测试选择器无从取巧。

### 4.2 行为签名（全 240 trial 轨迹）

- **"加代码"倾向**：补丁新增行中位数 149、均值 181，删除均值仅 4。对 `bug-fix`/`refactor` 这类本应以修改为主的任务是负相关。
- **大补丁反而低分**：新增 > 400 行的 17 个 trial 平均 0.5178，≤ 100 行的 81 个平均 0.6876。大量新增常是"没定位准就重写"的症状（见 `markup_errors` 795 行得 0.0556）。
- **失败在烧钱打转**：`reward<0.3` 的 trial 步数中位 105、成本 $11.47；`reward≥0.9` 的 32 步、$1.29。失败不是没干活，是长时间跑偏。240 trial 共 $1,875.95（$7.82/trial）。
- **重构是全有或全无**：`refactor-hard-validation_error_paths` 三次 0.9167/0.0/0.0，后两次因丢失公开入口 `best_match` 全线 `AttributeError`。保住公开 API = 近满分，丢了 = 归零。

### 4.3 失败类型分布

失败以 `AssertionError` 为主（51 trial 命中），而非崩溃或导入错误——**代码能跑，行为对不上**。`reward<0.6` 的 84 个 trial 中：AssertionError 27、TypeError 12、AttributeError 8、KeyError 8。TypeError/AttributeError 多来自 3.3 那类"公开符号缺失导致调用点拿到 None/缺属性"。

---

## 5. 归因汇总：0.7029 的缺口拆给谁

把"距离满分的差距"按可归因来源拆解（数量级估计，非精确加性分解）：

| 来源 | 方向 | 证据锚点 | 可否通过修评测/改配置回收 |
|---|---|---|---|
| `verifier_user` 权限 | 已修复，曾吃掉全部分数 | 0.0 → 0.7029 | ✅ 已随 `5c0ad8d` 进主仓 |
| verifier 隐藏契约 + 精确命名 | 压低合成任务约 0.22 | 有/无 smoke test 0.9505 vs 0.7275 | ✅ 属评测设计，可通过放开命名/给契约回收 |
| 真实 OSS 仓库 SWE 难度 | 压低到 0.5467 | `pytest_injected` 22 任务 | ❌ 真实能力，只能靠模型/agent 改进 |
| API / 网络 / Docker 环境 | ≈ 0 | 计分 trial 0 个 API 错误 | —— 不是瓶颈 |
| 聚合口径（vs 官方 77.06） | 差 6.77，非能力/环境 | 逐 attempt 均值 70.29 vs best-of-3 79.70（§1.5） | ✅ 对齐口径即可，无需归因到模型或环境 |

**给评测方**：`script_verifier` 任务对可自由命名的实现细节做硬匹配、又对 agent 隐藏测试，会把工程选择差异误记为能力缺陷。两个修法二选一——(a) 在指令中显式给出公开符号/字段/错误码契约；(b) 像 9 个高分任务那样在工作区放一个揭示 schema 的 smoke test。任一都能让这批任务的分数更反映真实能力。

**给模型/agent 方**：真正的能力短板是真实仓库 bug 的精确定位与最小化修改。可操作的抓手——抑制"重写式"大补丁、提交前核对公开 API 回归、给失败轨迹加步数熔断（当前失败 trial 烧 3.3× 步数）。

---

## 6. 复现方法

```bash
RUN=results/glm-5.2.cc.code/2026-08-28__10-53-56

# 总分
uv run python -m workbuddy_bench.scorer.metrics "$RUN" --json | python -m json.tool

# 单 trial 逐条断言（命名严格度证据）
python - <<'PY'
import json
sc=json.load(open("results/.../<trial>/verifier/score.json"))
blob=sc["judges"][0]["metadata"]["stdout"]      # verifier.py 打印的 JSON
print(blob)                                      # tests[].name / passed / detail
PY

# verifier family / 契约来源
cat datasets/wb-bench-code-v1.0/tasks/<task>/tests/verifier.toml   # family
cat datasets/wb-bench-code-v1.0/tasks/<task>/tests/verifier.py     # 硬编码的名字/字段/错误码
cat datasets/wb-bench-code-v1.0/tasks/<task>/instruction.md        # 对比：指令是否给出这些名字
tar tzf datasets/wb-bench-code-v1.0/tasks/<task>/environment/workspace.tar.gz | grep -i test  # 是否有 smoke test
```

分析用中间产物（本次生成）：`$CLAUDE_JOB_DIR/tmp/` 下的 `tests_corpus.json`（240 trial 逐条断言）、`pertest_agg.json`（每断言通过率）、`always_fail.json`（每次必败的 105 条断言）、`workspace_tests.json`（工作区测试与 reward 关联）、`families.json`（verifier family）。

> 附总体分布数据见同目录 `results/glm-5.2.cc.code/2026-08-28__10-53-56/report/report.md`（按类别/难度/复杂度/verification_mode/artifact_type 的完整分解与代表性案例）。本文档聚焦归因，不重复该分解。
