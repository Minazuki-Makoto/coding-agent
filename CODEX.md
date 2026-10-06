# Coding Agent 当前项目说明

更新时间：2026-10-05

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
├── backend.py / terminal_cli.py         # 交互终端入口，不是 HTTP 服务
├── AgentChat/
│   ├── Chatgpt_chat.py / GLM_chat.py / Claude_chat.py
│   └── DeepSeek_chat.py / Qwen_chat.py
├── AgentLoop/
│   ├── agent.py                         # 配置、dataclass、主调度与请求统计
│   ├── executor_loop.py                 # 单工具执行、总结、证据与重复调用管理
│   ├── supervisor_loop.py               # planning/evaluation/decision 与摘要生成
│   ├── loop_utils.py                    # 消息构造、JSON 转换
│   └── context_compression.py           # 显式异步高阈值压缩与整体字符预算
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
│   ├── sandbox.py / execution_gateway.py # 可信路径、真实隔离后端与单次授权
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
│   ├── task_summary.py                   # chat 隔离、Markdown 记录与摘要读取
│   └── session_checkpoint.py            # 类型化恢复、timeline、前置状态、分支
├── sandbox/Dockerfile                   # 可信 Python/JDK 工具链镜像构建说明
├── ExecutorBooks/Executor_textbook.md / Executor_mistake_book.md
└── tests/
    ├── test_agent_loop.py / test_bfs_read.py / test_java_code.py
    └── test_task_summary.py / test_task_summary_retrieval.py
~~~

旧修改建议、运行记录和教材不等于运行时实现；ExecutorBooks 当前不在实际加载的 skill 映射中。

## 4. 主运行流程

入口保留异步 AgentLoop.agent.main(query, session_address, chat_id)，backend.py 改为 terminal_cli.py 的终端启动入口。CLI 负责新建/精确恢复/连续问答、权限和授权；恢复或回退本身不运行模型、工具，/continue 才继续当前阶段。查看 README.md 的实际命令。

~~~text
main：datetime.now() 记录开始时间
  → 加载配置与角色 State，恢复已有 Executor / Supervisor / 原始工具编号；CLI 载入明确选择的 checkpoint
  → 启动 MCP，建立 ToolRegistry，加载 Supervisor skills
  → planning：生成顺序 task_list
  → initial_guidance：为本次请求的首个 task 生成指导、原始完成条件与 handoff
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

Executor execute 模式可调用工具；synthesize 模式关闭工具，只整合已验收的前置成果。成功调用签名覆盖本轮和跨尝试成功集合；允许不同参数、失败重试和明确重跑。总结重试不重复执行成功工具；对象变化后是否应重新读取仍需正确指导。

### 5.5 三个编号互不替代

| 编号 | 标识对象 | 初始化/恢复 |
| --- | --- | --- |
| executor_seq | Executor 历史事件与 turn | 按当前 chat 全部角色日志最大 seq 继续 |
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

新增 AgentLoop/context_compression.py：在两角色异步调用点准备完整消息的副本，不修改原始事实。删除提前裁剪第 10/第 6 条历史、16000 query 和12000 observation 的旧路径；用户要求与完整 skill 规则不盲目压缩。

- summarize 只有材料字符数严格大于 context_trigger_chars（默认 60000）才调用无工具模型；目标 context_summary_max_chars 默认 6000。
- truncate 是独立显式策略，不调用模型。总结失败/超长最多重试两次，然后标明 fallback 截断与原始 locator；不伪装成功摘要。
- 超大材料按请求预算分块，每个原文字符按原顺序进入摘要输入，不在摘要前删除中间正文。
- 工具身份、状态、error/message、路径、分页、参数和 locator 作为确定性 source_facts 保留；正文的压缩不能改变验收控制字段。
- 内容、目标、分支、阈值、输入/输出限制共同确定缓存键；回退新分支清空缓存。
- 摘要调用使用已有适配器，计入模型请求预算和 token；不会递归进入 prepare_request。
- 组装后整个 messages + tools JSON 严格校验 context_max_chars（默认 80000）；包括压缩请求。无法在保留要求/规则的情况下满足预算时明确 blocked，不用低阈值额外摘要掩盖。
- 这是字符预算，不是模型 token 上下文窗口保证。大量不压缩的元数据、单独超大 query/规则仍可能 blocked。

decision 继续区分本轮 Executor output/工具证据、本轮 Supervisor evaluation、历史辅助摘要可用性、TaskOutcome 和已查询 memory_query_results。task_summary.md 仍由模型按需查询，不自动灌入全部历史。本轮有效证据优先，Executor 自述不等于验收。

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

