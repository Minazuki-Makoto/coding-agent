# 给 Codex：保持 Supervisor / Executor 架构的修复与优化指导

> 本次任务是把既定架构实现正确、接通、验证通过。**千万不要更换架构，不要退回旧实现，也不要把两个角色合并。**
>
> 审阅基线：2026-09-26，main 提交 `4b6cede7e06c4b08178ad440e4c4ea84456aee80`（`rewrite agent-loop`）。
> 依据：用户已经确认的设计，以及该提交的两个 loop、状态定义、模型适配器、MCP 路由与历史读写代码。下面“当前问题”是该版本的事实，“修正要求”是实施目标。动手前核对本地改动，不覆盖用户后续工作。

## 1. 必须保留的设计

项目采用 **Supervisor 规划与监督 + Executor 执行 + Agent 主循环调度**。

- **Agent 主循环**：管理会话、状态初始化、角色交接、任务进度、预算与最终保存，不替模型作业务规划。
- **Supervisor**：理解用户需求，必要时联系聊天上下文；结合 executor 实际工具能力和 skill 简介生成计划；为当前任务提供执行指导；验收 executor 结果；决定继续、推进、修复或调整计划。
- **Executor**：接受当前任务与 supervisor 的新指导，在本轮内部通过 ReAct 调用工具、观察结果、生成 description；可以为完成当前目标进行局部调整，但不自行改总计划、不推进 task_id。
- **Supervisor 内部保留两个阶段**：`evaluation_loop` 负责验收；`making_next_plan_loop` 负责后续指导和计划决策。这不是两个新 agent，不要恢复旧的“正确性 supervisor + 错误 supervisor”架构。
- **Host + ToolRegistry + MCP**：Host 管连接与路由；ToolRegistry 管角色权限、工具 schema 和结果规范化；loop 通过注册表执行工具。
- **结构化状态 + JSONL 历史**：内存状态服务当前控制流，持久化历史服务恢复与定位。保留现有 dataclass / Pydantic 思路，不引入数据库、向量记忆或新编排框架。

允许修正字段、初始化、接口、辅助函数、持久化格式和有限的兼容逻辑；这些是实现修复。不得改成单 agent、无限共享 messages、executor 自主检索全量记忆、supervisor 直接写代码或执行命令。

现有 `SupervisorState`、`SupervisorEvaluationState`、`ExecutorState` 可以承担之前讨论的 SupervisorAnswer / ExecutorAnswer 的交接职责。优先明确其字段语义并生成稳定快照，不必为了名称一致再复制一整套状态对象。

## 2. 正确的调度顺序

1. 收到用户问题，分配或接收 chat_id，初始化 AgentState，保留 session_address。
2. 注册 MCP，创建角色工具列表与 skill 简介目录。
3. Supervisor 判断是否需要历史聊天背景；需要时调用 **`read_history_chat`**，然后结合用户目标、executor 工具能力、skill 简介生成非空 task_list。
4. 设置当前任务位置为 0。Supervisor 先分析第一个任务，生成具体 executor 指导与完成条件。此时没有上一轮 executor，不运行虚假的验收。
5. 初始化本轮 ExecutorState，注入当前任务和新的 supervisor 指导。
6. Executor 在上限内执行：选择动作 → 调用工具 → 观察真实结果 → 生成 description → 更新状态、保存记录。
7. Executor 自报完成、达到上限或无法继续时，保存本轮结果并交回 supervisor。
8. 初始化本轮 supervisor 临时状态，但保留刚收到的 executor 结果；依次执行 evaluation、decision。
9. Supervisor 认可当前任务完成，才允许推进任务位置；未认可则保持当前任务，形成新的指导，再初始化 executor 开始下一轮。
10. 当前任务位置达到 task_list 长度时结束；生成实际交付内容，调用 save_chat_history。预算耗尽则返回未完成部分与阻塞原因，不能伪装成功。

**Executor 的 `is_finished=True` 只结束它当前这一轮，不能跳过 supervisor 验收。** 即使 executor 自报完成，supervisor 发现缺少必要验证，也必须能让它重新执行当前任务。

Supervisor 可以调整尚未完成的计划，包括增删和重排；必须保留有效完成成果与历史引用，不能因为“优化”而禁用重新规划能力。

## 3. 状态与上下文规则

### 3.1 每次角色接手都重新初始化临时状态

