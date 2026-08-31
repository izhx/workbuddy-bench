# Harbor 实验原地恢复设计

本文说明 WorkBuddy Bench 的 `--resume-in-place` 模式，包括设计目标、命令入口、
trial 判定、补跑循环、预构建镜像约束、目录写入、失败恢复和已知风险。

对应实现：

- [`scripts/run.sh`](../scripts/run.sh)：用户入口、staging、镜像 preflight、proxy 和并发锁。
- [`in_place_resume.py`](../src/workbuddy_bench/runner/in_place_resume.py)：读取旧实验、生成恢复计划、归档无效 trial、调用 Harbor。
- [`run_post_judge.py`](../src/workbuddy_bench/runner/run_post_judge.py)：只对指定实验目录执行 host-side post-judge。

## 1. 目标与适用场景

该模式用于继续一个已经存在但未完整结束的 Harbor 实验，并把新结果写回同一个实验目录：

```text
results/<job>/<experiment-dir>/
```

它解决的是中断恢复，例如部分 trial：

- 没有 `result.json`；
- `result.json` 损坏；
- 被取消；
- verifier 没有写出 reward；
- agent 因模型 API、网络或容器故障崩溃，**即使 Harbor 已经写出了一个 reward**。

最后一类值得单独说明。agent 进程崩溃时 Harbor 仍会照常调用 verifier，于是 trial 里会留下一个
非空 reward（通常是 `0.0`，也可能是部分得分）。那个 reward 评的是一个空的或被截断的 workspace，
不是模型行为。把它当成有效结果会让分数系统性偏低，所以这类 trial 默认会被归档补跑。

原实验 `lock.json` 中的 planned trials 是最终目标。runner 保留已经有效的 trial，移走
无效 trial，再使用 Harbor 原生的 `harbor job resume --job-path` 补回空出的 planned slot。

该模式不负责：

- 从 A、B、C 多个实验累计或自动发现继承链；
- 创建新的实验目录；
- 把旧实验转换成不同的模型、dataset、task 或 proxy 配置；
- 一直重试到每个 task 获得若干个 reward `1`；
- 将原本使用 Dockerfile build 的实验转换成预构建镜像实验。

跨实验复用仍由 `--resume-job` 和 `sharded_eval` 负责，两者语义不同。

## 2. 使用方式

必须同时提供当前 job slug、一个显式预构建镜像 tag 和一个实验目录：

```bash
uv run ./scripts/run.sh \
  --job <slug> \
  --task-image-tag 2026-08-28 \
  --resume-in-place results/<job>/<experiment-dir>
```

建议先 dry-run：

```bash
uv run ./scripts/run.sh \
  --job <slug> \
  --task-image-tag 2026-08-28 \
  --resume-in-place results/<job>/<experiment-dir> \
  --max-extra-attempts 6 \
  --dry-run
```

`--resume-in-place` 支持仓库相对路径和绝对路径，只能出现一次。

`--max-extra-attempts N` 限制本次命令最多新启动多少个 trial。未指定时，默认 budget
等于旧实验 planned trial 总数。`N` 可以是 `0`，此时只允许已经完整的实验成功结束。

注意默认会把 API/网络/容器崩溃的 trial 也算进 `attempts_needed`，即使它们已经带有 reward。
这类 trial 往往数量不小，建议先 dry-run 看清 budget 需求，再决定 `--max-extra-attempts`。
崩溃判定的完整规则和相关开关见第 7 节。

## 3. 与 `--resume-job` 的区别

| 行为 | `--resume-in-place` | `--resume-job` |
|---|---|---|
| 写入目录 | 指定的原实验目录 | 新 Harbor 实验目录 |
| 输入实验 | 恰好一个 | 可以重复指定多个 |
| 计划来源 | 原实验 `lock.json` | 当前 job YAML 和 manifest |
| 部分 attempt | 只补原计划缺口 | 数量不足时整项 task 按当前 `n_attempts` 重跑 |
| 旧结果呈现 | 有效 trial 留在原处 | 复用 trial 链接到新结果根 |
| sharding | 不支持 | 通过 `sharded_eval` 支持 |
| 镜像策略 | 强制预构建镜像 | 由普通运行参数决定 |

