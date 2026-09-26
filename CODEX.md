# Coding Agent 项目进度与对话摘要

更新日期：2026-09-24

本文用于后续对话恢复项目背景。代码状态以本次实际读取的文件为依据；建议和计划不代表已经实现。后续修改前应重新读取相关代码，不要直接套用历史路径或旧结论。


## 启动与上下文恢复规则

- 每次新会话、恢复上下文或开始本项目任务时，优先读取本文件，再检查任务相关源码；不要仅凭历史对话推断现状。
- 本项目路径为 `D:\pycharmcode\coding_agent`；即使当前工作目录是 `D:\CodexWork`，处理本项目时也应先读取此处的 `CODEX.md`。
- 本文件记录背景、约定、完成情况与待办，不会自动启动程序，也不代表可以自动执行安装等操作。
- 用户当前明确指令优先；历史记录与实际代码不一致时，以重新检查的代码为准，并区分“已实现”“仅讨论”“待验证”。
- 第 2～7 节以 2026-09-22 重新核对的目录、Search 配置、MCP Host 和通用客户端为准；旧搜索实现已移除，不再保留为当前能力。2026-09-18 工具历史见第 11 节，2026-09-20 Java 进展见第 12 节，聊天接口见第 13 节，执行历史见第 14 节；第 15 节保留 2026-09-23 的 AgentLoop 历史快照，Agent 状态、Supervisor/Executor 循环、工具权限和当前待办以第 16 节为准。

2026-09-22 核对范围：项目目录、五个 AgentChat 模块、MCP Host/通用客户端/服务启动配置、状态保存模块。第 13 节记录当前聊天接口和最新集成边界；第 3～5 节已按当前 MCP 配置和通用客户端重写。Java 工具内部实现本轮未全面复核。

## 0. 2026-09-26 AgentLoop 闭环修复

- 按 `codex_agentloop_repair_guide.md` 保留 Agent 主调度、Supervisor 规划/验收/决策、Executor 执行的既定职责，没有合并角色或恢复旧查错 Agent。
- `AgentLoop/agent.py` 已补全状态默认值与主循环；配置改为启动时读取，MCP 和模型客户端生命周期覆盖整个任务，并加入任务尝试、角色回合和模型请求预算。
- `executor_loop.py` 已接通“选择动作—单工具调用—真实观察—结构化总结—JSONL 保存”，纯 JSON 输出也会解析；工具事实先保存，工具后总结失败只重试总结，不重复工具。
- `supervisor_loop.py` 保留 planning、evaluation、decision 三段职责。首次规划后直接指导第 0 项；Executor 自报完成必须经 evaluation，task 推进由主循环统一应用。
- 模型适配调用统一经异步线程封装；工具观察按 OpenAI 兼容协议或 Claude tool_use/tool_result 协议回传。每步多工具调用会被拒绝且不会静默丢弃。
- Executor 上下文只保留最近 10 条观察后 description；完整工具参数、规范化结果、错误、退出原因和监督引用写入 JSONL。
- 历史契约区分工具成功、Executor 回合完成和 Supervisor 通过；Supervisor 结论以追加 review 记录关联本轮全部 executor seq，旧 `is_solved` 记录仍可读取。
- MCP 规范工具名统一为 `read_history_chat`；Supervisor 保持只读历史权限，Executor 不获得历史工具权限。
- Skills 按确定顺序登记真实完整路径；当前三个 skill 均属于 Supervisor，Executor skill 列表允许为空。规划正文从项目相对路径 `Skills/supervisor_plan.md` 加载。
- 新增 `tests/test_agent_loop.py` 假模型/假 ToolRegistry 测试。离线验证覆盖工具只执行一次、总结重试、工具失败、纯 JSON、10 条窗口、非法 skill、监督否决、历史 review、权限和连续两个任务最终退出。
- 新增 `SupervisorDescriptionHistory`：按 seq/task/phase 收集每个 Supervisor turn 的 description。任务结束前由 Supervisor 以无工具请求生成最终上下文摘要；汇总失败不改变任务成败，退回最近记录的确定性摘要。聊天 JSONL 同时保存最终 description 与结构化 `supervisor_descriptions`，角色工具证据仍保留在各自历史文件中。
- 2026-09-26 实际验证：31 个 Python 文件 AST 解析通过，目标模块导入通过，17 个确定性测试通过。未调用真实付费模型、远程 MCP 或用户项目写操作；真实端到端连接仍待单独验证。
- `backend.py` 原有字符串转义警告不属于本次闭环修复范围，未修改。

## 1. 项目目标与工作习惯

- 构建由 LLM 参与决策、通过 MCP 工具完成搜索、文件操作和命令执行的 coding agent。
- 用户希望逐步学习和实现，当前先完善工具层，不提前搭建复杂架构。
- 交流使用中文，解释代码的用途和原因，优先最小修改。
- 用户询问代码是否正确、函数是否有用时，先检查并解释，不擅自进行无关重构。
- 默认 Python 解释器为 `D:\anacode\python.exe`。子进程优先使用 `sys.executable`，保持解释器一致。
- 不输出或写入真实 API Key、Token、密码。使用环境变量传递配置。

## 2. 当前目录与职责

项目根目录：`D:\pycharmcode\coding_agent`。以下为 2026-09-22 实际发现的业务文件结构，省略 `.idea`、缓存等目录。

```text
coding_agent/
├── CODEX.md
├── README.md
├── PLAN.md
├── INFORMATION.json
├── backend.py
├── AgentChat/
│   ├── Chatgpt_chat.py
│   ├── GLM_chat.py
│   ├── Claude_chat.py
│   ├── DeepSeek_chat.py
│   └── Qwen_chat.py
├── AgentLoop/
│   └── agent.py
├── Context/
│   ├── mcp_resources.py
│   ├── mcp_supervisor_tools.py
│   └── History_Resorce/
│       ├── history_state.py
│       ├── mcp_history_error.py
│       ├── mcp_history_resource_service.py
│       └── mcp_supervisor_history.py
├── ExecutorBooks/
│   ├── Executor_textbook.md
│   └── Executor_mistake_book.md
├── Skills/
│   ├── executor_plan_skill.md
│   └── supervisor_skill.md
├── State/
│   ├── save_chat_history.py
│   ├── save_executor_state_history.py
│   └── save_supervision_state_history.py
└── MCP_functions/
    ├── MCP_hosts.py
    ├── MCP_client.py
    ├── Search/
    │   └── mcp_search_server.py
    ├── collect_information/
    │   ├── collect.py
    │   └── mcp_collect_server.py
    ├── supervision/
    │   └── check_valid.py
    └── System_Files/
        ├── Files_function/
        │   ├── bfs_read.py
        │   ├── java_code.py
        │   ├── python_code.py
        │   └── write_in.py
        └── Files_server/
            └── mcp_system_server.py
```

- `AgentChat`：五家模型的同步聊天请求封装；统一接收外部 `client`，具体签名见第 13 节。
- `AgentLoop/agent.py` 已有模型选择、规划、MCP 注册、任务循环及 checkpoint 数据类草稿；`Skills` 已有规划和监督技能。当前闭环仍未完成且 `agent.py` 存在语法错误，具体边界见第 15 节。
- `MCP_client.py`：`mcp_client(session)` 包装 `initialize()`、`list_all_tools()`、`call_tool(tool_name, argument)`。
- `MCP_hosts.py`：`mcp_host` 使用外部 `AsyncExitStack` 管理 stdio 与 ClientSession；保存客户端和工具名路由。重复客户端名直接返回，重复工具名保留先加入的映射。
- `Search/mcp_search_server.py`：当前提供 `get_remote_mcp_link()`，返回 GitHub、Playwright、Docker MCP 的 `StdioServerParameters`，不是旧版自定义搜索 FastMCP 服务。
- `System_Files/Files_server/mcp_system_server.py`：已有文件读取/分类/写入、Java/Python 检查与执行、安装/打包等包装工具注册，并提供 stdio 启动入口；注册存在不代表运行链路已验证。
- `collect_information`：已有信息收集函数与 FastMCP 包装；`State` 已有历史保存函数。本轮仅阅读源码，未执行交互、文件写入或服务启动。
- `Context/History_Resorce`、`ExecutorBooks`、`supervision`：按实际拼写记录；不能从目录名称推断上下文资源、监督流程或经验学习已接通。
- `backend.py`：Flask 应用及 `create_session()`；默认父目录 `D:\coding-agent` 必须已存在，否则抛 FileNotFoundError。创建会话子目录后启动 Flask；未见模型/MCP 主循环接入。
- 旧 `Search/Functions`、`Search/MCP_Client`、`Search/MCP_Server` 和 `Run_Command` 未在本次目录发现中出现；旧搜索实现说明、旧启动命令和依赖旧函数的待办已从当前记录移除。

