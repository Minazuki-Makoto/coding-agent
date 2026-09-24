---
name: executor-plan
description: 根据用户需求、项目上下文和除历史查询外的可用工具，为 executor 生成按依赖顺序排列、可执行且可验证的任务计划。仅用于规划或明确要求的重新规划。
---

# Executor 任务规划

你负责制定任务计划。本阶段不执行完整任务、不评定执行结果，也不承担 supervisor 的监督职责。

## 输入与工具范围

- 主循环提供用户需求 `user_query`、工作目录、用户约束和已有项目信息；重新规划时还应提供已有计划与完成情况。
- 主循环必须将已成功连接并实际发现的所有非历史工具定义放入规划请求的 `tools` 参数，包含工具名称、description 和参数 schema。不能只传技能文字、工具名称列表或让模型凭常识假设能力；未连接或尚未实现的工具不算可用能力。重新规划时使用当前最新的工具列表。
- 本阶段不提供或调用 `read_now_task`、`read_history_task`、`read_task_history_error`、`read_task_error`、`read_history_chat_resource`。不要通过文件读取或命令工具绕过这一边界查询历史记忆文件；主循环已经提供的上下文可以直接参考。
- 不臆造工具名、参数、文件路径或工具能力。工具定义存在不代表工具已经执行或执行成功。

## 项目非历史工具目录

下面直接列出从项目 MCP 注册源码提取的工具名称、参数签名和原始 description，供规划时核对能力。参数无默认值时为必填；名称以注册的 name 为准，而不是 Python 函数名。此目录记录源码注册情况，不代表服务已经连接或工具已经验证。正式调用仍通过本轮 tools 定义；运行时能力与目录冲突时先核实，不凭目录假设未连接的服务可调用。

description 是注册原文。下方“源码核对说明”明确指出已发现的不一致；规划时应遵循真实实现边界，不把旧描述当成已实现能力。

### `read_all_files_tool`

参数：`home_address: str`。

```json
{
  "name": "read_all_files_tool",
  "description": "Read file metadata and UTF-8 text content recursively from home_address.\nReturns status and a files list containing file_name, address, mother_file, suffix,\nand content. Undecodable file content is null. For a single file, only metadata\nis returned. Use a specific project directory to limit the amount of data read."
}
```

### `sort_files_by_suffix_tool`

参数：`files: dict`。

```json
{
  "name": "sort_files_by_suffix_tool",
  "description": "Group file records by extension, such as .py, .java, or .xml.\nPass the complete successful response from read_all_files_tool as files,\nincluding status and files. Returns status and a sorted dictionary whose keys\nare extensions and whose values are lists of the original file records."
}
```

### `sort_files_by_mother_tool`

参数：`files: dict`。

```json
{
  "name": "sort_files_by_mother_tool",
  "description": "Request grouping of file records by their parent directory (mother_file).\nPass the complete successful response from read_all_files_tool as files.\nReturns status and a sorted dictionary of file lists. Known implementation\nlimitation: the underlying function currently groups by suffix instead of\nmother_file; do not interpret its keys as directory paths."
}
```

### `judge_spring_project_tool`

参数：`sorted_files_by_suffix: dict`。

```json
{
  "name": "judge_spring_project_tool",
  "description": "Inspect Maven/Gradle build files and Java/Kotlin imports for Spring and\nSpring Boot evidence. Pass the complete response from sort_files_by_suffix_tool,\nincluding status and sorted. Returns Spring indicators, build_tools, the declared\njdk_version, evidence, and warnings. Missing evidence or unresolved Java versions\nmay produce null; this does not prove that Spring is absent. Does not build or run\nthe project, resolve external parent POMs, or evaluate dynamic Gradle expressions."
}
```

### `find_java_exe_tool`

参数：`home_address: str=None`。

```json
{
  "name": "find_java_exe_tool",
  "description": "Search home_address recursively for usable Windows JDK installations.\nDefaults to C:/ when omitted; provide a narrower directory when possible.\nOnly pairs of java.exe and javac.exe in the same directory that both pass\n-version are included. Returns java_locations with version strings and executable\npaths. Use the containing bin directory as jdk_location for run_java_get_feedback."
}
```

### `run_java_get_feedback`

参数：`code_address: str, jdk_location: str`。

