# WorkBuddyBench-Office 评测报告 — dsv4-flash

## 1. 数据与方法核验

- **运行目录**：`results/dsv4-flash.cc.office/2026-09-03__09-56-29`
- **模型**：`dsv4-flash`（`model`：`dsv4-flash.cc.office-251106-1788400573__dsv4-flash`）
- **Agent / 编排**：`claude-code/2.1.187`（`CcAgent`，`CLAUDE_CODE_MAX_TURNS=256`，`reasoning_effort=high`，`max_output_tokens=384000`）
- **数据集**：`wb-bench-office-v1.0`（canonical runtime id）
- **任务 / 试次**：50 个任务 × `n_attempts=3` = 150 个 trial，`attempts_per_task=[3]`，`missing_tasks=[]`
- **评分模式**：**Rule + Judge**（`CompositeVerifier`）。每个 trial 的 `verifier/score.json` 含两个 judge：`rule`（确定性规则，`judge.rule`，`judge_type=rule_script`）与 `llm_judge`（`judge-kimi-k2.6`，`llm_judge`，模式 `in_container`）。两条 verdict 链同时生效，`reward` 为合并后的连续 credit。

### reward 与 pass_rate 口径

- `reward` 是该任务 3 次 attempt 的平均连续 credit，主评分口径。
- `pass_rate` 定义为**verifier 最终得分 ≥ 1.0 的 trial 占比**。本次运行 **`pass_rate=0.0`**：150 个 trial 中无一达到 1.0。
- 这**不是"全部失败"**，而是 office 的连续 credit 合约性质所致：奖励窗口为 (0,1)，既没有满分也没有零分任务（`full-score=0`，`zero-score=0`），单次 attempt 最高 `0.978`（`stock-fund-return-compare-L3-010__ne4YKEJ`）。
- 上限来自双通道：`stock-fund-return` 的 attempt `test_pass_rate=0.9728`、`llm_judge_component_score=1.0` → 上限被 Rule 通道拉低；`ticket-weekly` 的 attempt `test_pass_rate=1.0`、`llm_judge_component_score=0.7778` → 上限被 Judge 通道拉低。两个通道都会把 `reward` 压到 1.0 以下。

### 完整性

- 50/50 任务均有 `score.json`，无 `build_error`，无异常 trial 数（每任务正好 3 个）。
- 所有 trial 均产生最终 score，`n_trials=150` 与期望一致。

## 2. 执行摘要

| 指标 | 值 |
|---|---|
| **reward** | **0.7710** |
| **pass_rate** | **0.0**（无 trial ≥ 1.0） |
| 满分任务数 | 0 |
| 零分任务数 | 0 |
| 低分任务数（reward < 0.4） | 1（`crypto-backtest-chain-L4-002`，0.3146） |
| 高分任务数（≥ 0.8） | 27 / 50 |
| 任务 reward 中位数 | 0.804 |
| 单次 attempt 最高 | 0.9782 |

**分次 attempt 表现**：

| attempt | reward |
|---|---|
| 1 | 0.7718 |
| 2 | **0.7823** |
| 3 | 0.7589 |

三次 attempt 均在 0.76~0.78 区间，无明显的"越试越好"或"越试越差"趋势，说明表现由任务本身决定，而非尝试次数。

**任务级方差最大的 5 个任务**（attempt 间 spread）：

| 任务 | sd | mean | spread | category / difficulty |
|---|---|---|---|---|
| `delivery-package-readonly-diff` | 0.386 | 0.649 | 0.831 | automation-workdir / hard |
| `health-quote-L2-003` | 0.287 | 0.601 | 0.626 | data-file-ops / easy |
| `recruiting-search-skill-mock-mcp-hardened` | 0.240 | 0.539 | 0.523 | automation-workdir / medium |
| `crypto-backtest-chain-L4-002` | 0.223 | 0.315 | 0.480 | data-file-ops / medium |
| `stock-fund-return-compare-L3-010` | 0.076 | 0.904 | 0.179 | data-file-ops / easy |

`delivery-package-readonly-diff` 是唯一一个三次 attempt 出现**量级断裂**的任务：0.934 / 0.910 / **0.102**（详见 §6）。

## 3. 任务类型分布

### 按 category

| category | 任务数 | reward 均值 | min | max | easy | medium | hard |
|---|---|---|---|---|---|---|---|
| `data-file-ops` | 24 | **0.775** | 0.315 | 0.959 | 0.849 (7) | 0.746 (15) | 0.734 (2) |
| `doc-ops` | 17 | **0.792** | 0.430 | 0.911 | 0.826 (5) | 0.811 (7) | 0.732 (5) |
| `automation-workdir` | 9 | **0.722** | 0.539 | 0.930 | 0.930 (1) | 0.620 (2) | 0.721 (6) |