## 4. 入口约束

`scripts/run.sh` 在 staging、proxy 启动和 Harbor 执行前检查：

1. `--job` 对应的 job YAML 存在。
2. `--resume-in-place` 指向一个实际目录。
3. 必须在命令行显式提供 `--task-image-tag`；只设置环境变量 `TASK_IMAGE_TAG` 不满足该要求。
4. 不能同时使用 `--resume-job`。
5. 不能设置 `SHARDS > 1`。
6. 不能设置 `DISABLE_VERIFICATION=1`。
7. 系统必须提供 `flock`。
8. 旧实验必须包含可读取的 `config.json`、`lock.json` 和至少一个 planned trial。
9. 旧配置必须只记录一个 WorkBuddy staged dataset 路径，格式为
   `.workspace/tmp/staged/<instance-id>/...`。

这些限制避免 native Harbor resume 在新目录、错误 dataset 或不同运行身份下启动。

## 5. 恢复身份与最小校验

该实现不计算完整的 job 配置指纹，也不要求当前 job YAML 与旧实验元数据逐字段一致。
它只校验继续执行所必需的身份：

- 指定实验的父目录必须等于旧 `config.json.jobs_dir`；
- 已记录的 `job_name` 必须等于实验目录名；
- staged dataset 中的 instance id 必须与 trial 中记录的 instance id 一致；
- 当前 job 的 `model_connection` 必须与旧 trial 一致；
- 如果旧 trial 记录了 model route，当前 route 必须与其一致；
- local proxy URL、host 和 port 必须保持一致；
- planned task 必须仍然存在；
- 已完成 trial 的 task checksum 必须匹配当前重新准备后的 task。

这是一组运行安全检查，不是实验 provenance 审计。当前 job YAML 中未影响上述运行身份的字段
可能已经变化，runner 不会对其做完整比较。

## 6. `lock.json` 是计划来源

planned 数量直接由旧 `lock.json.trials` 统计：

```text
planned_by_task[task_name] = 该 task 在 lock.json 中出现的 trial 数
planned_total = 所有 task planned 数量之和
```

当前 job YAML 中的 `task_selection` 不参与恢复目标计算。runner 会额外生成一个临时
`resume-task-manifest.json`，把 `selected_tasks` 设置为旧 `lock.json` 的 planned tasks，
专门用于镜像注入和 preflight。

因此，即使当前 job YAML 的 task selection 已经变化，原地恢复仍以旧实验计划为准；如果
旧 planned task 已从当前 dataset 删除，则直接失败。

## 7. trial 有效性判定

一个已有 trial 只有同时满足以下条件才保留在原实验目录：

1. trial 目录可识别；
2. `config.json` 存在、是合法 JSON object；
3. `result.json` 存在、是合法 JSON object；
4. `exception_info.exception_type` 不属于可重试崩溃集合（见下）；
5. `verifier_result.rewards.reward` 存在且不为 `null`；
6. `task_checksum` 与当前准备后的 task checksum 一致。

具体结果如下：

