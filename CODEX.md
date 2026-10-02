# Coding Agent 当前项目说明

更新时间：2026-10-01

本文记录当前源码的整体架构、运行顺序、状态和持久化边界，不把历史建议当作已实现功能。用户当前指令优先；本文与代码不一致时，重新核实源码。本文不是自动启动或修改项目的指令。

## 1. 开始任务时的读取顺序

1. 先读取本文件，恢复项目背景与用户约定。
2. 再读取当前任务涉及的源码，必要时查看 PROBLEM.md。
3. 不因历史待办自动运行程序、安装依赖、启动外部服务或修改无关功能。
4. 默认解释器为 D:\anacode\python.exe；运行前确认 sys.executable，不要静默切换环境。
5. 不打印配置凭据，不修改或提交 INFORMATION.json 中的密钥。

## 2. 项目目标与当前状态

项目是 Supervisor–Executor 双角色 coding agent，AgentLoop 负责调度，MCP 提供工具执行与历史查询。

| 层 | 核心模块 | 职责 |
| --- | --- | --- |
| 请求与调度 | backend.py、AgentLoop/agent.py | 会话、配置、状态与预算、任务推进、请求结果 |
| 监督与执行 | supervisor_loop.py、executor_loop.py | Supervisor 规划/验收/决策；Executor 执行与反馈 |
| 模型适配 | AgentChat/ | 对接 provider，统一响应和 token 用量 |
| 工具与权限 | MCP_hosts.py、MCP_client.py、tool_registry.py | MCP 会话、工具发现、角色权限、结果规范化 |
| 状态与记忆 | State/、Context/、Skills/ | 原始事实、角色历史、累计摘要、按需检索与行为说明 |

已经实现独立原始工具结果层、跨尝试 handoff、任务验收成果 TaskOutcome、每 chat 累计任务摘要，以及模型自主选择摘要检索。保留原有阶段划分、Supervisor 否决权和 task_id 推进规则。

## 3. 当前项目结构

~~~text
coding_agent/
├── CODEX.md / PROBLEM.md / README.md
├── INFORMATION.json                     # 运行配置；不得输出凭据
├── backend.py                           # 示例调用入口，不是完整 HTTP 服务
├── AgentChat/
│   ├── Chatgpt_chat.py / GLM_chat.py / Claude_chat.py
│   └── DeepSeek_chat.py / Qwen_chat.py
├── AgentLoop/
│   ├── agent.py                         # 配置、dataclass、主调度与请求统计
│   ├── executor_loop.py                 # 单工具执行、总结、证据与重复调用管理
│   ├── supervisor_loop.py               # planning/evaluation/decision 与摘要生成
│   └── loop_utils.py                    # 消息构造、JSON 转换、上下文窗口
├── Context/
│   ├── mcp_resources.py                 # Executor 历史查询 MCP
│   ├── mcp_supervisor_tools.py           # Supervisor 历史及任务摘要查询 MCP
│   └── History_Resorce/
│       ├── history_state.py             # 根据 review 重建 accepted/pending
│       ├── mcp_history_resource_service.py
│       ├── mcp_history_error.py
│       └── mcp_supervisor_history.py
├── MCP_functions/
│   ├── MCP_client.py / MCP_hosts.py / tool_registry.py
│   ├── Search/mcp_search_server.py       # 可选远程 MCP 配置
│   ├── collect_information/collect.py / mcp_collect_server.py
│   └── System_Files/
│       ├── Files_function/bfs_read.py / java_code.py / python_code.py / write_in.py
│       └── Files_server/mcp_system_server.py
├── Skills/
│   ├── supervisor_plan.md
│   ├── supervisor_decison.md             # 沿用实际文件名拼写
│   └── memory_retrieval.md
├── State/
│   ├── save_executor_state_history.py
│   ├── save_supervision_state_history.py
│   ├── save_chat_history.py
│   ├── save_tool_result.py               # 原始结果、locator、summary、追加锁
│   └── task_summary.py                   # chat 隔离、Markdown 记录与摘要读取
├── ExecutorBooks/Executor_textbook.md / Executor_mistake_book.md
└── tests/
    ├── test_agent_loop.py / test_bfs_read.py / test_java_code.py
    └── test_task_summary.py / test_task_summary_retrieval.py
