# WB-Bench-Web 评估分析报告

## 1. 数据与方法学检查

- **运行路径**: `results/dsv4-flash.cc.web/2026-09-03__16-12-24`
- **模型**: `dsv4-flash`(deepseek-v4-flash)
- **Harness**: `claude-code` / `2.1.187`
- **数据集**: `wb-bench-web`(运行时 id `wb-bench-web-v1.0`),70 个任务
- **每任务尝试数**: 3(`attempts_per_task=[3]`,共 210 次 trial),无 `missing_tasks`,全部 70 个任务各 3 次尝试均完整。
- **评分模式**: `CompositeVerifier`(web profile),`llm_judge.enabled=true`,`mode=in_container`,`judge-kimi-k2.6`。`score.json` 含 `dimensions._penalty.items`,每条 item 带 `source`(`llm`/`vlm`)、`severity`(`major`/`normal`)、`actual` 证据。
- **指标语义**:
  - `reward`: 主分,每任务 reward 均值,build_error 计 0。
  - `pass_rate`: 单次 trial verifier 分数 ≥ 1.0 的比例(全对率),与 `reward` 不同义。
- **完整性**: `n_tasks=70`, `n_trials=210`, 每任务 3 次尝试。**0 次 build_error**——所有 210 次 trial 均正常完成评分,无任何环境/构建崩溃或超时。
- **score_sources**: 全部 210 次来自 `reward`(`score.json` 的 `reward` 字段),无 `overall` / `test_pass_rate` 兜底,无异常来源。

> **关于图片与崩溃(重要)**:本评测集 web 任务全程**没有任何一次把图片发送给模型 API**。全量扫描 210/210 个 `agent/cc-output.txt` 中 `"type":"image"`、`image_url`、`data:image/<mime>;base64` 长载荷均为 **0**;210/210 个 `agent/trajectory.json` 的 `steps[]`(共 8996 条消息)亦无任何 image content block。`verifier/evidence/visual/*.png` 是 verifier 侧在 agent 完成后对最终页面截的图,用于 judge 评分,**从未回喂给 agent 模型**(score.json `judge_meta.image_input_used=false`)。因此 dsv4-flash 在 web 上没有出现 glm5.2 那种"带图→API 不识图→崩溃"的问题。

## 2. 执行摘要

| 指标 | 值 |
|---|---|
| **reward(主分)** | **0.3690** |
| pass_rate(全对率) | 0.1286 |
| n_tasks / n_trials | 70 / 210 |
| 满分任务(reward ≥ 1.0) | 4 |
| 低分任务(reward < 0.4) | 37 |
| 零分任务(reward = 0) | 12 |
| build_error 任务 | 0 |

**Per-attempt reward / pass_rate**:

| Attempt | reward | pass_rate |
|---|---|---|
| 1 | 0.3571 | 0.1286 |
| 2 | 0.3771 | 0.1286 |
| 3 | 0.3729 | 0.1286 |

三次尝试 reward 在 0.357–0.377 窄幅波动,pass_rate 恒定 0.1286——说明分数稳定,**不是**单次侥幸或偶发失败;任务的得分能力在三轮尝试间保持一致。

**分数分布**:

| 区间 | 任务数 |
|---|---|
| reward = 0 | 12 |
| 0 < reward < 0.2 | 14 |
| 0.2 ≤ reward < 0.4 | 11 |
| 0.4 ≤ reward < 0.6 | 17 |
| 0.6 ≤ reward < 0.8 | 6 |
| 0.8 ≤ reward < 1.0 | 6 |
| reward = 1.0 | 4 |

中位数 reward = 0.35,均值 0.369。整体偏低,呈明显的"双头"分布:少数任务能拿满(4 个满分),大量任务卡在中低区间。

**满分任务(4 个)**:

| 任务 | category | 说明 |
|---|---|---|
| `atmosphere-game-L4-035` | data-visualization | 3/3 次全部 8/8 通过,wall_time 78–90s |
| `firmware-card-interaction-tests-L3-074` | code-testing | 3/3 全对 |
| `research-homepage-visual-repair-L3-064` | visual-design | 3/3 全对 |
| `runbook-handoff-conversion-L3-072` | document-conversion | 3/3 全对 |

