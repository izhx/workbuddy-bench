# GLM-5.2（Claude Code）WB-Bench-Web 评测结果分析报告

> 分析日期：2026-09-03
> 运行目录：`results/glm-5.2.cc.web/2026-08-28__17-33-11`
> 模型：`glm-5.2`（`reasoning_effort: high`，Claude Code harness 2.1.187，`CcAgent`）
> 数据集：`wb-bench-web`，70 任务 × 3 attempts = 210 trial
> 评测时间：2026-08-28

---

## 1. 数据与方法核对

| 项 | 值 |
|---|---|
| 运行路径 | `results/glm-5.2.cc.web/2026-08-28__17-33-11` |
| 模型 / harness | `glm-5.2` / `cc`（`CcAgent`, claude-code 2.1.187） |
| 数据集 | `wb-bench-web`（70 任务） |
| 任务数 / trial 数 | 70 / 210 |
| attempts/task | 3（`attempts_per_task=[3]`） |
| `reward`（主分） | **0.4467** |
| `pass_rate`（全测试通过率） | **0.1905** |
| `missing_tasks` | 0（70 任务全部有 trial） |
| `score_sources` | `{'reward': 210}` → 全部 trial 均走 reward 路径，**0 个 build_error** |

**`reward` 与 `pass_rate` 语义**（来自 `metrics.json` 的 `definitions`）：
- `reward`：主分数，每个任务取其 3 次 attempt 的均值，再对 70 任务求均值（build_error 计 0）。本运行 **无 build_error**。
- `pass_rate`：`score >= 1.0` 的 trial 占比（本运行 0.1905）。**`reward` ≠ `pass_rate`**，两者口径不同。

**数据完整性**：210 个 trial 全部有 `score.json`（210/210）、全部有 `reward.json`（210/210），无缺失；`missing_tasks=[]`。**无 build_error，无 verifier API 非零退出（`llm_judge_exit`/`rule_judge_exit` 全部为 0）。** 本次是干净、完整的一次运行。

### 评分机制（重要，影响对分数的理解）

本轮使用 `CompositeVerifier` 的 **固定判官（`penalty_fixed_judge`）** 评分策略。经对全部 210 个 `score.json` 的 `dimensions._penalty` 字段核验：

- **`scoring_reason` 全部为 `penalty_sum`**（210/210）。
- 每个任务由一组 check item 组成，每个 item 有独立权重：**major=0.3 / normal=0.2 / minor=0.1**。
- **`reward = max(0, 1.0 − Σ 失败 item 的 penalty)`**，即**按失败 item 扣分**，而非按 `tests_passed/tests_total`。
- 因此 **reward 可能为 0 分，但 `tests_passed` 并非 0**。实测：**45 个 reward=0 的 trial，其 `tests_passed` 均 ≥2**（最高 13/13 还得分 0.0）。这是理解本运行分数的**关键**——只看 `tests_passed` 会严重高估表现。

---

## 2. 执行摘要

| 指标 | 值 |
|---|---|
| `reward` | **0.4467** |
| `pass_rate` | **0.1905** |
| 满分任务（3 次 attempt 均 ≥1.0） | **8** 个 |
| 零分任务（3 次 attempt 均 =0） | **4** 个 |
| reward=1.0 的 trial | 40 个 |
| reward=0 的 trial | 50 个 |
| 单次 attempt reward | attempt1 0.4600 / attempt2 0.4829 / attempt3 0.3971 |

### top / bottom 任务（按任务 3 次 attempt 均值）

**Top 8（满分）**：
`claims-drawer-state-review-report`、`compile-repair-L4-008`、`experiment-result-story-L3-056`、`firmware-card-interaction-tests`、`markdown-release-editor-L4-038`、`miniprogram-source-structure-L2`、`runbook-handoff-conversion-L3-07`、`visual-qa-report-L3-048`。

**Bottom 4（零分）**：
`browser-clipper-extension-L4-005`、`config-diff-review-console-L3-06`、`mobile-booking-flow-L4-036`、`schedule-reschedule-flow-L4-061`。

**task 均值分布**：min 0.000 / p25 0.167 / median 0.433 / p75 0.667 / max 1.000。32 个任务 task 均值 ≥0.5，21 个 ≤0.2。