~~~

旧修改建议、运行记录和教材不等于运行时实现；ExecutorBooks 当前不在实际加载的 skill 映射中。

## 4. 主运行流程

入口为异步 AgentLoop.agent.main(query, session_address, chat_id)。backend.py 创建 D:\coding-agent 下的随机会话目录和字符串 chat_id，再调用硬编码示例 query；虽创建 Flask 对象，但没有 HTTP 路由和服务启动逻辑。

~~~text
main：datetime.now() 记录开始时间
  → 加载配置与角色 State，恢复已有 Supervisor / 原始工具编号
  → 启动 MCP，建立 ToolRegistry，加载 Supervisor skills
  → planning：生成顺序 task_list
  → initial_guidance：为 task 0 生成指导、原始完成条件与 handoff
  → 当前 task：
      Executor 执行 → Supervisor evaluation → 保存 Executor review
      → Supervisor decision：
          不通过/未整体完成 → 保存累计摘要 → 同一 task 再尝试
          验收通过且决定推进 → 建立 TaskOutcome → 保存累计摘要 → task_id + 1
  → 请求完成或 blocked：汇总 Supervisor descriptions
  → 计算累计 tokens 和 datetime.now() 时间差
  → 保存 chat_history.jsonl，返回结果
~~~

evaluation 判断本轮 Executor 子目标；decision 判断整个当前 task 及下一步。子目标通过不等于整个 task 完成。is_next_target 是推进控制字段，但代码同时要求 evaluation 通过，摘要成功保存后主循环才推进。

默认限制：同 task 最多 3 次尝试，Executor 单轮最多 10 步，Supervisor 阶段最多 10 轮，整次请求最多 80 次模型请求。达到预算或出现无法恢复的错误进入既有 blocked 流程。

计划调整保留已完成前缀。need_date_back 等字段是回溯意图与定位，不是文件回滚或完整 checkpoint 恢复。

## 5. 状态对象职责

### 5.1 AgentState

请求级共享状态：session/chat/query、任务列表与当前 task、三个独立 seq、模型配置、预算、token 累计、运行环境、Supervisor description 历史。

task 级映射保留原始完成条件、handoff、previous/accepted/remaining work、证据定位、已执行调用签名、可复用读取、正式 TaskOutcome、累计摘要及已保存摘要 seq。

### 5.2 ExecutorState

当前执行周期的输入/输出、指导、工具名称/参数/状态、完成与错误字段、最近 description 窗口和模型校验状态。

长期字段只保存 last_tool_result_ref、tool_result_refs、tool_summaries 等轻量信息，不保存完整 tool_result；原始结果作为调用函数局部变量立即总结。

重入会清空本轮临时窗口、参数、summary/ref 列表；同一 task 的 latest_tool_context 保留，跨尝试成果与证据由 AgentState/handoff 提供。

### 5.3 SupervisorState 与 SupervisorEvaluationState

SupervisorState 保存计划、指导、推进与计划调整字段、最终答案、工具引用、累计 task_summary 和正式 task_outcome。EvaluationState 保存本轮验收结论、拒绝原因、错误与证据引用。

二者不长期保存完整工具结果；历史查询正文在当前阶段的局部窗口使用。evaluation 结论明确传入同轮 decision。

### 5.4 Handoff、证据与 TaskOutcome

- SupervisorHandoff：instruction、previous/accepted/remaining work、锁定完成条件、证据 locator、已执行调用和前置 TaskOutcome。
- LatestToolContext：root/address/paths/status/pagination 等操作对象元数据，不携带完整正文。
- EvidenceReference：tool_result_seq、tool_name 和路径/状态等证据定位。
- TaskOutcome：正式验收后的累计成果，包括 accepted_summary、verification_summary、证据和可复用读取。

Executor execute 模式可调用工具；synthesize 模式关闭工具，只整合已验收的前置成果。成功调用签名避免重复操作，总结重试不重复执行成功工具；对象变化后是否应重新读取仍需正确指导。