**零分任务(12 个)**,见第 4/5 节归因。

## 3. 能力构成拆分

### 按 category

| category | n | mean reward | 零分 | 满分 |
|---|---|---|---|---|
| analytical-report | 7 | **0.581** | 0 | 0 |
| visual-design | 9 | **0.541** | 0 | 1 |
| code-testing | 7 | 0.538 | 1 | 1 |
| document-conversion | 5 | 0.433 | 0 | 1 |
| page-implementation | 6 | 0.394 | 1 | 0 |
| data-visualization | 15 | 0.333 | 3 | 1 |
| **page-interaction** | 21 | **0.171** | 7 | 0 |

**page-interaction 是绝对短板**(mean 0.171,21 个任务里 7 个零分、0 个满分)。这类任务要求 agent 驱动真实交互(点击、表单、多步流程、持久化状态),dsv4-flash 在这类"需要操作 + 验证状态流转"的场景表现最弱。analytical-report / visual-design 是其强项(纯文案/视觉产出,无需复杂状态机)。

### 按 task_mode

| task_mode | n | mean reward |
|---|---|---|
| Review & Analysis | 7 | **0.581** |
| Test Generation | 7 | 0.538 |
| Format Conversion | 5 | 0.433 |
| Bug Fix | 8 | 0.354 |
| From Scratch | 35 | 0.317 |
| **Extend Existing** | 8 | **0.238** |

**Extend Existing(在已有代码上扩展)最难**(0.238)。这与 page-interaction 短板互为印证——扩展型任务往往要读懂既有交互逻辑再延续它,正是 dsv4-flash 的弱项。Review & Analysis(分析评论)和 Test Generation(生成测试)相对容易。

### 按 interaction_state_complexity

| interaction_state_complexity | n | mean reward |
|---|---|---|
| No Interaction | 25 | **0.515** |
| Light Interaction | 8 | 0.433 |
| Single-flow State | 15 | 0.324 |
| Persistence/Offline/Cross-state | 13 | 0.251 |
| **Multi-step Workflow** | 9 | **0.152** |

**交互复杂度与得分强负相关**。No Interaction(静态页/纯产出)0.515 → Multi-step Workflow 0.152,单调下降。**Multi-step Workflow(多步工作流)是最短板**(0.152),其次是 Persistence/Offline/Cross-state(0.251)。这强烈指向:dsv4-flash 在"必须连续驱动多步流程并保持跨步骤状态"的场景下能力不足。

### 按 difficulty(L2/L3/L4)

| difficulty | n | mean reward |
|---|---|---|
| L2 | 8 | 0.542 |
| L3 | 29 | 0.414 |
| L4 | 33 | 0.288 |

难度分层有明显的梯度下降(L2 0.542 → L4 0.288),说明难度标注与模型表现一致。

## 4. 失败模式归因

综合 23 个零分/低分任务的 verifier 证据(`dimensions._penalty.items` 的 failed items + `actual` 字段),失败可归为 6 类主题。**关键在于:大多数失败不是"功能坏了",而是 agent 没有驱动/验证应有的交互路径,或数据加载失败导致页面根本无法渲染。**

### 模式 A: 数据/资源 404,页面无法渲染(高置信机械性硬伤)

`../public-assets/...` 相对路径从 serve root 解析失败,导致 JSON 数据加载 404 → 图表/列表为空 → 所有下游检查项连锁失败。

影响任务(全站 25/210 trial 命中 404/`unable to load`):

| 任务 | 证据 |
|---|---|
| `dispatch-health-chart-extension-L3-069` | 3/3 attempt 全 404 `Unable to load ../public-assets/data/dispatch-health-window.json`,图表不渲染 |
| `pwa-offline-checklist-L4-039` | sw.js 注册 404 + `inspection-items.json` 404,无 items 渲染,状态机不可触达 |
| `support-flow-sankey-state-repair-L4-070` | `support-path-events.json` 404,图表空,详情面板"对每条 link 显示硬编码数据" |
| `state-sync-table-workflow-L3-009` | 3/3 全 404 |
| `support-inbox-triage-L3-040` | 数据 404/JSON 解析失败,或回退到内联数组 |