**最高方差任务**（3 次 attempt 差距大，反映不稳定而非稳定失败）：
`chart-generation-L2-025`（0/0.8/1.0，best 1.0 vs worst 0）、`release-note-migration-guide-L4`（0/1/1）、`atmosphere-game-L4-035`（0.1/0.2/1.0）、`csv-import-mapping-wizard-L4-037`（0/0.8/0）、`portfolio-showcase-L2-032`（0/0.8/0）。

---

## 3. 分类 / 模式 / 交互复杂度分解

（按 task 3 次 attempt 均值，括号为任务数）

### 3.1 按 main_category
| reward | 任务数 | 类别 |
|---|---|---|
| 0.7000 | 7 | Analytical Report |
| 0.6733 | 5 | Document Conversion |
| 0.4714 | 7 | Code Testing |
| 0.4667 | 6 | Page Implementation |
| 0.4296 | 9 | Visual Design |
| 0.4178 | 15 | Data Visualization |
| 0.3222 | 21 | Page Interaction |

**结论**：Analytical Report 与 Document Conversion 强（几乎不依赖顺畅的交互）；**Page Interaction（21 任务，最大类）最弱（0.3222）**，是所有类别里唯一显著低于平均的。

### 3.2 按 task_mode
| reward | 任务数 | 模式 |
|---|---|---|
| 0.7000 | 7 | Review & Analysis |
| 0.6733 | 5 | Format Conversion |
| 0.4714 | 7 | Test Generation |
| 0.3962 | 35 | From Scratch |
| 0.3792 | 8 | Bug Fix |
| 0.3500 | 8 | Extend Existing |

**结论**：**Review & Analysis / Format Conversion**（纯产出、无动态操作）最强；**From Scratch（35 任务，最大类）0.3962** 平庸；**Bug Fix / Extend Existing** 最弱（0.35–0.38）。方向一致：**任务越依赖"改写/扩展既有代码并保持行为"，得分越低**。

### 3.3 按 interaction_state_complexity
| reward | 任务数 | 复杂度 |
|---|---|---|
| 0.5947 | 25 | No Interaction |
| 0.5208 | 8 | Light Interaction |
| 0.3974 | 13 | Persistence/Offline/Cross-state |
| 0.3667 | 15 | Single-flow State |
| 0.1741 | 9 | **Multi-step Workflow** |

**结论（最强单调信号）**：**交互复杂度与得分强负相关**。`No Interaction` 0.59 → `Multi-step Workflow` **0.17**。**Multi-step Workflow（9 任务）是最弱的组，比 No Interaction 低 4.2 倍**。这是本次最一致的归因维度。

---

## 4. 失败模式归因

对全部 210 个 `score.json` 的失败 item 做统计：共 **550 次失败 item**，覆盖 **314 个不同 item id**。失败来源分布：`llm` 258、`vlm` 100、`rule` 66、`agent_judge` 50、`auto` 52、`judge_error` 24。失败权重：major 350 / normal 196 / minor 4。

### 主题一：**模型不支持图片输入 → API 400 → 全量截断（最硬、最系统的失败）**

这是本次**最严重且此前被误判**的失败模式。**33 / 210（15.7%）的 trial 触发了 `UnknownApiError`，其真实原因是 glm-5.2 的 API 不接受图片输入。** 完整因果链：

1. agent 在执行交互/视觉任务时，为验证渲染结果，用 Bash 截屏（Puppeteer/通过 Playwright 截图），得到 `.png`；
2. agent 用 `Read` 工具读该图片 → harness 把文件作为 **`{"type":"image","source":{"type":"base64","data":"iVBORw0KGgo…"}}` 内容块**返回给模型；
3. 模型把图片进请求 → **API 返回 `400 Backend returned 400`**（`cc-output.txt` 中以 `<synthetic>` 模型、`"API Error: 400 Backend returned 400"` 文本的形式记录，`"error":"unknown"`）；
4. `claude --print` 子进程 exit 1 → harn类 (harbor) 归类为 `UnknownApiError`；
5. **trial 立即截断**——33 个里 **32 个**在报错后**不再有任何后续 turn**（报错是 cc-output 的倒数第 2 行，其后只有 1 行 `result`）。

