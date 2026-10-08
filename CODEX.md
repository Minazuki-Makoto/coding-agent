# Coding Agent 当前项目说明

更新时间：2026-10-08

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
├── terminal_render.py                  # 可信清洗、淡色过程显示、NO_COLOR
├── terminal_select.py                  # Windows 方向键/Tab 选择 yes/no，Enter 确认
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
│   # permissions.py：Host 不可变授权；terminal_settings.py：稳定用户级设置
│   # project_workspace.py：隔离副本、基线、固定变更包、受控应用事务
├── sandbox/Dockerfile                   # 可信 Python/JDK 工具链镜像构建说明
├── ExecutorBooks/Executor_textbook.md / Executor_mistake_book.md
└── tests/
    ├── test_agent_loop.py / test_bfs_read.py / test_java_code.py
    └── test_task_summary.py / test_task_summary_retrieval.py
~~~

旧修改建议、运行记录和教材不等于运行时实现；ExecutorBooks 当前不在实际加载的 skill 映射中。

## 4. 主运行流程

入口保留异步 AgentLoop.agent.main(query, session_address, chat_id)，backend.py 改为 terminal_cli.py 的终端启动入口。CLI 负责新建/精确恢复/连续问答、权限和授权；恢复或回退本身不运行模型、工具，/continue 才继续当前阶段。查看 README.md 的实际命令。

日常在仓库根目录启动：`& "D:\anacode\python.exe" -m backend`，无需 workspace/state-dir/sandbox 参数。
不会把 cwd/Agent 仓库自动绑定为目标项目；普通问答、设置、历史不依赖 Docker。
Windows 状态默认 D:\coding-agent-state；稳定用户设置在 %LOCALAPPDATA%\coding-agent\settings.json，
非 Windows 使用 ~/.local/share/coding-agent/state 和 ~/.config/coding-agent/settings.json。
`/settings state-dir` 验证后原子保存，对后续新会话/重启生效，不迁移活动会话。
`/project` 只选择候选；也可从工具明确绝对路径推导候选，不再要求用户在工具调用中输入目录。
路径缺失或无效反馈模型重新选择参数，不隐式使用 cwd。

每次普通工具调用、复制/检查副本、应用固定变更包是独立批准范围。
Executor 和 Supervisor 的普通文件/执行/信息工具每次都显示 yes/no 与实际参数；一次批准不授予下一次调用。
Supervisor 的九个历史查询工具不弹确认，仍检查角色权限、Host session/chat 绑定及分支范围。
ToolPermission 是 Host 生成的 frozen dataclass，初始 is_permitted=false，用户 yes 后由 Host 更新为 true；
绑定工具、角色、实际参数及执行环境摘要、根目录、session/chat/branch/request 和策略版本，执行前校验并消费。
模型不能修改 is_permitted 或在参数中提供批准。按当前用户 UI 约定，选项默认 yes，但必须 Enter 明确确认才批准；切到 no、EOF/取消/非交互拒绝停止当前请求，回到输入界面。
Windows 真实终端使用标准库 msvcrt 键盘菜单：方向键/Tab 切换，Enter 确认，Esc/Ctrl+C 取消。不支持键盘菜单时使用 1=yes/2=no 行输入兼容模式，Enter 确认默认 yes；非交互仍拒绝。Executor 普通过程为淡白、Supervisor 为淡绿，Host 灰色；授权黄色、错误红色优先，NO_COLOR/--color never/输出重定向保持纯文本。角色由可信调用点/actor 字段指定，不从模型正文推测，不把 ANSI 写入历史。
原有 Permission 继续用于复制、网络和固定包批准；终端启用逐次模式，旧显式可信 API 保持兼容。
真实项目仍只读，进程/编辑前先确认副本，之后再批准具体工具；副本位于 state_dir 同级的专用 staging 子目录，
复制当前磁盘（包含未提交修改），保留基线文件哈希/类型/模式/编码与原始字节、目录和排除清单。
源码写入在副本容器里，进程仅挂副本，状态、基线、变更包和凭据保持在容器外；网络单独确认。
Docker 缺失不阻止普通问答/只读分析，但阻止副本执行，不降级 Host。
默认排除依赖/构建产物/凭据，保留 .env.example/.gitignore/必要 wrapper；额外排除可通过设置添加。

