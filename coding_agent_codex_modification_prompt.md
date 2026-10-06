# coding-agent 完整修改提示词：终端问答、历史恢复、选步重新生成与沙箱

你正在修改 D:\pycharmcode\coding_agent 这个项目。请实际完成代码修改和验证，不要只给方案。

**一、严格限定本次范围**

用户已经确认的方向只有：

1. 修复同一次 Executor 执行轮内，相同工具与参数在成功后仍可能重复执行的问题。
2. 上下文只有超过一个较大的、可配置的字符阈值后，才额外调用模型生成摘要；普通截断和模型压缩分开处理。时间顺序历史中，最新结果和修正的优先级高于较早记录。
3. 添加真实可运行的终端入口，支持同一会话内连续问答、历史对话查看，以及 Executor、Supervisor 的每个 task、工具事件和控制阶段查看。
4. 用户选择恢复哪个会话，程序负责恢复，模型判断恢复后的下一步。
5. 用户可以选择历史步骤，从该步骤重新生成；选择权属于用户，原始历史必须保留。
6. 补齐终端中的沙箱和操作授权：用户或可信配置选择权限，Host/程序执行权限限制；模型可以提出操作请求，不能自行授权或提升权限。

以下默认参数、命令名称和模块拆分是为实现这些方向提供的建议，不冒充用户已经确认的细节。可以按现有代码做合理调整，但必须满足行为和验收要求。

以下尚未单独确认，不作为本次独立功能实现：

- 全项目的旧成果失效、文件版本驱动的证据撤销体系。
- 失败记录与修复记录的完整 repairs/invalidates 状态机。
- 与本次功能无关的 Supervisor 决策、验收标准或模型供应商重构。
- 摘要后阈值每次自动增加 3000 字符的默认策略。
- 把“用户选择的对话回退”实现成磁盘文件回滚、Git reset 或历史工具自动重放。

实现恢复与回退时必须过滤对应历史范围，排除回退点之后的成果；这是本次回退功能所必需的历史隔离，不等同于新增全项目失效体系。

**二、先阅读并核实这些实现**

优先查看：

- AgentLoop/agent.py：AgentState、ExecutorState、SupervisorState、SupervisorEvaluationState、SupervisorHandoff、TaskOutcome、state_init、_run_main、main。
- AgentLoop/executor_loop.py：run_executor_loop、_reset_executor_turn、_build_executor_messages、_summarize_tool_once、_remember、_tool_call_fingerprint、_tool_call_signature。
- AgentLoop/supervisor_loop.py：supervisor_making_plan、_apply_plan、_prepare_initial_guidance、run_supervisor_loop、evaluation_loop、making_next_plan_loop、_build_decision_message、_memory_query_observation、_evaluation_memory、_call_supervisor_model。
- AgentLoop/loop_utils.py：bounded_text、build_model_messages、build_tool_observation_messages、call_chat_function。
- State/save_executor_state_history.py、State/save_supervision_state_history.py、State/save_tool_result.py、State/save_chat_history.py、State/task_summary.py。
- Context/History_Resorce/ 下的历史读取与状态重建代码，以及 Context/mcp_resources.py、Context/mcp_supervisor_tools.py。
- MCP_functions/tool_registry.py：工具权限和 Host 对 session_address/chat_id 的绑定。
- backend.py、README.md、模型配置加载代码和 tests/。
- MCP_functions/System_Files/Files_function/write_in.py、bfs_read.py、python_code.py、java_code.py。
- MCP_functions/System_Files/Files_server/mcp_system_server.py、MCP_functions/MCP_hosts.py：MCP 服务启动、stdio 传输和授权边界。

先确认的代码事实：

- 当前 handoff.to_dict() 会复制 completed_tool_calls；它不是本轮 Executor 新增成功调用列表的实时引用。
- 当前去重判断读取 handoff.completed_tool_calls 和前序 reusable_read_calls，但成功后更新 executor_state.completed_tool_calls。
- bounded_text 按字符截断；keep_tail=True 保留首尾，删除中间，不进行模型摘要。
- 工具 observation 当前在总结之前被硬截到 12000 字符。
- 多处 memory_window 会无条件删掉第 10 条之前的记录。
- MODEL_CONTEXT_MAX_CHARS 当前没有完整约束整个请求。
- state_init 每次新建 AgentState；只读取 Supervisor 序号，不完整恢复任务状态或 Executor 序号。
- load_last_tool_result_seq 实际扫描整个 session 的工具日志，取所有 chat 中的最大值；不要误改为只考虑当前 chat。
- backend.py 当前只创建目录并运行一个硬编码示例，不是完整的终端会话入口。
- write_in.py 的可信可写根目录检查只约束它自己的文件写入，不能限制 Python、Java、pip、Maven/Gradle 或外部 MCP 的副作用。
- python_code.py、java_code.py 存在直接 subprocess 调用；只设置 cwd 或 CREATE_NO_WINDOW 不是进程沙箱。
- mcp_system_server.py 的安装和打包工具描述宣称不读取 stdin，但实际传 interactive=True；底层会 input()。必须修复这个协议/终端职责冲突，不能继续让 stdio MCP 服务消费用户输入。