**量级**：
- 影响 **21 个任务**；其中 `data-storytelling-L4-034`、`product-landing-page-L3-033` **3 次 attempt 全部**被该错误杀死（无健康 attempt 兜底）。
- 这 33 个 trial 的 reward 均值 **0.3667**（远低于全部 trial 均值 0.4810）；其中 7 个 reward=0。分布在 `[0, 0.5]` 居多（30/33 ≤ 0.8）。
- **反事实测算**：把被图片错误杀死的 attempt 换成同一任务的健康 attempt 中位数，reward 由 **0.4467 → 0.4643**（**+0.0176，约 1.8 分**）。这是**评测设计（模型-API 能力不匹配）的单点可归因损失**。

**为什么会集中出现在 web 子集**：web 任务几乎都要求"看"渲染结果（视觉验证类任务需要截屏确认，或读配置/页面里的截图），而 glm-5.2 无视觉输入。模型**努力用视觉去自检，结果反而被 API 拒绝**——它是**模型自主努力的副产物**，而不是偷懒。

**注意这是一个"聪明反被聪明误"的信号**：早期把它归为"agent 命令失败/不致命"是错误的（见 §7 修正）。它直接指向需要**:要么给模型视觉能力，要么在 harness 里把 image 转成文本/禁用 Read 图片**。

### 主题二：`[auto] Not submitted` —— 流程后段未执行（强、系统）

有 **52 个失败 item 的 `actual` 明确为 "Not submitted"**，跨 19 个任务，且 25 次出现在 reward=0 的 trial 中。它们的 item id 全是**流程后段的状态验证**，例如：
`reserved_slot_survives_review_restart_path`、`optimistic_assign_rollback`、`wall_stress_protocol_passes`、`invalid_to_valid_recovery_reenables_preview`、`operator_type_change_clears_stale_value_and_recomputes_matches`、`nested_group_delete_updates_preview_and_export_without_stale_clauses`、`post_submit_slot_reserved_state`、`best_score_persists_across_reload`、`boundary_no_penetration`…

含义：**agent 把 UI 或前半段流程搭出来了，但从未真正走完后面那些"修改→重算→提交→持久化"的交互步骤**，导致 verifier 无证据可判而直接判 `Not submitted`（扣 full penalty）。案例：
- `rule-builder-preview-L4-043`：5 个 `Not submitted`（operator 编辑、嵌套分组删除、invalid→valid 恢复），reward 0。
- `support-inbox-triage-L3-040` / `particle-boundary-simulation-L3` / `review-card-queue-persistence-L4` / `snake-gameplay-implementation-L3`：各 4 个。

### 主题三：布局渲染后主内容区空白 / 缺视觉空状态 —— 视觉完整性不足

`sidepanel_empty_state`（browser-clipper，3 次失败）、`wizard_layout_readability`（csv-import）、`skeleton_shimmer_loading`、`countdown_a11y_live_region`、`aria_state_semantics`、`responsive_gallery_quality`。

以 `browser-clipper-extension-L4-005`（0 分）为例：规则判官 4 项里 `manifest_valid`/`permission_scope` 通过，VLM 截屏显示**侧栏有 UI 外壳（"0 clips" + 搜索框）、但主内容区为空白**——空状态/message 未实现。同时 workflow trace 显示 `click_text "save"/"cancel"` **全部超时**（页面根本没有 Save/Cancel 按钮可点），`successful_action_count=0`。**agent 建了 UI 壳、没建完整功能流**。

### 主题四：工作流 trace 中行动 errored —— 交互节点报错

`workflow_backoffice` 类、`schedule-reschedule-flow-L4-061`、`expense-wizard-state-repair-L4-0` 等：trace 显示 `click_text apply/confirm` 的 `status='error'`（如 `TypeError`、`Locator.click: Timeout`），导致后段状态断言失败。这类 item 的 `actual` 常以 "Workflow trace shows … action failed with status 'error'" 开头。

### 主题五：`judge_error` —— 判官自身异常（数量少，非主要因素）