### 5.5 三个编号互不替代

| 编号 | 标识对象 | 初始化/恢复 |
| --- | --- | --- |
| executor_seq | Executor 历史事件与 turn | 新请求从初始值开始，不是完整会话恢复 |
| supervisor_seq | Supervisor 历史事件与 turn | 按当前 chat 已有历史最大 seq 继续 |
| tool_result_seq | 原始工具事实 | 按同 session 原始结果最大编号继续 |

task_id 是计划索引，不是工具编号。ToolResultLocator 使用 session_address + chat_id + tool_result_seq；不同角色 seq 数值相同也不代表同一事件。

## 6. 模型输出契约

### 6.1 Executor

ExecutorOutPut 使用 extra="forbid"；description 为非空字符串，is_finished 必须是 JSON bool，tool_name 为非空字符串或 null，skill name/id 必须成对存在。

len(tool_calls) > 1 时不执行任意工具、不立即终止 task，反馈协议错误并要求重新选择一个工具；重试仍受现有预算约束。

普通工具执行顺序：

~~~text
调用 MCP → 局部 tool_result → 分配 tool_result_seq → 保存原始结果
→ 获得 locator → 用当前局部结果生成 description → 保存轻量事件与 turn
~~~

工具执行后总结失败只重试总结。Executor 总结最多重试两次，仍失败时使用保守事实总结，不声称完成；原始结果保留。保存失败不得产生虚假 locator。

### 6.2 Supervisor

planning 返回非空任务列表；initial_guidance 建立指导与完成条件；evaluation 只验收；decision 用结构化字段决定继续、调整或推进，不能仅凭 description 绕过控制字段。

decision 同时生成 TaskSummaryDraft，包含本轮结果、累计有效成果、当前验收、旧结论修正、剩余工作、下一步与正式成果摘要。继续或否决可写累计摘要，但不能新增已完成 TaskOutcome。

description 的现行用户约定：提示词要求少于 3000 字符，说明本轮动作、关键结果、验证和剩余问题；代码不因超过 3000 字符单独报错、重试或硬截断。非空、类型及其他结构化校验仍保留。

TaskOutcome 字段有独立可配置长度校验：

| 字段 | 默认最大字符数 | 环境变量 |
| --- | ---: | --- |
| accepted_summary | 3500 | CODING_AGENT_ACCEPTED_SUMMARY_MAX_CHARS |
| verification_summary | 1500 | CODING_AGENT_VERIFICATION_SUMMARY_MAX_CHARS |

超限反馈模型重写，复用现有重试机制，不通过 bounded_text 或字符串切片截断。accepted_summary 概括整个 task 已确认的累计成果；verification_summary 说明原始条件、实际检查结果和证据定位，不得编造验证。

## 7. 上下文预算

以下窗口常量不是对所有消息合计进行严格 token 限制的统一管理器：

| 内容 | 字符预算/行为 |
| --- | --- |
| 总模型上下文目标常量 | 80000，不是每次请求严格总上限 |
| 用户 query 上下文视图 | 16000 |
| 部分 description 上下文视图 | 6000，不是保存字段长度校验 |
| 工具 observation | 12000，必要时保留头尾并标记省略 |
| 最终汇总的 Supervisor description 历史 | 30000 目标，按完整条目选择 |
| 任务摘要读取 | 12000 目标；当前 task 整条记录优先保留 |
| 验收/决策历史查询窗口 | 最近 6 次局部查询 observation |

完整结果保存与模型可见正文不同。原始结果不自动灌入下一轮；定位后读取也可能受 observation 窗口限制。Executor 最近 description 使用有界窗口。

决策消息默认明确区分：

- 本轮最新 Executor output、完成状态、tool_summaries 与 locators；
- 本轮 Supervisor evaluation、通过状态、拒绝或错误原因；
- 历史辅助摘要的可用性和读取状态，而非自动加载全文；
- 当前/已完成 TaskOutcome、锁定条件及现有成果字段；
- 模型主动查询后的局部 memory_query_results。