**三、修复成功工具在同一执行轮内重复执行**

在 run_executor_loop 的工具执行前，成功调用检查至少覆盖：

1. handoff 中进入本轮前已经完成的调用。
2. executor_state.completed_tool_calls 中本轮新增的成功调用。
3. 现有前序 TaskOutcome 提供的可复用读取记录。

保留现有工具名加规范化参数的 fingerprint/signature 生成方式，不引入模糊匹配，不把两个不同参数的调用当成相同调用。

核心修复相当于：

~~~python
already_completed = (
    fingerprint in handoff.get("completed_tool_calls", [])
    or fingerprint in executor_state.completed_tool_calls
    or reusable_signature in prior_reusable_read_calls
)
~~~

接入原有重复提示、有限重试和退出逻辑。维持已有 Supervisor 明确要求重试/重新执行时的放行行为，本次不扩展关键词策略。

约束：

- 工具返回 ok=True 后才能加入成功调用集合。
- 工具失败后，允许相同参数再次尝试。
- 成功工具的模型观察总结失败，不得因此再执行同一个工具。
- 进入 synthesize 模式仍然不允许执行工具。
- 回退分支不能把回退点之后的成功调用继承到去重集合，否则重新生成会被旧记录错误阻止。
- 程序恢复历史本身不调用真实工具。

**四、上下文压缩：高阈值触发，分离同步截断与异步模型调用**

4.1 保留 bounded_text 的同步、确定性职责

bounded_text 只处理普通截断和失败兜底，不在内部调用模型，不把它改成隐式异步函数。

继续直接截断：

- raw_model_response_excerpt，现有 4000 字符。
- 校验反馈中的原始响应片段，现有 2000 字符。
- 多工具调用协议反馈的调用列表，现有 1600 字符；可以用程序保留工具名和必要参数以减少体积。
- 校验错误说明，现有 1200 字符。
- latest_tool_context.message，现有 1000 字符。

这些诊断内容保留原始错误，不能通过模型改写后冒充原始响应。完整工具输出继续使用现有工具日志保存。

4.2 新增显式的异步 compress_context

可以新增 AgentLoop/context_compression.py，或放在职责清晰的现有模块中。参数必须区分：

- trigger_chars：超过多少输入字符才触发模型压缩。
- summary_max_chars：模型摘要的目标长度。
- strategy：summarize 或 truncate。
- content_type：工具正文、记忆正文、时间顺序历史、answer 等。
- 模型调用依赖、当前用户目标和证据引用等必要上下文。

具体返回类型可以使用 dataclass 或清晰的结构，至少能表示处理后的内容、是否摘要、是否截断、原始字符数、原文 locator 和压缩失败原因。不要把 coroutine 塞进 information 字典或 JSON 序列化。

强制行为：

- strategy=summarize 且输入没有超过 trigger_chars：原样返回，不额外调用模型，不先做低阈值截断。
- strategy=summarize 且输入严格大于 trigger_chars：调用模型摘要。
- strategy=truncate：调用 bounded_text，完全不调用模型。
- 同一份大材料已经处理过时，不在 action_selection、tool_summary 和下一轮消息组装中重复摘要同一版本。缓存或复用必须包含内容版本、任务目标和历史分支范围，不能仅按路径复用。
- 摘要失败时保留原文定位、明确标记降级/截断，并按兜底预算返回；失败不等于真实工具失败，也不代表任务已经通过验收。
- 摘要调用无工具 schema，不允许产生工具操作。
- 所有额外模型调用进入现有 model_request_count/max_model_requests 和 total_token 统计。
- 压缩器自身的模型请求禁止再次触发 compress_context，避免递归摘要。
- 为摘要调用设置有限重试，例如最多 2 次；不能无限循环。
- 模型写出的“少于多少字符”仅是指令，程序还要检查长度并执行明确的有限重试/降级策略。

阈值与默认值：

- 用户确认的是“超过较大的字符数后才摘要”，没有确认一个不可更改的具体数值。
- 起始实现可采用 60000 字符作为较大触发阈值、6000 字符作为摘要目标长度，并通过配置覆盖；这是实施默认值，不是用户固定要求。
- 不采用此前讨论中的 12000/24000/30000 作为默认的额外模型摘要触发阈值。
- 默认使用固定高阈值。本次不默认实现“每次摘要后阈值增加 3000”；如记录后续讨论项，也不能悄悄启用。
- 日常工具执行后的 _summarize_tool_once 仍然存在。高阈值规则限制的是新增的压缩调用，不取消现有的观察总结流程。

4.3 哪些内容接入模型压缩

| 内容 | 精确要求 |
| --- | --- |
| 工具 observation | 压缩 output 中的长正文/代码/日志，不把整个 ToolExecutionResult JSON 当普通文本任意改写 |
| memory_query_results | 压缩长查询正文或超阈值的查询材料集合，保留来源与 locator |
| memory_window / recent_descriptions / supervisor_history | 对累计历史压缩，保留历史摘要和近期记录，不每条 description 都调用一次模型 |
| handoff.previous_work / accepted_work | 累计超阈值时压缩描述，继续分开未验收尝试和已接受成果 |
| dependency_context / completed_task_outcomes | 累计过长时压缩成果描述，保留 task_id、验收依据、证据引用和来源分支 |
| 最终工作摘要阶段输入的 answer | 能容纳时直接交给现有最终摘要模型；超阈值或超预算时才处理长材料，必要时分块 |