| trial 状态 | 行为 |
|---|---|
| 无 exception，reward 为正数 | checksum 匹配时保留 |
| 无 exception，reward 为 `0` | checksum 匹配时保留 |
| 无 exception，reward 为其他非 `null` JSON 值 | checksum 匹配时保留；当前宽松解析不额外限制类型 |
| reward 缺失或为 `null` | 归档后补跑，reason `missing_reward` |
| `CancelledError` | 归档后补跑，reason `cancelled` |
| 模型 API 崩溃（`UnknownApiError`、`ApiRateLimitError` 等） | 即使带有 reward 也归档后补跑，reason `crashed:<type>` |
| 网络或 agent 退出码崩溃（`NetworkConnectionError`、`NonZeroAgentExitCodeError`） | 同上 |
| 容器与环境故障（`RuntimeError`、`EnvironmentStartTimeoutError`、`OSError` 等） | 同上 |
| verifier 侧 reward 文件缺失或不可解析 | 同上 |
| `AgentTimeoutError`、`ContextLengthExceededError`、`OutputLengthExceededError` | 保留；这些是真实评测结果，不是崩溃 |
| 缺少或损坏 `config.json` | 归档后补跑 |
| 缺少或损坏 `result.json` | 归档后补跑 |
| 非空 reward 的 trial checksum 不匹配 | 立即终止，不归档任何 trial |
| trial 对应的 task 不在旧计划中 | 立即终止 |
| trial 目录数或某个 task 的 trial 数超过计划 | 立即终止 |
| 实验目录中出现无法识别的子目录 | 立即终止 |

这里的“有效”表示已有一个可计入评测的 reward，不表示 task 成功。reward `0` 是完整评测结果，
不会为了获得 reward `1` 被反复重跑。

### 崩溃与真实失败的界线

判定的依据是**这一次 attempt 有没有真的跑起来**，不是 reward 高低：

- agent 因为模型 API 报错、网络中断或容器起不来而崩掉时，Harbor 仍会跑 verifier 并写出 reward。
  这个 reward 评的是空 workspace，保留它等于把基础设施故障计成模型失败。这类 trial 会补跑。
- agent 自己耗尽 wall-clock 预算（`AgentTimeoutError`）或输出超长，是模型行为的一部分，reward 有效，
  不补跑。

默认可重试集合定义在 `in_place_resume.py` 的 `RETRYABLE_AGENT_EXCEPTIONS` 和
`RETRYABLE_INFRA_EXCEPTIONS`。任何以 `ApiError` 结尾的未见过类型也按崩溃处理，以覆盖 Harbor
后续新增的 provider 错误子类。

三个命令行开关可以调整该策略，均只在 `--resume-in-place` 下可用：

| 开关 | 含义 |
|---|---|
| `--retry-exception TYPE` | 追加一个按崩溃处理的 exception 类型，可重复 |
| `--keep-exception TYPE` | 把某类型当成有效结果保留，可重复；优先于 `--retry-exception` |
| `--no-retry-crashed` | 退回旧行为，只补跑 reward 缺失和 `CancelledError` |

`--no-retry-crashed` 与 `--retry-exception` 互斥；同一类型同时出现在 `--retry-exception` 和
`--keep-exception` 会直接报错。dry-run 与实际补跑使用同一组开关，因此打印的计划与真正会归档的
内容一致。计划输出会额外按 reason 汇总一行，例如：

```text
  archive reasons: cancelled=2 crashed:UnknownApiError=7 missing_reward=3
```

代码只要求 trial 配置和结果是可读取的 JSON object，并读取恢复所需字段；它不对每个旧文件
执行完整的 Harbor Pydantic schema 校验。这是有意保留的宽松元数据策略。

只要 result 中存在非 `null` reward 且能够识别 task，checksum 检查优先于归档原因。因此
即使该 trial 同时缺少 config 或标记为 cancelled，checksum 漂移仍会先终止整个恢复。

## 8. 补跑循环与 attempt budget

每轮计算：

```text
valid_total = 当前原实验目录内的有效 trial 总数
attempts_needed = planned_total - valid_total
```

执行顺序为：

1. 扫描已有 trial 并打印计划。
2. 如果 `attempts_needed == 0`，结束。
3. 如果本轮所需数量超过剩余 budget，返回失败，不归档、不启动半轮。
4. 把本轮无效 trial 移到 attempt history。
5. 执行 `harbor job resume --job-path <experiment>`。
6. 把本轮 `attempts_needed` 计入已使用 budget。
7. 重新扫描 Harbor 新写出的结果，直到完整或 budget 耗尽。