- `doc-ops` 最强（0.792），`data-file-ops` 居中（0.775），`automation-workdir` 最弱（0.722）。
- `data-file-ops` 的 reward 全距最大（0.315~0.959），且其 easy/medium/hard 单调递减（0.849→0.746→0.734），是难度敏感度最高的类别。
- `automation-workdir` 的 medium 子集（0.620，仅 2 任务）异常拉低，但 hard 子集（0.721，6 任务）反而高于 medium，表明该类别表现与小样本、特定任务强相关，而非线性难度。

### 按 difficulty

| difficulty | 任务数 | reward 均值 |
|---|---|---|
| `easy` | 13 | **0.846** |
| `medium` | 24 | **0.754** |
| `hard` | 13 | **0.727** |

reward 随难度严格单调递减（0.846 → 0.754 → 0.727），符合预期，但 **`easy` 与 `hard` 差距（0.119）远小于 `easy` 与 `medium` 差距（0.092）** —— 说明从 easy 到 medium 是主要瓶颈，medium→hard 的边际衰减不明显。

## 4. 失败模式归因

对低分/高方差任务逐条提取 `llm_judge` 的 fail verdict `reason`，归纳出 6 类反复出现的失败签名。每条均绑定具体任务与证据。

### 4.1 汇总计数与明细行对不上（denominator 不一致）—— 最常见单点失败

模型在 summary rollup 里报的数与 detail sheet 实际行数不吻合，且未说明排除/合并口径。这是数据类任务的**头号扣分点**。

- `crypto-backtest-chain-L4-002`（0.315，medium）：`summary_metrics` 报 `total_trades=17`，但 `state_audit` 显示处理了 24 个事件（含 `entry_fill`、`reject`、`force_exit`），`rule_exceptions` 另有 8 条；`nav_curve` 的 `closed_trades_to_date` 与 `trade_log` 的 closed 记录在日期上对不上。verdict：`hardcheck_denominator_matches_visible_populations`（fail）、`expanded_denominators_are_not_ambiguous`（fail）。
- `indicator-window-rules-L4-005`（0.630，medium）：summary `final_insufficient_history_count=18`，但 detail 实际 19 行；`review_row_count=160` vs detail 161 行；`risk_and_halt_overlap_count=40` 但 `risk_overrides` 只有 8 行满足同时 halted+风险。verdict：`hardcheck_denominator_matches_visible_populations`（fail）、`expanded_summary_uses_same_counts`（fail）。
- `effective-control-state-L5-036`（0.430，hard）：`summary.total_effective_steps=12/13`，但 4 个 control 的 `effective_steps` 相加为 14；`active_control_population` 无法与可见数组 reconcile。verdict：`hardcheck_denominator_matches`（fail）。
- `execution-closeout-reconcile-L4-003-successor`（0.733，medium）：summary 中 12 个账户的 `position_ok_count`/`position_break_count` 等列**完全重复相同值**（1, 0, …），疑似模板化；`execution_lifecycle` 出现 `executed_qty=200` 但 `confirmation` 记录对不上。verdict：`expanded_record_level_reasoning`（fail）、`expanded_material_contradictions`（fail）。

### 4.2 异常/例外记录未分离、未标记（无 status-reason-source / 无 exception rows）

模型把干净数据与需要人工复核的异常混在一起、直接丢进汇总，违背了"必须显式分离并说明"的合约。

- `health-quote-L2-003`（0.601，easy）：产物只有 `报价表` + `类别汇总` 两个 sheet，**没有把 missing price / excluded / 待复核行单独分离**，也没有 status-reason-source 三元组。verdict：`expanded_clean_and_exception_populations`（fail）、`expanded_status_reason_source_triple`（fail）、`expanded_summary_rollups_preserve_material_exceptions`（fail）。
- `indicator-window-rules-L4-005`：4 个 sheet 中**完全没有 exception rows**，无任何失败/修复信息。verdict：`hardcheck_exception_rows_have`（fail）。
- `execution-closeout-reconcile`：多条 **关键空白字段未解释**（`portfolio_id`、`linked_exec_ids` 为空），且异常表 18 条连续记录用完全相同的 `NO_EXEC_MATCH` 模板。verdict：`critical_blanks_explained`（fail）。

### 4.3 多来源拼接无单一权威 / inactive 材料混入 active 列表

文档/控制类任务要求明确"最高有效权威"，模型输出了 concat 字符串及被废弃材料。

