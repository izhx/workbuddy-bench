# WB-Bench-Code 评估分析报告

## 1. 数据与方法学检查

- **运行路径**: `results/dsv4-flash.cc.code/2026-09-03__09-50-50`
- **模型**: `dsv4-flash`(deepseek-v4-flash)
- **Harness**: `claude-code` / `2.1.187`
- **数据集**: `wb-bench-code`(运行时 id `wb-bench-code-v1.0`),80 个任务
- **每任务尝试数**: 3(attempts_per_task=[3],共 240 次 trial)
- **评分模式**: `CompositeVerifier`(code profile),**纯 Rule 模式,无 llm_judge**(`llm_judge.enabled=false`)。`score.json` 中 `judge_type=rule_script`。
- **指标语义**:
  - `reward`: 主分,每任务 reward 均值,build_error 计 0。
  - `pass_rate`: 单次 trial verifier 分数 ≥ 1.0 的比例(全对率),与 `reward` 不同义。
- **完整性**: `n_tasks=80`, `n_trials=240`, 每任务均有 3 次尝试,无 `missing_tasks`。**1 个任务含 build_error 尝试**(`data_reporting-hard-invoice_line` Vb54cvU,0/1,得分 0),其余 239 次均正常评分。

## 2. 执行摘要

| 指标 | 值 |
|---|---|
| **reward(主分)** | **0.6268** |
| pass_rate(全对率) | 0.2667 |
| n_tasks / n_trials | 80 / 240 |
| 满分任务(reward ≥ 1.0) | 11 |
| 低分任务(reward < 0.4) | 19 |
| build_error 任务 | 1 |

**Per-attempt reward / pass_rate**:

| Attempt | reward | pass_rate |
|---|---|---|
| 1 | 0.6206 | 0.2625 |
| 2 | 0.6525 | 0.2875 |
| 3 | 0.6074 | 0.2500 |

三次尝试接近,无明显衰减或渐增,说明结果稳定、非单次侥幸或偶然失败。

> 注:本次 run 已包含一次 in-place 补跑。补跑针对 `bug_fix-medium-permission_error`(原 exit 137/OOM 崩溃),重跑 attempt 仍因 OOM 崩溃,故该任务当前有效尝试为 2/3(reward 0.25)。该异常为运行期环境故障,详见第 5 节,不应直接归因于模型能力。

## 3. 能力构成拆分(按 category)

| Category | reward | 任务数 |
|---|---|---|
| testing | 0.9348 | 4 |
| feature-pipeline | 0.9206 | 4 |
| schema-behavior | 0.8195 | 4 |
| repo-understanding | 0.8039 | 4 |
| model-evaluation | 0.7564 | 4 |
| data-quality | 0.7372 | 4 |
| data-reporting | 0.7340 | 4 |
| refactor | 0.7153 | 4 |
| tool-behavior | 0.7084 | 2 |
| reliability | 0.6959 | 4 |
| python-port | 0.6945 | 4 |
| performance | 0.6325 | 4 |
| security-hardening | 0.6041 | 4 |
| bug-fix | 0.5051 | 10 |
| api-contract | 0.4613 | 4 |
| product-policy | 0.4188 | 3 |
| feature | 0.4125 | 10 |
| **product-analytics** | **0.0855** | 3 |

**最弱点**: `product-analytics`(0.0855, 3 任务全 hard/L4/functional_tests,reward 0.077–0.103),远低于均值;其次 `feature`(0.41, 10 任务)、`api-contract`(0.46)。
**强项**: `testing`(0.93)、`feature-pipeline`(0.92)、`schema-behavior`(0.82)。

### 按 difficulty

| difficulty | reward | 任务数 |
|---|---|---|
| easy | 0.6087 | 7 |
| medium | 0.6087 | 31 |
| hard | 0.6432 | 42 |

difficulty 非单调(medium≈easy,hard 甚至略高)——**hard 任务数远多于 easy/medium**(42/31/7),且 easy 组主要被 `feature-easy-lru_caching_to_tzof`(0.0)拖累。

### 按 verification_mode

| mode | reward | 任务数 |
|---|---|---|
| mutation_tests | 0.9348 | 4 |
| structured_report | 0.8039 | 4 |
| mixed | 0.6945 | 4 |
| functional_tests | 0.6520 | 50 |
| unit | 0.4558 | 16 |
| **integration** | **0.2613** | 2 |

`integration`(0.26)与 `unit`(0.46)最弱,`mutation_tests`(0.93)最强。

## 4. 正确性与失败模式

### 模式 A: 输出 schema 不匹配 / 键缺失 —— `product-analytics`

三个任务(ab_test_a / day7_rete / signup_fu)全部 hard/L4/functional_tests,reward 0.08–0.10。`verifier/score.json` 显示 rule_script 报 **`KeyError: 'control'` / `KeyError: 'treatment'`**,即 agent 产出的分析结果里缺少 AB 分组键,无法通过规则校验。这是**确定性输出契约问题**,非随机失败;三任务同类,说明模型在该数据产品分析类任务上系统性产出不合 schema 的结果。