budget 是本次命令中新启动的 trial slot 总数，不是循环轮数。实现不会在剩余 budget 不足时
只启动部分 task，因为 Harbor native resume 负责按原 lock 计划匹配并补齐空缺。

例如原计划有 3 个 trial，当前有 1 个有效、2 个无效，默认 budget 为 3：

1. 第一轮归档 2 个无效 trial，并启动 2 个新 trial，已使用 budget 为 2。
2. 如果新结果有 1 个有效、1 个仍无 reward，下一轮需要 1 个，剩余 budget 为 1，可以再跑。
3. 如果最后一个仍无 reward，budget 已耗尽，命令失败；不会无限重试。

如果希望允许更多轮，必须显式增大 `--max-extra-attempts`。这仍然只追求“planned slots 都有
非空 reward”，不会追求“成功满 3 次”。后者会引入按结果选择样本的评分偏差，需要独立的
评测政策，不能作为中断恢复的隐式行为。

## 9. 预构建镜像约束

原地恢复强制使用预构建 task 镜像，不提供自动 build fallback：

- `run.sh` 强制设置 `NO_FORCE_BUILD=1`；
- 旧 `config.json.environment.force_build` 不能为 `true`；
- 旧 `lock.json` 中任何 trial 的 `environment.force_build` 都不能为 `true`；
- 每个 planned task 的 `task.toml` 必须注入精确的
  `<dataset-id>/<normalized-task-name>:<tag>`；
- 镜像 tag 必须与命令行 `--task-image-tag` 一致；
- `task_images preflight` 会检查镜像在本机存在，并验证 dataset、task 和 source hash labels；
- 缺失、过期或没有正确 labels 的镜像直接导致恢复失败，不会临时构建。

可以预先构建指定 dataset 的镜像：

```bash
uv run python -m workbuddy_bench.runner.task_images build \
  datasets/<dataset>/tasks \
  --tag 2026-08-28
```

原地恢复使用旧 `lock.json` 的 task 集合进行 preflight，而不是当前 job YAML 中可能已经变化的
task selection。

## 10. staged dataset 行为

旧 Harbor config 和 lock 记录的是：

```text
.workspace/tmp/staged/<recorded-instance-id>/<dataset>/tasks
```

恢复时 `run.sh`：

1. 从旧实验读取 recorded instance id 和完整 staged path；
2. 使用当前 `--job` 指向的 dataset 重新创建同一个 staged path；
3. 在 staged copy 中执行普通 task 准备和预构建镜像引用注入；
4. 要求最终 manifest dataset path 与旧记录完全相同；
5. 命令退出时删除该 staged copy。

源 dataset 不会被修改。`--dry-run` 在该模式下也必须实际创建 staged copy，才能计算 checksum
并检查精确镜像；退出时同样清理。

不要让其他进程同时使用相同 recorded instance id 的 staged path。

## 11. proxy 与模型行为

旧 trial 记录的 connection 是新 trial 的运行身份之一：

- `direct` 只能由当前仍为 `direct` 的 job 恢复；
- `local_proxy` 只能由当前仍为 `local_proxy` 的 job 恢复；
- model route 必须相同；
- proxy URL 必须相同。

对于 job-private proxy，runner 必须重新使用旧 host 和 port。如果该 port 已被占用，命令直接
失败，不会像普通新实验一样自动寻找下一个空闲端口。

对于 shared proxy，实际 URL 必须与旧 URL 相同，runner 会把当前 job 所需 routes 合并到
shared proxy 并 reload。

旧实验中的 instance/proxy 值会在加载 `.env` 后重新覆盖环境变量，因此 `.env` 不能把原地
恢复重定向到另一个 staging 路径或 proxy endpoint。

## 12. 原实验目录和归档目录的写入

假设目标实验为：

```text
results/<job>/<experiment>/
```

无效 trial 被移动到：

```text
results/<job>/<experiment>.attempt-history/
├── attempt-history.jsonl
└── <UTC timestamp>-round-<N>/
    └── <task>__<trial-id>/
```

