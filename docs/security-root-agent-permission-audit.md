# Security 任务 Agent 权限审计与当前运行方案

> 状态：当前生效的临时兼容方案
> 审计日期：2026-08-24
> 适用范围：`datasets/wb-bench-sec-v1.0/tasks` 中的全部 60 个任务

## 当前方案

目前所有 Security 评测统一使用 root agent user 运行：

```yaml
agent_user: root
```

现有 Security job 已在 `configs/jobs/glm-5.2.cc.sec.yaml` 中显式设置该覆盖项。后续新增 Security job 时也应保持相同设置，直至本文列出的目录权限和服务启动问题完成修复并通过非 root 回归验证。

这是为了避免任务镜像中的 root-owned 工作目录导致 agent 无法写入规定产物，进而把运行环境故障误判为模型能力不足。该方案是临时兼容措施，不表示底层任务权限问题已经解决。

Code、Office 和 Web 数据集不受本问题影响，仍可使用默认的非 root `dev` agent user。

## 审计结论

本次检查覆盖了全部 260 个任务的 Dockerfile、任务说明和 verifier 输出路径。

| 数据集 | 任务数 | Dockerfile 工作目录非 `/workspace` | 确定写入失败 | 额外服务执行问题 |
|---|---:|---:|---:|---:|
| Code | 80 | 0 | 0 | 0 |
| Office | 50 | 0 | 0 | 0 |
| Web | 70 | 0 | 0 | 0 |
| Security | 60 | 60 | 35 | 5 |
| 合计 | 260 | 60 | 35 | 5 |

Security 的 Dockerfile 最终工作目录分布如下：

| 最终工作目录 | 任务数 |
|---|---:|
| `/workdir` | 27 |
| `/app` | 21 |
| `/opt` | 8 |
| 未显式设置，继承基础镜像 | 4 |

不能仅凭工作目录不是 `/workspace` 就判断任务一定失败。60 个 Security 任务中，35 个存在确定性的规定产物写入失败，5 个存在额外的服务执行问题，其余 20 个的规定产物位于可写的 `/logs/artifacts` 或 `/tmp`。

## 根因

默认 bench 配置使用：

```yaml
agent_user: dev
```

运行时辅助逻辑会创建 `dev` 用户，但只把 `/workspace` 的所有权交给该用户：

```text
mkdir -p /workspace
chown -R dev /workspace
```

它不会处理 `/workdir`、`/app` 或 `/opt`。代表性镜像的实际权限探测结果为：

```text
/workspace=dev:root:755   dev writable=yes
/workdir=root:root:755    dev writable=no
/app=root:root:755        dev writable=no
/opt=root:root:755        dev writable=no
```

因此，只要任务明确要求 agent 在这些 root-owned 目录中创建文件，失败就是环境权限导致的确定性故障，与模型是否能推导出正确答案无关。

## 确定写入失败：`/workdir` 任务

以下 27 个任务要求 agent 写入 `/workdir/findings.json`，但 `/workdir` 为 `root:root 0755`：

```text
agent-to-agent-injection-hard-multistep
apt-multi-source-correlation-hard-multistep
blind-ssrf-redis-write-hard-multistep
cache-deception-static-suffix-hard-multistep
cache-poisoning-host-header-hard-multistep
deserialization-gadget-chain-hard-multistep
ecdsa-nonce-reuse-jwt-hard-multistep
edr-bypass-syscall-direct-hard-multistep
false-positive-trap-bind-hard-multistep
firmware-implant-uefi-hard-multistep
graphql-alias-jwt-none-idor-hard-multistep
house-of-apple2-safe-linking-hard-multistep
logic-chain-race-idor-hard-multistep
multi-modal-prompt-chain-hard-multistep
nft-uaf-cred-overwrite-hard-multistep
oauth-state-csrf-account-takeover-hard-multistep
order-of-validation-2fa-bypass-hard-multistep
privilege-escalation-via-import-hard-multistep
privilege-token-exfil-via-summarize-hard-multistep
react-thought-hijack-hard-multistep
realworld-cms-0day-style-hard-multistep
rootkit-ld-preload-hard-multistep
saml-xml-signature-wrap-hard-multistep
ssti-inheritance-rce-hard-multistep
supply-chain-npm-postinstall-hard-multistep
time-delayed-trigger-hard-multistep
tool-schema-confusion-attack-hard-multistep
```

