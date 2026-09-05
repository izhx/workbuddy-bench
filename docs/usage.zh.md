# WorkBuddy Bench 使用文档（草稿）

本文只保留日常配置、运行、恢复和算分所需的最短流程。以下命令均在仓库根目录执行。

## 1. 配置环境

### 1.1 宿主机环境

按项目 [README](../README.zh.md) 准备 Python 3.12、uv 和 Docker，然后创建环境、安装依赖并配置凭据：

```bash
uv venv --python 3.12
uv sync
cp .env.example .env
```

在 `.env` 中填写模型服务地址和密钥。数据集按需下载到 `datasets/`：

```bash
./scripts/dataset/fetch-dataset.sh code   # 也可用 web、office、sec 或 all
```

下载脚本会校验并解压数据，详情见 [`datasets/README.md`](../datasets/README.md)。

### 1.2 在 Docker 容器中运行项目

也可以准备一个包含 Python 3.12、uv、Docker CLI、Compose/Buildx，以及 `ps`、`fuser`、`setsid` 的控制镜像，通过
[`configs/controller.compose.yaml`](../configs/controller.compose.yaml) 启动项目。控制容器复用宿主机
Docker daemon，因此不需要在镜像内启动 Docker daemon。

```bash
export WB_REPO_ROOT="$(pwd -P)"
export WB_DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
export WB_CONTROLLER_IMAGE=<controller-image:tag>
export DEV_DATA_ROOT=<absolute-agent-data-dir>

docker compose -f configs/controller.compose.yaml config --quiet
docker compose -f configs/controller.compose.yaml up -d
docker compose -f configs/controller.compose.yaml exec controller bash
```

`$DEV_DATA_ROOT/dot-claude` 和 `$DEV_DATA_ROOT/dot-codex` 需要预先存在。进入容器后，环境和数据配置与
1.1 相同，不再重复。仓库在容器内必须保持与宿主机相同的绝对路径，否则宿主 Docker daemon 无法正确
解析 task 容器的 bind mount。挂载 `docker.sock` 权限很高，只应给可信的控制容器使用。

### 1.3 配置模型和实验

从模板创建 model YAML，填写后端模型、协议以及 `.env` 中对应的地址和密钥变量名：

```bash
cp configs/models/_template.model.yaml configs/models/<model-slug>.yaml
```

再从模板创建 job YAML，组合 model、harness 和 dataset：

```bash
cp configs/jobs/_template.job.yaml configs/jobs/<job-slug>.yaml
```

最小 job 配置如下：

```yaml
model: <model-slug>
harness: <harness-family>/<version>
dataset: datasets/<dataset-version>/tasks
harness_backend: local
model_connection: local_proxy
```

完整字段见 [`configs/README.zh.md`](../configs/README.zh.md) 和
[`configs/jobs/_reference.yaml`](../configs/jobs/_reference.yaml)。

## 2. 手工使用

### 2.1 运行和恢复实验

先 dry-run 检查解析后的配置，再正式启动：

```bash
uv run ./scripts/run.sh --job <job-slug> --dry-run
uv run ./scripts/run.sh --job <job-slug>
```

结果默认写入 `results/<job-slug>/<experiment-dir>/`。

启用 `record_full_io: true` 时，必须使用 `model_connection: local_proxy`，且不能设置 `SHARED_PROXY=1`。
`run.sh` 会在评测进程组和 private proxy 退出后拆分日志，按实际记录生成：

- `<trial>/agent/requests.jsonl`：agent 请求和响应。
- `<trial>/verifier/requests.jsonl`：`in_container` judge 请求和响应。

归档也包含原地恢复移入对应 `.attempt-history` 的 trial。runtime YAML 保存在本次运行的
`scripts/logs/instances/<instance-id>/jobs/` 下，避免同一个 job 后续启动时覆盖它。
如果只需手工重做拆分，先确认评测及该 proxy 已停止，再执行：

```bash
uv run python -m workbuddy_bench.runner.split_proxy_log \
  --manifest scripts/logs/instances/<instance-id>/manifest.json \
  --job-dir results/<job-slug>/<experiment-dir>
```

