# 预构建 Task 镜像

预构建镜像用于提前构建各 task 的 `environment/`，评测时直接复用本地镜像，减少重复构建时间和外部网络波动。该能力是显式启用的，不修改原始 dataset，也不改变 Harbor 原有的构建逻辑。

## 设计概览

每个 task 对应一个本地镜像：

```text
<dataset-id>/<normalized-task-name>:<tag>
```

例如：

```text
wb-bench-office-v1.0/rule-based-stock-exclusion-l4-011:2026-08-27
```

- `dataset-id` 优先取自 `dataset.toml`。
- task 名会转成小写、Docker-safe 的 repository component；归一化后重名会直接报错。
- tag 只允许 `latest` 或真实日期 `YYYY-MM-DD`。
- 推荐使用日期 tag 保存稳定快照；`latest` 是可变 tag。

构建上下文是 task 的 `environment/`。镜像构建后会带上三个 label：

```text
workbuddy-bench.task-source-sha256
workbuddy-bench.dataset
workbuddy-bench.task
```

评测启动时，runner 只在 staged dataset 副本的 `task.toml` 中注入：

```toml
[environment]
docker_image = "<resolved-image-reference>"
```

原始 dataset 和 benchmark manifest 都不会写入镜像 tag、引用或 source hash。

## Source hash 与校验范围

`workbuddy-bench.task-source-sha256` 是 WorkBuddy Bench 为预构建镜像定义的完整 SHA-256。它对 `environment/` 下排序后的相对路径和文件内容计算 hash，并使用长度前缀避免不同路径、内容组合发生拼接歧义。

以下内容不参与计算：

- `.git/`
- `__pycache__/`
- `.DS_Store`
- `docker-compose.yaml`

`docker-compose.yaml` 按当前约定只负责 runtime 编排，而且 staged dataset 会为它注入 host-gateway 等配置，因此不应让这类运行时改动使镜像失效。符号链接则按“链接路径及目标字符串”参与计算。

`preflight` 使用本地 `docker image inspect`，比较镜像上的 source hash、dataset 和 task label。它不拉取镜像，也不重新计算镜像文件系统的 hash。

校验通过表示：在镜像由本仓库 builder 构建、label 未被篡改的受控流程中，镜像声明的 `environment/` 源码快照与当前源码一致。因此它适合作为本地镜像 freshness 和来源对应关系校验，但不是严格的供应链证明，也不证明构建可复现。

它不覆盖：

- 可变 base image；
- 构建时网络下载或其他外部输入；
- build arg、时间等非源码状态；
- 构建后修改镜像但保留原 label；
- `environment/` 之外的 instruction、tests 和 solution。

如果需要密码学意义上的来源证明，应额外记录并签名 `source hash -> image digest` 的映射。

## 与 Harbor 自身 hash 的区别

Harbor runtime 也会计算 `environment_content_hash`，但它用于生成 Harbor 自建镜像名 `hb__<environment-id>`，不是用于检查外部预构建镜像。

| 项目 | Prebuilt source hash | Harbor environment hash |
|---|---|---|
| 用途 | 校验指定预构建镜像是否匹配当前源码 | 标识 Harbor 自己构建的 environment |
| 保存方式 | 写入镜像 label | 用在 `hb__<environment-id>` 镜像名中 |
| 长度 | 64 位十六进制 | 默认截断为 32 位十六进制 |
| `docker-compose.yaml` | 排除 | 包含 |
| 符号链接 | 记录链接目标 | 忽略 |
| 编码 | 路径和内容使用 8-byte 长度前缀 | 使用 4-byte 长度前缀 |
| 检查预构建镜像 | 是 | 否 |

两者虽然都是 SHA-256，但输入和编码不同，结果不能比较或互换。Harbor 的 `task_checksum` / `TrialLock.task.digest` 又是整个任务的身份 hash，会覆盖 `task.toml`、instruction、tests 等内容，主要用于结果追踪和续跑，也不应代替镜像 source hash。

## 使用方法

以下命令都从仓库根目录执行，并始终显式提供 tag。

### 1. 查看目标

查看全部 task 的镜像引用和 source hash：

```bash
uv run python -m workbuddy_bench.runner.task_images list \
  datasets/wb-bench-office-v1.0/tasks --tag 2026-08-27
```

只查看指定 task：

```bash
uv run python -m workbuddy_bench.runner.task_images list \
  datasets/wb-bench-office-v1.0/tasks --tag 2026-08-27 \
  --include-task rule-based-stock-exclusion-L4-011
```

`--include-task` 可以重复传入。省略时处理该路径下的全部 task。

### 2. 构建镜像