## 3. 当前搜索能力与已完成工作

### 3.1 Search 当前职责

- 当前业务源文件只有 `MCP_functions/Search/mcp_search_server.py`，提供 `get_remote_mcp_link() -> dict[str, StdioServerParameters]`。
- 它集中配置外部 MCP 服务的启动参数，不在项目内实现搜索请求，也不注册自定义搜索工具。函数返回配置，不会自行启动服务。
- 尽管函数名包含 remote，当前三项均由 Host 启动本地子进程并通过 stdio 连接；不是远程 HTTP/SSE MCP 地址配置。
- 原来的自写 GitHub/Tavily 搜索、HTML 清理、结果整理和专用搜索客户端已不在当前源码中；不能继续把它们的实现、测试或待办写成现有能力。缓存目录中残留的 `proxy.cpython-312.pyc` 不代表仍有对应源文件。

### 3.2 三项服务配置

| 配置键/客户端名 | 当前启动配置 | 当前源码中的环境设置 |
|---|---|---|
| `github` | `docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN -e GITHUB_READ_ONLY ghcr.io/github/github-mcp-server` | 从 `GITHUB_API` 读取令牌，映射为 `GITHUB_PERSONAL_ACCESS_TOKEN`；`GITHUB_READ_ONLY=1` |
| `browser` | `cmd /c npx -y @playwright/mcp@latest` | 未显式传入 env |
| `docker` | `uvx --from git+https://github.com/L337-org/docker-mcp.git docker-mcp-server` | `DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1` |

- 三个键是 Host 使用的客户端标识，不是实际工具名称；每个服务可以暴露多个工具，具体名称和数量必须从 `list_tools()` 获取。
- `browser` 对应 Playwright MCP；不能把它描述成旧 Tavily 搜索接口。`docker` 是 Docker MCP 服务配置，说明该目录承载的内容已超出单一搜索功能。
- GitHub 环境变量通过 `os.environ["GITHUB_API"]` 直接读取；缺失会在构造整个返回字典时抛 KeyError，函数不会先返回其他两项。这里只记录源码行为，未修改。
- 只读/非破坏性设置是当前传递的配置值，未测试第三方服务实际执行效果。

### 3.3 已确认与未验证

- 已逐行核对 Search、Host、通用客户端源码，并搜索项目 Python 文件确认旧搜索函数不再存在。
- 本轮没有运行 Docker、npx 或 uvx，没有拉取镜像/安装包，没有读取令牌值，没有启动服务或获取实际工具列表。
- 因此当前能确认的是启动配置和连接代码已存在，不能声称三项服务均已连通、搜索/浏览/容器操作均已成功。

## 4. MCP Server 当前状态

- `Search/mcp_search_server.py` 只有启动配置函数；没有 `FastMCP` 注册和 `run()` 入口，直接执行该文件不会启动这些 MCP 服务。
- 真正启动三项服务的是 `MCP_hosts.py` 中的 `stdio_client(stdio_parameters)`；各外部程序承担 MCP 服务端职责。
- 项目自己的 FastMCP 服务另外位于 `System_Files/Files_server/mcp_system_server.py` 和 `collect_information/mcp_collect_server.py`；它们不在当前 Search 配置返回的三个键中。
- 旧搜索服务的包路径、`net_search` 注册及启动命令已失效，不能作为当前运行方式。
- Host 有 `asyncio.run(main())` 入口，但其当前顶层导入方式和外部依赖仍需在目标运行环境验证；本轮没有启动它。

## 5. MCP Client 四个函数的结论

当前文件为 `MCP_functions/MCP_client.py`，使用通用类 `mcp_client`。原先搜索专用的四个函数已移除；下面记录当前构造函数和三个异步方法。

| 当前接口 | 源码行为 |
|---|---|
| `mcp_client(session: ClientSession)` | 将外部会话保存为 `self.session` |
| `await client.initialize()` | 调用 `session.initialize()`；包装方法没有返回其结果 |
| `await client.list_all_tools()` | 返回 `session.list_tools()` 的完整结果，工具列表在 `.tools` |
| `await client.call_tool(tool_name, argument)` | 调用 `session.call_tool(name=tool_name, arguments=argument)`，返回原始结果 |

- 客户端不创建或关闭会话，生命周期由 Host 的外部 `AsyncExitStack` 管理。
- 当前没有搜索专用的格式转换函数或静态工具路由函数。不要调用已移除的旧接口。
- MCP 工具定义转为模型所需格式、工具结果转为对话消息，仍需调用层实现；通用客户端本身没有完成这些转换。

## 6. 已确定的 Host 与主循环分工

- `mcp_host(stack)` 保存外部栈，并初始化 `session_dictionary`、`tool_dictionary`。
- `run(stdio_parameters, client_name)` 的实际顺序为：检查客户端是否已存在 → 进入 stdio 上下文 → 进入 ClientSession 上下文 → 构造通用客户端并初始化 → 保存客户端 → 列工具并建立路由。
- `session_dictionary[client_name]` 实际保存的是 `mcp_client` 包装对象；`tool_dictionary[tool.name]` 保存所属 `client_name`。
- 当前 main 遍历 `github`、`browser`、`docker` 三项配置，逐个 await 建立连接，不是原先一个 Search 服务对应两个固定工具。
- 重复客户端名会直接返回；重复工具名保留先加入的路由，不会报冲突。当前没有统一的模型工具定义列表或 Host 级工具分发方法。
- main 退出 `async with AsyncExitStack()` 后才打印字典；字典仍在，但对应连接已结束，不能据此认为客户端仍可调用。
- 后续主循环需在栈有效期内完成模型请求、按路由调用工具、回传工具结果；当前尚未接入。模型客户端也应复用至任务结束，见第 13.1 节。

## 7. 搜索工具是否需要扩展

- 不再沿用“当前两个搜索工具已足够”或“为 Tavily 结果补 URL”等基于旧实现的结论。
- 当前应先验证三项 MCP 服务能否启动、实际暴露哪些工具以及所需权限，再按实际任务判断是否缺少检索或浏览能力。
- 源码没有固定“先 GitHub 后互联网”的工具调用策略；LLM 选择工具和结果整理尚未接入主循环。
- 是否增加其他搜索服务、内容清理或去重，应由实际工具输出和用户任务决定；本轮仅更新文档，没有新增或修改服务。

## 8. 让 AI 写代码的后续规划

以下是能力规划；部分文件读取和语言工具已实现（见第 11 节），通用工具与 LLM 主循环的完整集成尚未验证：

- `System_Files`：`list_directory`、`read_file`、`write_file`，后续增加 `apply_patch`。
- `Run_Command`：`run_command`，返回退出码、标准输出和错误输出。
- LLM 决定调用哪个工具并生成参数，Host/loop 实际执行，再把结果反馈给 LLM。
- 修改已有项目应先读代码，再做最小局部修改，避免默认整文件重写。
- 文件访问边界、命令超时等应由工具实现保障，不能只靠提示词。
- 推荐第一个完整任务为“在指定工作目录创建 hello.py 并运行”，验证生成调用、分发执行、结果回传链路。
- 不提前实现上述扩展，按用户后续指令逐步进行。

## 9. 后续待办与验证边界

1. 按用户指令检查当前 Host 启动方式、外部命令依赖及环境变量，并分别验证三个 MCP 服务的初始化、列工具和调用。
2. 接入模型主循环，在连接有效期内维护动态工具路由、转换模型所需工具格式并回传结果。
3. 根据需要接入系统文件与信息收集服务，处理其当前独立启动配置及确认交互问题。
4. 实现外部模型客户端的创建、复用和任务结束清理，维护消息历史。
5. 根据真实服务输出和任务反馈判断是否需要扩展搜索能力，不恢复已移除的旧搜索待办。