`attempt-history.jsonl` 每行记录归档时间、round、task、trial、原因和目标路径。

有效 trial 不会复制或创建 symlink，仍留在原实验目录。Harbor 新 trial、job-level
`result.json` 和日志继续由 Harbor 写入原实验目录。

### 旧 `job_name` 的兼容处理

部分 Harbor 0.18 旧 `config.json` 没有保存 `job_name`。直接加载这种配置时，Harbor 会生成
新的当前时间戳，导致所谓 resume 写入新目录。

在第一次实际调用 Harbor 前，runner 会：

1. 确认 `config.json.jobs_dir` 指向目标实验的父目录；
2. 如果 `job_name` 缺失，把它设置为目标实验目录名；
3. 首次修改前保存 `config.json.before-in-place-resume`；
4. 如果备份已经存在，不覆盖它。

`--dry-run` 不修改 `config.json`。

## 13. 并发控制

每个实验使用一个同级锁文件：

```text
results/<job>/<experiment>.resume.lock
```

`flock -n` 持有锁直到整个 `run.sh` 退出。同一实验的第二个恢复进程会立即失败。锁文件本身
可能在进程退出后继续存在，但内核锁已经释放，文件无需删除。

不同实验使用不同 lock，不会因实验目录写入而直接冲突。但它们仍可能竞争：

- 相同的 recorded job-private proxy port；
- Docker、CPU、磁盘或模型并发额度；
- 被人为复用的相同 instance id/staged path。

因此不同实验理论上可并发，实际仍需先检查 proxy 和资源边界。

## 14. dry-run 的准确含义

该模式的 `--dry-run` 会执行：

- 读取并校验旧 config/lock；
- 获取实验锁；
- 重建 recorded staged dataset；
- 准备 staged tasks；
- 计算 checksum；
- 打印 planned、valid、invalid、missing 和 budget；
- 执行 planned task 镜像 preflight。

它不会：

- 移动旧 trial；
- 写入 attempt history；
- 补写 `config.json.job_name`；
- 启动 Harbor trial；
- 启动 job-private proxy；
- 调用 post-judge。

如果所需 attempts 已超过 `--max-extra-attempts`，dry-run 返回非零。

## 15. post-judge 和 proxy log

恢复成功后，如果 manifest 启用了 `host_side` LLM judge，`run.sh` 使用：

```text
run_post_judge --manifest <manifest> --job-dir <exact-experiment>
```

只 judge 本次指定的实验，避免扫描同一 `results/<job>/` 下其他时间戳实验或 attempt history。
`in_container` judge 仍由 verifier 内部执行。

随后普通 proxy-log split 流程仍会运行。归档目录不是当前原地 post-judge 的输入，但其他自行
编写的递归报表或 `rglob` 脚本如果从共享父目录扫描，仍可能读到 attempt history。生成指标时
应把目标指向原实验目录，并显式排除 `*.attempt-history`。

## 16. 失败场景与可恢复性

| 失败阶段 | 原实验状态 | 后续处理 |
|---|---|---|
| 参数、身份、checksum 或镜像检查失败 | trial 未移动 | 修正输入后重新 dry-run |
| budget 不足 | 本轮 trial 未移动 | 增大 budget 或保留现状 |
| 归档过程中中断 | 可能只移动了部分无效 trial | 检查 history 后重新 dry-run；缺少的 slot 会再次显示 |
| 归档完成、Harbor 尚未启动时中断 | 无效 trial 已在 history，原目录出现空 slot | 直接重新执行，Harbor 会补空 slot |
| Harbor 返回非零 | history 保留，原目录可能有部分新 trial | 检查新 trial 和 Harbor 日志后重新 dry-run |
| 新 trial 仍无 reward | 下一轮会再次判为无效 | 在 budget 允许时再次归档和补跑 |
| `job_name` 写入时异常 | 原文件可能受影响 | 使用 `config.json.before-in-place-resume` 检查和恢复 |