| 状态 | 交接时处理 |
| --- | --- |
| session_address、chat_id、user_query、当前计划与任务位置 | 保留并同步 |
| executor_seq、supervisor_seq | 保留，按各自序列单调递增，不随角色重启归零 |
| 当前任务累计尝试次数、整次请求预算 | 保留；只有切换到新任务时才能按约定重置任务尝试次数 |
| 当前角色 memory_window、旧工具结果、旧输出、错误字段、完成标志、已选 skill、本轮计数 | 清空或恢复默认值 |
| executor 的 passed_seq_list | 清空，只收集新一轮实际执行记录编号 |
| supervisor evaluation 临时记忆和旧判定 | 在新的 supervisor 回合开始前清空 |
| 刚交接的 executor 结果 / 新 supervisor 指令 | 作为本轮输入保留，不得在初始化时一起清除 |

“初始化”不是把整个 AgentState 重新构造为零；也不是只清空 memory_window。先保存或复制交接输入，再重建接收方临时状态。

同一次 supervisor 回合中，evaluation 产出的结论必须传给 decision。不要在阶段交接时清除结论，也不要让两个 state 共用同一个可变列表。

### 3.2 Executor 上下文固定有界

保留最近 **10 条、看到工具结果后生成的 description**，并提供最新工具观察。使用 `deque(maxlen=10)` 或等价裁剪均可。

每次模型调用还应包含本轮必要的用户约束、当前 target、supervisor 指导、可用 skill 简介和当前已加载 skill。角色再次接手时，上轮窗口全部清空。

仅限制 10 条不能保证 token 大小固定：还要应用已有 description、工具结果和整体上下文预算。超长结果要保留错误、关键路径、验证证据及记录定位，不能靠拼接累计 output_content 绕过预算。

持久化可以保留完整工具输入输出；传给模型的上下文使用有界摘要。不要把“上下文只用 description”错误实现为“磁盘也不保存工具证据”。

### 3.3 区分几个完成与错误概念

- 工具 `ok`：这一调用是否成功。
- Executor `is_finished`：executor 是否认为本轮委派目标完成。
- Supervisor evaluation `is_finished`：本轮验收是否结束，不等于任务通过。
- Supervisor `is_passed`：委派目标是否被接受。
- Supervisor decision `is_next_target`：整个当前 task 是否达到推进条件。
- 模型适配器 `status`：模型请求是否成功，不能代替工具是否成功。

委派目标可能只是当前 task 的一部分。已经完成这部分时可以认可它，但 task 未完成就继续给下一条指导。不要把“子目标完成、当前 task 未完成”一律当作执行失败。

历史上发生过一次工具错误不应永远阻止任务完成；保留错误记录，由 supervisor 根据后续修复与验证判断当前是否仍有未解决问题。

## 4. 按优先级修正当前代码

### P0：先修复启动、返回契约与无法执行的分支

| 位置 | 当前问题 | 修正要求 |
| --- | --- | --- |
| `AgentLoop/agent.py::main` | 函数没有函数体，整个模块无法解析 | 实现第 2 节的主调度闭环；不能只加 pass 后宣称完成 |
| `agent.py::state_init` | AgentState 多个必填字段未赋值 | 补全初始化和默认值；列表使用 default_factory；写入 query、任务位置、预算、seq、空输出 |
| `agent.py::SupervisorEvaluationState` | 只有类注解，没有 dataclass 构造和实例默认值 | 补上与其他状态一致的初始化机制 |
| `executor_loop.py` 顶部 | `from agent import ...` 包路径错误，并存在循环依赖风险 | 仅类型使用的导入放 TYPE_CHECKING；若运行时确实需要实例化，可提取轻量状态定义模块，保持职责不变 |
| `executor_loop.py::run_executor_loop` | 读 `response["tools"]`，而五个适配器均返回 `tool` | 统一 `status/message/tool` 契约，空工具统一用空列表 |
| 同上 | `tool_registry.call(...)` 没有 await | await 异步工具调用，拿到实际 ToolExecutionResult 后再继续 |
| 同上 | 第二次调用却 `await call_chat_function(...)`，该函数目前同步返回 dict | 统一同步/异步边界。推荐保留同步 SDK 适配器，在统一异步封装中用线程执行，并让所有调用点一致 await；不要只对 dict 加 await |
| 同上 | `response.get(message)` 的 key 是消息列表 | 应取字符串键 `"message"`；同时处理 tool-call 回答没有文本的合法情况 |
| 同上 | JSON bool 被调用 `.lower()` | 对最终 JSON 做 Pydantic 校验，直接使用 bool；不要对 bool/None 调字符串方法 |
| `executor_loop.py::_update_executor_state` | 刚设置 is_finished 后又强制设为 False | 删除覆盖；状态应反映本轮真实解析结果 |
| `supervisor_making_plan` | `len(response.get("tool") == 0)` 对 bool 求 len | 先规范化工具列表，再判断是否为空 |
| `evaluation_loop / making_next_plan_loop` | 用 `tools == {}` 判断无工具，但适配器返回 [] | 统一用规范化后的空列表判断；禁止空列表直接取 [0] |
| `evaluation_loop` 无工具分支 | 没解析 message 就把 None 的 supervisor_results 传给更新函数 | 解析、校验本轮 SupervisorEvaluation，再保存和判断是否结束 |

