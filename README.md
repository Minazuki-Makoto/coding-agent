# Coding Agent

一个在终端里使用的代码助手。可以让它阅读项目、解释代码、修改文件、运行检查，再根据结果继续处理。

它分成两个角色：Executor 负责动手，Supervisor 负责规划和检查。两个角色的执行说明会显示在终端里，最后再给出回答；没有完成的任务会说明原因。

## 功能

- 浏览项目目录、读取代码和配置，分析项目用途与结构。
- 在项目副本中修改代码、运行 Python 或 Java 工具、执行构建和检查。
- 显示修改差异，经你确认后写回原项目。
- 保存问答、任务进度、工具结果、模型 token 用量和耗时。
- 打开以前的会话继续提问，也可以回到某个任务或事件重新开始。

Executor 的过程显示为淡白色，Supervisor 为淡绿色，最终回答保持普通字体。

## 技术栈

主要使用 Python、MCP 和模型服务的 SDK。模型接口支持 OpenAI、智谱、DeepSeek、通义千问和 Claude；两个角色可以使用同一个服务，也可以分别配置。

终端界面使用 Python 标准库，不需要启动网页服务。代码执行环境使用 Docker，历史记录保存在本地 JSONL 和 Markdown 文件中。

## 运行前准备

### Python 和依赖

当前 Windows 环境使用 `D:\anacode\python.exe`，已在 Python 3.12.4 下运行离线测试。如果 Python 安装位置不同，请替换下面的解释器路径。

```powershell
& "D:\anacode\python.exe" -c "import sys; print(sys.executable)"
```

已有环境能正常启动，就不必重复安装。新环境可以安装当前使用的依赖版本：

```powershell
& "D:\anacode\python.exe" -m pip install "mcp==1.27.0" "openai==2.24.0" "anthropic==1.7.0" "zai-sdk==0.2.3" "pydantic==2.12.5"
```

程序会导入各模型 SDK，即使只使用一个模型服务，也需要安装这些依赖。

### 模型配置

配置文件是项目根目录的 `INFORMATION.json`。已有配置可继续使用，不要把含密钥的文件提交到公开仓库。

`supervisor` 和 `executor` 选择服务名称：`chatgpt`、`glm`、`deepseek`、`qwen` 或 `claude`。对应服务中的 `mode` 设为 `on`，`api` 填自己的 API key，`model` 填该服务实际可用的模型名。`temperature` 控制生成温度。

同一个服务可以同时负责两个角色。模型请求需要联网，也可能产生服务费用；显示的 token 数来自接口返回的用量，不等同于费用账单。

### Docker

普通问答、历史查看和本地只读分析不需要 Docker。需要在隔离环境里修改、运行或构建项目时，请先启动 Docker Desktop，并确认 Docker Engine 正常运行。

第一次使用代码执行环境时，在项目根目录构建镜像：

```powershell
Set-Location "D:\pycharmcode\coding_agent"
docker build -t coding-agent-sandbox:local -f .\sandbox\Dockerfile .\sandbox
```

已经构建过，且 Dockerfile 没有改动，就不必每天重新 build。程序会自行创建执行容器，不需要手动 `docker run`。镜像内包含 Python、JDK 17、Maven 和 Gradle。

Docker 不可用时，相关执行操作会停止，不会偷偷改成在宿主机上运行。

## 启动

在 PowerShell 中运行：

```powershell
Set-Location "D:\pycharmcode\coding_agent"
& "D:\anacode\python.exe" -m backend
```

启动后选择新建或恢复。看到 `agent>` 就可以直接输入问题，例如：

```text
请分析 D:\pycharmcode\ElectricityLLM\llm-back 这个项目在做什么，不修改文件。
```

不需要提前新建工作目录，也不需要在启动命令里指定读写权限。输入路径只是指定对象，并不代表授权。

需要调用普通工具时，终端会显示操作类型和实际参数，让你选择：

- `yes`：只允许这一次操作。
- `no`：不执行，停止当前请求，回到输入界面。

使用方向键或 Tab 选择，Enter 确认，默认选中 yes。Supervisor 查询当前会话的历史不需要再次确认。

## 修改项目

可以在问题里给出项目路径，也可以先选择项目：