24 次失败 item 来源为 `judge_error`，集中在 4 个 trial：`canvas-webgl-scene-L4-026__BLNCVdm`（9）、`portfolio-showcase-L2-032__MzgrERf`（6）、`portfolio-showcase-L2-032__LYzaMvn`（6）、`chart-generation-L2-025__9vTDxmo`（3）。item 多为 Canvas/视觉类（`canvas_nonblank_pixel_check`、`scene_graph_nodes_present`、`histogram_kde_visual_readability`、`typographic_hierarchy` 等）。这类是 **VLM/LLM 判官看图失败**（判官没产生有效判定），属评测工具噪声，**不是模型能力问题**，但数量仅占失败 item 的 ~4%。

### 主题六：低分 trial 中有一批"极廉价"attempt —— 非环境问题，是 agent 提前收工

有 6 个 reward=0 的 attempt **成本 <$2、输入 token <30 万**，最强信号：
- `portfolio-showcase-L2-032__LYzaMvn` **$0.33** / 56k in / **1,806 out**
- `chart-generation-L2-025__9vTDxmo` **$0.46** / 80k in / **2,193 out**
- `release-readiness-review-L4-019__236PbUC` $1.04 / 164k
- `canvas-webgl-scene-L4-026__BLNCVdm` $1.25
- `interaction-state-authoring-L3-0__njDiPaa` $1.40
- `csv-import-mapping-wizard-L4-037__kwy6aDb` $1.83

以 `portfolio-showcase-L2-032__LYzaMvn` 的 agent trajectory 为例：**23 步，只读到 public assets、建了 6 个 TaskCreate 任务清单，最后停在 "I'll write the complete index.html…" 却从未写出文件**；`cc-output.txt` 尾部是一长串 `thinking_tokens` 流（估计 token 停在 ~8,175，接近 8k 思考预算）——这是 **agent 把输出 token 大量消耗在思考、而没执行 artifact 写入** 的典型表现。注意这**不是 LLM API 错误**（CC 正常返回），是 **agent 在长思考后未能落地**。

**对比**：`chart-generation-L2-025` 里成功 attempt 成本 $2.39（0.4M in），而廉价失败 attempt 只 $0.46（80k in）——**差距在投入，不在 API**。

### 费用与失败相关性
| reward 桶 | n | 平均成本 |
|---|---|---|
| 0 | 50 | $14.07 |
| [0, 0.3) | 32 | $12.20 |
| [0.3, 0.7) | 49 | $10.36 |
| [0.7, 1) | 39 | $7.45 |
| 1.0 | 40 | $13.30 |

总成本 **$2,424.31**。整体规律：**reward 越低、平均越贵**（0 分 $14.07 vs 0.7–1.0 分 $7–13），但高分桶（1.0）因有意外的长任务（如 data-storytelling $62）被拉高，故并非单调。更多是**高方差**：每个 reward 桶内部成本跨度极大。真正的信号是上面那 6 个**极端廉价**的 0 分 attempt。

---

## 5. 代表性案例

### 案例 0（最硬故障：图片输入被拒）`canvas-webgl-scene-L4-026__A5h6GrA`（Data Visualization / From Scratch / No Interaction）— reward 0.5
**这是"模型读图 → API 400 → 全量截断"的完整、可复现样本。**

- 轨迹里 agent 完成 3D 雪山场景代码后，用 Puppeteer 截了 3 张图验证渲染（`/tmp/shots/desktop_before.png` 等）。
- 时间线（`agent/cc-output.txt` 行号）：
  - `11790` 行：agent `Read` 图片 `/tmp/shots/desktop_before.png`
  - `11791` 行：tool 返回 **`{"type":"image","source":{"type":"base64","data":"iVBORw0KGgo…"}}`**（base64 长度 70752）
  - `11792` 行：模型（`<synthetic>`）返回 **`"API Error: 400 Backend returned 400"`**，`"error":"unknown"`
  - `11793` 行：`result` 行 `is_error=True, api_error_status=400`，**`num_turns=35`，这就是文件最后一行**
- 整个 cc-output 只有 11793 行，**报错后没有任何后续 turn**。
- `exception_info.exception_type = UnknownApiError`，`exception_message = "Command failed (exit 1): … claude --print …"`。
- **代价**：该 trial 只跑到 reward **0.5**（`9/11` tests passed），$3.94 / 704k in / 16.6k out。agent 本意是"看渲染结果再修"，**结果因为看不进图而被掐死在验证环节**——这是**能力问题（无视觉）叠加反馈退化**，不是任务本身难。