程序原样保留工具名、ok、error_type、错误信息、路径、分页游标/has_more、tool_result_seq、task_id、证据引用等影响执行和验收的字段。摘要清楚标记为压缩后的正文，不伪装成原始工具返回值。

时间顺序历史的摘要指令：

- 当前用户要求、最新结果、最近的修正优先。
- 较早内容允许更强压缩，但仍有效的约束、成果、未解决问题和必要证据定位必须保留。
- 不按每轮等长复述。
- 新旧内容冲突时写明冲突与最新证据，不擅自改变验收状态。
- 单份工具正文按任务相关性和证据重要性压缩，不使用“文本越靠后权重越高”的规则。

用户问题优先完整保留；skill 优先完整读取或拆分后按需加载。本次不新增通用的用户需求改写器，也不通过盲目截断删掉末尾限制或 skill 执行规则。

4.4 修改调用链，而不只新增一个没人调用的函数

在执行和监督的异步阶段准备压缩后的材料，然后交给同步消息构造器：

- build_tool_observation_messages 不得先执行旧的 12000 字符硬截断，再把残缺内容交给 compress_context。
- _memory_query_observation 的异步依赖由调用链显式 await，不能在同步函数里阻塞运行事件循环。
- _remember、_evaluation_memory 和 memory_query_results 的数量裁剪不能在累计摘要之前删掉事实。
- SupervisorDescriptionHistory.model_context 的历史选择应与高阈值压缩协调，不能先悄悄删除旧记录再宣称摘要覆盖完整历史。
- 避免把同一事实同时以完整 previous_work、accepted_work、tool_summaries 和其他副本重复塞入请求。
- 保持 OpenAI/GLM 等消息格式和 Claude tool_use/tool_result 格式正确，保持调用 id 与 observation 对应。
- 保持工具原文的持久化顺序：真实结果保存后，才产生可丢弃的模型上下文副本。

对最终组装的 messages 和 tools schema 检查整体预算。total_token 是累计消费量，不能当作当前上下文长度。字符预算与 token 预算明确区分；若使用 token 估算或供应商提供的计数，说明估算边界。不能因为单字段没有超阈值，就无限堆积整个请求；也不能为了压缩预算静默删除当前用户限制和必要规则。

当整个请求超过配置的硬预算时，先去除重复上下文，再对超过高阈值的历史材料集合进行压缩。没有可合理压缩的大材料且仍不能容纳时，给出明确的预算受阻状态，保留记录；不能偷偷提前按很小阈值调用摘要，也不能未经用户要求无限上调预算。

**五、终端入口、连续问答与历史查看**

5.1 提供真正可运行的入口

把 backend.py 的硬编码演示改成终端入口，或由它转到新 CLI 模块。至少支持 python backend.py --help 和交互启动；也可以提供 python -m coding_agent，但不能只写文档而没有对应入口。

保留 AgentLoop.agent.main 的程序调用能力和现有配置加载，兼容 Windows 路径、中文输入、UTF-8 输出和现有 Python 环境。不得硬编码 D 盘目录或示例 query；工作目录、状态目录、模型配置和沙箱策略来自参数/配置。终端入口不依赖 Flask/Web 服务，不把这次任务扩成 Web UI 或复杂 TUI 重写。

支持交互模式，以及通过明确启动参数恢复指定会话。参数可包括 --workspace、--state-dir、--config、--sandbox，以及 resume 的 session/chat/branch 标识。以实际实现的 --help 为准，README 中的每个命令必须能运行。

没有恢复参数的启动流程提供“新建会话 / 恢复会话”选择；不要让模型挑选目录或 chat_id。同一活跃会话收到下一条用户消息时自动接续，不每轮重新询问会话。

5.2 连续问答和本地控制命令

普通文本作为当前会话的新用户消息进入既有 AgentLoop。最终回答输出后回到输入提示，用户可以继续提问、补充要求或查看记录。只有用户明确新建/切换会话，才改变会话身份。提供有明确结束方式的多行输入，保留换行；空输入不触发模型。

不要为普通问答另造一套不共享上下文的聊天机器人。新用户轮次以当前有效历史、必要成果和最近上下文开始，历史过长按第四部分处理。不要把所有旧工具原文无条件重发给模型。未完成请求先显示可恢复状态，由用户选择继续或从选定边界建立新分支；不能把新问题悄悄塞入旧任务，也不能静默丢弃旧请求。

本地命令由 CLI/Host 解析，不送进模型。建议实现以下命令，名称可微调，但不能缺少对应能力：