代码审阅中的实际语法检查使用 Python 3.12.14：两个 loop 均能通过 AST 解析，`agent.py` 因空 main 失败。嵌套同类引号的 f-string 在 3.12 可合法，不要错误地报告它们全部是语法错误；若支持 3.11，则改成兼容写法并明确最低版本。

### P1-A：修正 Executor 的完整执行链

针对 `run_executor_loop`、`_build_executor_messages`、`_update_executor_state`：

1. **无工具分支必须解析最终输出。** 现在直接 return 会丢失纯分析回答、description 和完成状态。无工具既可能是有效完成，也可能是选择 skill、等待信息或格式错误，不能一律当成功或空返回。
2. **执行前后顺序正确。** 工具真实返回后再生成 description，第二次输出也必须经过 ExecutorOutPut 校验。当前只校验第一次输出，第二次却直接读原始 dict。
3. **以工具结果判断工具错误。** 使用 ToolExecutionResult.ok/message/error_type；不能把第二次模型回答成功当成工具成功。成功时不要把整段模型 JSON 填进 error_message。
4. **填全实际执行字段。** 每步记录 tool_name、arguments、工具结果、description、当前 task 和 seq；`input_content` 不能写成模型输出 JSON。委派目标与工具参数分开表达。
5. **接回持久化。** 当前 executor loop 没有调用 save_state_history。每次真实工具调用都必须留下记录，包含失败调用；总结模型失败时也先保留工具事实，不自动重新执行写文件或安装等有副作用的工具。
6. **修复异常死循环。** 当前 except 只更新 now_state，不保存异常、不增加计数、不退出，重复失败可永久循环。异常路径必须消耗预算并产生可交接的错误结果。
7. **修正上限。** 当前先递增再比较 `max_executor_steps - 1` 会提前退出，部分自然退出路径又不更新交接状态。统一在一个结束出口整理结果，覆盖上限为 1、正常达到 N、异常和纯文本返回。
8. **使用正确状态键。** 删除 `response.get("state")` 与 `status` 混用。
9. **补足输入。** 当前消息没有 user_query、环境信息、skill 目录和稳定的最新工具观察；prompt 还提到未传入的 previous_description。同步 prompt 与真实字段。
10. **输出完整但有界。** 交接给 supervisor 的内容应含本轮 description、实际产出、必要证据、未完成项和记录编号，不传无限增长的原始 context。
11. **每步最多一个工具由代码保证。** 当前直接取 tool_calls[0] 会悄悄丢弃其余调用。收到多个调用时返回明确的单工具约束反馈，不执行后续工具，也不能声称全部执行了。
12. **passed_seq_list 不是“验收通过列表”。** 它代表本轮执行记录集合，应包含失败调用；可保留字段名，但把语义写清楚。

### P1-B：修正 Supervisor 的三条路径

#### 初始规划：supervisor_making_plan

- `_load_planning_skill` 当前硬编码加载 `D:\pycharmcode\coding_agent\Skills\memory_retrieval.md`，并把它作为完整规划 prompt。这会丢失规划职责。改为项目相对路径加载 `Skills/supervisor_plan.md`；memory_retrieval 仅在需要时作为检索流程使用。
- 实际输入目前只有 query、session_address、chat_id 和摘要，没有 executor_tools / skill_info。必须注入 executor 的实际能力说明，但 supervisor 的 tools 参数仍只提供它有权调用的工具。
- `task_list` 在最终完成时必须是非空 `list[str]`，每项非空。未完成规划时可为空，但不能索引 [0]，不能用“字段存在”判定完成。
- 规划输出同时要求 description；修正 prompt 中“只允许 task_list”与示例包含 description 的冲突。
- 不要将 `planning_times + 1` 当作全局 supervisor_seq。更新同一个权威计数源，再保存同步后的 state。
- 工具后若使用独立总结请求，应禁用工具；否则必须处理第二次真实 tool_calls，不能默默忽略。
- 规划完成后同步 now_task_id、now_target、SupervisorState.task_id/target；生成首轮指导时不验收不存在的 executor 输出。