这些是后续方向，不代表本轮已执行或自动获得安装、启动和调用权限。源码检查和离线测试不等于 MCP 或模型 API 端到端可用。

## 10. 本轮对话关键知识点

- `StdioServerParameters` 描述子进程启动方式，配置函数返回参数，Host 才负责启动和建立 MCP 会话。
- 客户端标识、服务名称、工具名称是不同概念；当前工具路由来自连接后的动态发现。
- `list_all_tools()` 返回完整列工具结果，Host 遍历其中的 `.tools`。
- 保存客户端对象不代表其连接一直有效；工具调用必须发生在管理连接的 AsyncExitStack 退出之前。
- 五家模型聊天函数接收外部 `client`，复用连接池；对话上下文仍由调用方传入的消息历史维护。
- 当前优先最小修改，不把已删除实现、旧测试记录或讨论过的架构当作当前源码事实。

## 11. 2026-09-18 工具层进展与当前待办

以下为历史记录；Java/JDK 查找、Spring 判断、打包确认及启动检查的最新实现见第 12 节。

### 11.1 Python 探测和运行

- 当前讨论文件为 `MCP_functions/System_Files/Files_function/python_code.py`，此前修改过 `get_code_feed_back.py`；后续优先核实实际调用位置，不假定旧文件仍是入口。
- `find_the_python_editor()` 只探测名为 `python.exe` 的候选，用 `-I -c` 输出固定标记验证，并通过 `CREATE_NO_WINDOW` 避免控制台闪窗。不得遍历执行全部 `.exe`。
- 集合只在参数为 None 时初始化；递归共用集合且不在首个结果处提前返回。路径存字符串，不能向 set 添加字典。
- 当前返回键为 `python_editor_location`，值仍为 set；若后续传 JSON，需先处理序列化，尚未擅自改变接口。
- `run_code_get_feedback()` 用指定解释器执行文件，返回退出码和输出，超时 10 秒；不是长期服务进程管理器。
- Python 搜索默认 `Path("C:")`、目录访问异常等此前发现的问题未统一修复，后续根据实际代码最小修改。

### 11.2 安装依赖与用户确认（已实现，未接入主循环）

- `download_package(editor_address, package_name, confirmed=False)` 验证参数，未确认时返回 `confirmation_required`；只有 `confirmed is True` 才调用指定解释器的 `-m pip install --no-input`。
- 安装捕获输出、隐藏控制台、超时 300 秒；超时可能留下环境变更，不表示自动回滚。
- `download_package_with_confirmation()` 供终端宿主调用：展示解释器和依赖，只接受 y/yes；回车、其他输入、EOF、Ctrl+C 均不安装。非交互 stdin 返回待确认状态，不读取协议输入。
- 用户已明确：安装函数可以作为宿主的普通 Python 函数，不必注册 MCP。当前没有自动检测缺包并自动安装的完整流程。
- `confirmed` 必须来自用户真实决定，由宿主控制，不能由模型自行填写 True 代替授权。
- stdio MCP 的 stdin/stdout 用于协议通信，服务端工具不能直接用 `input()` 读取终端确认。若未来通过 MCP 提供安装功能，应由客户端确认或接入 elicitation；这些集成尚未实现。
- 模拟验证覆盖同意、拒绝、默认拒绝、取消、EOF、非交互调用、安装失败和超时；没有实际安装依赖。

### 11.3 Java/JDK 查找（已实现）

- `java_code.py` 的 `find_java_exe(home_address=None)` 使用 deque 广度优先搜索。
- 仅收录同一目录同时包含 `java.exe` 和 `javac.exe`，且两者 `-version` 命令退出码都为 0 的组合；收集全部组合，不在第一套 JDK 处结束。
- 返回 `java_locations` 列表，条目为 `java_path`、`javac_path`。当前不再接收旧的 `java_editor_set` 参数。
- 记录已解析目录避免重复访问，跳过目录访问/链接解析错误和程序启动失败，隐藏探测控制台。
- 已通过语法检查与模拟目录验证（逐层顺序、全部组合、不完整组合跳过、启动失败、目录不存在），没有实际全盘搜索或验证项目编译。
- 单文件、Maven、Gradle 是构建方式；普通短程序和长期服务是运行方式；Spring Boot 不应作为 Maven/Gradle 的互斥分类。长期服务不能因为 10 秒内未退出就判失败。
- C/C++ 编译器查找只给过示例，未据此宣称项目中已实现。

### 11.4 Spring 判断（仅讨论方案，尚未替换旧实现）

- `judge_spring_project()` 当前仍以存在 `.xml` 分类判断 Spring，逻辑不完整。
- 计划：先识别 `pom.xml`（Maven）或 `build.gradle` / `build.gradle.kts`（Gradle），再检查构建配置中的 Spring 依赖、父项目及插件，并结合源码真实 import、注解和启动调用。
- Maven 与 Gradle 是不同识别途径，不要求同时存在；XML 文件或 pom.xml 本身不能证明使用 Spring。
- Spring 与 Spring Boot 应分别记录。返回 evidence；依赖继承、别名、多模块或读取不全时保留“无法确定”，不能把未发现特征等同于确定不是 Spring。
- 文本匹配示例只是初步识别，可能受注释、测试代码影响；Maven 更适合解析 XML 节点，不能宣称已完成完整依赖解析。
- 已只读检查 `D:\pycharmcode\ElectricityLLM\llm-back`：它是 Maven Spring Boot 项目，pom.xml 包含 `org.springframework.boot` / `spring-boot-starter-parent`，`LlmBackApplication.java` 包含 Boot 导入、`@SpringBootApplication` 和 `SpringApplication.run(...)`。无需 .gradle 文件，未修改该参考项目。

### 11.5 文件分类相关待修问题

- `bfs_read.py` 的 `sort_files_by_suffix()` 将局部字典初始化为 `{}` 后访问 `sorted_files["sorted"]`，会触发 KeyError；应检查 `file_suffix not in sorted_files`。本轮仅指出，尚未修改。
- `read_all_files()` 的目录分支将所有文件当文本读取，可能在 jar/class/图片上解码失败；单文件分支缺少 content。后续读取构建配置前需处理这些问题，尚未修改。
- 下一步可按用户授权修正分类链路及 Spring 判断；不要因为此待办记录就自动实施。

## 12. 2026-09-20 Java 工具进展与当前约定

本节以本轮核对的 `MCP_functions/System_Files/Files_function/java_code.py` 为准，更新第 11 节的历史状态；未复核的其他模块不能据此视为已修复。

### 12.1 用户约定

- 用户反复强调最小修改：只修改明确指定的函数或问题，不增加未要求的回调、重构或架构。
- 当前优先在 PyCharm 中验证。前端确认界面、后端待确认请求管理和 Agent 恢复流程仅讨论，用户明确暂缓实现。
- 用户可能同时编辑文件；写回时校验目标代码，只替换本次修改的区块，保留其他最新改动。

### 12.2 Spring 判断与项目 Java 版本

- `judge_spring_project(sorted_files)` 已解析 Maven XML 中的 Spring 依赖、父项目、插件，并识别常见 Gradle 声明和 Java/Kotlin import；不再是仅看是否存在 XML。第 11.4 节旧实现描述已过时。
- 成功返回包含 `status`、`is_spring_project`、`is_spring_boot_project`、`build_tools`、`jdk_version`、`evidence`、`warnings`。
- 新增 `jdk_version`，为整数，例如 17；读取当前 POM 的 `java.version`、`maven.compiler.release/target/source`、直接声明的编译插件配置，解析当前 POM 属性引用；Gradle 支持常见 `JavaLanguageVersion.of(...)` 和兼容性字面量配置。
- 未解析到版本、存在无法解析的 Maven 版本引用或多个版本冲突时返回 None；通过 warnings 说明。不是完整 Maven/Gradle 求值器，不解析外部父 POM、动态 Gradle 表达式等。
- 该字段是项目构建配置声明的版本，不等于实际运行 JDK，也不是所有依赖的精确运行时最低版本；尚未实现自动匹配本机启动器。
- 用真实 `D:\pycharmcode\ElectricityLLM\llm-back\pom.xml` 验证得到 `jdk_version=17`、Spring Boot=True；其 Spring Boot 父版本为 4.0.7，IDEA 项目 SDK 配置为 26。未修改该项目配置，未实际打包或启动该项目。
- Maven/Gradle 示例、属性引用、旧式 1.8、缺失和冲突版本的离线检查通过。