| 命令 | 必须实现的行为 |
| --- | --- |
| /help | 显示命令、参数和当前模式 |
| /new | 新建会话，保留当前会话的已保存记录 |
| /sessions | 列出会话、最近更新时间、任务状态和可恢复标识 |
| /resume | 用户指定或选择 session/chat/branch，程序恢复 |
| /history | 默认查看用户消息与最终回答，支持分页、用户轮次/分支筛选和文本检索 |
| /tasks | 列出目标、task_id、尝试次数、验收与完成状态 |
| /tools | 列出 Executor/Supervisor 的工具事件，可按 actor、task、phase、状态筛选 |
| /show | 展开选中消息、task 或事件的详情、参数、原始结果和证据定位 |
| /rewind | 用户选择 task/事件，从规定的前置边界重新生成并建立分支 |
| /sandbox 或 /permissions | 查看当前实际权限、有效根目录、网络规则、后端和限制；用户可以显式修改策略 |
| /exit | 保存状态并退出 |

查看、检索、分页和列会话是本地读操作，不调用模型，不重新规划、不产生工具副作用、不增加模型请求计数。查看其他会话不会自动把它切换成活跃会话。标识解析有歧义时列出候选项，由用户选择。

5.3 对话历史和执行历史分别展示，但能互相定位

历史对话至少记录原始用户文本、用户轮次/请求标识、最终回答、时间、完成/中断/失败状态、所属分支和关联 task。用户消息在开始执行前落盘，最终回答在完成后落盘；中断或错误信息不能伪装成完整回答。对已有 chat_history.jsonl 做兼容读取，缺少的新字段按明确旧格式规则解释，不伪造精确状态。

支持查看原始历史，不仅是 description/task_summary。压缩用于模型输入，不能把历史展示的用户文本、最终回答或工具原文永久改成摘要。允许用户查看旧分支的全部记录，但当前模型只能使用有效历史范围。

工具和控制事件至少展示：

- session/chat/branch、用户轮次和稳定 event_id/event_seq。
- task_id、目标、尝试次数和完成状态。
- actor=executor 或 supervisor。
- phase，如 planning、action_selection、tool_summary、evaluation、decision。
- 工具名称、参数、真实 ok/error、执行/等待授权/拒绝/中断状态、tool_result_seq 和关联序号。
- 描述、验收判断、剩余工作。
- 事件属于当前有效前缀、旧分支还是其他分支。

工具摘要失败与工具真实失败分别展示。用户能展开实际参数与结果；每次真实执行对应真实调用事件，不因观察总结制造额外工具执行。大正文支持分页，并给出完整记录定位。

执行过程中显示当前 actor、task、阶段、工具开始/结束、真实结果状态、预算受阻和最终回答。通过结构化事件/回调连接 CLI，不从散落 print 文本推断运行状态，不暴露模型隐藏推理。CLI 输出与 MCP stdio 协议隔离；工具输出的终端控制字符在展示时安全转义，原始记录仍保留。

5.4 中断、退出与非交互运行

Ctrl+C 中断当前请求并返回终端控制；Ctrl+D/EOF 有明确退出行为。先停止新的模型/工具调度，取消可取消请求，终止由本程序拥有的运行子进程及其子树，保存中断边界。不能只取消 Python coroutine，而把 Java、pip 或构建进程留在后台。

已完成且落盘的工具结果不丢失。正在执行、无法确认结束结果的操作标记为 interrupted/unknown，不写成成功，也不因恢复自动重跑。非交互模式的授权请求按配置拒绝或返回明确受阻结果，不能挂住等待 input()。

模型调用和 MCP 连接由运行器管理，终端输入只归 CLI 所有者读取。授权等待时冻结对应操作，避免同一 stdin 被多个协程或服务争用。

**六、会话恢复：用户选择，程序重建，模型继续**

6.1 持久化与恢复

新增显式 save/load/checkpoint 机制；可放在 State/session_checkpoint.py 或职责等价的模块。检查点至少记录：

- 格式版本、session_address/chat_id/branch_id。
- 原始用户要求、当前用户轮次、任务清单、now_task_id、now_target。
- 正在进行的阶段、待总结的工具结果、已经落盘并应用的控制决策。
- executor_seq、supervisor_seq、tool_result_seq 和统一时间线序号。
- handoff、completion_criteria、previous_work、accepted_work、remaining_work。
- 已有 TaskOutcome、证据引用、成功调用集合。
- 上下文历史摘要、近期记录，以及恢复执行所需的选中 skill/指导。
- 当前请求已使用的模型调用预算、token 统计等必要计数。

不保存 API key、SDK client、ClientSession、AsyncExitStack、线程锁或运行中的 subprocess 对象。恢复时重新创建模型客户端和 MCP 连接，权限仍由当前 Host/配置约束。

序列化要能还原 dataclass、整数键、set 与 locator，不能只把 JSON 读成一个层次混乱的 dict 后冒充 AgentState。

检查点原子写入，并与追加日志的高水位关联。在真实工具结果保存、执行轮结束、监督验收、决策和 task 推进等边界保存。考虑“工具成功日志已落盘，但检查点尚未更新”的中断：通过已保存事件补齐状态，不能再次执行已经成功的操作。

工具操作开始前记录调用意图，结束后记录真实结果。若进程中断于外部操作和结果落盘之间，仅凭本地日志无法证明外部副作用是否发生，恢复时保留 unknown 状态并显示核对要求，不宣称具备所有外部操作的 exactly-once 保证，也不自动重放。