同 task_id/seq 标记避免正常重试重复追加。写入失败进入既有错误处理，不声称保存成功、不推进 task；此前内存更新和 decision JSONL 不会自动事务回滚。CLI 在整个执行期间持有 session 单写者锁，串行保护摘要读改写与编号；直接调用旧 main 接口不自动取得 CLI 单写者锁。

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

evaluation 和 decision 支持多次模型请求依次查询多个结果，每次一个工具，不是每阶段只能核查一个。查询观察在高阈值压缩前保留全部条目，相同查询去重；checkpoint 只保存查询动作/locator，恢复时从原始记录或分支内历史视图重建，不复制返回正文。旧内联记录仍可经旧读取路径查看，新格式通过 read_tool_result 显式取正文，不自动展开全部 JSONL。

## 10. MCP 与权限

MCP Host 管理 stdio ClientSession 生命周期和工具发现；ToolRegistry 检查重名、按角色生成 OpenAI/Claude schema、验证权限并统一 ToolExecutionResult。协议错误与业务 error/failed/failure 影响 ok；历史 not_found 不等同于验收通过证据。

### Supervisor 私有

上一节九个历史查询工具。

### Supervisor 与 Executor 共享只读

read_all_files_tool、read_files_content_tool、sort_files_by_suffix_tool。

### Executor 私有

Java/Spring 检查与运行、文件写入/替换、Python 查找/运行/安装、信息收集，以及动态发现且可信配置明确 capability、经 Host 授权放行的 GitHub/Browser/Docker 工具。

必需 MCP 为 memory、supervisor_memory、system、collect，启动失败阻塞请求。GitHub、Browser、Docker 为可选，配置或启动失败可跳过。Python 子进程使用 sys.executable。

工具角色权限仍为第一层；新增 ExecutionGateway/SandboxPolicy 检查文件、网络和副作用，SandboxRunner 通过真实 Docker 隔离用户进程。未知外部 MCP capability 默认拒绝。

## 11. 当前文件读取设计

### read_all_files_tool

单层目录浏览，不递归、不读正文；返回 directories/files 元数据、紧凑 tree 和分页游标，底层最多 200 项；实际 MCP wrapper 固定每页 50 项。查看子目录需再调用其 address。

### read_files_content_tool

底层精确文件支持字符偏移、最多 20000 字符；实际 MCP wrapper 固定每次8000字符，不新增分页参数，通过 next_start_char 续读。目录只读直接文件、不递归，每页最多 50 文件，总正文预算最多 12000 字符，单文件预览最多 8000 字符。环境文件、当前排除的 JSON 和常见二进制/归档不返回正文。

### sort_files_by_suffix_tool

底层接收 files 元数据并按 suffix 分类；实际 MCP schema 接收 home_address，在可信层扫描后分类。不要假定存在已删除的 sort_files_by_mother_tool。

## 12. Java 与 Python 工具

judge_spring_project_tool 根据本地路径完整解析 POM，不以截断预览代替原文件；提取 Maven 坐标/父项目、Wrapper/版本要求、Java 声明、Spring Boot 版本来源、依赖、插件、模块与 profiles。不解析外部父 POM，也不执行构建。

其他工具提供 JDK/Python 查找、代码运行、Spring Boot 打包/启动检查、依赖安装和文件操作。安装与打包 MCP 包装均为 interactive=False，stdio 不读取 stdin；终端 Host 绑定实际不可变操作、会话/分支及策略版本完成单次授权，拒绝/非交互时不执行。受限执行使用容器可信 Python/JDK，所有原有 subprocess 调用已接统一执行层，无自动主机降级。

当前 collect MCP 的 get_needed_info_tool 是信息收集占位响应，不应将旧 collect.py 辅助实现描述为已接入的真实信息获取能力。

## 13. Skills

运行时注册三个 Supervisor skills：supervisor_plan.md、supervisor_decison.md、memory_retrieval.md；验收阶段加载记忆检索说明。Executor 当前 skill 列表为空。

Skill 提供提示词和操作建议，不增加工具权限。摘要按需检索要求同时存在于 decision skill、memory skill 和代码默认提示词。

## 14. 配置与安全边界

INFORMATION.json 选择角色 provider、模型、温度、Java/Python 环境和 writable_roots；支持 chatgpt、glm、deepseek、qwen、claude 适配器。同步 SDK 调用经 asyncio.to_thread 执行，归一化响应与 token usage。