### 12.3 查找全部 JDK 版本

- `find_java_exe(home_address=None)` 保留 deque 广度优先搜索；默认仅搜索 C:/，可用 `find_java_exe("D:/")` 搜索 D 盘，不自动扫描所有盘。
- 同目录的 java.exe、javac.exe 均通过 `-version` 验证后才收录，每套只添加一次；保留访问异常跳过、超时和隐藏探测窗口逻辑。
- 从 java 的 stdout 与 stderr 合并内容提取完整版本号；未识别版本格式时用字符串 `unknown`。
- `java_locations` 当前结构为 `[{"17.0.12": {"javac": "...", "java": "..."}}, ...]`，路径为字符串，可序列化 JSON。第 11.3 节的 java_path/javac_path 条目描述已过时。
- 多套同版本 JDK 仍可以有多条记录，不按版本合并；返回指定搜索范围内发现的全部可用组合。
- 模拟检查覆盖多个版本、stderr 输出、每套只记录一次、失败组合、超时与 JSON 序列化；真实验证 PyCharm 自带 Java/javac 均为 17.0.12。
- 本机已确认 Java 17 路径为 `D:\pycharm\PyCharm 2024.1.7\jbr`（JetBrains Runtime，包含 javac）；此外实际验证过 `C:\Program Files\Java\jdk-26`、`D:\java\jdk-25.0.4`。目录搜索存在无权访问区域，不声称已穷尽全部安装。
- `JAVA_HOME` 应指向 JDK 根目录，不带 bin；单文件函数的 `jdk_location` 指向 bin；启动验证函数的 `java_path` 指向具体 java.exe。尚未修改系统或 PyCharm 环境变量。

### 12.4 普通 Java 编译与执行

- `run_java_get_feedback(code_address, jdk_location)` 按无 package 的普通 Java 单文件处理；源文件和 JDK bin 路径转为绝对路径。
- 编译使用 javac、源文件完整路径以及父目录 classpath；成功后用 java、父目录 classpath、文件 stem 类名运行。编译失败不继续执行旧 class。
- cwd 设置为源文件父目录，编译及运行各超时 10 秒，返回 stdout、stderr、returncode；提示已由“安装依赖”修正为 Java 执行。
- 编译命令最后参数可用完整路径；设置了正确 cwd 后也可以用 `.name`。`-cp` 不负责切换工作目录。
- 真实 JDK 验证正常输出、编译错误、运行异常；超时分支经过模拟验证。
- 已按用户要求创建 `D:\java-exercise\Hello.java`，无 package，预期输出 `Hello, Java!` 和 `10 + 20 = 30`。

### 12.5 Spring Boot 打包与用户确认

- 当前有 `package_spring_boot(project_path, temp_path, confirmed=False)`：先检查项目，未获确认返回 confirmation_required；确认后通过 Maven/Gradle 构建并复制 JAR 到临时目录。构建超时 300 秒，capture_output 会等结束后才返回日志。
- `package_spring_boot_with_confirmation(project_path, temp_path, interactive=False)` 只新增显式交互开关；额外 confirm_callback 方案已撤掉，不存在该参数。
- 旧 `isatty()` 判断会让 PyCharm Run 控制台提前返回；现在宿主显式传 `interactive=True` 才使用 input()，默认仅返回待确认状态。拒绝、空输入、EOF、Ctrl+C 不执行打包。
- 直接运行验证需显式调用函数并传 interactive=True；本次最终核对文件中已没有 `__main__` 调用入口，不要沿用早期“直接运行就会询问”的描述。
- 打包确认实际涉及构建、可能下载依赖及修改构建目录、复制或覆盖同名 JAR；现有提示文字仍偏向“写入临时目录”，本轮按最小修改未扩展提示。
- 未来前端应显示允许/拒绝按钮，后端保存待确认请求，绑定用户和原始参数、避免重复执行，收到真实同意后再调用底层函数 confirmed=True；不让模型自行填写确认参数。此流程仅讨论，未实现接口、任务暂停恢复或 MCP 确认通道。
- stdio MCP 服务端不能启用控制台 input()；终端/网页宿主负责确认。Agent 不必固定运行在终端。
- 控制台及确认分支做过模拟验证，未实际执行 Spring Boot 打包。不能声称 Maven、Gradle、数据库等已验证可用。

### 12.6 Spring Boot 试启动验证

- `check_spring_boot_startup(java_path, jar_path, timeout=30)` 使用 `java -jar ... --server.port=0` 和 subprocess.run；它是试启动检查，不是长期服务管理器。
- 超时后 run 会终止 Java 进程，再检查捕获日志；success 表示日志显示曾启动完成，不代表服务仍在运行。默认即使提前启动成功也会等到 30 秒。
- 按用户要求仅修复两点：先判断 APPLICATION FAILED TO START、BeanCreationException、UnsatisfiedDependencyException；再用 `\bStarted [\w.$]+ in \d+(?:\.\d+)? seconds\b` 匹配启动日志。
- 普通 `Started background worker` 不再算成功；成功日志和上述失败标志同时存在时返回 error；没有明确证据返回 unknown。
- 已通过 6 组模拟日志检查和语法检查，未真实启动 llm-back。日志判断仍是启发式，并非 HTTP 就绪或业务可用性验证。
- 其余行为未改：超时前自行退出一律 error（包括退出码 0）；随机端口不验证原配置端口；没有显式 cwd；若需要持续服务或即时返回需另行授权实现 Popen/就绪检查。

### 12.7 后续范围

- 当前继续按用户指定函数逐步实现；不自动新增前端、确认接口、MCP 工具注册或主循环。
- 项目声明版本与 find_java_exe 完整版本号的匹配、构建 JAVA_HOME 设置、启动器自动选择尚未接通。
- 第 11.5 节 bfs_read 问题本轮未重新核验，不应当作当前仍存在的确定缺陷，也不能声称已修复。


## 13. 2026-09-22 五家模型聊天接口与最新集成状态

### 13.1 已确定的客户端生命周期约定

- 用户明确要求五个聊天函数全部接收外部创建的 `client`，以便多轮复用连接池。
- 聊天函数内部不创建客户端、不读取密钥、不调用 `close()`，也不使用 `with ... as client` 管理客户端。请求失败后仍把客户端留给调用方处理。
- 调用方在 Agent/会话开始时创建所需客户端，配置 API Key、服务地址、代理；在整个任务结束时关闭。不要把每次请求后关闭客户端的旧实现恢复回来。
- 连接复用不等于对话记忆；调用方仍需维护 `query` 消息历史，并处理工具调用结果回传。
- 本轮只完成聊天封装接口改造，没有新增全局单例、客户端管理类或 Agent 主循环。

### 13.2 当前函数签名与密钥读取函数

```python
chatGpt_chat(client: OpenAI, query: list[dict], model: str,
             tools: list[dict], expected_temperature)
zhipu_chat(client: ZhipuAiClient, model: str, query: list[dict],
           tools: list[dict], temperature: float)
claude_chat(client: Anthropic, model: str, query: list[dict],
            tools: list[dict], temperature: float | None = None,
            max_tokens: int = 4096)
deepseek_chat(client: OpenAI, model: str, query: list[dict],
              tools: list[dict], temperature: float)
qwen_chat(client: OpenAI, model: str, query: list[dict],
          tools: list[dict], temperature: float)
```

- 保留函数名和其余参数顺序；GPT 的 `query` 在 `model` 前，温度参数名为 `expected_temperature`。推荐关键字传参，避免混用位置。
- 旧的 `api`、`zhipu_api`、`claude_api`、`deepseek_api`、`qwen_api` 参数均替换为 `client`；GPT 的 `http_proxy`、`https_proxy` 参数已移除，代理应在外部构造客户端时配置。