同一个会话/分支的两个进程不能同时写状态或恢复执行；使用明确的单写者保护，错误信息可理解。

6.2 两种接续场景

- 尚未完成的请求：恢复原 task、阶段、完成条件、指导和预算，不重新调用 _apply_plan 把 now_task_id 清零，不重复 initial guidance 或已完成的任务。
- 已完成会话接收新用户请求：保留历史事实和旧记录，新请求以新的用户轮次进入规划与执行；新的任务编号/请求作用域不能与旧记录冲突。

可以使用同一 chat 内追加任务并保持 task_id 唯一的方案，或显式 request/turn 作用域；选择一种并贯穿状态、locator、历史查询和测试。不要只在一处加字段，其他读取仍把新旧 task_id=0 混在一起。

恢复时校验目录、身份、分支范围和日志序号：

- executor_seq 至少为选中 chat 的已有 Executor 最大序号。
- supervisor_seq 至少为对应已有 Supervisor 最大序号。
- tool_result_seq 按 session 级日志的真实最大值续接。
- 新事件不能覆盖旧编号或产生模糊 locator。
- 非法恢复目标不自动退化为新会话，也不恢复另一个“看起来相似”的目录。

将选中历史的有效摘要和当前待办提供给模型，明确标记为恢复上下文。模型判断下一步和需要核对的环境变化；模型不决定恢复哪个会话，也不通过“读取了摘要”宣称已恢复全部运行状态。

**七、用户选步重新生成：逻辑回退与历史分支**

回退的确定语义：

- 用户选中一个 task 或事件 E，并要求从它重新生成。
- 默认恢复到 E 之前的状态，从该阶段再次让模型产生新结果。
- 选择 task 时，对应任务开始之前的边界。
- 如果另提供“从 E 之后继续”，必须单独命名并显示，不能与“重新生成 E”混用。
- 新结果写入新分支，原分支的事件、输出与证据保留可查看。
- 用户选择后程序执行状态恢复；模型只处理所选分支的下一步。

新增统一事件时间线/索引，例如 timeline.jsonl，给新事件单调 event_seq 或稳定 event_id。现有 executor_seq、supervisor_seq 是各自的计数，不能把 executor_seq=5 与 supervisor_seq=5 当成同一个时间点。时间线事件应引用现有工具原文和已有日志，不复制大量正文。

事件与状态索引至少能定位：

- Executor 每次真实工具调用及其前置模型选择阶段。
- Executor 的工具总结与执行轮结束。
- Supervisor 的规划、工具查询、evaluation、decision。
- task 开始、推进和完成边界。

为可回退边界保存前置状态，或用验证过的检查点加事件重建前置状态。恢复者必须知道选中阶段：

- 选择 Executor 工具调用：重新生成该调用前的行动选择，不自动复用该调用之后的 observation。
- 选择工具总结：保留已经执行的工具结果，只重新生成总结，不能再次执行工具。
- 选择 Supervisor 查询工具：恢复到相应阶段的工具选择之前。
- 选择 evaluation/decision：保留其之前的 Executor 结果，重新生成对应监督阶段，不重新跑 Executor。
- 选择 task：重新进入该 task 的规划/指导/执行边界，不丢掉更早任务的必要成果。

建立分支 lineage 和可见历史范围。检查点、模型上下文、memory 查询、TaskOutcome、已完成调用集合、摘要缓存和 task_summary 读取都必须遵守分支截止点。祖先前缀可见，祖先截止点之后的事件与其他分支不可作为当前有效事实。

原始日志可以共同追加保存，但不能只改 now_task_id，然后仍把“未来” accepted_work、TaskOutcome 或去重集合传给模型。历史读取可以展示旧分支全部内容用于查看，模型使用的有效上下文必须单独按范围过滤。

Host 继续绑定 session/chat/branch 的授权范围；必要的祖先前缀由程序解析，不能让模型任意指定另一个 chat 或目录来绕过范围。

对话回退不撤销真实世界副作用：旧文件写入、命令执行、网络操作不会自动还原。终端应简洁说明“重新生成对话步骤，当前文件保持现状”；后续模型依据当前文件与真实结果继续。禁止自动 Git reset、文件覆盖还原或无提示重放旧工具。用户选择回退并不意味着要求重复所有历史副作用。

旧格式日志兼容：

- 没有 branch_id 的旧记录按明确的 legacy/main 分支处理。
- 恢复可验证的序号与状态，不静默覆盖旧文件。
- 若可以验证旧日志顺序和所选边界，则迁移出时间线索引。
- 缺少关键记录或检查点，无法准确重建某步之前状态时，明确标记该边界暂不可精确回退，仍允许查看可读历史；不伪造已完整恢复。
- 新格式必须支持前述全部 task 和事件边界。

**八、沙箱与授权：Host 执行限制，模型只提出操作**

这里的“沙箱”专指文件、进程、网络和工具副作用的执行权限；会话恢复是第六部分的状态管理。两者分别实现，不能用“模型判断能否继续”代替执行权限控制。

8.1 明确策略和有效权限

新增职责清晰的 SandboxPolicy/PermissionPolicy 与统一执行入口。保留现有工具 actor 权限作为第一层，再检查本次操作的文件、进程和网络权限。actor=executor 并不等于拥有任意主机权限。

