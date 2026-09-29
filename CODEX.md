# Coding Agent 当前项目说明

更新时间：2026-09-27

本文只记录当前源码已经存在的结构、行为、验证结果和已知问题。历史讨论、旧设计和计划不代表已经实现；开始新任务时必须以实际源码为准。

## 1. 开始任务时的读取顺序

1. 先读取本文件，恢复当前项目边界。
2. 再读取用户当前任务直接涉及的源码和 `PROBLEM.md`。
3. 历史说明与源码冲突时，以重新读取的源码为准。
4. 不因本文件存在待办就自动修改、运行、安装依赖或启动外部服务。
5. 默认 Python 解释器为 `D:\anacode\python.exe`；项目当前没有专用虚拟环境声明。

## 2. 项目目标与当前状态

项目目标是构建一个由 Supervisor 规划、验收和决策，由 Executor 调用 MCP 工具执行任务的 coding agent。主链路已经具备：

- 多模型适配器；
- Supervisor planning/evaluation/decision 三阶段；
- Executor 单工具执行循环；
- MCP Host、动态工具发现与角色权限；
- Executor、Supervisor、聊天三类 JSONL 保存；
- Pydantic 结构化输出校验；
- 历史查询 MCP；
- 文件结构浏览、定向内容读取、Java/Python 辅助工具；
- 离线单元测试。

当前主要未解决问题不是 JSON 字段校验，而是跨轮记忆与证据传递。详见 `PROBLEM.md`。真实付费模型和全部外部 MCP 的最新端到端运行效果尚未因本轮文档重写而重新验证。

## 3. 当前项目结构

```text
coding_agent/
├── CODEX.md
├── PROBLEM.md
├── README.md
├── INFORMATION.json
├── backend.py
├── codex_agentloop_repair_guide.md
├── AgentChat/
│   ├── Chatgpt_chat.py
│   ├── GLM_chat.py
│   ├── Claude_chat.py
│   ├── DeepSeek_chat.py
│   └── Qwen_chat.py
├── AgentLoop/
│   ├── agent.py
│   ├── executor_loop.py
│   ├── supervisor_loop.py
│   └── loop_utils.py
├── Context/
│   ├── mcp_resources.py
│   ├── mcp_supervisor_tools.py
│   └── History_Resorce/
│       ├── history_state.py
│       ├── mcp_history_resource_service.py
│       ├── mcp_history_error.py
│       └── mcp_supervisor_history.py
├── MCP_functions/
│   ├── MCP_client.py
│   ├── MCP_hosts.py
│   ├── tool_registry.py
│   ├── Search/mcp_search_server.py
│   ├── collect_information/
│   │   ├── collect.py
│   │   └── mcp_collect_server.py
│   └── System_Files/
│       ├── Files_function/
│       │   ├── bfs_read.py
│       │   ├── java_code.py
│       │   ├── python_code.py
│       │   └── write_in.py
│       └── Files_server/mcp_system_server.py
├── Skills/
│   ├── supervisor_plan.md
│   ├── supervisor_decison.md
│   └── memory_retrieval.md
├── State/
│   ├── save_executor_state_history.py
│   ├── save_supervision_state_history.py
│   └── save_chat_history.py
├── ExecutorBooks/
│   ├── Executor_textbook.md
│   └── Executor_mistake_book.md
└── tests/
    ├── test_agent_loop.py
    ├── test_bfs_read.py
    └── test_java_code.py
```

## 4. 主运行流程

入口为 `AgentLoop.agent.main(query, session_address, chat_id)`；`backend.py` 当前创建会话目录后用硬编码示例问题调用该入口，虽然创建了 Flask 对象，但没有定义 HTTP 路由。

主流程：

```text
读取 INFORMATION.json
  ↓
创建 AgentState / SupervisorState / EvaluationState / ExecutorState
  ↓
启动必需 MCP：memory、supervisor_memory、system、collect
  ↓
尝试启动可选 MCP：github、browser、docker
  ↓
Supervisor planning 生成 task_list
  ↓
Supervisor 为 task 0 生成初始指导和完成条件
  ↓
Executor 执行当前 task（每步至多一个工具）
  ↓
Supervisor evaluation 验收 Executor 本轮结果
  ↓
Supervisor decision 决定继续当前 task、调整未完成计划或推进 task_id
  ↓
最后一个 task 通过后保存最终聊天记录
```

同一 task 默认最多尝试 3 次；Executor 单轮最多 10 步；Supervisor 各阶段最多 10 轮；整个任务默认最多 80 次模型请求。达到限制会返回 blocked，而不是假装完成。

## 5. 状态对象职责

### 5.1 AgentState