| 文件 | 保留的密钥读取函数 | 环境变量名 | 客户端类型 |
|---|---|---|---|
| `Chatgpt_chat.py` | `get_gpt_api()` | `OPENAI_API_KEY` | `OpenAI` |
| `GLM_chat.py` | `get_zhipu_api()` | `GLM_API_KEY` | `ZhipuAiClient` |
| `Claude_chat.py` | `get_claude_api()` | `ANTHROPIC_API_KEY` | `Anthropic` |
| `DeepSeek_chat.py` | `get_deepseek_api()` | `DEEPSEEK_API_KEY` | `OpenAI` |
| `Qwen_chat.py` | `get_qwen_api()` | `DASHSCOPE_API_KEY` | `OpenAI` |

- 读取函数仍返回 `status/message`；成功的 `message` 是密钥，不能打印或记录到日志。聊天函数不再自动调用这些读取函数。
- DeepSeek 先前使用的地址为 `https://api.deepseek.com`，Qwen 为北京地域 `https://dashscope.aliyuncs.com/compatible-mode/v1`；地址现在由调用方设置，不能给两者传入默认指向 OpenAI 的客户端。

### 13.3 请求、返回和兼容性边界

- 成功返回 `{"status": "success", "message": 响应字典}`；异常返回 `{"status": "error", "message": str(e)}`。GPT/GLM 使用 `to_dict()`，其余三个使用 `model_dump()`。
- `message` 保存完整供应商响应，不只是文本；Claude 的 `content` 与其他接口的 `choices` 没有统一转换，工具格式也没有自动跨供应商转换。
- 无工具时省略 `tools/tool_choice`；Claude 有工具时使用 `{"type": "auto"}`，其余使用字符串 `"auto"`。Claude 的工具需使用原生 `input_schema` 等格式，不能把旧 OpenAI 嵌套 function 定义直接当作已适配。
- Claude 已补上 `max_tokens`，默认 4096，可显式覆盖。本机 Anthropic 1.7.0 的 `messages.create()` 不接受直接的 `temperature=`；封装默认不发送温度，显式传值时通过 `extra_body` 写入。模型是否支持温度仍须按实际模型确认，不能声称所有 Claude 模型都兼容。
- GLM 当前传 `thinking={"type": "disabled"}`；GPT/GLM 显式设置请求超时 90 秒。模型自身是否支持这些选项仍需实际确认。
- DeepSeek 显式 `stream=False`；其余也未实现流式消费。没有自动切换模型、重试策略改造或自动修复模型参数的逻辑。

### 13.4 本轮验证记录

- 使用 `D:\anacode\python.exe`；本轮检查到 openai 2.24.0、httpx 0.28.1、zai-sdk 0.2.3、anthropic 1.7.0。版本仅为本机检查记录，不是依赖锁定文件。
- 早期修复阶段：GPT/GLM 通过 26 项离线检查，Claude/DeepSeek/Qwen 通过 38 项离线检查，包含真实 SDK 配合模拟 HTTP 传输的参数和错误处理检查；当时还采用每次调用创建/关闭客户端，已被最终外部 client 方案替代。
- 最终外部 client 版本：五个原文件通过语法检查及共 20 次模拟调用，覆盖成功、连续调用、失败及失败后继续复用；确认聊天函数不创建或关闭传入的客户端。
- 没有调用真实模型 API，没有验证真实密钥、网络代理、模型权限、连接复用性能或完整 Agent 工具循环。不要把旧测试脚本的参数接口当作最终版本。
- 临时验证文件位于 `D:\CodexWork\temp\chat_bugfix`、`chat_bugfix_more`、`chat_clients`；它们不属于项目正式测试套件。

### 13.5 当前 MCP 配置与后续范围

- `get_remote_mcp_link()` 配置 GitHub Docker 镜像、`npx -y @playwright/mcp@latest` 和 `uvx` Docker MCP 服务。GitHub 配置从环境变量 `GITHUB_API` 读取并传给子进程的 `GITHUB_PERSONAL_ACCESS_TOKEN`，设置 `GITHUB_READ_ONLY=1`；Docker MCP 设置 `DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1`。这些是源码中的配置，不代表已验证实际权限或防护效果。
- 当前 Host 通过通用 `mcp_client` 初始化连接并列工具；示例 `main()` 退出 AsyncExitStack 后再打印保存的客户端/路由，此时连接已关闭。正式调用必须放在栈的有效生命周期内。
- Host 当前使用 `from MCP_client import ...`、`from Search... import ...` 的导入方式；未验证从项目根目录按包启动，不能直接沿用旧搜索模块命令。
- 系统文件服务、信息收集服务已有独立包装，但当前 Host 示例只遍历 `get_remote_mcp_link()` 的返回值；未确认它们已被接入同一 Host。
- 本轮聊天函数调用搜索只发现定义，尚未发现项目内调用方；外部 client 的创建、关闭、消息历史维护、工具执行和结果回传仍待用户指定后接入。
- 后续可按用户指令逐步接入主循环、核对各供应商消息/工具格式、验证 MCP 与真实模型调用。记录待办不代表授权自动安装依赖、启动容器、调用收费 API 或扩展架构。


### 13.6 本次只读发现、尚未修复的问题

- `State/save_chat_history.py` 原有Path序列化及JSONL分隔问题已于2026-09-23修复；保留原函数名，最新状态见第14节。
- `collect_information/collect.py` 使用 `if not str`，检查的是内置类型而不是 `needed`；信息文件不存在时没有明确返回。本轮未读取 INFORMATION.json 内容，未运行收集工具。
- `Context/History_Resorce` 已实现上下文读取、查错和只追加的修复/失效关联恢复；最新设计与验证边界见第14节，原零字节及草稿记录已过时。
- 系统文件服务的安装工具包装传入 `interactive=True`，与 stdio 服务不能使用控制台 input 的约定冲突；尚未验证其他确认工具的完整调用链。
- `get_system_stdio_parameters()` 当前为 `command='cmd'`、参数 `['python', '-c', 'MCP_functions/System_Files/Files_server/mcp_system_server.py']`，不是已验证的正确脚本启动命令，且没有使用约定的显式解释器路径。后续接入前需要单独检查修复。
- 上述仅为代码阅读发现，未授权扩展到本轮修复；不能把“已有文件/工具注册”当成“已可用”。

## 14. 执行历史、监督评价与上下文检索（2026-09-23 更新）

本节覆盖此前第14节的草稿状态，以当前源码为准。记忆读写、关联恢复及历史 MCP Tool 注册已实现；模型调度虽已有 AgentLoop 草稿，但自动修复和完整闭环尚未接通，最新状态见第 15 节。

### 14.1 用户约定与标识范围

- 完整执行历史只追加，不回写旧记录。seq=3失败、seq=5修好时，第3轮仍保持原内容，修复信息写在第5轮。
- 会话由 session_address 目录区分；State三个保存函数和Context两个历史模块已删除 session_id 参数、保存字段和筛选条件。旧JSONL无需迁移，多余字段可被忽略。backend创建目录所用session_id仍保留。
- chat_id区分同目录中的聊天，task_id标识plan拆分的子任务，seq标识执行记录。调用方负责编号，同一chat内(task_id, seq)必须唯一；当前未检查重复键，共用读取器遇到重复键会覆盖内存条目，调用方不得复用。
- repairs/invalidates仅引用同一chat的记录，格式均为列表，如 [{"task_id": 1, "seq": 3}]。不通过session_id定位，不跨chat引用；可跨task引用。
- 实际先后顺序取JSONL追加顺序，不按seq排序；关联应指向已出现的旧记录，不支持引用未来记录。

### 14.2 保存结构与字段语义