至少支持以下模式；这些默认值是实施建议，允许配置覆盖，但必须展示最终生效的策略：

| 模式 | 行为 |
| --- | --- |
| read-only | 用户工作区只读；禁止改写用户项目。程序自身可以写专用状态/日志目录，受隔离进程可以使用专用临时目录 |
| workspace-write | 建议作为开发任务默认模式，仅允许写入用户选定工作区及明确配置的构建缓存/临时目录；不默认开放其他主机路径 |
| danger-full-access | 只有用户参数或可信配置显式启用，明确显示当前没有进程沙箱隔离；模型不能开启 |

在受限模式中，读取也受可信 readable_roots 约束。工作区外确有必要的工具链读取目录由可信配置提供，不能直接把整个 C:/、用户主目录或盘符根设成默认读取根。工作区是用户选定的目录，不能由工具参数扩大。

模型 API 客户端运行在 Host 中，不属于执行沙箱内的用户代码。模型 API 连通性和工具联网权限分别配置；允许 Host 调用模型不等于允许项目代码、pip 或浏览器联网。

建议受限执行默认关闭网络，依赖安装等联网操作由用户明确授权。网络放行的实际粒度取决于后端能力：只能开/关网络时，不声称支持域名级隔离。限制、后端可用性和额外读取/写入范围都显示在 /sandbox 中。

启动和恢复时以当前可信配置/用户选择的权限为准。历史记录可以展示旧策略，但恢复或回退不能自动重新启用旧的 full-access、旧的额外路径授权或旧批准记录。

8.2 让文件工具真正遵守策略

统一处理 bfs_read.py、write_in.py、Java 项目读取、解释器搜索和其他本地文件入口的路径权限。保留已有原子写入行为。

路径校验在 Host/MCP 可信层执行；模型不能传 trusted_roots、修改权限环境变量、提供 confirmed=True 或修改策略对象来放行。验证绝对规范路径、..、符号链接、Windows junction/大小写/盘符与 UNC 路径，不能仅做字符串 startswith 判断。目录遍历逐项校验，不能从可读根中的链接跳到根外。

程序专用的状态目录、授权记录和配置文件由 Host 写入，不能因为位于工作区内就允许普通文件工具任意改写。历史工具仍只能读取 Host 绑定的 session/chat/branch 有效范围；用户终端查看完整旧记录不代表模型工具有跨会话任意读取权限。

文件根目录检查是路径权限层；不能把这一层描述成足以限制任意 Python/Java 代码的操作系统沙箱。

8.3 所有项目代码与构建进程走真正的执行后端

抽取统一 SandboxRunner/ExecutionGateway，将 python_code.py 和 java_code.py 的 Python 运行、解释器探测、pip、javac/java、Maven/Gradle、打包与启动检查接入。扫描所有相关 subprocess/Popen 入口，不能只包裹一个运行工具，而其他入口直接在主机执行。

真实隔离必须由操作系统或容器实现。根据开发环境选择一个可验证后端，优先兼容用户的 Windows 环境；例如 Docker Desktop 容器后端，或者已实现并验证的 Windows/WSL 隔离后端。至少完成一个真实可用后端，不能只交付空接口和文档。

若采用容器：仅挂载必要工作区，按模式设只读/读写，专用缓存/临时目录明确授权；不挂载主机用户目录、API 配置、会话状态、Docker socket 或整个文件系统，不使用 privileged 或默认 host network。受限代码不能接触 Host 模型 API key。解释器和 JDK 使用后端配置的可信工具链；Windows 宿主路径与容器路径必须有明确映射，不能把宿主的 python.exe/java.exe 路径直接塞进容器。

后端未安装、不可用或无法提供所需限制时，拒绝需要隔离的进程执行并显示原因；不能静默退回普通 subprocess。文件查看和终端历史仍可使用。用户若明确切换 danger-full-access，才允许走无隔离的本地执行路径，并在界面与事件中准确标记。

禁止把 cwd、shell=False、命令关键字黑名单、CREATE_NO_WINDOW 或进程组本身当作隔离保证。统一执行入口要管理参数、超时、输出上限、取消和子进程树清理；使用最小必要环境变量，不继承模型密钥或其他无关凭据。原始退出码、stdout/stderr、后端、限制和执行状态可追溯。

超时或取消后不能宣称没有发生文件变更。若只能确认进程被终止而不能确认操作结果，记录 unknown/interrupted，由恢复流程处理，不自动重试有副作用的未知调用。

8.4 授权由终端完成，MCP 不读取用户输入

修复 mcp_system_server.py 中两处 interactive=True 与说明冲突：安装/打包 MCP wrapper 只进行参数校验并返回结构化 confirmation_required；不调用 input()，不打印到协议 stdout。

Host 接到授权请求时，终端显示具体工具、规范化参数、工作目录、目标路径、网络需求和请求原因，由用户选择允许本次或拒绝。已有沙箱内的普通操作直接执行，不每次读文件都弹确认；越权、安装依赖、配置要求确认的构建等按策略请求授权。本次不新增模型自动授权器。