`/diff` 创建不可变 cs_<SHA256> 包并展示差异和实际测试证据；`/apply ID` 重新展示和批准固定版本，
模型完成且有可应用改动时自动展示该包并单独询问 yes/no；应用/拒绝另存 Host 事实事件并供后续上下文使用。
模型最终答案明确标注副本执行范围，不把副本完成当成真实项目已应用。
先检查原文件基线、新增目标、路径/链接/大小写与保护范围，再用包内固定字节应用，不能读另一版本副本。
单文件原子替换、备份和逐文件事务日志不等于整批事务；失败尝试恢复，不覆盖外部并发的新修改。
`/apply-status` 检查 applying/unknown/已应用日志与当前哈希，不自动重放；`/export ID` 导出 patch 到包目录。
二进制/超大文本/特殊文件变化不自动应用；纯权限和空目录变化不写回。
新分支没有对应副本文件快照时明确阻塞重新生成，不能共享可写副本或把逻辑回退说成磁盘回滚。
恢复只加载状态/项目候选，不继承批准；继续项目工具时重新授权并验证副本版本。

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

project_root/staging_root/workspace_revision/workspace_hash 用于资源身份与恢复校验，不代表授权；
实际 Permission 由当前 Host workflow 创建，不恢复旧 is_permitted。

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

decision 的 is_finished 仅表示本次控制决策形成；否决并明确要求 Executor 修改时也必须为 true。
Host 对无工具、完整 task_summary 且有具体下一步（或推进决定）的回复，仅纠正被误写为 false
的阶段完成标志，再执行原有计划、否决及 TaskOutcome 校验；不根据自然语言自动判定任务通过。
原始模型标志保留在 raw_model_response_excerpt，实际应用状态保存 decision_finished。
不完整/非法 decision 最多默认 3 次契约失败（CODING_AGENT_DECISION_CONTRACT_RETRIES 可配置），
达到上限明确 decision_contract_exhausted；多工具协议错误独立最多连续 3 次，不占有效查询轮次。
模型请求失败保留实际原因，不再统一覆盖成 decision_budget_exhausted。
手动恢复并重新进入 decision 时清除上次退出原因，避免新决定成功后仍被旧 blocked 标志终止；
不清除原始历史、不自动重置任务尝试或恢复权限。

synthesize 模式的 Executor 若返回合法但未完成结果，立即交回 Supervisor 核查缺口，
exit_reason=executor_synthesis_needs_review，不在没有工具的情况下重复等待；仍保留未完成判断。

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

跨 task 的摘要不是事实白名单。已有成功目录证据的 evidence_references.paths 能证明文件/包存在，
不因 accepted_summary 省略而失效；不能据此认定源码职责、部署成功或实际调用链。
evaluation/decision 消息显式提供 evidence_usage 说明证据层次、摘要省略、未通过 previous_work
和原始验收条件的边界。Supervisor 有疑问时按 locator 查询旧事实，不要求 synthesize Executor 重读项目。

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

终端路径使用 project_workflow 动态建立策略，不启动/调用可绕过副本网关的外部 MCP。
直接 main API 不再默认授予 cwd，只支持无项目问答或调用方显式传可信 policy/workflow。
Supervisor-only 历史权限保持原样。副本读缓存绑定工作区/分支/request/版本；正文和目录有独立失效范围。
逐次模式先取得 ToolPermission，再进行文件内容版本校验；缓存复用不重执行工具，成功后的总结重试不重复确认。
逐次批准审计保存在 workspace 元数据 tool_permissions.json，checkpoint 不恢复批准。历史查询不走普通执行网关。
成功工具先保存原始事实再总结；恢复/格式失败不会重做成功操作。恢复后的完整否决清除旧 resume 标志，
下一次循环确实转回 Executor，不反复停在 decision。

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

2026-10-08 续聊/主动回退：新增16项离线测试，完整回归207项：204通过、3项真实Docker测试跳过。覆盖无checkpoint旧chat直接续聊、两轮请求旧问答只继承一次、blocked/interrupted新问题不强制continue、任务前缀和正式成果不虚增、只含任务日志的chat发现、事件/task回退及重新规划确实调用模型、旧聊天/原始事实/Markdown的分支截止过滤、跨chat编号、原JSONL未改写、源变更拒绝和写入失败不切换。不调用真实模型、不修改真实session或业务项目；终端视觉和真实模型续聊质量未验收。

2026-10-08 session 菜单与 description 显示：新增13项离线测试通过，完整回归191项：188通过、3项真实Docker测试跳过；AST检查通过。覆盖自动加载历史问答/任务、自动选最近chat（不提供chat菜单）、分支选择、取消不切换、无checkpoint仅查看、启动输入错误不崩溃、编号/模拟键盘菜单、角色description及最终答案样式不变、分支历史隔离。不调用真实模型、不修改业务项目；尚未人工Windows终端视觉验收。

2026-10-07 历史浏览/旧 chat 打开：新增10项离线测试通过，完整回归178项：175通过、3项真实Docker测试跳过。覆盖非main chat重启后 /open 接着问答、Supervisor planning收到旧问答、task_id衔接、回退排除后续答案、未完成请求不能混入新问题、旧日志只读查看、chat隔离、分支前缀、分页和非法身份。未调用真实模型、未进行人工终端交互验收。