```text
/project "D:\my-project"
请修复登录接口的参数校验，并运行相关测试。
```

修改和执行在经批准的项目副本中进行。副本完成不代表原项目已经修改；写回前会展示差异并单独请求确认。

| 命令 | 用途 |
| --- | --- |
| `/diff` | 查看并固定当前修改版本 |
| `/apply ID` | 审阅并批准指定版本写回原项目 |
| `/apply-status` | 查看写回状态 |
| `/export ID` | 导出指定版本的补丁 |
| `/permissions` | 查看本次操作的授权情况 |

这里的 `ID` 要替换为 `/diff` 显示的实际变更编号，不要直接输入字母 ID。

## 历史、续聊和回退

输入 `/session`，从菜单选择以前的 session。程序会自动打开最近保存的 chat，显示历史对话、任务和执行说明；有多个分支时再选择分支。

打开后直接输入新问题就能接着聊，不必先回退，也不必输入 `/continue`。即使上次任务中断或受阻，也能开始新的提问。旧会话没有执行快照时，会根据已有历史恢复上下文。

| 命令 | 用途 |
| --- | --- |
| `/session` | 选择并切换会话 |
| `/sessions` | 列出会话 |
| `/conversation` | 查看当前对话历史 |
| `/tasks` | 查看任务目标和验收情况 |
| `/descriptions` | 查看两个角色的执行说明 |
| `/tools` | 查看工具调用记录 |
| `/steps` | 查看执行步骤和事件编号 |
| `/show EVENT_SEQ` | 展开某个事件的参数和结果 |
| `/continue` | 继续原来中断的任务，而不是开始新问题 |
| `/new` | 新建会话 |

历史支持分页，例如 `/conversation --page 2`、`/descriptions --page 2`。

想回退时，先用 `/tasks` 或 `/steps` 找到实际编号，再输入 `/rewind task TASK_ID` 或 `/rewind EVENT_SEQ`。例如，存在 task 0 时可以输入 `/rewind task 0`。

回退会建立新分支，旧记录仍然保留。它只回退对话和任务状态，不会撤销已经写回磁盘的代码。有快照时恢复执行状态；没有快照时恢复历史上下文，需要继续旧目标时重新规划。

不同 chat 不自动混用上下文。只用历史查看命令查看另一个会话，不会切换当前会话。

## 设置和文件位置

Windows 下，首次默认历史目录是 `D:\coding-agent-state`，设置保存在 `%LOCALAPPDATA%\coding-agent\settings.json`。实际位置可在 `/settings` 中查看。

```text
/settings
/settings state-dir "D:\my-agent-history"
/settings exclude "**/private/**"
```

更改历史目录只影响后续新建会话或重启，不会搬走当前历史。也可以在启动时用 `--state-dir` 指定历史目录。

问答和角色历史保存为 JSONL；完整原始工具结果独立保存在 `tool_results.jsonl`；同一个 chat 的累计任务摘要保存在一份 `task_summary.md` 中。项目副本放在历史根目录同级的 staging 目录，不会直接把原项目挂进执行容器。

复制前请检查排除规则。默认会排除常见凭据和构建产物，但无法可靠识别所有写在源码里的密钥；自定义敏感文件应手动加入排除规则。

## 项目目录

- `AgentLoop/`：任务调度、执行和验收。
- `AgentChat/`：模型接口适配。
- `MCP_functions/`：文件、代码执行工具及权限控制。
- `State/`、`Context/`：历史保存、检索和恢复。
- `Skills/`：规划、决策和记忆检索说明。
- `sandbox/`：Docker 执行环境。
- `tests/`：离线回归测试。

`CODEX.md` 记录内部实现，`PROBLEM.md` 保留问题分析。日常使用看这份 README 即可。

## 测试与其他命令

在项目根目录运行离线测试：

```powershell
& "D:\anacode\python.exe" -m unittest discover -s tests
```

真实 Docker 集成测试默认跳过。离线测试不会验证真实模型的回答质量或远程服务可用性。

输入 `/help` 查看全部终端命令，`/exit` 退出。长问题可以用 `/multiline` 输入，单独一行 `/end` 提交。启动参数可用 `-m backend --help` 查看。