#### 验收：evaluation_loop

- 进入新 supervisor 回合时初始化 `supervisor_evaluation_state`；当前代码清的是 supervisor_state.memory_window，实际消息却读取 evaluation_state.memory_window，旧验收记忆可能残留。
- 无工具回答也要解析、保存 description、is_passed、reason、is_finished；`is_finished=False` 不能直接转入 decision。
- 修正“未返回 needed_check 就默认检查完成”的提示。结束必须依据明确结构化结果，不能用字段省略代替完成信号。
- prompt 要区分“本轮 executor 子目标完成”和“当前 task 全部完成”。前者由验收判断，后者由决策据验收证据判断。
- 即使 executor 报 is_finished=True，也必须审查必要验证证据。无错误不等于有完成证据；信息不足应继续查询或给补充验证指导。
- `needed_check` 目前只含 seq_num/chat_id，定位还需要 task_id 和 session_address。使用 Host 给出的完整定位，不让模型猜编号。
- 验收 prompt 中 `read_history_by_seq` 是 Python 内部函数，并未按该名称注册 MCP。模型应调用实际注册的 `read_task_error`，不得绕过 ToolRegistry。
- 处理 skill 选择结果：当前 skill 字段定义了但未加载内容。按合法目录加载，下一次请求中生效。
- `_update_supervisor_evaluation_state` 不要用未正确递增的 evaluation_state.supervisor_seq 覆盖 supervisor_seq；列表按需要复制，避免别名共享。
- `_update_Agent_state` 的 `now_state.supervisor_seq = now_state.supervisor_seq` 没有同步作用，改为本轮真实编号。
- 达到验收上限仍未形成判断时返回“验收未完成/受阻”，禁止按默认 False/空理由直接继续执行。

#### 决策：making_next_plan_loop

- 输入字段与 prompt 对齐：现在传 `target_list / now_task`，prompt 要的是 `task_list / now_target`。
- `_build_decision_message` 现在根据 executor_is_passed 决定是否传 is_error，导致未通过时错误反而变为 False。直接传验收的真实错误状态。
- 保留 evaluation 结果，明确初始化 decision 自己的临时数据；不要通过清空错误对象碰运气。
- 无工具分支也必须保存 content.description、最终指导、判定与历史，且尊重 content.is_finished。目前该分支丢失 description，输出仍为空。
- 任务推进前检查 supervisor 的完成判定与必要证据，统一应用决策，避免两条分支分别修改进度造成行为分歧。
- 最后一个 task 完成后，先判断新位置是否等于任务数，再访问列表；目前自增后直接索引会越界。
- 修改 task_list 时校验非空项、已完成部分、当前任务位置和下一目标；不要先替换列表再盲目 task_id += 1。
- 保留顺序执行架构。若允许重排已分配编号的任务，增加轻量的“计划位置与历史任务编号”对应关系；若只调整未执行后缀，可保留简单列表。不要把历史 task_id 重编号，也不要改为 DAG 编排。
- `date_back_location` 的 prompt 是列表，模型却定义成单个 DateBack；统一为列表并校验每条引用。chat_id 必须为 str，保留前导零；不要转成 int。
- 回溯是依据历史重新指导 executor，不是回滚磁盘或重放旧工具。指导必须包含原目标、当前问题、修改意见和重新验证要求。
- executor 消息当前把 date_back_input 同时填进“原问题”和“修改意见”；分别传递真实内容，不能复制同一字段冒充两者。
- decision 上限耗尽时也必须形成可交接的受阻结果，不能隐式返回 None 让主循环使用旧指导。

### P1-C：Skill 目录与加载

针对 `agent.py::_load_skills` 和 `executor_loop.py::_load_skill_content`：