**一句话**：这个案例说明 web 子集里有相当一部分失败，源头不是"模型不会做"，而是**"模型想看（截屏自检）却被 API 拒收，直接终止"**。

### 案例 1（弱，零分）`browser-clipper-extension-L4-005`（Page Interaction / From Scratch / Persistence/Offline/Cross-state）— reward 0
- 9/13 个 check 通过（`manifest_valid`、`permission_scope`、`clip_save_flow`、`tag_note_persistence`、`sidebar_clip_list` 等），但 4 项失败 penalty 累加 1.1 > 1.0，**reward 归零**。
- 失败项：`sidepanel_empty_state`（VLM：桌面+移动截屏显示 0 clips 时主区空白，无空状态提示）、`unsupported_page_fallback`（rule）、`storage_schema_stable`（rule）、`selected_text_saved`（LLM：无选中文本保存证据）。
- 截屏（`evidence/visual/T05-desktop.png`）：侧栏有 "Web Clipper / 0 clips / Clips / Audit / Search clips…"，**主内容区全白**。
- workflow trace：`click_text save` / `click_text cancel` **均 Timeout**，页面无这些按钮。**"UI 壳无功能流"**。

### 案例 2（偏弱）`mobile-booking-flow-L4-036`（Page Interaction / From Scratch / Multi-step Workflow）— reward 0
- 页面渲染 "Could not load the service catalog"，slot 一直 "Loading times…"，**service catalog 没加载出来**，整个多步预约流程在第一步就卡死。13 项检查几乎全失败（penalty 3.3）：`service_and_slot_data_used`、`sold_out_slot_disabled`、`form_validation`、`review_step_correct`、`submitted_payload_correct`、`mobile_visual_finish`、`submit_booking_id_visible`…
- 这是**一个根因（catalog 加载失败）连带拖垮全部下游检查**的典型，与"多步流程"组整体最弱相吻合。

### 案例 3（高方差，不稳定）`chart-generation-L2-025`（Data Visualization / From Scratch / No Interaction）— task mean 0.6
- 3 次 attempt = **0 / 0.8 / 1.0**。成功的 attempt 只花 $2.39（0.4M in）；**失败的 attempt 只花 $0.46（80k in）**、且含 3 个 `judge_error`。这是**同一任务下投入差异巨大、且混有判官噪声**的典型：不是能力边界，是单次 attempt 的执行状态/判官不稳定。

### 案例 4（偏强）`compile-repair-L4-008`（Page Implementation / Bug Fix / No Interaction）— reward 1.0
- 3/3 全满分，19/19 全通过。`build_break_fix` family，**无交互依赖**，agent 修复编译/构建问题干净利落。说明在**明确、无状态、无交互**的任务上，GLM-5.2 能稳定拿满分。

### 案例 5（偏强）`runbook-handoff-conversion-L3-07`（Document Conversion / Format Conversion / No Interaction）— reward 1.0
- 3/3 满分。文档格式转换（doc→md 等）任务，纯产出、无运行态校验，模型展示出强大的结构化转换能力。

### 案例 6（中等）`visual-qa-report-L3-048`（Analytical Report / Review & Analysis / No Interaction）— reward 1.0
- 3/3 满分。视觉 QA 报告生成，分析与产出类任务，模型在"读图→写分析"上表现出色。

---

## 6. 改进建议

结合类别（category）、模式（task_mode）、交互复杂度（interaction_state）与重复失败签名（**图片输入被 API 拒**、`[auto] Not submitted`、主区空白、trace action error）：