保存整个请求的共享状态：会话地址、字符串 `chat_id`、用户问题、任务列表、当前 `task_id/target`、全局 executor/supervisor seq、当前 task 尝试次数、模型与温度、循环预算、Java/Python 环境，以及 Supervisor description 历史。

### 5.2 ExecutorState

保存当前 Executor 尝试：当前 task、Supervisor 指导、工具名和参数、真实工具结果、工具成功状态、description、`is_finished`、错误、最近 description 窗口、所选 skill、模型原始响应摘要和校验错误。

每次重新进入 `run_executor_loop()` 时，`memory_window`、`tool_result`、工具参数、description 等临时字段都会清空；seq 保持全局递增。这是当前跨尝试记忆丢失的重要原因之一。

### 5.3 SupervisorState 与 SupervisorEvaluationState

SupervisorState 保存 planning/decision 所需数据、当前 Executor 指导、是否推进、回溯信息、计划修改、最终答案、工具查询结果和模型校验状态。SupervisorEvaluationState 保存本次验收结论。

每次进入 `run_supervisor_loop()` 时，两者的临时窗口和工具状态会重置。evaluation 结论会传给同轮 decision，但旧轮证据不会自动形成稳定的 task 级证据状态。

## 6. 模型输出契约

### 6.1 Executor

`ExecutorOutPut` 使用 `extra="forbid"`，要求：

- `description` 必须是非空字符串；
- `is_finished` 必须是真正的 JSON bool，字符串 `"true"` 不接受；
- `tool_name` 只能为非空字符串或 null；
- skill 的 name/id 必须同时存在或同时为空；
- 校验失败会把错误类型、字段错误和最多 2000 字符原始响应摘要反馈给模型重试。

工具一旦执行，工具事实先保存。工具后总结失败最多重试两次总结，不重新执行工具。

### 6.2 Supervisor

- planning 必须返回非空 `task_list`；
- evaluation 只验收，不推进 task；
- decision 的 `is_next_target` 是唯一推进控制字段；
- decision 使用 `extra="forbid"`，拼错字段或缺少 `is_next_target` 会校验失败且不会推进；
- 计划调整不得改写已完成前缀；
- 最后一个 task 完成时应提供 `final_answer`。

## 7. 上下文预算

`AgentLoop/loop_utils.py` 当前限制：

| 内容 | 上限 |
| --- | ---: |
| 总模型上下文目标常量 | 80000 字符 |
| 用户问题 | 16000 字符 |
| description | 6000 字符 |
| 送回模型的单次工具结果 | 12000 字符 |

`bounded_text(..., keep_tail=True)` 会保留头尾并省略中间。完整工具结果仍可能保存在 JSONL，但模型看到的 observation 最多 12000 字符。因此“已经保存”不等于“下一轮模型能看到关键证据”。

## 8. 历史保存格式

### 8.1 executor_history.jsonl

记录：

- `tool_event`：工具名、参数、完整规范化结果、成功状态等；
- `executor_turn`：本轮 description、输出、完成/错误状态、seq 列表等；
- `supervisor_review`：关联被验收的 executor seq，保存是否通过和原因。

`read_memory_state()` 根据 review、旧版 `is_solved`、`repairs` 和 `invalidates` 重建 accepted/pending 状态。当前 `tool_event` 和 `executor_turn` 使用不同 seq，并都可能携带同一工具结果，造成重复保存。

### 8.2 supervisor_history.jsonl

保存 planning、initial guidance、evaluation、decision、final summary 的 turn，以及 Supervisor 的工具事件。字段包含 task/seq、description、工具参数和结果、验收状态、推进字段、计划修改、模型原始响应摘要及校验错误。

当前工具事件后的 turn 没有主动清除 `tool_result`，因此同一历史查询结果可能在后续多个 Supervisor turn 中重复写入。

### 8.3 chat_history.jsonl

每次完整请求保存用户问题、最终答案、description、任务数、最终 seq 和结构化 `supervisor_descriptions`。它是请求级总结，不替代 task 级证据。

## 9. 当前记忆查询能力

Supervisor 私有历史工具：

- `read_now_task`：读取当前 task 已验收的 executor turn 摘要和未解决失效提示；
- `read_history_task`：读取同一 chat 的较早 task 摘要；
- `read_history_chat`：读取请求级聊天记录，可按字符串 chat_id 过滤；
- `read_task_history_error`：读取当前仍 pending/失效的执行记录；
- `read_task_error`：按 task_id + seq 精确读取原始 executor 记录，名称虽含 error，也可读取成功记录；
- `read_supervisor_task`：读取指定 task 的 Supervisor 记录；
- `read_supervisor_history`：读取并筛选 Supervisor 历史。

`Skills/memory_retrieval.md` 描述按需查询流程，但工具可用不代表循环会自动恢复最合适的证据。当前仍依赖 Supervisor 主动选择并总结。