这 5 个任务里,`dispatch-health-chart-extension`、`pwa-offline-checklist`、`support-flow-sankey-state-repair`、`state-sync-table-workflow` 是**全部 3 个 attempt 都 404**——确定性数据路径问题,不是偶发。

### 模式 B: 测试基建缺依赖,测试根本跑不了(不应归因模型)

| 任务 | 证据 |
|---|---|
| `fixture-and-golden-tests-L4-017` | `pytest` 未安装 → "runner exits with dependency_missing";rule 检查 "explicit mutation guard functions: fail"、"assertion density minimum: fail"。2/3 attempt 命中 |
| `csv-mapper-component-tests-L3-053` | 一次 attempt `vitest: not found`,测试不可执行;且 mutant 存活:`duplicate_mutant_fail=false` |

这类是**评测环境/依赖配置问题**,与模型能力无关,应单独剔除或修复环境后复评。

### 模式 C: agent 未驱动应走的交互流程(占比最高)

这是最大的失败来源。verifier 的 LLM judge 反复报告"no evidence"——workflow trace 显示 agent 只导航到部分节点或根本没推进到目标状态:

| 任务 | 证据 |
|---|---|
| `config-diff-review-console-L3-062` | 只导航到 D-102/D-103;judge:"no evidence that D-102 was approved";D-117/D-108/D-144 从未处理;DOM 计数(0 approved)与导出 JSON(2 approved)不符 |
| `survey-form-workflow-L4-030` | `steps_progressed: 0`——未驱动任何分支切换/提交 |
| `mobile-booking-flow-L4-036` | 卡在 step 1,报 `0 successful actions, 1 error clicking 'Next'`,`Please select a service and time slot` 校验阻塞 |
| `interaction-state-authoring-L3-027` | Apply 未真正过滤,grid 显示 8 个产品而非 2 个;draft-dirty 指示缺失 |
| `review-card-queue-persistence-L4-063` | 恢复状态错误,URL query 未读取,reload-persistence/seed-conflict "Not submitted" |
| `animated-explainer-L3-028` | 截图显示静态首帧,无可见运动;glossary 术语孤立在 YAML,未整合进课文 |
| `blog-editor-draft-recovery-L4-057` | 草稿未恢复(展示的是已发布内容而非本地草稿);save 状态停留在 'Unsaved changes' |
| `schedule-reschedule-flow-L4-061` | 无提交按钮 → 确认/通知摘要 UI 从未暴露;确认状态 `initialized to null` |

这一组说明核心问题:**dsv4-flash 往往做出了页面/功能,但没有真正驱动完整的交互流程(点击、等待、校验、提交、刷新验证),也没有在关键路径上留下可验证的证据**。这是模型能力的真实体现。

### 模式 D: 数据/语义计算错误,复现了 bug 而非修复

| 任务 | 证据 |
|---|---|
| `particle-boundary-simulation-L3-029` | `wallBounce=1.07 (>1)`,碰撞导致能量增益——**复现了原 bug 而非修复** |
| `chart-generation-L2-025` | stats 与 CSV 不符(computel);一次 attempt 图表全空白,JS 崩溃 `median is not a function` |
| `text-spec-dashboard-implementation-L3-001` | KPI "appear hardcoded... no evidence of derivation";图表数据绑定规则失败;missing header/sidebar |

### 模式 E: 状态持久化/恢复失败

| 任务 | 证据 |
|---|---|
| `review-card-queue-persistence-L4-063` | 恢复后显示 card-101 而非 card-104 |
| `blog-editor-draft-recovery-L4-057` | 草稿未恢复,未达 'saved' |
| `pwa-offline-checklist-L4-039` | 状态机不可达 |
| `mobile-booking-flow-L4-036` | reserved-slot-survives-restart "Not submitted" |

### 模式 F: 视觉/布局(次要,多为 VLM 判定,少为得分主导)

- `portfolio-showcase-L2-032`: 等宽卡片墙,无策展式排版;仅 3/13 catalog 项目;320px 重排证据缺失
- `schedule-reschedule-flow-L4-061`: 面板重叠/裁切
- `blog-editor-draft-recovery-L4-057`: 移动端横向溢出

## 5. 代表性案例

### 强项(满分/高分)