- State/save_executor_state_history.py：save_state_history()保存chat_id、task_id、target、seq、tool_name、input_content、output_content、exists_error、error_message、supervisor、is_solved、repairs、invalidates。
- supervisor嵌套字段为description_content、supervisor_judge_error、supervisor_judge_message；message注解已为str。总结读取路径是supervisor.description_content。
- exists_error/error_message记录工具错误；supervisor字段记录监督评价。is_solved表示“本次执行当时是否通过检查”，不是随后更新的任务状态；历史True也可能后来失效。调用方负责这些字段的一致性及真实验证，保存函数不自动判断正确性。
- repairs是可选参数，默认None，保存为[]；声明本次执行修复了哪些旧记录。只有is_solved is True且这条修复记录自身未失效时，才从待处理列表移除目标。无关成功和失败的修复尝试不会解决旧问题。
- invalidates是可选参数，默认None，保存为[]；声明已确认哪些旧结论失效，不要求当前执行成功。仅怀疑时先回查，确认后由调用方明确传入，读取器不会自动推断。
- 两类引用在保存时检查列表、字典、整数task_id/seq及不可自引用；不验证目标存在性。JSONL已有数据假定符合结构，损坏JSON、异常引用结构尚无统一容错。
- `State/save_chat_history.py` 当前函数名为 `save_chat_history`；已把 `session_address` 转为字符串，并用 `ensure_ascii=False` 及换行保存 UTF-8 JSONL。没有修复或迁移旧损坏文件。
- State/save_supervision_state_history.py保存监督者的工具执行历史；修复和失效关联存于executor记录，不在该文件新增另一套评价机制。

### 14.3 当前Context与查错分工

| 文件/函数 | 当前行为 |
|---|---|
| history_state.py / read_memory_state(path, chat_id) | 共用状态恢复，返回records、invalid、pending；只读原文件 |
| mcp_history_resource_service.py / read_task_all_history(session_address, chat_id, task_id) | 当前task的有效总结及必要的失效待解决提示 |
| 同文件 / read_chat_history(session_address, chat_id, task_id) | 同chat中task_id小于当前task的总结/提示；识别关联时仍扫描整个chat，故后续task的修复也可生效 |
| 同文件 / read_history_chat(session_address) | 读取该目录chat_history.jsonl的全部聊天记录；不自动发现其他会话目录 |
| mcp_history_error.py / read_question_task(session_address, chat_id, task_id) | 指定task当前尚无有效后续修复的问题，包括原失败记录和后来失效的旧成功记录 |
| 同文件 / read_history_by_seq(session_address, chat_id, task_id, seq) | 按编号读取原始输入输出及总结；不受is_solved或旧supervisor错误判断限制 |

- 共用恢复先遍历同chat记录，收集有效指向旧记录的invalidates；再按追加顺序恢复pending。失效声明将目标加入pending；有效成功的repairs移除目标；is_solved is False的执行本身加入pending。
- 一条修复记录后来被invalidates时，其修复效果不再计入，先前被它解决的问题会重新出现。当前失效声明持续有效：即使声明记录自身后来失效，也不会自动撤回其旧声明；尚无“撤回失效判断”机制。
- 正常上下文只纳入is_solved is True且未失效的记录，返回task_id、seq、target、tool_name、description，不携带原始大段输入输出。
- 已失效且仍pending的旧记录不输出旧结论，而输出status="unresolved_invalidated"、invalidated_by={task_id, seq}和简短待解决说明，保留原task_id、seq、target用于回查。说明来自最新失效声明的supervisor_judge_message，缺失时用description_content。
- 目标得到有效修复后移除该提示，但旧结论仍失效，不能重新进入上下文。新修复结果按其所属task正常读取。
- 普通False失败记录默认不进入普通上下文，其详情在查错通道提供；失效提示仅针对后来被明确invalidates的记录。
- 查错返回失效原记录时保留原始is_solved和输入输出，并附invalidated_by及最新监督修改建议。读取返回对象的裁剪不会修改磁盘。

### 14.4 实际例子与调用规则

1. task=1、seq=3把接口字段user_id改成id，编译通过，保存is_solved=True。
2. seq=30联调确认第3轮与前端不兼容，新记录写invalidates=[{"task_id":1,"seq":3}]。旧第3轮不改；其错误结论退出普通上下文，出现待解决提示，并可由查错模块按编号读详情。
3. seq=32恢复字段且验证通过，新记录写is_solved=True、repairs=[{"task_id":1,"seq":3}]。第3轮提示消失，旧结论不会复活；第32轮成为有效上下文。
4. 如果第30轮自身保存为False，它也是独立失败记录；第32轮需要同时关联第30轮以及确实已解决的其他失败尝试，不能只关联第3轮就假设全部问题解决。
5. 同一记录可以同时声明invalidates与repairs（已确认原因且本次直接修好），先使旧结论失效，再清除该目标待处理状态。

完整设计流程：工具执行 → supervisor判断与总结 → 追加保存 → 加载有效上下文 → 发现疑点 → 按task/seq回查原始记录并核对当前代码 → 确认后追加失效关联 → 修复并验证 → 追加成功修复关联。读取历史不等于自动发现错误，也不等于回滚代码。

### 14.5 已验证与尚未接入

- 已通过语法、正常保存/读取、旧格式缺少关联字段、空行、任务筛选、跨task修复、chat隔离、失败修复不生效、无关成功不生效、失效提示、修复后提示消失、修复记录失效后问题重新出现、自引用拒绝和旧记录字节不变等离线验证；聊天历史连续追加与读取也已验证。
- 两个历史模块以Context.History_Resorce.history_state包路径导入共用函数；已在项目根目录加入sys.path的环境验证导入，调用方应确保项目根目录可导入。
- 当前每次上下文/待处理查询仍读取整个executor文件并在内存恢复同chat状态，无缓存、分页或索引；不能宣称已实现按需增量读取。
- 用户希望优先检索supervisor总结而非代码正文；关键词搜索仍未实现，当前直接读取范围内总结。暂不引入向量检索。description应保留文件/函数/字段名、变化、验证情况和未解决事项。
- 后续Host/AgentLoop计划在任务开始或恢复时加载，在执行中维护内存上下文；失效或修复发生后需刷新内存，否则旧模型上下文不会自动更新。当前已有五个 executor 历史 MCP Tool 和一个 supervisor 历史 MCP Tool 的注册代码，AgentLoop 也尝试注册它们，但调度闭环尚未接通；真实模型端到端调用、自动测试修复和跨目录会话发现未验证。
- 本轮仅按授权实现记忆读写和关联机制，不宣称完整Agent闭环可运行。原先JSONL空行判断、缺少结果追加、总结字段名、错误pop、session_id冗余及聊天Path序列化等问题已修复，不再作为当前待办。

## 15. AgentLoop、技能与 checkpoint 当前状态（2026-09-23 最新核对）

本节记录当前磁盘源码，覆盖第 2、6、8、9、13.5、13.6 和 14.5 节中关于“尚未发现 AgentLoop”“没有历史 Tool 注册”或“尚未出现调用方”的旧描述。这里只记录现状，不代表主循环已经可运行。

### 15.1 已写入源码的结构

- `AgentLoop/agent.py` 已定义 `Checkpoint` 数据类，字段为 `checkpoint_id`、`chat_id`、`task_id`、`seq`、`target`、`user_query`、`task_list`、`executor_messages`。
- `create_checkpoint(state, executor_messages, checkpoint_id)` 已存在，使用 `deepcopy` 复制 `task_list` 和 `executor_messages`，避免后续修改原消息时连带改变旧快照。
- `state` 已增加 `backtrack_to` 和 `backtrack_reason`，`state_init()` 将其初始化为 `None` 和空字符串。`Checkpoint` 不是 `state` 的完整副本，只保留计划位置和 executor 输入相关字段。
- `main()` 已包含模型供应商映射、executor/supervisor 客户端创建、读取 `executor_plan_skill.md` 生成任务列表、注册历史/监督历史/系统/外部 MCP 服务，以及 `while task_id < len(task_lists)` 的任务循环草稿。
- 工具权限意图已经体现在列表上：executor 的工具列表排除 `read_supervisor_history`；supervisor 预定获得 Host 中的全部工具。当前 supervisor 调用部分尚未完成，因此这只是声明和筛选结构，未形成可运行的权限闭环。
- `Skills/executor_plan_skill.md` 要求规划阶段根据实际连接的非历史工具定义制定计划；`Skills/supervisor_skill.md` 约定监督 JSON 包含 `repairs`、`invalidates`、`is_task_finished`、`backtrack_to` 和 `backtrack_reason`。项目中没有 `executor_memory_skill.md`，不要再把它写成当前文件。

### 15.2 历史记录能否构造 checkpoint

