# dsv4-flash code 子集 OOM（exit 137）分析

/ harness: `claude-code/2.1.187` · model: `dsv4-flash` · 镜像 tag: `2026-08-26`
实验目录：`results/dsv4-flash.cc.code/2026-09-03__09-50-50`
任务：`bug_fix-medium-permission_error`（修复 `copier` 拷贝文件时的 `PermissionError` 处理）

## 结论（TL;DR）

- 该 task 的崩溃是 **exit 137 = SIGKILL**，由**容器自身 cgroup 内存上限**触发的 OOM，**与宿主机空闲内存、与并发 job 数量无关**。
- 内存上限来自 task 自带配置：`task.toml` 声明 `memory_mb = 4096`（4GB），本地 docker 后端把它落成容器 **cgroup 硬 limit**。
- **不是每次必现**：同一 slot 的 4 个 attempt 中 **2 个正常跑完（reward 0.25）、2 个 OOM**。是否爆内存取决于 agent 的**测试验证策略**，属 trajectory-dependent。

## 内存上限从哪来

`datasets/wb-bench-code-v1.0/tasks/bug_fix-medium-permission_error_skip_continue/task.toml`：

```toml
cpus = 2
memory_mb = 4096
```

Harbor 本地 docker 后端的落地路径：

- 默认 `memory_enforcement_policy = ResourceMode.AUTO`
  （`harbor/models/trial/config.py:172`）
- docker 后端对 memory 把 `AUTO` 映射为 **LIMIT**
  （`harbor/environments/docker/docker.py:463-467`，`auto_mode=ResourceMode.LIMIT`）
- 写出 resources compose：`memory = "4096M"`
  （`harbor/environments/docker/docker.py:246`），`memory_limit=True`

即容器被**硬卡在 4GB**；超过即由 memory cgroup 的 OOM killer SIGKILL 容器内进程，`claude` 命令返回 137，被包成 `NonZeroAgentExitCodeError`。宿主机 OOM killer 不参与，所以宿主机看着很空是完全一致的现象。

## 为什么会爆：轨迹对比

同一 slot 4 个 attempt，改的代码都差不多（给 `_render_file/_render_symlink/_render_folder` 的写操作加 `try/except PermissionError`），分叉在**验证阶段跑了哪些测试**：

| attempt | 结局 | 关键最后动作 |
|---|---|---|
| OK（48 turns） | completed 0.25 | `pytest tests/ -x -q --ignore=tests/test_output.py -k "not test_cli"` — 排除重测、只跑窄子集 |
| OK（28 turns） | completed 0.25 | 始终带 `-x`（首错即停）+ 定向子集 |
| OOM（62 turns） | killed 137 | 全量 `tests/` → 最后 `pytest tests/ -q --no-header --ignore=tests/test_tools.py --tb=no`（**去掉了 `-x`**）← 死在这 |
| OOM（41 turns，已归档） | killed 137 | `pytest tests/ -x -q` 全量 → `pytest tests/test_copy.py tests/test_recopy.py -x -q` ← 死在这附近 |

`copier` 是项目脚手架工具，测试**不是纯函数单测**：每个用例会起真实子进程（`copier` 端到端、`git init/commit`、`test_output.py` 的 pexpect 交互式 PTY），并往 tempdir 整树拷贝模板 / 建临时 git 仓库。在**一个 pytest 进程里**把 `tests/` 跑到底，几百个用例的子进程缓冲 + pytest 捕获的输出/fixture **累积 RSS**，峰值越过 4GB → OOM。

- **爆的两次**：跑全量套件（最致命的一条还去掉了 `-x`，跑完整套到底）。
- **没爆的两次**：排除重测跑窄子集，或全程 `-x` 首错即停，内存没累积到 4GB。

## 影响与建议

- 这不是解题质量问题：OOM 与非 OOM 的 attempt reward 都是 0.25，OOM 只是打断收尾。
- **补跑注意**：exit 137 表层是 `NonZeroAgentExitCodeError`（在 in-place resume 的 RETRYABLE 集合里，会被重跑），但由于 4GB cap 是每容器固定值，**solo / 换低负载时段重跑并不能规避**——只要 agent 又走"全量测试"的路径就会再爆（已实测重跑再次 exit 137）。
- 若要让它稳定跑过，二选一：
  1. **抬内存上限**：补跑加 `--override-memory-mb 8192`（harbor CLI `jobs.py:696` 支持），给该 task 更大容器内存；
  2. **视作合法资源超限失败**：若认为 4GB 是标准约束、该 task 本应在 4GB 内完成，则等同 timeout 计为合法失败，不补。

## 排查入口（下次遇到 code 子集 exit 137）

1. `result.json` → `exception_info.exception_message` 看是否 `Command failed (exit 137)`。
2. 对照 `task.toml` 的 `memory_mb`，确认容器 cgroup 上限。
3. 解析 `agent/cc-output.txt` 的 tool_use 序列，看崩溃前是否跑了全量 `pytest tests/`（尤其无 `-x`）。