重复拆分不会重复追加已归档的请求；无法确认运行、trial 或请求用途的记录保留在运行级源日志中，默认位于
`scripts/logs/proxy/`，可由 `PROXY_LOG_DIR` 覆盖；手工拆分时用 `--log-dir` 指定对应目录。旧版实例日志文件名仍支持。
原地恢复的 state 目录使用 `<instance-id>-resume-<pid>-<timestamp>`，应使用对应目录中的 manifest。

旧公共 `proxy_requests.jsonl` 不参与自动拆分。`host_side` post judge 不纳入本次 judge 归档支持。

如果后续需要 `--resume-in-place`，初次运行就必须使用预构建 task 镜像，并保持相同 tag：

```bash
uv run python -m workbuddy_bench.runner.task_images build \
  datasets/<dataset-version>/tasks --tag <YYYY-MM-DD>

NO_FORCE_BUILD=1 uv run ./scripts/run.sh \
  --job <job-slug> \
  --task-image-tag <YYYY-MM-DD>
```

中断后先预览恢复计划，再执行原地恢复：

```bash
NO_FORCE_BUILD=1 uv run ./scripts/run.sh \
  --job <job-slug> \
  --task-image-tag <YYYY-MM-DD> \
  --resume-in-place results/<job-slug>/<experiment-dir> \
  --max-extra-attempts <N> \
  --dry-run

NO_FORCE_BUILD=1 uv run ./scripts/run.sh \
  --job <job-slug> \
  --task-image-tag <YYYY-MM-DD> \
  --resume-in-place results/<job-slug>/<experiment-dir> \
  --max-extra-attempts <N>
```

该模式保留有效 trial，只补原计划中的缺口；reward 为 `0` 的正常 trial 也算有效，不会为了提高分数而
重跑。详细约束见 [`docs/resume-in-place.md`](resume-in-place.md)。

### 2.2 计算实验结果

对一个准确的单次运行目录执行：

```bash
uv run python -m workbuddy_bench.scorer.metrics \
  results/<job-slug>/<experiment-dir> --json
```

如需把未运行的计划任务也计入分母，再传入对应的 `--manifest <manifest.json>`。主要关注 `reward`、
`pass_rate`、`n_tasks`、`n_trials`、`attempts_per_task` 和 `missing_tasks`。

## 3. 通过 Agent 使用

### 3.1 使用 `wbb-runner` 运行实验

`wbb-runner` 只使用预构建 task 镜像。需要提供 job slug、明确的镜像 tag、运行模式和补跑预算：

```text
$wbb-runner 使用 handoff 模式运行 job <job-slug>，预构建镜像 tag 为 <YYYY-MM-DD>，补跑总预算为 30。
```

- `handoff`：完成 dry-run、镜像和启动检查后返回，后续按请求检查。
- `managed`：持续等待实验结束，并在预算内使用 `--resume-in-place` 修复无效 trial。

Agent 会记录 manifest、operator log、实验目录和 `wbb-runner-state.json`。完成条件是每个 task 都有
3 个有效 attempt，即最终 `attempts_needed=0` 且 `valid=planned`；不会为了获得更高 reward 重跑有效
结果。已有实验也可以直接请求：

```text
$wbb-runner 使用 managed 模式继续实验 results/<job>/<experiment>，job 为 <job-slug>，镜像 tag 为 <YYYY-MM-DD>，补跑总预算为 12。
```

详细说明见 [`docs/wbb-runner.md`](wbb-runner.md)。

### 3.2 使用 skill 计算结果并输出报告

Office、Web、Code 和 SEC 都可以使用 `wbbench-score-job`：

```text
$wbbench-score-job 分析 results/<job-slug>/<experiment-dir>，输出中文报告。
```

该 skill 只读分析 Harbor 产物，计算 task-balanced score、检查缺失 attempt 和运行异常，并输出：

```text
<RUN_DIR>/report-wbb/<UTC时间>/
  score-analysis.json
  score-report.md
```

需要 Office、Web 或 Code 的进一步轨迹分析时，可以改用 `wbbench-report-skills`；它默认输出
`<RUN_DIR>/report/metrics.json` 和 `<RUN_DIR>/report/report.md`。输入必须是准确的单次运行目录；如果
一个 job 下有多个运行目录，不应让 Agent 自动选择最新的一次。