- `SKILL_BELONG` 当前的 `(AgentRole.SUPERVISOR)` 不是元组，未配置的文件又返回 None，`agentRole in ...` 不可靠。统一保存角色集合，未配置项返回空集合。
- `Skill.address=item.root` 是盘符根或文件系统根，不是 skill 文件路径。改为该文件的真实完整路径。
- 按确定顺序收集文件，校验 id/name；不要默认 id 永远等于列表下标。
- 当前 `skill_list[skill_id] != skill_name` 是对象与字符串比较，会阻止加载。改为查到对象后核对 name，并处理 None、未知 id、负数、越界和文件缺失。
- 保存加载函数返回值到本轮 selected skill；Claude 和其他 provider 都必须返回一致的结果。
- 初始只提供 id/name/description。模型选择后，由 Host 根据登记路径读取完整 skill；不得把所有正文首轮塞给模型。
- **skill 应在它指导的操作之前加载。** 当前先执行工具再加载 skill，无法指导该次操作。可增加一次无工具的选择阶段，然后加载并进入原有执行循环；不新增 agent。
- tool-call 回答允许 message 为空，不得要求每次 tool call 同时附带完整 JSON 才能执行。
- 重建消息时保留当前已加载的 skill 内容或受控引用，使它在本轮后续请求中仍有效；角色再次接手时重新初始化。
- 当前目录表只配置 memory_retrieval，其他文件缺少角色映射或简介。补齐有效项，允许 executor 的 skill 列表为空，不强制选择不存在的 skill。

### P1-D：工具结果回传协议

当前两个 loop 都把结果拼成 `role=user` 文本，且未附带对应 assistant tool call。

在 `loop_utils.py` 集中构造本步的调用与观察消息：

- OpenAI 兼容适配器：保留 assistant.tool_calls，然后对应 role=tool、tool_call_id 和序列化结果。
- Claude：保留 assistant 的 tool_use 块，再提供对应 tool_result / tool_use_id。
- 使用 ToolExecutionResult.to_dict() / to_model_content()，不要把 Python 对象或 coroutine 的 repr 当成工具内容。
- 只在本次调用—观察—总结阶段保留完整协议；下一步重建有界上下文，不因此引入全量 messages 历史。
- 工具执行后总结失败，只重试总结阶段，不再次执行已经产生副作用的工具。

## 5. 历史写入与记忆读取必须一起修正

这是闭环必需项，不能只修两个 loop 就结束。

### 5.1 当前已确认的不匹配

| 位置 | 具体不匹配 |
| --- | --- |
| `State/save_executor_state_history.py` | 从 state.tool 取名称，而 ExecutorState 字段是 tool_name；没有保存 tool_result、独立 description 或工具 arguments |
| 同文件与 `history_state.py` | 写入 is_finished/is_error，读取器却依据 is_solved、invalidates、repairs 恢复有效与待处理记录 |
| `mcp_history_resource_service.py` | 从 supervisor.description_content 取摘要，新写入记录没有该结构 |
| `save_supervision_state_history.py` | 未保存独立 description、output_content、验收通过情况以及被检查的 executor_seq 关联 |
| 同文件 | loop 将 ToolExecutionResult 对象直接写入 tool_result，json.dumps 无法序列化 |
| `mcp_supervisor_tools.py` | 读取 description、output_content、executor_seq 等字段，但新 writer 未完整提供 |

已用本提交的 writer / reader 在临时目录复现：一条 is_error=True 记录查不出待处理问题，一条 is_finished=True 记录查不出有效摘要；保存的工具名为空；直接保存 ToolExecutionResult 触发 TypeError。

### 5.2 最小修正目标

1. 定义一份明确的记录契约，并同步修改 writer、reader 和 loop；不要仅加字段让写入不报错。
2. 每个工具事件至少保存：session/chat/task 定位、角色 seq、工具名、真实 arguments、规范化结果、工具是否成功、错误信息、观察后的 description。
3. 保存角色回合的最终结果与退出原因。无工具的完成、验收、决策也需要记录，但要能与工具事件区分。
4. Supervisor 判定需要关联它审查的 executor 记录；本轮包含多个 seq 时保存明确引用集合，不能只关联最后一个而遗漏失败尝试。
5. **执行完成、工具成功、监督通过不能共用一个布尔字段。** 新 executor 记录在 supervisor 判断前属于待验收，不能因 is_finished=True 自动变为有效完成，也不能因缺少判定自动当成失败。
6. 保留“旧记录不改、追加后续判定、失效与修复引用”的记忆思路。可在现有 JSONL 中增加记录类型和引用，由读取器合并得到有效视图；不需要新存储架构。
7. `is_solved` 若用于兼容旧历史，只在归一化层依据确实存在的监督结论解释；不能简单映射 `is_finished`。未知旧状态保持未知。
8. seq 的产生只有一个权威来源。建议每个角色在当前 chat 内单调递增，任务切换也不重置；精确定位仍带 task_id。每条记录保存自己的固定编号，不因模型解析重试重复执行或重复分配同一事件。
9. JSONL 序列化前显式转换 dataclass、Pydantic、Path、工具结果和窗口；不要用 `default=str` 把关键结构全部变成不可恢复字符串。
10. 记忆恢复不能将读不到记录解释为任务成功。存储失败、格式错误、记录不存在应能区分。