1. **面向评测方/模型侧（最优先）——解决"glm-5.2 无视觉输入"导致的 `image → 400` 截断**。33/210 trial（15.7%）因此被掐死，是**单点可归因损失（+0.0176）**。两条修复路径：a) 给模型一个**视觉能力**（或对 Read 图片做描述性 fallback：把图片喂给一个 VLM 生成文字描述，再把描述给主模型）；b) 至少让 harness **不要把 `image` 内容块原样塞给不支持图片的模型**——要么转成占位文本提示"这是一张图片"，要么让 agent 侧禁用 `Read` 图片、改用 `file`/`identify` 等命令读图片元数据（尺寸/pixel 采样）。这能避免"模型想看却被拒收、直接终止"的最硬故障。
2. **面向评测方——多步流程任务（Multi-step Workflow，9 任务，0.1741）应提供交互引导或更宽的证据采集**。这些任务的失败集中表现为 `[auto] Not submitted` + `click_text` 超时：verifier 用 Playwright 去点 Save/Submit/apply，但 agent 产出的页面**没有这些按钮**，或交互后状态未进入 verifier 期望的后段。建议这些任务在指令里显式列出**必须在页面上暴露的可用操作名/按钮文本**，避免 agent 造出功能不达标的 UI 壳。
3. **面向评测方——视觉/Canvas 类检查项的 `judge_error` 需降噪**。`canvas-webgl-scene`、`portfolio-showcase`、`chart-generation` 共 4 个 trial 因 **VLM 判官看图失败**（24 个 `judge_error` item）而额外丢分。这是评测工具噪声，不是模型能力；建议为判官增加重试/备用判官，或改用规则做 Canvas 检查。
4. **面向评测方——把"可自由命名的空状态/布局结构"从硬性判分点中松绑**。`browser-clipper` 的 `sidepanel_empty_state`、`csv-import` 的 `wizard_layout_readability`、`aria_state_semantics`、`skeleton_shimmer_loading` 等，是对"空状态长什么样"的强规范。若规范未在指令中给出，模型自由发挥的视觉结构会被误记。建议要么在指令给出明确的空间/文案结构，要么降低这类视觉判分权重。
5. **面向模型/agent 方——抑制"长思考不落地"**。6 个**极端廉价 0 分 attempt** 是 agent 把预算花在思考、没写出 artifact 的典型（`portfolio-showcase` $0.33 / 1,806 输出 token）。建议：a) 给 agent 加"先产出可运行 artifact 再结束"的硬约束；b) 捕获"最后消息是计划但未执行"的情况并强制补写；c) 对极短失败轨迹做重试。
6. **面向模型/agent 方——多步流程任务分阶段自检**。`mobile-booking-flow`（一个 catalog 加载失败拖垮 13 项）、`schedule-reschedule-flow`（TypeError 卡死）、`rule-builder-preview`（5 个 Not submitted）共同指向：**agent 未在每一步交互后验证"这一步真的生效了"**。建议 agent 在提交前主动用 Playwright 走一遍关键路径（点击→断言状态变化→再下一步），而不是只写完代码。
7. **面向模型/agent 方——扩展/修复类任务（Bug Fix / Extend Existing，0.35–0.38）需更保守地保持既有行为**。这些任务是"在既有代码上改"，最容易因大改破坏原有路径。

---

## 7. 附：环境噪声说明（含修正）

- **无 LLM API 错误**：全部 210 trial 无 build_error；`llm_judge_exit`/`rule_judge_exit` 全部为 0；判官流水线日志正常（LLM/VLM 正常响应，如 browser-clipper 9 item 28.1s）。
- **33 个 `UnknownApiError` ← 修正：根因是"图片输入被 API 400 拒收"，非普通命令失败**。初版误判为"agent 内部 Bash 命令 exit 1，不致命"。经全量轨迹核验（见 §4 主题一）：这 33 个 trial 的真实链路是 `Read 图片 → image 内容块进请求 → API 400 → claude --print exit 1 → UnknownApiError`，且 **32/33 在报错后立即截断**（没有后续 turn）。它们并**不是**已被其它 attempt 兜底的无害失败——其中 2 个任务三次 attempt 全部被此错误终止，21 个受影响任务的这些 trial 均值仅 0.3667，反事实可回收 **+0.0176 reward**。**结论修正：这是模型-API 能力不匹配的系统性损失，不是环境噪声。**
- **无 build_error**：0 个。
- 结论：**评测环境本身干净（verifier/judge 正常，无 build_error，无 judge API 报错）**，但 glm-5.2 不支持图片输入这一能力缺口，通过 harness `Read` 工具返回 image 内容块的路径，造成了可量化的系统性得分损失（约 1.8 分），这是本次分析识别出的最重要的**可修复问题**。

---

*注：报告基于 `results/glm-5.2.cc.web/2026-08-28__17-33-11/report/metrics.json` 及各 trial 的 `verifier/score.json` 全量证据。`metrics.json` 已由 `workbuddy_bench.scorer.metrics --json` 生成。*