- `effective-control-state-L5-036`（0.430，hard）：`source_doc` 写成 `controlled/base_PROC-CC-036_rev3.md + site/signed_site_appendix_SC-2026-07.md` 这种拼接，未指明赢家；`authority_level` 含解释性散文（`signed_site_appendix (highest applicable)`、`regulatory_notice > base_sop`）而非规范 token。verdict：`control_control_level_source_docs`（fail）、`control_authority_level_uses`（fail）。
- 同任务：`source_docs_used` 把 `archive/…rev1_archived.md`、`archive/…rev2_archived.md`、`draft/unsigned_…`、FAQ-only、future-training 等 inactive 材料与当前有效来源混在同一平面列表。verdict：`current_source_docs_exclude`（fail）、`metadata_source_docs_have`（fail）。

### 4.4 必需 artifact 缺失 / 被安全检测扣留

- `cloudagent-sdk-doc-validation-report`（0.693，hard）：3 次 attempt 全部 fail，其中一次因确定性 safety/leakage 检查失败（`safety_leakage: 1`）导致 `submitted_validation_report_text` **被扣留、未提供给 judge**，后续 `triangulation_clari`/`recommendation_acti`/`unresolved_blocker_`/`unsupported_specula` 全部因"required Judge evidence missing"返回 fail。其余尝试则直接"required artifact submitted_validation_report_text is missing or has no faithful text"。
- `crypto-backtest-chain-L4-002` 的 attempt 1（reward 0.0）：`backtest_workbook` 未提交，13 条 verdict 全 fail（rule + llm 全部）。

### 4.5 推断被当作已确认事实 / 源码锚点误读（caveat discipline）

- `deepep-api-source-anchor-explain`（0.833，hard）：attempt 1（0.85）`caveat_discipline_for_public`：报告把 `src/backend/deferred_route_backend.cpp` 中 `park_deferred_route` 函数描述为已确认，但 cited extract 并未支持；attempt 3（0.76）`semantic_source_fidelity`：声称 `api.py` 存在 `route = route_async` 别名，但锚点仅显示 `from nimbuspipe.routing.route_async import …`。

### 4.6 召回坍塌（一次性整体失败，非渐进）

- `delivery-package-readonly-diff` attempt 3（**0.102**）：检查项总数从 166 掉到 127，110 failed / 17 passed，失败项全部为 `missing_recall_MF-*`（如 `120_missing_recall_MF-0049_edu_toolkit_src_validation_rules_locale_rules_py`）。是 agent 少枚举了大量应覆盖文件导致的召回失败，而非局部错误。

## 5. 与 glm-5.2 的横向对比（同 50 任务、同为 3 attempt office run）

- **总分持平**：dsv4-flash **0.7710** vs glm-5.2 **0.7749**（Δ=+0.004，在噪声范围内）。
- **但胜负结构不对称**：以任务 reward 差 |Δ|>0.02 计，dsv4-flash **13 胜 / 12 平 / 25 负** —— 输的任务更多，但赢的任务赢得更大。
- **dsv4-flash 明显落后**的任务（Δ<-0.10）：

| 任务 | dsv | glm | Δ | category / difficulty |
|---|---|---|---|---|
| `crypto-backtest-chain-L4-002` | 0.315 | 0.759 | **-0.445** | data-file-ops / medium |
| `service-channel-ticket-daily` | 0.558 | 0.957 | **-0.400** | data-file-ops / medium |
| `delivery-package-readonly-diff` | 0.649 | 0.934 | -0.285 | automation-workdir / hard |
| `recruiting-search-skill-mock-mcp-hardened` | 0.539 | 0.763 | -0.224 | automation-workdir / medium |
| `health-quote-L2-003` | 0.601 | 0.822 | -0.221 | data-file-ops / easy |
| `rank-ic-topn-L4-001` | 0.665 | 0.772 | -0.108 | data-file-ops / medium |

- **dsv4-flash 明显领先**的任务（Δ>+0.10）：

| 任务 | dsv | glm | Δ | category / difficulty |
|---|---|---|---|---|
| `ticket-weekly-L3-010` | 0.948 | 0.230 | **+0.718** | data-file-ops / easy |
| `priority-sync-notification-pipeline` | 0.857 | 0.553 | +0.303 | automation-workdir / hard |
| `news-event-multifile-extract-L4-013` | 0.758 | 0.506 | +0.252 | doc-ops / medium |
| `deepep-api-source-anchor-explain` | 0.833 | 0.618 | +0.215 | automation-workdir / hard |
| `device-incident-attribution-L5-037` | 0.739 | 0.532 | +0.207 | data-file-ops / medium |
| `channel-period-compare-L4-017` | 0.855 | 0.648 | +0.206 | data-file-ops / medium |

