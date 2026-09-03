# Code 子集 verifier 函数名与字段名约束审计

审计日期：2026-09-03  
审计对象：`datasets/wb-bench-code-v1.0`（80 个任务）

## 结论

Code 子集中确实存在 verifier 对题面未明确公开的函数名、类名或输出字段名做精确匹配的情况。

这意味着：agent 即使实现了等价行为，只要使用了其他合理名称或数据表示，对应 verifier 检查仍会失败。自定义 verifier 通常按通过检查数计算任务内 reward，因此多数情况是确定性扣分，不一定整题归零；如果错误名称导致入口导入或调用失败，则可能连带失败更多检查。

本轮识别出：

- 4 个明确的函数名或字段名过严任务；
- 2 个相关的隐藏 contract 风险任务；
- 没有发现整个 Code 子集普遍要求 gold patch 内部 helper 名称的情况。

## 扫描范围与方法

80 个任务的 verifier 类型为：

| verifier 类型 | 任务数 |
|---|---:|
| `script_verifier` | 54 |
| `pytest_injected` | 22 |
| `repo_understanding` | 4 |

扫描时将以下内容视为 agent 可据以实现的公开 contract：

- 任务的 `instruction.md`；
- `environment/workspace.tar.gz` 内的初始源码、README 和设计说明；
- 初始代码中已经存在的公共 API。

随后检查 `tests/verifier.py`、注入测试和 `gold.patch` 中的：

- 精确导入、`getattr()`、方法调用和属性访问；
- JSON/CSV 字段下标及固定字段值；
- 是否要求 gold patch 新增、但任务侧材料没有声明的名称；
- 精确名称不匹配时受影响的任务内检查数量。

本报告是静态 contract 审计，没有结合具体 agent 轨迹统计实际失败频率。

## 明确问题

### 1. `refactor-medium-request_merge_path`

题面要求整理 `RequestBuilder` 的合并流程，并明确写了“最终 build 出来的内容别变”：

- [instruction.md](../datasets/wb-bench-code-v1.0/tasks/refactor-medium-request_merge_path/instruction.md)

但 verifier 强制要求返回结果新增 `body_type` 字段：

- [verifier.py](../datasets/wb-bench-code-v1.0/tasks/refactor-medium-request_merge_path/tests/verifier.py)
- [gold.patch](../datasets/wb-bench-code-v1.0/tasks/refactor-medium-request_merge_path/tests/gold.patch)

影响：1/12 个任务内检查直接依赖 `body_type`。如果 agent 使用 `body_kind`、只返回 `body`，或者为了保持原返回结构而不增加字段，都会失败。

此外，verifier 还要求 method 转大写及自动设置 JSON `content-type`，这些行为也没有在题面中明确说明。

判断：这是最明确的题面与评分 contract 冲突。

### 2. `api_contract-hard-markup_errors`

题面要求固定 parse/render/escape 行为，以及异常携带 `code/position`：

- [instruction.md](../datasets/wb-bench-code-v1.0/tasks/api_contract-hard-markup_errors/instruction.md)

verifier 进一步固定了：

- 函数必须叫 `parse_markup()`；
- 必须公开名为 `Segment` 的类型；
- parse 结果必须通过 `.text` 和 `.style` 字段访问；
- error code 必须精确为 `unclosed_tag` 和 `unmatched_close`。

证据：

- [verifier.py](../datasets/wb-bench-code-v1.0/tasks/api_contract-hard-markup_errors/tests/verifier.py)
- [gold.patch](../datasets/wb-bench-code-v1.0/tasks/api_contract-hard-markup_errors/tests/gold.patch)

影响：仅 `Segment` 及其字段表示就牵连 6/12 个任务内检查。返回 tuple、dict 或其他等价 segment 对象的实现会被扣分。

判断：parse 的命名可以从已有 `render_markup()` 弱推断，但 `Segment` 类型及 `.text/.style` 返回结构没有公开 contract，属于明显过严。

### 3. `reliability-hard-stream_release`

题面只要求收口 read、迭代、close 和上下文管理路径：

- [instruction.md](../datasets/wb-bench-code-v1.0/tasks/reliability-hard-stream_release/instruction.md)

初始代码没有迭代 API，verifier 却固定调用 `ClientResponse.iter_chunks()`：