## 确定写入失败：`/app` 任务

以下 8 个任务要求 agent 在 root-owned `/app` 中创建 `report.jsonl`、`run_poc.sh`、`asan_output.txt`、PoC 源码或其他中间文件：

```text
binutils-oob-write-fr30-hard-multistep
curl-tftp-heap-overflow-hard-multistep
fluentbit-heap-overflow-trace-hard-multistep
jq-heap-overflow-jv-hard-multistep
junrar-path-traversal-localfolderextractor-hard-multistep
nginx-heap-overflow-rewrite-hard-multistep
php-unserialize-uaf-hard-multistep
vim-tabpanel-modeline-escape-hard-multistep
```

## 额外服务执行问题

以下 5 个任务的 Dockerfile 使用 `CMD` 启动本地服务：

```text
bb-bin-dns-parse-010
bb-bin-firmware-audit-007
bb-bin-format-log-004
bb-bin-ipc-cache-001
bb-bin-oob-read-003
```

Harbor 的默认构建 compose 使用 `sleep infinity` 覆盖 Dockerfile 的 `CMD`，导致 agent 阶段服务不会按 Dockerfile 自动启动。相应启动或重启脚本还涉及 `chown`、root-owned 配置、PID 文件或低端口，非 root `dev` 无法自行恢复服务。

`bb-bin-stack-auth-002` 不受该问题影响，因为它自带 `environment/docker-compose.yaml`，显式恢复了服务启动命令。

需要注意：改用 root agent 只是让 agent 具备手动启动或恢复服务的权限，并不会取消 Harbor 对 Dockerfile `CMD` 的覆盖。因此这 5 个任务仍应最终补充明确的 compose 启动配置。

## 为什么其余 20 个非 `/workspace` 任务不算确定失败

这些任务主要从 `/app`、`/opt` 或其他系统目录读取输入，但规定输出位于：

- `/logs/artifacts`
- `/tmp`

Harbor 会把 `/logs/artifacts` 作为可写 bind mount 挂载，并在容器启动后设置为非 root 可写；`/tmp` 通常为 `1777`。因此它们在 `dev` 下没有已确认的规定产物写入阻塞。

不过，为了避免同一个 Security 数据集中按任务切换用户所引入的额外变量，当前仍对全部 60 个 Security 任务统一使用 root agent user。

## 使用 root agent 的影响

统一使用 root 可以暂时消除上述目录权限阻塞，但评测结果需要按以下边界理解：

- root 会掩盖任务镜像和输出契约中的权限缺陷。
- root 可以修改普通用户无法修改的挑战二进制、服务配置和系统目录，执行空间与非 root 环境不同。
- root 创建的 bind-mount 文件可能在异常退出后成为宿主机上的 root-owned 文件。
- root 结果不能与使用 `dev` 的历史结果直接混合比较。
- 该运行方式只能保证当前 Security 实验内部使用一致的权限环境，不能证明模型在生产级非 root 环境中也能完成任务。

容器 root 不等于直接获得宿主机 root，但 Security 运行环境仍不应挂载 Docker socket、敏感宿主目录，也不应使用 `privileged` 模式。

## 后续正式修复方向

当前 root 方案应在以下修复完成后退出：

1. 将 27 个 `/workdir/findings.json` 输出迁移到 `/workspace`，或在运行时只对任务工作目录授予 agent 用户写权限。
2. 将 8 个 `/app` 产物迁移到 `/logs/artifacts`，或为需要修改源码的最小目录设置明确所有权。
3. 为 5 个服务任务增加任务级 `docker-compose.yaml` 启动命令和健康检查，不依赖 agent 以 root 身份恢复服务。
4. 使用 `agent_user: dev` 对全部 60 个 Security 任务回归，确认任务规定产物、动态服务和 verifier 均正常。
5. 非 root 全量回归通过后，删除 Security job 中的 `agent_user: root` 覆盖项。

在完成上述条件前，Security 的正式运行约定保持为：**全部任务统一使用 root agent user**。