最新有效证据优先于旧摘要，但 Executor 自述不是验证证据。新失败/回归应修正旧结论；本轮未涉及的旧有效成果不自动失效。

## 8. 历史保存格式

~~~text
session_address/
├── executor_history.jsonl
├── supervisor_history.jsonl
├── chat_history.jsonl
├── tool_results.jsonl
├── tool_results.jsonl.lock
└── chat_<编码chat_id>_<摘要哈希>/
    └── task_summary.md
~~~

JSONL 位于 session 根目录，带 chat_id 供过滤。task_summary.md 按 chat 子目录隔离，同 chat 的所有 task 共用一份文件，不按 task_id 拆分。

### 8.1 executor_history.jsonl

- tool_event：仅工具索引、状态等轻量信息，不存完整 arguments/content。
- executor_turn：输入/输出、完成状态、工具引用与 ToolSummary。
- supervisor_review：关联被验收的 Executor seq，记录通过/未通过及原因。

执行周期调用 N 个普通工具，产生 N 条原始结果、对应轻量工具事件和 1 条 executor_turn；review 职责独立，不因去重删除。

read_memory_state() 根据 review 重建 accepted/pending，兼容旧 is_solved/repairs/invalidates。旧内联结果不自动迁移或删除；新记录不写内联原始结果，正文通过 locator 查询。

### 8.2 supervisor_history.jsonl

保存 planning、initial_guidance、evaluation、decision、final_summary 等 turn，以及轻量工具事件。turn 保留 description、判断、控制字段、引用、handoff、累计 task_summary 与正式 task_outcome，不重复写工具返回正文。

历史查询事件记录名称、参数、状态和 referenced_tool_result_seqs；普通工具事件记录原始结果 locator。查询返回的历史正文不再次成为原始事实。

### 8.3 chat_history.jsonl

请求级记录保存 query、answer、最终 description、Supervisor description 历史、任务数、seq、session/chat，以及 total_tokens、elapsed_time_seconds、started_at、finished_at。

token 来自适配器返回 usage，包括两角色、重试与最终汇总；没有 usage 的调用不估算，不能视为完整账单审计。时间以 datetime.now() 起止差计算，覆盖模型/工具/调度与最终汇总，不包含随后聊天记录写入耗时。

summarize_supervisor_descriptions() 在请求收尾调用，以各阶段有序 description、query、完成/blocked 状态和正式 TaskOutcome 生成请求级总结；不是 task_summary.md 的写入函数。模型失败时 fallback_summary() 拼接最近两条完整 Supervisor description，不因超过 3000 字符报错，也没有严格 6000 上限。

### 8.4 tool_results.jsonl：独立原始事实层

save_tool_result.py 定义 ToolResultRecord、ToolResultLocator、ToolSummary。普通工具成功与失败均保存实际 arguments、规范化完整结果 content、独立 seq、UUID event_id、角色、phase、task/chat 和时间。

每条 JSONL 占一行，使用现有 JSON 安全转换。线程锁与跨进程文件锁保护追加和编号冲突检查；写入成功才返回 locator，不回写旧记录。并发请求分配同一编号时后到写入被拒绝，当前没有自动重新分配编号重试。

九个历史查询工具返回已有事实的视图，不将返回正文再次写入本文件，避免递归复制历史。

### 8.5 task_summary.md：累计进展与验收摘要

有效 decision 为当前 task 追加一条累计状态记录，标明 task_id、Supervisor seq、目标、本轮结果、累计成果、验收与证据、失效结论、剩余工作、决定和下一步；最终通过时附正式 TaskOutcome。

同 task 最后追加记录代表当前状态，旧记录供追溯。读取识别 task_id/seq，不将多轮摘要误当多个成果；可选读取较早 task 最新记录。

保存顺序：

1. 验证模型决策和累计摘要。
2. 更新 AgentState/SupervisorState；最终验收通过时更新 task_outcomes。
3. 分配本轮 Supervisor seq，保存结构化 decision 历史。
4. 用已更新状态及本轮 task_id/seq 组装 Markdown。
5. append_task_summary_record() 读取旧内容、检查重复标记、拼接记录。
6. 调用已有 write_in() 保存，成功后标记 saved seq，再返回主循环推进。