归档操作按 trial 逐个 `move`，不是一个文件系统事务；`attempt-history.jsonl` 也在每次移动后
逐行追加。因此进程在归档中途被强制终止时，目录和日志可能只完成一部分，但已移动的数据
仍然保留，可以人工核对。

不要直接删除 attempt history，除非已经确认不再需要失败轨迹和审计记录。

## 17. 输出与退出码

计划输出示例：

```text
In-place resume plan: planned=3 valid=1 invalid_to_archive=2 \
attempts_needed=2 extra_attempts_used=0/3
  archive reasons: crashed:UnknownApiError=1 missing_reward=1
  task-a: valid=1/3 invalid=2 missing=0
  archive task-a__trial-2: missing_reward
  archive task-a__trial-3: crashed:UnknownApiError
```

内部 runner 的主要退出语义：

- `0`：已经完整，或恢复后达到完整状态；dry-run 计划可执行时也返回 `0`；
- `1`：输入、身份、文件、checksum 或预构建镜像契约错误；
- `2`：attempt budget 不足；
- 其他非零值：转发 Harbor resume 的失败退出码。

`scripts/run.sh` 使用 `set -e`，因此恢复 runner 返回非零时不会继续执行 post-judge。

## 18. 检查命令

查看旧计划总数和每个 task 的 planned 数量：

```bash
jq -r '.trials[].task | (.name // (.path | split("/")[-1]))' \
  results/<job>/<experiment>/lock.json \
  | sort | uniq -c
```

按 exception 类型和 reward 有无交叉统计现有 trial，用来预估这次会补跑多少：

```bash
find results/<job>/<experiment> -mindepth 2 -maxdepth 2 -name result.json -print0 \
  | xargs -0 -r jq -r \
      '[(.exception_info.exception_type // "none"), \
        (if .verifier_result.rewards.reward == null then "null" else "has_reward" end)] \
       | @tsv' \
  | sort | uniq -c | sort -rn
```

`has_reward` 且 exception 为 `UnknownApiError` 一类的行，就是旧规则会静默计入、新规则会补跑的部分。

查看当前非空 reward：

```bash
find results/<job>/<experiment> -mindepth 2 -maxdepth 2 -name result.json -print0 \
  | xargs -0 -r jq -r \
      'select(.verifier_result.rewards.reward != null) \
       | [.task_id.path, .task_checksum, .verifier_result.rewards.reward] | @tsv'
```

查看归档记录：

```bash
jq . results/<job>/<experiment>.attempt-history/attempt-history.jsonl
```

再次运行前应以 `--dry-run` 输出为准，不要只按目录数量推断完成状态。

## 19. 已知限制与验证状态

- 只支持一个 recorded staged dataset 路径。
- 不支持 sharded in-place resume。
- 不支持多个 ancestor 的链式继承。
- 不计算完整配置指纹，也不保存额外 provenance snapshot。
- 不支持按 task 单独设置 attempt budget。
- 不支持“直到成功 N 次”的评测政策。
- 归档和 `job_name` 写入不是跨文件事务。
- 不会自动清理 attempt history 或 lock 文件。
- 崩溃判定基于 Harbor 的 `exception_type` 字符串，而不是异常类的继承关系；Harbor 若重命名
  异常类型，需要同步更新 `RETRYABLE_*` 集合（以 `ApiError` 结尾的兜底只覆盖 provider 错误）。
- 崩溃 trial 的补跑不区分故障原因是否已经排除。API 额度未恢复或镜像仍然缺失时，补跑会再次
  崩溃并消耗 budget，因此应先修复根因再补跑。
- 当前实现已有针对 trial 判定、崩溃重试策略、budget、归档、CLI 约束和精确 post-judge 的
  focused tests（`tests/test_in_place_resume_retry.py` 覆盖崩溃判定，`uv run pytest` 即可运行），
  但尚未使用真实
  benchmark 任务完成一次端到端原地恢复。因此生产运行前必须先对目标目录执行 dry-run，
  并建议保留文件系统快照或可恢复备份。