2026-10-07 终端 UI：新增 14 项颜色/模拟按键测试，完整离线回归 168 项：165 通过、3 项真实 Docker opt-in 跳过；AST 检查通过。覆盖默认 yes 必须 Enter、切换 no、取消/EOF、Windows 扩展键解码及 TTY 路由、编号兼容、非交互拒绝、角色配色和 NO_COLOR。未进行人工 Windows 终端视觉体验或付费模型复跑；本次不改权限范围/任务逻辑，不启动 Docker。

2026-10-07：逐次授权新增 test_per_tool_permissions.py，测试含 Supervisor 历史免确认及普通文件仍需确认、拒绝后不再调用模型、task_id/TaskOutcome 不推进、参数反馈、授权篡改/重用/写盘失败、总结重试和成功调用去重。
真实 Docker 测试尝试受引擎管道 dockerDesktopLinuxEngine 不存在阻塞，不自动启动引擎、拉镜像；付费模型和专用 Python/JDK 工具链未验证。下面历史验收结果不代表本轮 Docker 已通过。
最终离线完整回归 154 项：151 通过，3 项真实 Docker opt-in 跳过；新文件 25 项中 24 项离线通过，1 项真实 Docker 未完成。语法检查通过。

2026-10-06 副本终端：最终完整回归 129 项全部通过（显式开启真实 Docker，无失败、无跳过），
包含 Windows 临时 junction 排除、授权拒绝后立即停止、固定包/冲突/失败恢复、恢复否决转回 Executor，
及 nginx:alpine 的真实副本隔离。默认 Python/JDK 镜像在本机不存在，因此该工具链与付费模型未验证。
包括资源身份路径防逃逸、项目级应用锁、自动最终 yes/no、执行后元数据失败停止而不重执行。
副本元数据只存验证事实/有界日志摘录；完整原始工具正文仍保存在 tool_results.jsonl，不复制到 workspace.json。

2026-10-06 决策空转与跨 task 证据使用修复：独立源码副本完整离线回归 98 项，97 通过、
1 项 opt-in 真实 Docker 集成跳过。新增 11 项回归覆盖完整否决的阶段结束纠正、否决不可绕过、
正式成果不虚增、契约/协议有界重试、原始错误保留、写盘失败、证据上下文、综合结果交回监督，
以及实际循环的两 task“否决→修改→通过→最终交付”。使用模拟模型，不代表真实模型一定通过。
失败会话的 15 条原始 task 1 decision 回复逐条离线重放：均一次请求后返回明确否决，
没有工具重执行或正式成果虚增；原会话日志未改写。

最终原项目开启实际 Docker 后端后：87项全部通过，CLI --help 正常；下述默认跳过行为不影响单独实际验收结果。

2026-10-05 使用 D:\anacode\python.exe，在临时工作区运行 unittest discover：87 项，86 通过，1 项实际 Docker 测试需 opt-in 默认跳过。原有67项回归仍通过；新增去重、完整压缩输入/计费预算、连续问答、checkpoint补偿、恢复与回退、分支读取、单写者、权限和授权检查。

单独开启 CODING_AGENT_DOCKER_INTEGRATION=1、使用已有 nginx:alpine 镜像：test_sandbox.py 的5项全部通过，包含真实容器可写工作区、未挂载主机文件不可读写、伪测试 API key 不继承、只读失败、关闭网络、取消后自有容器清理。该镜像仅验证通用隔离后端，不证明 Python/JDK 工具链镜像可用。

专用 sandbox/Dockerfile 的镜像构建因 Docker Hub 连接超时失败；真实 Python/Java/Maven/Gradle 集成及依赖安装未运行。未使用付费模型/外部服务验收；模拟模型两轮端到端完成与 evaluation/decision 重新生成通过。Windows junction 需要系统创建权限的情况尚未专项验证。

命令、覆盖边界与未验证项见 README.md。不要将这些测试解释为跨文件事务、任意代码绝对安全或真实模型摘要质量保证。

## 16. 当前优先级

2026-10-08 终端易用性：/session 弹 session 选择菜单，不要求选 chat；自动选择最近更新且有检查点的 chat（同时间优先main），若该 chat 有多个分支再选择。复用 resume/open，自动显示对话最后一页、任务清单及角色历史 description 最后一页；不执行任务、不恢复旧批准。旧无检查点 session 也打开，在单写者锁内用 State/history_recovery.py 建立历史索引和 ready checkpoint；不改旧 JSONL、不复制工具正文，空目录没有可凭空回退的历史。启动 r 使用同一菜单，初始错误捕获后回到 agent>。