write_in 是覆盖写：同目录临时文件 + flush/fsync + os.replace。因此采用“读取旧内容并拼接后原子替换”保留旧记录，不另建写入机制。内部 trusted_roots 限定摘要目录，不暴露为模型 MCP 参数。

同 task_id/seq 标记避免正常重试重复追加。写入失败进入既有错误处理，不声称保存成功、不推进 task；此前内存更新和 decision JSONL 不会自动事务回滚。同 chat 并发摘要拼接尚无独占读改写锁。

## 9. 当前记忆查询能力

Supervisor 私有历史工具共九个：

| 工具 | 用途 |
| --- | --- |
| read_now_task | 当前 task 已验收 Executor description 与未解决提示 |
| read_history_task | 同 chat 较早 task 的历史摘要 |
| read_history_chat | 请求级聊天历史 |
| read_task_history_error | 当前 pending/失效执行历史 |
| read_task_error | 按 task_id + seq 读取 Executor 记录，也可查成功记录 |
| read_supervisor_task | 指定 task 的 Supervisor 历史视图 |
| read_supervisor_history | 筛选 Supervisor 历史记录 |
| read_tool_result | 按 tool_result_seq 精确查询完整原始结果 |
| read_task_summary | 当前 task 最新累计摘要，可选附较早 task 最新摘要 |

Host 强制覆盖 session_address/chat_id，模型不能借历史工具切换会话目录。任务与结果编号按当前上下文验证；read_tool_result 找不到匹配 chat/seq 时明确返回 not_found。

read_task_summary 的模型 schema 不暴露 session_address/chat_id，只选择 task_id 和 include_other_tasks（默认 false）。默认 decision 不读取 Markdown 正文，只告知摘要是否存在。

提示词要求：对累计成果、旧结论失效、依赖、剩余工作或推进条件“没有特别准确的把握”时，先调用 read_task_summary；当前结果充分且无矛盾可直接决定。判断由模型作出，不是代码置信度测量。

evaluation 记忆 skill 优先用 Executor 当前反馈；不足时读取当前 description、之前 task，再按需核查原始结果。错误判断先看当前错误与实际证据，不凭旧“已完成”摘要通过验收。

evaluation 和 decision 支持多次模型请求依次查询多个结果，每次一个工具，不是每阶段只能核查一个。查询观察保留局部有界窗口，相同查询去重。旧内联记录仍可经旧读取路径查看，新格式通过 read_tool_result 显式取正文，不自动展开全部 JSONL。

## 10. MCP 与权限

MCP Host 管理 stdio ClientSession 生命周期和工具发现；ToolRegistry 检查重名、按角色生成 OpenAI/Claude schema、验证权限并统一 ToolExecutionResult。协议错误与业务 error/failed/failure 影响 ok；历史 not_found 不等同于验收通过证据。

### Supervisor 私有

上一节九个历史查询工具。

### Supervisor 与 Executor 共享只读

read_all_files_tool、read_files_content_tool、sort_files_by_suffix_tool。

### Executor 私有

Java/Spring 检查与运行、文件写入/替换、Python 查找/运行/安装、信息收集，以及动态发现的 GitHub/Browser/Docker 工具。

必需 MCP 为 memory、supervisor_memory、system、collect，启动失败阻塞请求。GitHub、Browser、Docker 为可选，配置或启动失败可跳过。Python 子进程使用 sys.executable。

工具角色权限不等于操作系统级执行隔离，不能与安全 sandbox 混为一谈。

## 11. 当前文件读取设计

### read_all_files_tool

单层目录浏览，不递归、不读正文；返回 directories/files 元数据、紧凑 tree 和分页游标，单次最多 200 项。查看子目录需再调用其 address。

### read_files_content_tool