> 注：glm-5.2 的 office run（`2026-08-28__11-27-07`）包含 pre-inplace / attempt-history 变体目录，本次对比读取的是 canonical 主目录（150 trial 全部解析）。该 run 有 1 个 errored trial，但 50 任务 × 3 attempt 均已覆盖。对比用于方向性判断，非严格置信区间。

## 6. 代表性案例

### 6.1 强项：`drug-inventory-split-L2-006`（0.959，data-file-ops / easy）
三次 attempt 分别为 0.945 / 0.959 / 0.972，reward 稳定且接近上限。说明模型在**单一数据结构、规则清晰**的拆分/归一任务上表现可靠，是 office 上最稳的一类。

### 6.2 强项（相对 glm-5.2）：`ticket-weekly-L3-010`（0.948，data-file-ops / easy）
dsv4-flash **+0.718** 大幅领先 glm-5.2（后者 0.230）。同任务不同模型的巨大差距说明该任务对"语义理解 + 结构化输出"的分辨度极高，dsv4-flash 在此类任务上有明显优势。

### 6.3 弱项：`crypto-backtest-chain-L4-002`（0.315，data-file-ops / medium）
最低分任务。三次 attempt = 0.0 / 0.480 / 0.464：
- attempt 1 全 0：`backtest_workbook` artifact 未提交（或无法被判读），13 条 verdict 全 fail。
- attempt 2/3 部分通过（15/33 tests），但 judge 判定多处**汇总口径不一致**（17 trades vs 24 events；`daily_positions` 全为 `open` 状态、无 `closed`/`no-position`/`paused`；`entry_signal_ref`/`entry_order_ref` 存在空白值无说明）。
- 对 glm-5.2（0.759）落后 **-0.445**，是该 run 最大单点短板。

### 6.4 高方差：`delivery-package-readonly-diff`（mean 0.649，automation-workdir / hard）
- attempt 1（0.934，155/166）、attempt 2（0.910，151/166）表现优秀。
- attempt 3（**0.102**，17/127）：一次**召回坍塌**，大量 `missing_recall_MF-*` 失败，覆盖文件清单大幅收窄。**同一任务一次做对、一次几乎全错**，说明稳定性而非能力缺陷。

### 6.5 弱项+证据扣留：`cloudagent-sdk-doc-validation-report`（0.693，automation-workdir / hard）
3 次 attempt 全部 fail，但诱因是一条**确定性 safety/leakage 检查失败**（`safety_leakage: 1`）把 `validation_report.md` 从 judge 证据中扣留，导致 5 条 rubric 全部"无证据可评估"而 fail。这类失败**不应完全归因于模型能力**——产物本身存在，是安全过滤触发导致 judge 拿不到内容。

### 6.6 代表中段：`execution-closeout-reconcile-L4-003-successor`（0.733，data-file-ops / medium）
典型的多错叠加：summary 列值模板化重复（12 账户同值）、关键空白字段未解释（`portfolio_id`/`linked_exec_ids`）、异常表 18 条同质 `NO_EXEC_MATCH`、冲突记录未同时列出"被选中"与"被拒绝"的备选。是横跨 §4.1/§4.2/§4.3 三类签名的一个综合样本。

## 7. 改进建议

1. **产出前做"汇总↔明细"对账**：数据类任务（尤其 `data-file-ops` 的 medium）在写 summary rollup 前，强制用脚本把 detail sheet 行数重算一遍再填入，避免 `17 vs 24`、`18 vs 19` 这类最频繁的扣分。可针对性覆盖 `crypto-backtest-chain`、`indicator-window-rules`、`effective-control-state`。
2. **显式分离并标记异常/例外记录**：即使没有明确异常，也应在产物中提供一个空的"exception/review"区或一条 status-reason-source 说明，而不是把所有数据混在一个 sheet。重点命中 `health-quote-L2-003`、`indicator-window-rules`、`execution-closeout`。
3. **控制类任务指明"最高权威"而非拼接/散文**：`source_doc` 用单一规范 token、`authority_level` 用枚举值、把 archive/draft/inactive 材料从 `source_docs_used` 中剔除或用显式状态区分——直接修复 `effective-control-state-L5-036`。
4. **对源码锚点保持 caveat discipline**：不要把"未在 cited extract 中出现"的结论写成已确认事实；涉及锚点引用时逐条核对。修复 `deepep-api-source-anchor-explain`。
5. **针对 `delivery-package-readonly-diff` 的召回坍塌，增加覆盖度自检**：在提交前枚举应覆盖的 `MF-*` 清单与产物里实际出现的清单做 diff。这类一次性全错是最浪费 score 的失败。
6. **注意安全过滤误伤**：`cloudagent-sdk-doc-validation-report` 这类因 `safety_leakage` 扣留证据导致的 judge 全 fail，建议在评测侧将其归为 verifier 事件而非模型短板，避免误判。