terminal_select.choose_option 提供 Windows TTY 上下/Tab/Enter 选择和编号 fallback，空行/取消不切换。/sessions 保留列表，/open 保留显式 chat 入口。terminal_render.show_description 与普通过程同角色样式：Executor 在 _remember 显示，Supervisor 在 _save_supervisor_turn 保存后显示；最终回答的普通输出未改。/descriptions 从角色日志只读读取累计 description，不显示轻量工具事件/review 重复摘要，按共享 event_seq 排序和分支前缀过滤，无可靠 event_seq 的旧角色记录不比较各自seq。不新增模型请求、不改变持久化正文。

按用户单独批准，将 AgentLoop/agent.py 的配置类名 cod 恢复为 RuntimeConfig（调用点和测试一直引用 RuntimeConfig），仅修改类名、不改配置字段或 INFORMATION.json。

2026-10-07 历史浏览入口：State/history_browser.py 仅只读枚举 session 下的 chat、按有效分支读取 timeline 问答；没有 branches/checkpoint 的旧 chat_history.jsonl 仅允许 main 查看，不伪造恢复。终端新增 /chats SESSION、/conversation --session SESSION --chat CHAT（每页8000字符）、/open SESSION CHAT [BRANCH]，/history 保留 JSON 视图并支持独立选择 chat、无活跃会话时查看旧 session。/sessions 显示当前存储根；不搜索其他根目录。

/open 是显式切换并复用 resume，不自动执行。直接输入问题始终开始同 chat 新请求，blocked/interrupted 不强制 /continue 或回退；旧未完成请求另记 request_superseded，并保留其 snapshot。/continue 仅显式继续旧目标，/rewind 仅主动回退。conversation_context 合并当前 chat 分支可见 timeline 与 chat_history，按请求/角色避免重复并补齐缺失问答；Supervisor planning 同时收到历史任务及正式成果。不同 chat 不自动混合；历史查询和 task_summary 仍绑定 chat/branch。

State/history_recovery.py 的 history_index.json 仅含旧记录源位置/哈希、导航事件与编号，不重复保存原始正文。session 全局 event_seq 包含所有 chat 的索引保留编号。角色 seq、tool_result_seq 不复用 event_seq。没有 snapshot 的主动回退通过可见前缀重建旧目标，/continue 使用当前配置明确重新规划，不能误跳过到历史任务末尾；没有可靠时间的旧日志标明 inferred_order，不伪称精确执行恢复。源被改变时拒绝；无法区分位置的重复旧记录保守按最晚匹配过滤。原始事实、角色读取和旧 Markdown 均遵守新分支截止边界。

以下是已知边界，不是自动执行指令：

1. 新增 State/session_checkpoint.py：typed dataclass 原子 checkpoint、session 单写者、统一 timeline event_seq、每边界前置 snapshot。CLI 恢复当前阶段，普通成功工具通过原始 locator 只重新总结；checkpoint 滞后时补齐已落盘事实。未知/中断操作不自动重放，需查看真实效果并选安全边界。
2. 会话路径为 --state-dir 下的精确 session ID，chat/branch 分离。JSONL 保持原文件位置/旧字段，增加 request_id/branch_id/event_seq；tool_result_seq 按整个 session 继续，角色 seq 按 chat 继续，同 chat 新请求 task_id 接在旧任务后。
3. /rewind 选择 event 或 task_start，创建有 parent/cutoff 的新分支。角色历史、原始事实读取、任务摘要、依赖成果与缓存遵守分支可见范围。原日志不修改，真实文件不回滚，无自动 Git reset。有 snapshot 精确恢复；无 snapshot 重建历史上下文并明确重新规划，不伪造精确执行状态。
4. 终端默认未授权；普通工具逐次 yes/no 选择（默认高亮 yes，仍须 Enter 确认），Supervisor 历史查询免确认；副本复制、网络、最终写回独立批准。底层显式 danger-full-access API 保留，但终端不接受此模式或额外根扩权。恢复不继承批准；状态目录不是目标项目。
5. 终端 Docker 只挂经批准的副本，不挂真实根/状态/密钥/socket；保留 network none、只读 rootfs、低权限与资源限制，无隐式 fallback。网络仅 on/off，外部 MCP 不得成为绕过副本的侧门。
6. 现有 write_in 的原子覆盖机制继续保存 task_summary.md，checkpoint 复用其 _atomic_write。JSONL、timeline、checkpoint、Markdown 不构成跨文件原子事务；写盘失败时仍须调查最后持久化边界，不声称绝对 exactly-once。
7. 静态路径检查不能抵御所有恶意并发替换 race；额外 Windows ACL/junction 专项、SDK 在途取消/计费、真实工具链与远端 MCP 的验收仍需单独验证。系统只清理自己创建的进程/容器。
8. 历史扫描和 snapshot/cache 会随长会话增长，尚无索引/保留策略；不自动删除旧记录。摘要质量和证据冲突解释仍依赖真实模型行为。

故障复现保留在 PROBLEM.md；判断是否修复以当前源码与对应测试为准。