精确文件按字符偏移读取，单页最多 20000 字符，通过 next_start_char 续读。目录只读直接文件、不递归，每页最多 50 文件，总正文预算最多 12000 字符，单文件预览最多 8000 字符。环境文件、当前排除的 JSON 和常见二进制/归档不返回正文。

### sort_files_by_suffix_tool

接收 files 元数据并按 suffix 分类。不要假定存在已删除的 sort_files_by_mother_tool。

## 12. Java 与 Python 工具

judge_spring_project_tool 根据本地路径完整解析 POM，不以截断预览代替原文件；提取 Maven 坐标/父项目、Wrapper/版本要求、Java 声明、Spring Boot 版本来源、依赖、插件、模块与 profiles。不解析外部父 POM，也不执行构建。

其他工具提供 JDK/Python 查找、代码运行、Spring Boot 打包/启动检查、依赖安装和文件操作。安装与打包 MCP 包装仍启用交互确认；stdio 下是否阻塞与用户授权边界不能视为已解决。

当前 collect MCP 的 get_needed_info_tool 是信息收集占位响应，不应将旧 collect.py 辅助实现描述为已接入的真实信息获取能力。

## 13. Skills

运行时注册三个 Supervisor skills：supervisor_plan.md、supervisor_decison.md、memory_retrieval.md；验收阶段加载记忆检索说明。Executor 当前 skill 列表为空。

Skill 提供提示词和操作建议，不增加工具权限。摘要按需检索要求同时存在于 decision skill、memory skill 和代码默认提示词。

## 14. 配置与安全边界

INFORMATION.json 选择角色 provider、模型、温度、Java/Python 环境和 writable_roots；支持 chatgpt、glm、deepseek、qwen、claude 适配器。同步 SDK 调用经 asyncio.to_thread 执行，归一化响应与 token usage。

系统文件 MCP 从 CODING_AGENT_WRITABLE_ROOTS 获取写入范围；摘要内部 trusted_roots 限定目录。模型不能自行扩大允许写入的根目录。

配置凭据和远程 MCP 环境变量均按秘密处理：不打印、不写文档或测试、不提交公开仓库。密钥迁移/轮换需另行授权；本次文档更新不读取凭据值、不修改配置。

## 15. 已验证结果

2026-10-01 使用指定解释器运行离线测试：

~~~powershell
& "D:\anacode\python.exe" -c "import sys; print(sys.executable)"
$env:PYTHONDONTWRITEBYTECODE = "1"
& "D:\anacode\python.exe" -m unittest discover -s tests -v
~~~

共 67 项测试通过。覆盖结构化输出、协议多工具重选、单次执行与总结重试、独立原始结果/保存失败、权限与 Host 绑定、同 task 重入、证据复用、Supervisor 否决与推进、TaskOutcome、摘要累计/纠错/chat 隔离/追加去重/写入失败、按需摘要查询、description 超长不报错、成果字段超限重写、文件分页及大型 POM 解析。

这是离线回归测试，不证明真实模型必定按提示词检索；未验证付费模型端到端、真实 GitHub/Docker/Playwright、实际构建或安装。本次架构记录更新不修改运行代码。

## 16. 当前优先级

以下是已知限制与后续候选工作，不是自动执行指令：

1. 完整断点恢复未实现：编号恢复不等于恢复 task_list、Executor seq、handoff 和 TaskOutcome；复用旧 chat/task_id 需审查历史身份冲突。
2. 原始结果追加有锁，但独立请求编号冲突只拒绝，未实现原子分配并重试；角色 JSONL 与摘要不是完整跨文件事务。
3. 同 chat 摘要读改写缺少并发串行化；失败时 State/decision 历史可能已更新，单文件原子替换不是全流程原子提交。
4. 历史读取主要为线性扫描；长会话读取开销、原始证据可见性和上下文总预算仍需关注。
5. 自主检索与累计成果纠错依赖模型行为，需另行真实运行验收；不自动加入全部历史，也不假定历史查询以后完全不需要。
6. 交互确认、真实执行隔离和凭据管理需独立评估，不能由现有单元测试宣称全部解决。

故障记录与复现线索见 PROBLEM.md；旧问题是否修复以当前源码及对应测试为准。