- [verifier.py](../datasets/wb-bench-code-v1.0/tasks/reliability-hard-stream_release/tests/verifier.py)
- [gold.patch](../datasets/wb-bench-code-v1.0/tasks/reliability-hard-stream_release/tests/gold.patch)

影响：3/12 个任务内检查依赖这个方法名。agent 使用 `__iter__()`、`iter_bytes()` 或其他合理接口，即使 release 行为正确，也会失败。

判断：应当公开方法签名，或者让 verifier 使用题面已经定义的迭代协议。

### 4. `model_evaluation-hard-multiclass_report`

题面要求输出 per-label precision/recall/f1/support，以及 macro、weighted 和 accuracy：

- [instruction.md](../datasets/wb-bench-code-v1.0/tasks/model_evaluation-hard-multiclass_report/instruction.md)

工作区 README 只声明顶层 `summary`、`labels`、`predictions`，以及 label/prediction 行字段，没有声明 summary 内部字段名。verifier 固定要求：

- `summary.macro_f1`；
- `summary.weighted_f1`；
- `summary.known_rows`；
- `summary.unknown_truth`。

证据：

- [verifier.py](../datasets/wb-bench-code-v1.0/tasks/model_evaluation-hard-multiclass_report/tests/verifier.py)
- [gold.patch](../datasets/wb-bench-code-v1.0/tasks/model_evaluation-hard-multiclass_report/tests/gold.patch)

影响：4/13 个任务内检查依赖这些精确字段名。其中 `known_rows` 和 `unknown_truth` 无法从题面直接推出；`macro_f1` 和 `weighted_f1` 虽然容易猜到，但仍未形成公开 JSON schema。

判断：如果要求稳定机器可读输出，应当公开完整字段 schema，而不是只在 verifier 中定义。

## 相关隐藏 contract 风险

### 5. `api_contract-hard-validation_errors`

题面公开了 `loc/msg/type/input` 字段名，但没有公开 `type` 字段的枚举值。verifier 精确要求：

- `missing`；
- `int_type`；
- `extra_forbidden`。

证据：

- [instruction.md](../datasets/wb-bench-code-v1.0/tasks/api_contract-hard-validation_errors/instruction.md)
- [verifier.py](../datasets/wb-bench-code-v1.0/tasks/api_contract-hard-validation_errors/tests/verifier.py)

影响：约 4/14 个检查依赖这些值。语义等价的 `value_error.missing` 或 `type_error.integer` 仍会失败。

判断：这不是函数名问题，但属于同类的未公开精确字段值 contract。

### 6. `api_contract-hard-openapi_params`

题面点名 alias、nullable、examples、deprecated，但 verifier 还要求：

- `description`；
- `format`；
- `default`；
- `style` / `explode`；
- path 参数强制 `required=true`。

证据：

- [instruction.md](../datasets/wb-bench-code-v1.0/tasks/api_contract-hard-openapi_params/instruction.md)
- [verifier.py](../datasets/wb-bench-code-v1.0/tasks/api_contract-hard-openapi_params/tests/verifier.py)

影响：约 5/12 个检查覆盖题面未明确列出的字段或规则。

判断：这些要求符合常见 OpenAPI 语义，因此不一定应该放宽 verifier；更合适的做法是把“保留所有已识别的 OpenAPI 参数字段”及具体字段写入公开 contract。

## 未发现普遍问题的部分

- `pytest_injected` 任务使用的 API 名大多是题面明确给出、初始仓库已有或上游公共 API；没有发现新的未公开函数名。
- `repo_understanding` 任务要求的 `analysis.json` 字段已经在题面中完整示例化，精确校验字段名是合理的。
- 数据清洗、数据报表、product analytics、Python port 等任务的大部分 CLI、文件名和输出字段，都在工作区 README 中明确声明。
- gold patch 中的内部 helper 名称通常不受 verifier 约束。例如 `_merge_headers`、`_merge_params`、`_body` 可以换成其他内部实现；真正被锁定的是外部调用和输出 contract。

因此，这不是“agent 只要没有照抄 gold patch 就失败”的系统性问题，而是少数任务把本应公开的 API/schema 只写在 verifier 中。

## 对总体成绩的理论影响