## 6. 上下文检索和权限边界

### 6.1 工具名必须统一为 read_history_chat

用户已明确要求模型调用 **`read_history_chat`**。当前仓库却是：

- 内部 Python 函数叫 read_history_chat；
- MCP 对外注册名仍为 read_history_chat_resource；
- ToolRegistry 权限表仍列旧名称；
- memory_retrieval skill 使用新名称；
- supervisor 的规划 fallback 仍引用旧名称。

同时修正 MCP 注册名、权限表、prompt 和相关测试。内部导入可起别名，例如 `_read_history_chat`，避免包装函数改成相同名字后意外递归。可以保留旧名称兼容既有调用，但新模型上下文只推荐规范的新名称。

先判断当前输入是否足以理解用户意图；涉及缺失的历史约束、引用或决定时读取聊天记录，再结合当前问题回答或规划。信息已足够就跳过；只需要执行进度或查错时，不必先读聊天原文。读取结果按 chat_id 与明确关联筛选，不混入无关聊天。

### 6.2 保留此前商定的任务记忆流程

正常：读取当前 task 的有效 description，按内容选择相关记忆，必要时按 locator 查原始执行。

异常时按需逐步读取：

1. `read_task_history_error`：当前任务未解决问题。
2. `read_task_error`：有明确 task_id + seq 时核对原始执行。
3. `read_now_task`：当前任务仍有效的摘要，避免重做。
4. `read_history_task`：仅当涉及前序依赖时读取。
5. `read_supervisor_history`：需要时核对过去的监督检查。
6. `read_history_chat`：补充仍缺失的用户背景；开头已读且足够时不重复。

每一步信息足够就停止，不机械读完所有历史。计划重排后，`read_history_task` 当前按数字小于 task_id 筛选的实现需要与任务对应关系保持一致，不能把数字顺序当成真实依赖。

Supervisor 保持只读检查与记忆检索权限；Executor 维持执行工具和本轮有界窗口，不开放历史检索权限。工具 schema 过滤与实际 call 权限检查都必须保留。

## 7. 主循环接线与退出保障

在 `agent.py::main` 中完成：

- 按现有 provider 映射创建模型客户端，注册 MCP；连接生命周期覆盖整个任务，离开 AsyncExitStack 后不再调用。
- 用 `ToolRegistry.schemas_for` 分别构造角色工具列表；供规划参考的 executor 能力说明与 supervisor 可执行 tools 分开。
- 调度 planning → 首轮指导 → executor → evaluation → decision → 再次 executor，全部退出路径收敛为清晰结果。
- 由 supervisor 产生任务推进决定，由主调度入口统一校验并应用；不要让两个分支和 main 各自再推进一次。
- 对模型请求、单轮执行、监督回合、同一任务反复尝试设置有限预算；角色 init 不得清掉累计限制。
- 当前任务未完成、但 supervisor 不断产生新指导时，达到 max_task_attempts 必须退出并说明阻塞，防止外层无限重试。
- 最后一个任务完成后不再构造 task_list[len(task_list)] 的消息，也不再调用 executor。
- 输出真正的交付物或回答；不能只把“第几轮完成了什么”的内部轨迹当作最终用户答案。
- 保存 chat_history，并关闭模型客户端和 MCP 连接。
- 将读取 INFORMATION.json 和创建运行配置移到明确启动阶段，避免导入状态定义时就访问用户本地配置。保留现有配置文件方案，不引入新的配置框架。

## 8. 建议实施顺序与验收

按以下顺序做，每完成一段再进入下一段：