系统文件 MCP 从 CODING_AGENT_WRITABLE_ROOTS 获取写入范围；摘要内部 trusted_roots 限定目录。模型不能自行扩大允许写入的根目录。

配置凭据和远程 MCP 环境变量均按秘密处理：不打印、不写文档或测试、不提交公开仓库。密钥迁移/轮换需另行授权；本次文档更新不读取凭据值、不修改配置。

## 15. 已验证结果

最终原项目开启实际 Docker 后端后：87项全部通过，CLI --help 正常；下述默认跳过行为不影响单独实际验收结果。

2026-10-05 使用 D:\anacode\python.exe，在临时工作区运行 unittest discover：87 项，86 通过，1 项实际 Docker 测试需 opt-in 默认跳过。原有67项回归仍通过；新增去重、完整压缩输入/计费预算、连续问答、checkpoint补偿、恢复与回退、分支读取、单写者、权限和授权检查。

单独开启 CODING_AGENT_DOCKER_INTEGRATION=1、使用已有 nginx:alpine 镜像：test_sandbox.py 的5项全部通过，包含真实容器可写工作区、未挂载主机文件不可读写、伪测试 API key 不继承、只读失败、关闭网络、取消后自有容器清理。该镜像仅验证通用隔离后端，不证明 Python/JDK 工具链镜像可用。

专用 sandbox/Dockerfile 的镜像构建因 Docker Hub 连接超时失败；真实 Python/Java/Maven/Gradle 集成及依赖安装未运行。未使用付费模型/外部服务验收；模拟模型两轮端到端完成与 evaluation/decision 重新生成通过。Windows junction 需要系统创建权限的情况尚未专项验证。

命令、覆盖边界与未验证项见 README.md。不要将这些测试解释为跨文件事务、任意代码绝对安全或真实模型摘要质量保证。

## 16. 当前优先级

以下是已知边界，不是自动执行指令：

1. 新增 State/session_checkpoint.py：typed dataclass 原子 checkpoint、session 单写者、统一 timeline event_seq、每边界前置 snapshot。CLI 恢复当前阶段，普通成功工具通过原始 locator 只重新总结；checkpoint 滞后时补齐已落盘事实。未知/中断操作不自动重放，需查看真实效果并选安全边界。
2. 会话路径为 --state-dir 下的精确 session ID，chat/branch 分离。JSONL 保持原文件位置/旧字段，增加 request_id/branch_id/event_seq；tool_result_seq 按整个 session 继续，角色 seq 按 chat 继续，同 chat 新请求 task_id 接在旧任务后。
3. /rewind 选择 event 或 task_start，创建有 parent/cutoff 的新分支。角色历史、原始事实读取、任务摘要、依赖成果与缓存遵守分支可见范围。原日志不修改，真实文件不回滚，无自动 Git reset。旧日志没有可靠前置 snapshot 时只允许查看，拒绝伪造精确恢复。
4. 权限由当前 CLI 可信策略决定，不恢复旧 full-access 或批准。默认 workspace-write；read-only 禁止用户项目改写但 Host 可写专用状态；danger-full-access 必须用户显式选择并显示无进程隔离。工作区不得包含 Host 配置/状态后再挂入容器。
5. Docker Desktop Linux 容器后端只挂选定工作区，不挂 Host 家目录/状态/密钥/socket；默认 network none、只读 rootfs、去 capabilities、资源限制。后端/镜像缺失明确拒绝，不自动拉镜像、不退回无隔离。额外 file roots 只支持可信文件工具，不自动作为容器 mounts；网络仅 on/off，不是域名过滤。配置 tool_capabilities 后外部 MCP 才可能启动/放行，仍不受本地容器自动约束。
6. 现有 write_in 的原子覆盖机制继续保存 task_summary.md，checkpoint 复用其 _atomic_write。JSONL、timeline、checkpoint、Markdown 不构成跨文件原子事务；写盘失败时仍须调查最后持久化边界，不声称绝对 exactly-once。
7. 静态路径检查不能抵御所有恶意并发替换 race；额外 Windows ACL/junction 专项、SDK 在途取消/计费、真实工具链与远端 MCP 的验收仍需单独验证。系统只清理自己创建的进程/容器。
8. 历史扫描和 snapshot/cache 会随长会话增长，尚无索引/保留策略；不自动删除旧记录。摘要质量和证据冲突解释仍依赖真实模型行为。

故障复现保留在 PROBLEM.md；判断是否修复以当前源码与对应测试为准。