当前 Code 子集每个任务运行 3 次 attempt。正式聚合先计算同一任务内 attempts 的平均值，再对 80 个任务取平均：

- [Code bench 配置](../configs/bench/wb-bench-code-v1.0.yaml)
- [总体计分实现](../src/workbuddy_bench/scorer/metrics.py)

因此，当某个命名问题只影响一个 attempt 时：

```text
总体 reward 损失 = 该任务内 reward 损失 / 80 / 3
```

如果同一问题影响该任务的全部 3 次 attempt，则不再除以 3。

以下按“其他检查全部通过，3 次 attempt 都采用同一错误名称或结构”估算：

| 问题 | 单任务 reward 损失 | 总体 reward 损失 |
|---|---:|---:|
| `body_type` | 1/12，即 8.33% | 0.104 个百分点 |
| `Segment` / `.text` / `.style` | 6/12，即 50.00% | 0.625 个百分点 |
| `iter_chunks` | 3/12，即 25.00% | 0.313 个百分点 |
| multiclass 的 4 个 summary 字段 | 4/13，即 30.77% | 0.385 个百分点 |
| validation error 的精确 `type` 值 | 4/14，即 28.57% | 0.357 个百分点 |
| OpenAPI 未公开的附加字段和规则 | 5/12，即 41.67% | 0.521 个百分点 |

合计影响：

- 仅计算前 4 个明确问题：总体 `reward` 最多下降约 **1.43 个百分点**；
- 将 2 个隐藏 contract 风险也计入：最多下降约 **2.30 个百分点**；
- 如果 markup 实现连 `parse_markup` 名称也不同且没有 `Segment`，按现有 verifier 最坏会失败 11/12 个检查；此时前 4 个问题合计约 **1.95 个百分点**，全部 6 个约 **2.83 个百分点**。

如果每个问题只发生在 1/3 attempt，上述合计影响除以 3：

- 前 4 个明确问题约 **0.48 个百分点**；
- 全部 6 个约 **0.77 个百分点**；
- markup 取最坏命名情况时，分别约 **0.65** 和 **0.94 个百分点**。

### 对 `pass_rate` 的影响

`pass_rate` 只把 reward 等于 1 的 attempt 计为 full pass。即使只因一个未公开字段损失少量 reward，该 attempt 仍会整体计为非 full pass。

在这些 attempt 原本都能满分的前提下：

- 单个任务的 3 次 attempt 全受影响：总体 `pass_rate` 下降 1/80，即 **1.25 个百分点**；
- 4 个明确问题全部影响 3 次 attempt：下降 **5.0 个百分点**；
- 全部 6 个问题都影响 3 次 attempt：下降 **7.5 个百分点**；
- 如果每个任务只影响 1/3 attempt：前 4 个和全部 6 个分别下降约 **1.67** 和 **2.50 个百分点**。

这些数字是理论归因上限，不是某次现有实验的实际损失：

- 如果一个 attempt 原本就因其他检查未满分，命名问题不会再次降低 `pass_rate`；
- 多个失败原因可能落在同一个 verifier 检查中，实际 reward 影响不能简单重复相加；
- 计算实际影响需要指定确切 run，并逐个读取 trial 的 verifier 检查结果。

## 修订建议

按优先级建议：

1. 修复 `refactor-medium-request_merge_path` 的直接冲突：删除 `body_type` 检查，或者修改题面并明确这是新增返回字段。
2. 为 `markup_errors` 明确 `parse_markup()`、`Segment{text, style}` 和 error code 枚举；如果这些名称并非产品 contract，则 verifier 应改为行为检查。
3. 为 `stream_release` 明确 `ClientResponse.iter_chunks()` 签名，或改为已公开的通用迭代协议。
4. 在 `multiclass_report` 工作区 README 中补完整 `summary` JSON schema。
5. 为 validation error 补充 `type` 枚举值；为 OpenAPI 参数补充完整保留字段列表。
6. 避免一个检查同时绑定两个独立目标。例如 markup 的“正常 render”检查不应同时要求 `Segment` 类型存在。

后续可以增加一条数据集 lint：从 verifier AST 中提取精确导入名、属性名和输出字段名，检查它们是否在 `instruction.md` 或工作区 contract 文档中出现，再由人工确认公共 API 和普通测试数据之间的区别。