**`atmosphere-game-L4-035`(reward=1.0)** — data-visualization / From Scratch / Canvas simulation。这是一个"深海探索大气 Web 游戏"实现任务。dsv4-flash **3/3 次全部 8/8 通过**,wall_time 78–90s。说明它在中型单页游戏/模拟类任务上能完整实现并满足全部检查项。

**`research-homepage-visual-repair-L3-064`(reward=1.0)** — visual-design / Bug Fix。视觉修复类任务,3/3 全对。这印证了 visual-design 是强项(mean 0.541)。

### 中位

**`data-storytelling-L4-034`(reward=0.60, pass_rate=0.33)** — 3 次尝试:1 次全对、2 次部分正确。典型"一次碰对"的高方差任务,进一步说明模型在较复杂任务上不稳定。

### 弱项(零分/低分,含典型机械 vs 能力两类)

**`dispatch-health-chart-extension-L3-069`(reward=0.0, 3/3)** — data-visualization / Extend Existing。**机械性失败**:3 次全部因 `../public-assets/data/dispatch-health-window.json` 404 导致图表不渲染。这是数据路径问题,非模型逻辑能力问题,修复路径后应能显著提升。

**`survey-form-workflow-L4-030`(reward=0.0, 3/3)** — page-interaction / From Scratch / Multi-step。**能力性失败**:`steps_progressed: 0`——agent 从未驱动任何分支切换/提交/复核流程。这真实反映了 dsv4-flash 在多步工作流上的短板。

**`config-diff-review-console-L3-062`(reward=0.0, 3/3)** — page-interaction / From Scratch / Single-flow。既有**交互未完全驱动**(只处理了 D-102/D-103),又有**数据一致性 bug**(DOM 计数 0 approved vs 导出 JSON 2 approved)。两者叠加导致全零。

## 6. 改进建议

1. **修复数据/资源路径(先做,收益最大)**: `dispatch-health-chart-extension`、`support-flow-sankey-state-repair`、`pwa-offline-checklist`、`state-sync-table-workflow`、`support-inbox-triage` 全部因 `../public-assets/...` 相对路径 404 而失败。这类属于**确定性环境/路径问题**——应确认 serve 根目录与 `public-assets` 的解析关系,或让 agent 显式用正确路径加载数据,即可解锁一大片零分。

2. **补齐测试基建依赖**: `fixture-and-golden-tests`(pytest)、`csv-mapper-component-tests`(vitest)因测试框架未安装而无法运行。这 2 个任务应先在镜像/环境里装好 pytest/vitest 再复评,否则分数不反映模型能力。

3. **针对 Multi-step Workflow 与 page-interaction 做专项**: 这是最明确的短板(0.152 / 0.171)。dsv4-flash 需要强化:在任务结束时**真正驱动交互**(点击等待→校验 DOM→提交→刷新验证),而不是"做完页面就算完"。建议在 prompt 或 agent 行为层面强调 workflow trace 的完整性——每走一步就留下可验证的状态证据。

4. **状态持久化/恢复专项**: Persistence/Offline/Cross-state(0.251)多因 URL 参数未读取、reload 后状态丢失、草稿未恢复而失败。这是真实能力缺口,建议针对 `review-card-queue-persistence`、`blog-editor-draft-recovery`、`mobile-booking-flow` 这类任务做专项训练或提示词强化。

5. **区分真失败与没验证**: 大量零分是 judge 报 "no evidence"——即 agent 可能做对了但**没有驱动/没有验证目标路径**,导致无证据可判。建议在任务结束时让 agent 自检并明确执行目标交互,而非只产出代码。

## 附注

- 本报告基于单次 run 的 Harbor 工件,只读分析,未改动任何原始工件,未重跑任何评测。
- 数据来源: `results/dsv4-flash.cc.web/2026-09-03__16-12-24/report/metrics.json`(scorer 产出)与各 trial 的 `verifier/score.json`(含 `dimensions._penalty.items` 逐项证据)。
- task 元数据(`category` / `task_mode` / `interaction_state_complexity` / `difficulty`)取自 `datasets/wb-bench-web-v1.0/task_taxonomy.tsv`。
- 所有 210 次 trial 无 build_error;低分**部分**源于环境(404/缺依赖),**主要**源于模型在交互驱动上的能力缺口,两者已在第 4 节拆分。