- `read_history_by_seq(session_address, chat_id, task_id, seq)` 会从 `executor_history.jsonl` 返回完全匹配的原始执行记录列表，并附加 supervisor 描述。当前定位键仍包含 `task_id`，不能只按 `chat_id + seq` 假定唯一。
- 该函数可以作为回溯入口：先根据 supervisor 给出的 `backtrack_to={task_id, seq}` 查到旧执行，再依据返回记录构造一个简化 checkpoint。它读取历史，不会自动恢复 state，也不会撤销文件修改。
- 当前 executor 历史保存 `target`、`input_content`、`output_content`、工具错误、监督结论、`repairs` 和 `invalidates`；没有保存完整的 `task_list`、旧 `executor_messages` 或调用前完整 state。因此现有历史只能构造“旧任务和旧工具输入”级别的简化 checkpoint，不能还原当前 `Checkpoint` 数据类要求的完整消息快照。
- 如果后续要从历史精确构造当前 `Checkpoint`，需要在执行前保存必要输入，或给历史记录新增明确的输入快照字段/单独的 checkpoint 存储。恢复时不应把 `state.seq` 改回旧值；旧 `seq` 用于定位，新尝试继续分配新编号。
- checkpoint 只恢复 Agent 的输入和任务位置。文件、数据库、命令和外部服务状态不会自动回滚，重新执行前必须读取当前实际状态。

### 15.3 当前未接通和已确认的阻塞问题

- `AgentLoop/agent.py` 当前第 374 行只有一个不完整的 `def`，AST 解析直接报 `SyntaxError`；当前文件不能导入或运行。本轮仅更新本文档，没有修改该业务文件。
- `create_checkpoint()` 当前没有调用方，也没有 checkpoint 字典、JSONL 保存、按历史编号构造 checkpoint 或 `restore_checkpoint()`；`backtrack_to/backtrack_reason` 只是字段和技能输出约定，尚未执行回溯。
- 外层循环读取局部 `task_id` 和 `target_task`，但尚未把它们写入 `state.now_task_id/state.now_target`，也没有看到 `task_id += 1`。历史保存函数要求这两个 state 字段有效。
- executor 工具调用链仍不匹配：模型返回的 tool call 是字典时，当前代码把整个字典作为 `tool_dictionary` 的键并访问 `tool.name/tool.arguments`；`session_dictionary` 保存的是 `mcp_client` 包装对象，当前却检查 `ClientSession` 并按原始 session 接口调用。原始 MCP `CallToolResult` 也不能按普通字典直接 `.get("status")`。
- 当前循环没有调用 `save_state_history()`；`state.seq` 在工具执行后立即增加，尚未明确“保存本次编号再递增”的顺序。supervisor 调用、反馈解析、`save_supervisor_state_history()`、任务完成分支和回溯分支在当前文件末尾均未完成。
- 规划技能要求把已连接的非历史工具暴露给 plan，但当前规划发生在 MCP 注册之前，并明确传入 `tools=[]`；技能中的静态工具目录只能辅助规划，不能证明运行时工具已经连接。
- executor prompt 构造中 `seq` 行后缺少分隔逗号，且 `getattr(state, "supervisor_description")` 没有默认值，而 `state_init()` 没有初始化该字段；进入首轮时可能先触发属性错误。
- `MCP_hosts.py` 生成的 OpenAI 风格工具项顶层当前是 `{"name": "function"}`，通常应核对为 `{"type": "function"}`；Claude 工具 schema 还需要单独适配。`GLM_chat.py` 成功分支混用对象属性和字典下标读取响应。这些是当前源码事实，尚未在本轮修改。
- `get_system_stdio_parameters()` 的旧启动命令问题仍见第 13.6 节。历史工具和 supervisor 工具的 stdio 参数使用 `sys.executable -m ...`，但没有显式 `cwd`；能否从实际启动目录导入项目包仍需验证。

### 15.4 推荐的最小后续顺序

1. 先修复 `agent.py` 末尾语法错误，使文件可以导入；不要同时重写整个循环。
2. 明确一轮执行的编号顺序：设置 `now_task_id/now_target` → 保存或创建执行前输入 → 模型决策与工具执行 → supervisor 检查 → 以当前 `seq` 追加历史 → 再递增编号。
3. 先在内存中用 `(chat_id, task_id, seq)` 保存和查找 checkpoint，跑通一次回溯；需要跨进程恢复时再决定扩展 executor 历史还是增加单独 checkpoint 文件。
4. 按现有 `mcp_client.call_tool(tool_name, argument)` 接口最小修复工具解析和路由，再接 supervisor；不要先扩大到完整框架重构。
5. 回溯闭环跑通后，再验证不同模型供应商的原生工具 schema、消息回传和真实 MCP 服务，不能用语法通过代替端到端验证。

## 16. Agent 分层、ToolRegistry 与独立循环（2026-09-24 最新状态）

本节覆盖第 15 节中关于 `agent.py` 语法错误、Supervisor 技能结构、工具 schema、工具路由和独立循环尚未实现的旧状态。以下内容以 2026-09-24 实际写入磁盘并离线验证的源码为准；`agent.py` 的主任务循环仍保留旧草稿，尚未接入新组件。

### 16.1 AgentState 当前结构

- `AgentLoop/agent.py` 已通过 AST 检查和项目根目录真实导入检查；第 15.3 节所述“末尾不完整 def 导致 SyntaxError”已不再成立。
- 原先只有类型标注、字段未初始化的 `state` 已改为 `AgentState` 数据类；新增 `CorrectnessReviewState` 和 `ErrorReviewState` 两个子状态，分别承载正确性审查结果和错误修改建议。
- `AgentState` 当前初始化 `session_address`、字符串 `chat_id`、`task_list`、`now_task_id`、`now_target`、整数 `seq/supervisor_seq`、模型名称、浮点温度、循环上限、当前工具结果、executor 文本结果、错误信息、Java/Python 环境和两个审查子状态。
- 正确性子状态字段为 `review_status`、`supervisor_check`、`supervisor_description`、`is_task_finished`、`needs_error_review`、`error_review_context`、`repairs`、`invalidates`。
- 错误审查子状态字段为 `error_status`、`error_type`、`root_cause`、`supervisor_description`、`executor_instruction`、`verification_requirement`、`invalidates`、`backtrack_to/backtrack_reason`、`is_blocked/block_reason`。
- 为兼容现有历史保存和旧调用，`AgentState` 暂时保留 `executor_seq`、`supervisor_check`、`supervisor_description`、`supervisor_error_advice`、`is_task_end`、`repaired`、`invalid`、`backtrack_to/backtrack_reason` 属性映射；旧代码可以继续访问，但新循环应优先使用两个子状态。
- `state_init()` 已返回完整 `AgentState` 并写入 executor/supervisor 模型名、温度和 Java/Python 配置。当前没有把 API Key、模型客户端、MCP Session 或 Host 放入 state。
- 独立循环运行时会动态建立 `executor_tool_events` 和 `supervisor_tool_events`；这两个字段尚未正式声明进 `AgentState`。用户已明确暂缓修改 `agent.py` 主逻辑，后续接线时应决定是否将它们加入数据类并设置 `default_factory=list`。

### 16.2 两份 Supervisor 技能

- `Skills/supervisor_skill.md` 已替换为正确性与完成度审查技能：输出 `review_status`、`supervisor_check`、`supervisor_description`、`is_task_finished`、`needs_error_review`、`error_review_context`、`repairs` 和 `invalidates`。它可以判定错误或证据不足，但不负责撰写具体修复方案。
- `Skills/supervisor_error_skill.md` 已新增为错误诊断与修改建议技能：逐步读取当前任务待解决问题、精确旧记录、当前有效摘要、前序依赖、监督历史和必要的聊天原文；输出错误类型、证据支持的根因、给 executor 的单一最小指令、验证要求、失效关联和回溯建议。
- 两份技能都允许使用 `bfs_read.py` 暴露的 `read_all_files_tool`、`sort_files_by_suffix_tool` 和 `sort_files_by_mother_tool` 检查项目结构；均记录了递归读取可能过大、单文件不返回内容以及 `sort_files_by_mother` 当前实际按后缀分组的限制。
- 两份技能都禁止 Supervisor 直接写文件、运行代码、安装依赖、打包、启动服务或执行外部系统写操作；这些动作属于 executor。技能文本声明能力边界，真正的运行时强制权限由 `ToolRegistry` 执行。
- 两份技能均使用 UTF-8，并通过 `skill-creator/scripts/quick_validate.py` 的 UTF-8 模式校验。