授权必须绑定实际操作的不可变参数、会话/分支与策略版本；批准后由可信 Host 执行同一操作。模型文本中的“用户同意了”、模型生成的 approved/confirmed 参数和旧历史批准记录均不能成为凭据。参数改变、切换分支、请求取消或策略变化后，旧授权失效。

MCP 内需要落实授权时，使用 Host 控制的内部调用/授权通道；不要把能绕过授权的布尔参数直接注册到模型工具 schema。内部已有 confirmed=True 参数可以由可信代码使用，但不能由模型直接指定来跳过终端授权。

拒绝、取消、等待授权、权限受阻与真实执行失败分别记录。返回 confirmation_required 不计为工具成功，不进入 completed_tool_calls，不触发“已经执行完成”的状态推进。授权后产生的真实调用仍遵守第三部分的成功去重。

8.5 外部 MCP 与执行边界

对工具建立明确的 capability 信息，如只读、写入、代码执行、联网和外部副作用。权限检查在 Host 路由层执行，不能仅依赖工具自己的描述。不要因工具名称带 read/get 就把未知外部工具当作已验证只读。

GitHub、浏览器、Docker 等远端/外部 MCP 操作未必受本地进程沙箱限制，必须按其能力由 Host 放行、请求用户授权或拒绝。受限模式下未知写入/执行能力默认不可调用，直到可信配置补齐策略；不能宣传“本地沙箱会自动限制所有远端服务”。

受限环境要控制 MCP 启动配置和外部执行入口，防止绕过统一执行层。必要的可信连接配置由用户/Host 提供。文档说明哪些能力已经强制限制、哪些后端可用、哪些外部能力需要单独授权。

**九、验证要求**

优先扩展现有 tests/test_agent_loop.py、test_task_summary.py、test_task_summary_retrieval.py，以及文件、Python/Java 工具测试；必要时新增压缩、会话、时间线/回退、CLI 和沙箱测试。使用 QueueChat、FakeRegistry、临时目录或等价替身，不用真实模型请求、真实付费工具或用户项目写入进行回归验证。

必须覆盖以下行为：

工具去重：

1. 首次工具成功但任务未结束；模型再次返回同名同参调用，真实 registry.call 只发生一次。
2. 相同工具的不同参数可执行。
3. 失败调用允许同参重试。
4. Supervisor 明确要求重跑时保留原有放行行为。
5. 工具成功而总结失败后，不重执行成功工具。
6. 现有跨 task 复用、synthesize 禁用工具与请求预算测试通过。

压缩：

7. 低于阈值与恰好等于阈值时，新增摘要调用次数均为 0；严格超过时才触发。
8. truncate 模式始终不调用模型；压缩器不会递归调用自身。
9. 工具正文中间的关键事实不会在送去摘要之前被旧 12000 字符截断删掉；检查实际摘要输入。
10. tool_result 原始日志保持完整；身份、状态、错误、路径、分页和 locator 不被模型改写。
11. 历史在压缩之前不会因第 10 条/第 6 条裁剪而丢掉；输入保留顺序和分支范围。
12. 同一版本材料不重复压缩；内容、目标或分支改变后不错误复用。
13. 压缩调用正确计入模型预算和 token 统计；失败、超长摘要与预算耗尽有有限且明确的处理。
14. OpenAI/GLM 和 Claude 的工具消息配对仍正确；整体预算检查覆盖组装后请求。

恢复与终端：

15. 保存后重启，任务进度、阶段、handoff、成果与计数恢复一致。
16. 不重复规划已恢复任务，不重复 initial guidance，不因恢复自动调用工具。
17. 工具成功落盘而检查点滞后时，可以补齐成功集合和序号。
18. 已完成会话接收新请求后，旧 task/locator 不被覆盖，当前请求预算与会话历史计数区分清楚。
19. 不同 chat/branch 的状态隔离；无效目标或损坏数据给出明确错误。
20. 终端能列会话、显示每个 task 和 actor 的事件、展开参数/结果，并按用户选择恢复。
21. 单写者保护有效，旧日志可以读取或得到明确迁移限制。

回退：

22. 用户选定 Executor 工具事件，从调用之前生成新分支；其之后的成功记录不能阻止新分支的行动选择。
23. 用户选定工具总结，只重新总结原 observation，真实工具调用次数不增加。
24. 用户选定 Supervisor evaluation/decision，只重新生成对应阶段，不重新执行此前 Executor。
25. 选择 task 恢复对应开始边界；更早有效前缀保留，更晚成果排除。
26. 原分支可查看、未被改写；新事件归属新分支。
27. memory 查询、task_summary、压缩缓存和依赖成果遵守分支截止点。
28. 回退和恢复过程本身不修改用户项目文件、不重放命令、不执行 Git reset。

终端问答和历史：

29. 同一进程完成两轮用户问答，后续轮次接续有效历史，用户消息和最终回答完整保存，身份/任务作用域不冲突。
30. 重启后可查看原始用户消息和最终回答，分页/搜索/筛选不调用模型、不执行工具；查看其他会话不自动切换活跃目标。
31. /help、空输入、多行输入、EOF、无效命令和不明确的标识有确定行为；中文及含空格的 Windows 路径不被破坏。
32. 终端展示 Executor/Supervisor 的真实阶段与工具结果，可从用户轮次定位 task/event；摘要失败不伪装成工具失败或成功执行。
33. Ctrl+C/退出保存明确中断状态；模拟执行取消时清理拥有的子进程，不把 unknown 记录写成成功。
34. 非交互模式遇到授权请求不读取 stdin；stdio MCP wrapper 不调用 input()，协议输出不会被终端提示污染。