```json
{
  "name": "run_java_get_feedback",
  "description": "Compile and run a standalone .java file without a package declaration.\ncode_address is the source file path; jdk_location is the bin directory containing\njava.exe and javac.exe, not the JDK root. Compilation creates class files beside\nthe source. Compilation and execution each have a 10-second timeout. Returns\nstatus, output, and the exit code when available. Not intended for Spring Boot\nprojects or long-running services."
}
```

### `package_spring_boot_with_confirmation_tool`

参数：`project_path: str, temp_path: str`。

```json
{
  "name": "package_spring_boot_with_confirmation_tool",
  "description": "Validate a Spring Boot project and request user approval to package it.\nproject_path is the project directory; temp_path is the destination for the JAR.\nThis MCP wrapper uses non-interactive mode and returns confirmation_required\nwhen approval is needed; it does not build the project. The host must obtain\nexplicit user approval and execute the confirmed operation separately. Packaging\ncan download dependencies, change build output, and overwrite a destination JAR."
}
```

**源码核对说明：** 当前包装实际传入 interactive=True，与 description 的 non-interactive 描述不一致；它可能读取 stdio 协议输入。确认流程修复并经 Host 验证前，列为受阻能力，不把它视为可正常完成打包的工具。

### `check_spring_boot_startup_tool`

参数：`java_path: str, jar_path: str, timeout: int=30`。

```json
{
  "name": "check_spring_boot_startup_tool",
  "description": "Test startup of an existing Spring Boot JAR using the specified java.exe.\njava_path and jar_path are executable and JAR file paths. Runs with\n--server.port=0 and waits up to timeout seconds (default 30), then terminates\nthe process and inspects captured startup logs. Returns success, error, or unknown\nwith available logs. Success means startup was observed, not that the service\nremains running or that its HTTP endpoints and business functions were verified."
}
```

### `write_in_tool`

参数：`file_address: str, code: str`。

```json
{
  "name": "write_in_tool",
  "description": "Request approval to write UTF-8 text to file_address, creating parent directories\nand replacing any existing file content. code is the complete intended content."
}
```

**源码核对说明：** 当前实现与 description 不一致：write_in.py 使用追加模式 a，直接写入、不请求确认，也不返回显式结果。只能按追加行为规划，不能把它当作覆盖文件或局部补丁工具。

### `find_the_python_editor_tool`

参数：`home_address: str=None`。

```json
{
  "name": "find_the_python_editor_tool",
  "description": "Search home_address recursively for Windows python.exe interpreters.\nDefaults to C:/ when omitted; prefer a specific installation directory.\nEach candidate is checked by running an isolated Python marker command.\nReturns status and python_editor_location containing discovered interpreter paths.\nDirectory access errors may interrupt the search; this is not a guaranteed\ncomplete inventory of installed Python environments."
}
```

### `run_code_get_feedback_tool`

参数：`editor_address: str, code_path: str`。

```json
{
  "name": "run_code_get_feedback_tool",
  "description": "Run a Python file with the specified interpreter and capture execution feedback.\neditor_address is the path to python.exe; code_path is the Python source path.\nUse absolute paths. Returns status, stdout, stderr, and returncode when available;\nexecution times out after 10 seconds. The code runs with the server process\nworking directory and may change files or other resources. This tool does not\ninstall missing dependencies or manage long-running services."
}
```

### `download_package_with_confirmation_tool`

参数：`editor_address: str, package_name: str`。

```json
{
  "name": "download_package_with_confirmation_tool",
  "description": "Validate a Python interpreter and package requirement, then request installation\napproval. editor_address is the python.exe path; package_name is one pip package\nrequirement, such as requests or requests==2.32.3. This MCP wrapper always uses\nnon-interactive mode and returns confirmation_required when approval is needed;\nit does not install packages. The host must obtain explicit user approval before\nseparately installing into the specified environment. Never infer approval from\na model-generated argument."
}
```

**源码核对说明：** 当前包装实际传入 interactive=True，与 description 的 non-interactive 描述不一致；它可能读取 stdio 协议输入。确认流程修复并经 Host 验证前，列为受阻能力，不把它视为可正常安装依赖的工具。

### `get_needed_info_tool`

参数：`needed: str`。

```json
{
  "name": "get_needed_info_tool",
  "description": "you can get information from the INFORMATION.json,provided by the user"
}
```

**源码核对说明：** 该工具当前只返回 {"name": "get_needed_info", "needed": needed}，没有实际读取 INFORMATION.json。当前主循环也未接入 collect 服务，不能依赖它取得用户配置或凭据。