```bash
uv run python -m workbuddy_bench.runner.task_images build \
  datasets/wb-bench-office-v1.0/tasks --tag 2026-08-27 \
  --include-task rule-based-stock-exclusion-L4-011
```

builder 会复用 label 已匹配的镜像；缺失镜像会被构建。过期的 `latest` 可以自动重建，已有但 source 不匹配的日期 tag 默认视为不可变并报错。此时应优先使用新日期；只有明确需要覆盖该日期 tag 时才使用 `build --force`。

构建会继承常见的大小写代理环境变量为 Docker build args，并遵守 task `[environment].build_timeout_sec`。构建可能访问网络并修改本地 Docker image store。

### 3. 运行前校验

```bash
uv run python -m workbuddy_bench.runner.task_images preflight \
  datasets/wb-bench-office-v1.0/tasks --tag 2026-08-27 \
  --include-task rule-based-stock-exclusion-L4-011
```

任一镜像缺失、过期或没有受管 label 时，命令会返回非零。`run.sh --dry-run` 和正式运行都不会自动执行这一步，因此复用前应显式 preflight。

如果 job 配置了 `task_selection`，先生成 dry-run manifest，再按其精确选择校验：

```bash
uv run ./scripts/run.sh --job <slug> \
  --task-image-tag 2026-08-27 --dry-run

uv run python -m workbuddy_bench.runner.task_images preflight \
  <manifest中的dataset路径> --tag 2026-08-27 \
  --manifest <上一步输出的manifest.json>
```

### 4. 使用预构建镜像运行

复用需要同时满足两个条件：

1. 显式提供与构建、preflight 相同的 task-image tag；
2. Harbor 的有效配置为 `force_build=false`。

可在 job 中明确配置：

```yaml
environment_override:
  force_build: false
```

然后启动：

```bash
uv run ./scripts/run.sh --job <slug> \
  --task-image-tag 2026-08-27
```

也可以只对本次运行强制关闭构建：

```bash
NO_FORCE_BUILD=1 uv run ./scripts/run.sh --job <slug> \
  --task-image-tag 2026-08-27
```

多个 job 可以顺序运行：

```bash
NO_FORCE_BUILD=1 scripts/run-jobs.sh \
  --task-image-tag 2026-08-27 <job-a> <job-b>
```

中断的单个 Harbor 实验也可以在原目录中补齐有效 trial：

```bash
NO_FORCE_BUILD=1 uv run ./scripts/run.sh \
  --job <slug> \
  --task-image-tag 2026-08-27 \
  --resume-in-place results/<job>/<experiment-dir> \
  --max-extra-attempts 6
```

原地恢复只接受一个实验目录，并要求旧实验的有效 Harbor 配置已经是
`force_build=false`。checksum 匹配且包含 reward 的 trial 会保留（reward 为 `0`
也有效）；不完整或缺少 reward 的 trial 会先归档到同级
`<experiment>.attempt-history/`，再在原实验目录
补相应的 planned slot。`--dry-run` 会打印补跑计划并执行镜像 preflight，不会修改
实验目录。镜像注入和 preflight 以旧 `lock.json` 的 planned task 为准，不使用当前
job YAML 中可能已经变化的 task selection。checksum 不匹配会直接失败；它也不会把 reward 为 `0` 的有效评测结果
反复重跑到通过。
旧 `config.json` 如果缺少 `job_name`，入口会补成当前实验目录名，并先保存
`config.json.before-in-place-resume`，确保 Harbor 0.18 仍写入指定目录。

当前 `configs/bench/_default.yaml` 的默认值已经是 `force_build: false`，但在 job 或命令中显式表达复用意图可以避免配置默认值变化带来的歧义。

## 运行规则与常见问题

- 未提供 `--task-image-tag` 或 `TASK_IMAGE_TAG`：不注入 `docker_image`，Harbor 仍从 task Dockerfile 构建。
- job 配置了 `task_selection`：preflight 与运行时注入都只处理 resolved manifest 的 `selected_tasks`。
- 提供 tag 且 `force_build=false`：Harbor 使用注入的预构建镜像。
- 提供 tag 但 `force_build=true`，且 task 有 Dockerfile：Harbor 忽略预构建引用并执行构建。
- `force_build=false` 但本地镜像缺失：Compose 可能尝试从 registry 拉取，失败后终止；不会回退到 Dockerfile 构建。
- task 的 `docker-compose.yaml` 中 `services.main` 不能声明 `build` 或 `image`，否则会覆盖预构建镜像契约并被 task-image CLI 拒绝。
- harness split-mount 镜像与 task 预构建镜像相互独立。

实现入口位于 `src/workbuddy_bench/runner/task_images.py`；镜像引用注入位于 `src/workbuddy_bench/runner/prepare_tasks.py`，运行参数入口位于 `scripts/run.sh`。