## 10. MCP 与权限

`MCP_hosts.py` 负责 stdio 会话生命周期和动态工具发现；`ToolRegistry` 负责按角色过滤 schema、检查权限、调用工具并把 MCP 结果规范化为 `ToolExecutionResult`。

### Supervisor 私有

上述七个历史查询工具。

### Supervisor 与 Executor 共享只读

- `read_all_files_tool`
- `read_files_content_tool`
- `sort_files_by_suffix_tool`

### Executor 私有

- Spring/Java 判断、查找、运行、打包和启动检查；
- 文件写入；
- Python 查找、运行和安装；
- 信息收集占位工具。

GitHub、Browser、Docker 的工具名运行时动态发现，默认只授予 Executor。必需 MCP 启动失败会阻塞启动；可选 MCP 启动失败会跳过。

## 11. 当前文件读取设计

### read_all_files_tool

它现在不是“递归读取全部内容”，而是单层目录浏览器：

- 只列出目标路径当前一层的 `directories` 和 `files` 元数据；
- 返回紧凑 `tree`；
- 不读取正文，不进入子目录；
- 使用 `start_index/next_start_index` 分页，单次最多 200 项；
- 查看子目录时把其 `address` 再传入本工具。

### read_files_content_tool

- 精确文件：按字符偏移读取，最多 20000 字符；通过 `next_start_char` 继续；
- 目录：只读取该目录的直接文件，不递归；总正文预算最多 12000 字符，并可按文件索引翻页；
- 环境文件、当前规则排除的 JSON、常见二进制和归档格式不返回正文。

### sort_files_by_suffix_tool

接收成功结果中的 `files` 元数据，按 `suffix` 分类。`sort_files_by_mother_tool` 已删除，因为单层目录结果的文件父目录相同，继续分组没有意义。

## 12. Java 与 Python 工具

`judge_spring_project_tool` 会根据元数据中的本地路径完整读取 POM，不使用目录预览作为最终依据。当前可提取 Maven 项目/父项目坐标、modelVersion、Maven Wrapper 和要求版本、Java 声明、Spring Boot 版本来源、直接依赖、dependencyManagement、插件、模块和 profiles；不会解析外部父 POM，也不会执行构建。

其余工具包括 JDK/Python 查找、独立代码运行、Spring Boot 打包与启动检查、依赖安装和追加写入。安装与打包包装仍涉及确认交互边界，不应把模型自行传入确认值视为用户授权。

## 13. Skills

当前只向 Supervisor 加载三个 skill：

- `supervisor_plan.md`：按可用 Executor 工具制定顺序计划；
- `supervisor_decison.md`：依据 evaluation 形成推进或继续指导；
- `memory_retrieval.md`：按需恢复聊天、Executor 和 Supervisor 历史。

Executor 当前 skill 列表为空。Skill 只提供说明，不授予额外工具权限。

## 14. 配置与安全边界

`INFORMATION.json` 选择 Supervisor/Executor provider、模型、温度及 Java/Python 环境。当前支持 chatgpt、glm、deepseek、qwen、claude 适配器。

该文件当前包含明文 API 凭据。这是独立的高风险配置问题：

- 不得在日志、文档、测试输出或回复中打印其值；
- 不得提交到公共仓库；
- 应迁移到环境变量或本机私密配置；
- 已经暴露或共享过的凭据应撤销并重新生成。

本轮只重写文档，没有修改配置文件或凭据。

## 15. 已验证结果

使用 `D:\anacode\python.exe` 离线运行：

```text
Ran 29 tests
OK
```

覆盖范围包括：Executor JSON 严格校验、字段缺失/拼写错误、多工具拒绝、工具只执行一次、总结重试、窗口限制、Supervisor 否决和推进、任务尝试上限、历史 review 重建、权限、单层目录浏览、内容分页及大型 POM 完整解析。

另外已验证系统 MCP 模块可以导入，新 `read_files_content_tool` 同时授权 Supervisor 和 Executor。未在本轮重新调用真实付费模型、Docker、GitHub、Playwright 或真实构建/安装操作。

## 16. 当前优先级

1. 修复 task 级证据记忆：稳定保存原始完成条件和有界 `evidence_context`。
2. 消除 JSONL 中完整 `tool_result` 的重复保存，turn 只保存引用和摘要。
3. 下一轮 Executor 应接收已验收证据引用，避免重复调用同一路径/同一工具。
4. Supervisor evaluation 必须按锁定的完成条件验收，防止标准漂移。
5. 为上述行为增加确定性回归测试，再做真实端到端运行。
6. 单独处理 `INFORMATION.json` 的密钥迁移和轮换。

具体问题、复现证据和验收标准见 `PROBLEM.md`。