沙箱与授权：

35. read-only 模式拒绝用户工作区写入，Host 仍能保存专用状态记录；普通工具不能改写状态/权限配置。
36. workspace-write 允许授权根内写入，拒绝根外、..、符号链接/junction 跳转；目录读取和遍历同样受可信读取根约束。
37. 所有 Python/Java/依赖/构建/启动 subprocess 入口进入统一执行层，不存在漏掉的主机执行旁路。
38. 后端不可用时受限运行被拒绝；没有任何自动降级成无隔离执行的分支。用户显式 full-access 的状态可查看。
39. 模型提供 confirmed=True、假批准文本或修改 roots/策略参数均不能授予权限；批准后参数改变或分支切换不能沿用旧授权。
40. 用户拒绝不执行安装/打包，允许一次只执行对应操作；confirmation_required 不进入成功集合，成功后同参调用仍被去重。
41. 受限代码环境没有模型 API key；网络权限、文件挂载和外部 MCP 能力按真实实现限制，未知外部副作用不会默认放行。
42. 恢复/回退不自动提升沙箱模式，不继承已过期批准；未确认的中断操作不会自动重放。
43. 对实际隔离后端做可重复的集成检查：临时工作区内允许的操作成功，读取/写入未挂载的主机临时文件被阻止，read-only 工作区写入失败，关闭网络时外连失败，取消后无遗留子进程。仅用 mock 检查“调用了 runner”不算验证真实隔离；环境缺后端时明确说明哪些集成检查未运行。

先运行针对这些行为的测试，再运行相关既有测试。基础 unittest 命令可使用 python -m unittest discover -s tests；如果实际环境需要别的入口，说明原因。缺依赖、测试失败或未运行的集成检查必须如实记录，不能宣称全部通过。

**十、实现和交付方式**

按顺序实现：工具去重；压缩与调用链接入；持久化和身份/序号；统一时间线；终端连续问答/查看/恢复；用户选步重新生成；权限策略和真实沙箱后端；终端授权接入；回归和实际后端验证。新 CLI 启动任何进程工具前，必须先接好权限层，不能以“稍后补沙箱”为由默认无隔离执行。

保留现有 Executor/Supervisor 分工、工具权限、真实结果日志和 task_summary.md 机制。新增必要模块即可，不把项目重写成另一套框架。

完成后给出：

1. 改动文件和各自职责。
2. 真实可运行的终端命令及新建、连续问答、查看历史/工具、恢复、重新生成、查看沙箱权限和授权安装/打包示例。
3. 阈值、摘要目标长度、预算、工作区、状态目录、沙箱模式/后端、文件根目录和网络权限的配置说明。
4. 会话/分支身份规则、事件边界和旧日志兼容说明。
5. 实际运行的测试及结果，未验证内容和已知限制。
6. 简短说明如何保证“用户选择目标，程序恢复状态，模型决定下一步”，以及哪些操作不会自动重放。
7. 所选沙箱后端的实际安装/启用条件、Windows 支持情况、隔离边界、不可用时的受阻行为，以及实际运行过的隔离集成检查。

提供一个最小端到端操作示例：启动终端 → 新建会话 → 连续问答两轮 → 查看用户历史和 Executor/Supervisor 工具详情 → 退出重启 → 用户选择恢复 → 用户选择一个历史 evaluation/工具总结重新生成 → 查看新旧分支 → 查看沙箱状态 → 授权或拒绝一次需要确认的操作。示例必须对应实际实现，不能用示意命令冒充可运行功能。

不要仅输出建议、伪代码或未来计划；完成以上已确认范围内的修改后再报告结果。

参考 Codex 的职责划分，而非复制其全部架构：

- CLI 参数决定 resume 选择器、--last 或指定 session：
  https://github.com/openai/codex/blob/c8949e55c28a97eb2770971eb3c6a918bdefd9af/codex-rs/cli/src/main.rs#L2581-L2604

- 历史加载与记录续接由程序完成：
  https://github.com/openai/codex/blob/c8949e55c28a97eb2770971eb3c6a918bdefd9af/codex-rs/thread-store/src/local/live_writer.rs#L40-L135

- 会话恢复与 sandbox 权限是不同职责：
  https://github.com/openai/codex/blob/c8949e55c28a97eb2770971eb3c6a918bdefd9af/codex-rs/exec/src/lib.rs#L1459-L1483

- 沙箱模式是程序配置，不由模型推断：
  https://github.com/openai/codex/blob/c8949e55c28a97eb2770971eb3c6a918bdefd9af/codex-rs/protocol/src/config_types.rs#L104-L114

上述 Codex 链接用于参考职责划分，不代表其沙箱后端可以直接用于本项目。实现本项目权限机制时，以选用后端的官方资料和实际隔离验证为准。