### 模式 B: API 缺口 / 方法未实现 —— `feature-easy-lru_caching_to_tzof`

三个 attempt reward 全为 0.0,`test_pass_rate=0`。`verifier/test_output.txt` 显示:
- `AttributeError: 'GettzFunc' object has no attribute 'set_cache_size'`(需实现的新 API 未实现)
- `assert tzoffset('UTC',0) is None` 失败(弱引用缓存清空逻辑不对)

agent 的 patch(85 行)改了 `dateutil/tz/_factories.py`,但**漏掉了 `GettzFunc.set_cache_size` 方法**,且缓存清理分支语义错误。4/4 测试失败。这是"改了但 API 不完整"的代表。

### 模式 C: 环境 / 资源 —— 补跑仍 OOM

`bug_fix-medium-permission_error`(medium/unit):1 次崩溃 attempt(`exit 137` SIGKILL/OOM),2 次有效 reward 0.25。该任务本身 4 个测试中通过 1 个(1/4),是正常低分;但崩溃 attempt 是运行期内存资源问题(并发时触发),已在第 2 节单独标注,归因于环境而非能力。

### 失败模式汇总

- **输出/契约类**: `product-analytics`(KeyError 缺键)、`api-contract`(0.46, API 契约不符,如 `api_contract-hard-markup_errors` 0.028)。
- **API 完整性类**: `feature-easy-lru`(缺方法)、`feature-medium-add_support_for_d`(0.25, integration,attempt [0,3,0]/[4,4,4] 极不稳定)。
- **小样本硬题**: `product-policy-medium-coupon_eli`(0.154)、`data_reporting-hard-invoice_line`(0.167,含 1 build_error)。

## 5. Patch 本地化与轨迹

以 `feature-easy-lru_caching_to_tzof` 为例:patch 85 行,改为 `dateutil/tz/_factories.py`,动了目标文件,但**未完整实现**需要的 API(`set_cache_size`),出现测试专用旁路(实际是 API 缺口,不是 test-only 绕过)。
`product-analytics` 类任务 patch 产出与规则期望的输出结构不一致(缺 control/treatment 键),说明**对数据输出契约的把握不足**,而非文件没动。

## 6. 代表性案例

1. **`feature-pipeline-hard-window_agg`** (强项,reward 1.0): feature-pipeline 类模型表现优秀,说明数据管道/聚合类任务把握到位。
2. **`testing-hard`(测试生成 reward 0.93)** / **`mutation_tests`(0.93)**: mutation-tests 验证模式下的测试类任务,模型能生成高覆盖的测试。
3. **`product_analytics-hard-ab_test_a`** (弱项,0.077,functional_tests): 输出缺失 `control`/`treatment` 键,`KeyError`,规则校验全 fail。数据产品分析的输出 schema 是最强短板。
4. **`feature-easy-lru_caching_to_tzof`** (弱项,0.0,unit): API 未实现(`set_cache_size`),弱引用缓存逻辑错。
5. **`bug_fix-medium-incorrect_linenos`** (满分,1.0,unit): bug-fix 中模型能准确修复行号类问题。
6. **`data_reporting-hard-invoice_line`** (0.167,含 1 build_error): build_error 尝试 0/1,其余 0.25;记为该任务完整性不足,不可归因于模型能力。

## 7. 改进建议

1. **产品分析类(如 product-analytics)**: 这是最大短板,建议增强模型对"分析输出契约/schema"的理解——训练或提示中强化"按任务输出结构生成分组键与指标"的能力。
2. **feature/API 完整性**: 典型失败是"动了代码但漏实现 API"(如 `set_cache_size`)。建议在验证时明确检查 API 表面是否符合预期,并让模型更系统地遍历任务要求的接口。
3. **小样本硬题(integration/unit)**: `integration` 验证模式仅 0.26,多个 feature-medium 任务波动极大(如 add_support_for_d [0,3,0]),说明跨模块/集成场景稳定性差,建议在这些场景增加训练数据。
4. **运行期资源**: 补跑中 `bug_fix-medium-permission_error` 反复 OOM(exit 137),这是并发环境资源问题,不是模型能力,后续评估应控制并发以得到干净结果。

## 附注

- 本报告基于 `results/dsv4-flash.cc.code/2026-09-03__09-50-50/` 的 Harbor 输入产物,`metrics.json` 由 `workbuddy_bench.scorer.metrics` 生成。
- 数据/任务元数据来源:仓库本地 `datasets/wb-bench-code-v1.0/tasks/`(每个任务的 `task.toml`)。
- 补跑状态:该 run 已执行一次 in-place 补跑(`bug_fix-medium-permission_error`),因 OOM 未补上,有效尝试为 2/3。第 2 节的 reward/统计保留了该崩溃尝试标记,用于反映运行期完整性。