### 16.3 ToolRegistry 与权限分区

- 新增 `MCP_functions/tool_registry.py`，定义 `AgentRole`、`ToolExecutionResult` 和 `ToolRegistry`。
- `ToolRegistry.call(role, tool_name, arguments)` 先验证角色、工具名和参数字典，再通过 Host 的 `tool_dictionary` 找到所属客户端，并从 `session_dictionary` 取得现有 `mcp_client` 包装对象，最终调用当前接口 `client.call_tool(tool_name=..., argument=...)`。
- 未注册工具、角色越权、参数格式错误、客户端缺失和 MCP 调用异常都会转换为 `ToolExecutionResult(ok=False, error_type=..., message=...)`，不会绕过权限直接执行。原始 `CallToolResult` 通过 `model_dump()`/`dict()` 转为 JSON 可保存结构，同时识别 MCP `isError` 和工具业务返回中的 `status=error/failed/failure`。
- Supervisor 私有工具为五个 executor 历史读取工具和 `read_supervisor_history`；项目结构三项工具为 Supervisor/Executor 共享只读工具；系统写入、运行、打包、解释器探测和信息收集工具默认只属于 Executor。
- GitHub、Browser 和 Docker MCP 的具体工具名运行时动态发现，当前按 `client_name` 默认只分配给 Executor。显式工具规则优先于服务默认规则；没有显式规则也没有服务默认规则的工具默认拒绝。
- `schemas_for(role, provider)` 为每个角色只返回其允许看到的工具，并分别生成 OpenAI 风格 `type=function` schema 和 Claude 原生 `input_schema` schema。
- Registry 初始化时检查重复工具名并直接报错，避免 Host 原先静默保留第一条路由造成歧义。
- `MCP_functions/MCP_hosts.py` 的 OpenAI 风格 schema 顶层已从错误的 `{"name":"function"}` 修正为 `{"type":"function"}`；第 15.3 节对应问题已解决。

### 16.4 模型工具协议适配

- 新增 `AgentLoop/loop_utils.py`，集中处理五家模型聊天封装与工具循环之间的协议差异，避免 Supervisor/Executor 循环各自重复判断供应商。
- `call_chat_function()` 对 ChatGPT 使用其现有参数名 `expected_temperature`，其余封装使用 `temperature`。
- `normalize_tool_calls()` 把 OpenAI 兼容结构中的 `function.name/function.arguments` 和 Claude 结构中的 `name/input` 转为统一的 `NormalizedToolCall`；OpenAI 字符串参数会执行 JSON 解析，缺少名称或参数不是字典时拒绝继续。
- `append_tool_exchange()` 会把 assistant 原始 tool call 和 MCP 结果按供应商要求重新放回消息历史：OpenAI 兼容格式使用带 `tool_call_id` 的 `role=tool` 消息；Claude 使用 `tool_use` 和 `tool_result` content block。
- `parse_json_object()` 解析 Supervisor 最终 JSON，并兼容模型偶尔返回的 Markdown JSON 围栏；返回非对象或空内容会明确报错。

### 16.5 Executor 与 Supervisor 独立循环

- 新增 `AgentLoop/executor_loop.py`，提供 `run_executor_loop(now_state, executor, executor_client, chat_function, executor_tools, tool_registry)`。
- Executor 循环只推进当前 `now_target`，接收上次正确性总结、`error_review.executor_instruction` 和验证要求；每轮模型请求若产生工具调用，则通过 `ToolRegistry.call(AgentRole.EXECUTOR, ...)` 执行、记录事件并把结果回传模型，直到模型给出最终文本或达到 `max_executor_steps`。
- Executor 循环开始时重置本轮工具字段和错误字段；每个工具事件记录 step、call_id、工具名、参数和标准化结果。达到上限、模型请求失败、工具参数不可解析或既无文本也无工具时返回明确错误状态，不进行无限重试。
- `AgentLoop/supervisor_loop.py` 已重写，提供 `check_right_task(...)` 和 `review_error_task(...)` 两个独立入口，分别读取 `supervisor_skill.md` 和 `supervisor_error_skill.md`。
- 两个 Supervisor 入口共用 `_run_supervisor_model_loop()`；只有模型提出 MCP tool call 时才继续工具循环，没有工具调用时解析最终 JSON并返回。达到 `max_supervision_times`、模型报错或 JSON/字段校验失败时，写入 blocked 状态而不是静默返回。
- 正确性结果严格校验四种 `review_status`、布尔字段、完成条件及 repairs/invalidates 引用；错误结果严格校验三种 `error_status`、五种 `error_type`、回溯引用、阻塞字段和字符串字段。
- 正确性审查失败只返回是否需要错误审查及上下文；错误审查只提供 executor 指令，不直接修改代码。修复后应由外层调度器再次调用正确性审查。

### 16.6 已验证范围

- 使用 `D:\anacode\python.exe -X utf8` 对 `tool_registry.py`、`MCP_hosts.py`、`loop_utils.py`、`executor_loop.py`、`supervisor_loop.py` 和当前 `agent.py` 完成 AST 检查，六个文件均通过。
- 已用模拟 Host、MCP Client、模型响应和 AgentState 运行离线测试，覆盖：Supervisor 可读取项目结构但不能调用写入工具、Executor 工具可见性、OpenAI 参数 JSON 解析、Executor“模型 → MCP → 模型总结”回合、正确性 JSON 映射及错误建议 JSON 映射。
- 目标文件写入后重新从项目根目录运行同一离线测试并通过；`agent.py` 在写入循环组件时经哈希确认未改变。
- 本轮没有启动真实 MCP 子进程，没有调用真实模型 API，没有验证 GitHub/Playwright/Docker 服务、系统文件 MCP 启动配置、供应商真实 tool-call 响应或外部副作用。因此只能确认组件级协议和离线路由逻辑，不能宣称端到端 Agent 已可运行。

### 16.7 当前未接入与下一步

1. 用户明确决定暂不由本轮改写 `agent.py` 主循环，计划后续自行编写接线逻辑。当前 `main()` 仍是第 15 节所述旧草稿，没有实例化 `ToolRegistry`，也没有调用 `run_executor_loop()`、`check_right_task()` 或 `review_error_task()`。
2. 后续主循环应在所有 MCP 连接建立且 `AsyncExitStack` 仍有效时创建 `ToolRegistry(host)`，再分别调用 `schemas_for(AgentRole.EXECUTOR, executor)` 和 `schemas_for(AgentRole.SUPERVISOR, supervisor)`。
3. 全局 `task_id`、`seq`、任务尝试次数、历史追加顺序和状态转移只由 `agent.py` 管理；Executor/Supervisor 循环不应自行推进全局任务。推荐顺序仍为：设置当前任务 → Executor 回合 → 正确性审查 → 必要时错误建议 → 追加历史 → 增加 seq → 推进或重试任务。
4. 主循环接线时应把 `executor_tool_events` 和 `supervisor_tool_events` 正式加入 `AgentState`，或改为显式的回合返回对象；当前循环为兼容暂时动态创建属性。
5. `save_state_history()` 目前仍保存一个根级 `tool/tool_input/tool_results`，而 Executor 回合可能包含多个工具事件；后续必须决定按回合聚合保存还是逐工具保存，不能默默丢失中间事件。
6. 规划仍发生在 MCP 注册之前并传入 `tools=[]`；若继续遵守 `executor_plan_skill.md` 的真实能力规划约定，应在主循环改写时把 MCP 注册提前，或给规划阶段建立同样的有限工具循环。
7. checkpoint 创建、持久化和实际恢复仍未接入；错误技能输出 `backtrack_to` 只是一项建议，不能解释为文件或外部状态已经回滚。
8. `get_system_stdio_parameters()` 的启动命令问题、历史 MCP 子进程 cwd/包导入问题、`GLM_chat.py` 响应读取混用对象与字典的问题仍待单独验证或修复。
9. 完成主循环接线后，先用本地无副作用假 MCP 跑通任务推进和历史顺序，再分别验证真实供应商 schema 与 MCP 服务；不要直接以真实写文件、安装、容器或付费 API 调用作为第一次集成测试。