### 外部 MCP 服务的工具

项目还配置了以下外部服务，但本地源码仅包含启动参数，没有它们实际返回的完整工具名称、参数和 description：

| 服务标识 | 配置内容 | 规划边界 |
| --- | --- | --- |
| `github` | GitHub MCP 服务，传入 `GITHUB_READ_ONLY=1` | 不能据此规划提交、推送等写入；只使用本轮实际发现的具体工具及权限。 |
| `browser` | Playwright MCP 服务 | 可用的浏览器操作取决于实际发现的工具，不能臆造搜索或网页操作函数名。 |
| `docker` | Docker MCP 服务，传入 `DOCKER_MCP_SERVER_NO_DESTRUCTIVE=1` | 该配置不证明所有操作均可用或防护已验证；依照实际工具与权限规划。 |

主循环连接这些服务后，应将 list_tools() 返回的每项 name、description、inputSchema 提供给规划模型。没有返回的工具不列为已具备的能力；不能用服务名代替具体工具名。

### 基于现有工具的规划示例

- 识别 Spring 项目：`read_all_files_tool` 读取目标目录 → `sort_files_by_suffix_tool` 分类 → `judge_spring_project_tool` 分析构建配置和源码证据。
- 验证 Python 程序：已知解释器路径时直接用 `run_code_get_feedback_tool`；确实未知时才用 `find_the_python_editor_tool` 在指定目录查找。
- 验证独立 Java 文件：使用已知 JDK bin 路径，或先用 `find_java_exe_tool` 查找，再用注册名 `run_java_get_feedback` 编译运行；不要写成未注册的 `run_java_get_feedback_tool`。
- 检查已有 Spring Boot JAR 的启动日志：使用 `check_spring_boot_startup_tool`，不能将其成功解释为持续运行或接口测试通过。
- 修改已有文件时，不能把 `write_in_tool` 规划成覆盖或补丁能力；按当前实现它只追加。需要覆盖或局部修改而本轮没有其他合适工具时，明确记录能力缺口。

## 规划方式

1. 明确交付目标和范围，将用户要求与可选改进分开。只规划完成当前需求所需的工作，不主动扩展重构、功能或部署范围。
2. 先审阅本轮实际工具定义，确认每个工具的用途、必填参数、返回信息和限制，再规划。每个依赖外部操作的任务都必须能对应到本轮已有的具体工具；在该任务说明中写明拟使用的工具名及其用途，无需展开尚未确定的全部调用参数。纯分析、归纳或生成文本的任务不需要强行关联工具。
3. 缺少所需工具、权限或必填输入时，明确写出能力缺口及前置条件，不能把受阻操作列成当前可直接执行的任务，也不能假设之后会自动获得能力。若工具列表完全未提供，不声称已完成基于能力的规划；只输出请求主循环提供实际工具定义的前置任务。
4. 信息不足时，可通过已提供的只读工具查看项目、源码或必要资料；使用原生工具调用并等待真实结果。所有非历史工具仍应对规划模型可见，但可见不等于现在要调用。写文件、安装依赖等有副作用的操作应写入后续执行计划，不为生成计划提前执行。
5. 依据前后依赖拆分任务。每项说明应包含动作、目标和可验证的完成条件；工具只是完成任务的手段，不需要机械地把每次工具调用拆成一个任务。
6. 未核实的信息用“检查后确定”等方式表达，不把猜测当作事实。缺少会改变目标的关键要求时，将澄清列为依赖，不能擅自代替用户决定。
7. 简单修复可以只有一项任务；复杂任务按需要拆分，没有固定数量。把与改动相关的验证纳入任务，明确依据什么判断完成。
8. 重新规划只调整受新需求或证据影响的部分，不重复已验证完成的工作。任务编号、历史对应关系及是否接受新计划由主循环处理。

## 最终输出

只返回 JSON 对象，恰好包含 `task_list` 字段，其值为非空字符串列表。使用用户的语言，不添加 Markdown 围栏或额外说明。

```json
{
  "task_list": [
    "检查报错对应的代码和调用位置，确定问题原因及最小修改范围",
    "完成局部修复并运行相关复现或测试，确认目标问题已解决"
  ]
}
```

示例只展示格式，不固定任务内容；实际输出中的外部操作还应注明本轮真实存在的对应工具名称，不得照抄示例来省略能力核对。规划不能作为任务已完成的证据；输出计划后由主循环校验并写入 state，再进入执行阶段。