1. 修复语法、导入、状态初始化及模型/工具返回契约。
2. 修通 executor 的单步执行—观察—总结—保存，包含异常和无工具输出。
3. 修通 supervisor 的 planning、evaluation、decision，保留三者原职责。
4. 统一历史写入与读取，修正 skill 加载和工具命名。
5. 接通 main，并验证连续两个任务、被否决后重试、达到上限和最终结束。
6. 更新与新实现冲突的 CODEX.md 说明，记录实际验证范围。

使用假模型响应和假 ToolRegistry 做行为验证，先不要依赖真实付费 API 或用户项目的写操作。至少覆盖：

| 场景 | 必须观察到的结果 |
| --- | --- |
| 首次请求 | supervisor 先规划并指导第 0 项，不验收不存在的 executor 结果 |
| 工具成功并返回 JSON bool | 不调用 lower；观察后生成 description；真实工具只执行一次 |
| 工具失败但总结模型成功 | 仍保存工具错误，supervisor 能读取该次失败 |
| 工具已执行，总结 JSON 错误 | 保留执行证据，仅重试解析/总结，不重复副作用 |
| 模型只返回最终 JSON，没有工具 | 正确解析并交接，不丢失回答；未完成则有限继续 |
| Executor 自报完成但缺验证 | supervisor 否决；task 不推进；新指导要求补验证 |
| 本轮子目标完成但 task 未完成 | 接受有效成果，继续当前 task 的下一部分 |
| 再次进入任一角色 | 临时字段清空，上一轮窗口不残留；seq 和累计预算保留 |
| 第 11 条 description | 窗口仅保留最近 10 条，工具结果与总上下文受限 |
| 重复异常、executor 或 supervisor 上限为 1 | 有限退出，产生明确结果，不提前少跑或陷入死循环 |
| 最后一个 task 完成 | 结束并保存聊天，不越界、不额外执行 |
| 计划重排 / 回溯 | 原记录定位仍有效，不自动撤销文件、不重放历史工具 |
| 新格式日志写入后检索 | 有效摘要、待处理失败、精确记录、监督关联均可读取 |
| 非法 skill id / 缺文件 / 空 skill 列表 | 有限反馈或继续无 skill 流程，不越界、不重复死循环 |
| 权限 | supervisor 无法调用写入/运行工具，executor 无法调用历史工具 |
| read_history_chat | 注册名、权限与 prompt 一致；上下文足够时不强制调用 |

可先做静态检查，再做上述确定性行为测试；通过 AST 不等于运行正确。真实模型与真实 MCP 未运行时，在交付中明确说明，不声称端到端已通过。

## 9. 不要做的“优化”

- 不更换 Supervisor / Executor / Agent 主循环的职责划分。
- 不因为状态错误而绕过 supervisor 的最终否决权。
- 不恢复旧版独立查错 agent 或旧的整套 state 字段。
- 不改成全历史消息累积，不把记忆工具开放给 executor。
- 不用默认成功、吞异常、无限重试掩盖问题。
- 不把全部 skills 首轮加载，不让 skill 获得额外工具权限。
- 不重写所有业务工具；本次优先修 loop、状态与持久化闭环。遇到阻塞当前验证的工具实现问题，再做有针对性的修正。
- 不把旧 CODEX.md 中的历史描述当作当前目标。该文件仍有“supervisor 获得全部工具”“旧审查入口”等过时内容，应按本次用户要求纠正。

最终交付说明只需要：修改了哪些文件、分别解决什么问题、架构是否保持、实际测试结果和剩余阻塞。

## 源码依据

以下链接固定到审阅提交；以函数名定位，避免后续改动导致行号误导：

- [Agent 主入口与状态定义](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/AgentLoop/agent.py)
- [Executor loop](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/AgentLoop/executor_loop.py)
- [Supervisor loop](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/AgentLoop/supervisor_loop.py)
- [模型消息与调用适配](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/AgentLoop/loop_utils.py)
- [ToolRegistry 与权限](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/MCP_functions/tool_registry.py)
- [Executor 历史保存](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/State/save_executor_state_history.py)
- [Supervisor 历史保存](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/State/save_supervision_state_history.py)
- [记忆 MCP 注册](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/Context/mcp_resources.py)
- [历史有效性恢复](https://github.com/Minazuki-Makoto/coding-agent/blob/4b6cede7e06c4b08178ad440e4c4ea84456aee80/Context/History_Resorce/history_state.py)

