# Memory Retrieval

Supervisor 在 evaluation 阶段默认加载本技能。它规定查询优先级，但不要求每轮调用历史工具：
先使用 handoff、dependency_context、本轮结果和 locator，只有存在明确证据缺口时才逐层查询；得到足够信息立即停止。

## decision 的累计任务摘要查询

decision 每轮会收到本轮 Executor output 和 Supervisor evaluation；`historical_auxiliary_summary`
只提示 task_summary.md 是否存在，默认不附带正文。模型自主决定是否读取：对累计有效成果、
历史依赖、旧结论修正、剩余工作或推进条件没有特别准确的把握时，先调用
`read_task_summary(task_id=当前任务编号)`，不要凭最新单轮结果推断多轮成果。
输入已经充分且无矛盾时可直接决策，不必每轮例行读取。

默认只返回指定 task 最新累计记录；确需前序任务背景时使用 `include_other_tasks=true`
或指定已知前序 task_id。Host 绑定当前 session_address/chat_id，不允许跨 chat 或查询未来任务。
已查结果放在本轮 `memory_query_results`，先判断是否解决缺口，足够就停止；原文细节仍缺少时
再按 tool_result_seq 调用 `read_tool_result`。同一查询不要重复。not_found 或读取失败不代表验收通过。

## 一、先判断是否需要联系上下文

先检查本轮输入是否足以明确用户目标、当前任务、约束和已确认的决定。

- 输入已经包含所需背景：直接使用，不重复读取聊天记录。
- 用户要求“继续”“修改之前的内容”，或目标、约束依赖当前输入缺失的历史信息：调用 `read_history_chat(session_address, chat_id)`，恢复相关上下文后再检索任务记忆。
- Host 会绑定并校验 session_address 与 chat_id；模型必须沿用已提供的值，不自行构造路径、改写编号或扩大到其他聊天。只提取必要背景，不复制整段历史。
- 历史要求与当前用户明确要求冲突时，以当前要求为准；无法恢复的关键信息应说明缺失，不猜测。
- 若只需恢复执行进度、失败原因或监督结论，直接进入下面的任务记忆流程，不必先读聊天原文。

## 二、正常情况：按固定优先级补充记忆

### 第零步：使用 handoff 与跨 task 依赖

先检查 `supervisor_handoff`：

- `previous_work`、`accepted_work`、`remaining_work` 表示当前 task 跨 attempt 的交接；
- `dependency_context` 中的 `TaskOutcome` 是前序 task 已通过 Supervisor 验收的事实；
- `evidence_references` 提供 tool_result_seq、工具名、路径和状态；
- `execution_mode=synthesize` 表示当前 Executor 应只综合这些证据，不应产生新的工具结果。

dependency_context 足以支持当前验收时直接使用，不再调用 `read_history_task`，也不能要求综合任务重新读取原文件。
只有摘要与当前输出矛盾、缺少某个决定性事实，或必须核对原文时，才沿 locator 精确查询。

摘要不是事实白名单。成功目录结果的 evidence_references.paths 可直接证明文件/包存在，
摘要省略不会使这些前序证据失效；存在性不等于源码职责、构建或运行验证。
不要因摘要省略就否决；需要正文细节时由 Supervisor 查询原始历史，synthesize 模式的
Executor 不重新读取项目。未通过的 previous_work 不能作为已验收依据。

历史足够时结束本次阶段：Supervisor 的 is_finished 表示验收/决策已形成，否决也可为 true；
不是任务通过标志。明确交回 Executor 修正时，不再重复查询或无工具输出 false。

### 第一步：优先使用 Executor 本轮反馈

先检查 evaluation 输入中已经提供的：

- `executor_results`；
- `executor_is_finished`、`executor_is_error` 和 `executor_error_message`；
- `tool_summaries`；
- `executor_tool_result_locators`；
- 当前任务的完成条件。
- `supervisor_handoff` 和其中的 dependency_context。

这些信息足以验收时直接形成结论，不调用历史工具。工具成功摘要不能单独证明整个任务完成，但也不能因为模型总结失败就否认原始工具已经成功执行。

### 第二步：读取当前任务有效 description

调用：
```python
read_now_task(session_address, chat_id, task_id)
```

读取同一 chat_id、同一 task_id 已经通过 Supervisor 验收的 description。返回结果包含 description 和 executor locator；普通失败尝试不在此列表中。

根据 description 选择相关记忆：

逐条阅读 description，找出与当前 target 有关的已完成工作、已有结论和限制。

- 摘要足够时直接采用，不展开全部原始记录。
- description 足够时直接采用，不继续扩大查询。
- 需要定位该 Executor turn 对应的工具证据时，读取记录中的 `tool_result_refs` 或 `tool_summaries`；不要假设 executor seq 等于 tool_result_seq。
- 根据 description 的内容选择记录，不根据编号相近或路径相似猜测相关性。

### 第三步：读取前序 task 摘要

只有当前 task 明确依赖先前 task、dependency_context 没有对应 TaskOutcome，或者摘要仍缺少决定性接口、配置、路径或设计背景时，才调用 `read_history_task`。只采用与当前目标直接相关的前序摘要，不重复获取 handoff 已携带的 task。

### 第四步：精确读取原始工具事实

只有摘要不足以判断，且已经获得明确的 `tool_result_seq` 时，调用：

```python
read_tool_result(session_address, chat_id, tool_result_seq)
```

- 必须逐个读取需要核对的 locator；存在多个相关 locator 时，可以每轮查询一个，读取后再判断是否还需下一个。
- `read_tool_result` 返回 `tool_results.jsonl` 中的唯一原始事实；不得用 executor seq 或 supervisor seq 代替 tool_result_seq。
- 当前 evaluation 已经持有的 `memory_query_results` 不要重复查询。
- 不允许为了“更放心”无条件展开全部原始结果。

## 三、出现错误：先看本轮事实，再扩大范围

Executor 报错、总结失败、未完成或达到执行次数上限时，采用下面的顺序。每步读取后判断是否还缺信息。

### 第一步：以 Executor 本轮反馈为准

先分析 `executor_results`、错误字段、`tool_summaries` 和 locator。区分：

- 工具执行失败；
- 工具执行成功但模型总结失败；
- 没有执行工具；
- 工具结果存在，但完成条件尚未满足。

不能把“总结失败”改写成“工具没有结果”。

### 第二步：按 locator 核对调用结果

若本轮反馈无法说明真实工具输出，并且提供了 `tool_result_seq`，调用 `read_tool_result` 精确核对。存在多个可能影响结论的结果时逐个查询，不得只读取第一个就推断其余结果。

### 第三步：读取当前任务有效 description

调用 `read_now_task`，确认同一 task 已有哪些工作通过验收，避免重复执行。当前 attempt 尚未通过的记录不会出现在这里，因此空结果不代表本轮工具没有执行。

### 第四步：读取当前任务未解决问题

调用：

```python
read_task_history_error(session_address, chat_id, task_id)
```

- 找出当前仍未解决的失败尝试和被判为失效的旧执行。
- 区分原始失败、后来失效和无效修复。
- 空结果只表示没有恢复出历史待处理项，不代表当前错误不存在。

### 第五步：按 executor seq 读取轻量执行记录

当第一步或当前输入给出明确的 task_id 和 executor seq 时，调用：

```python
read_task_error(session_address, chat_id, task_id, seq)
```

- 核对当时的 description、错误、`tool_result_refs` 和 `tool_summaries`。新格式不在 Executor 历史中保存完整工具正文；需要正文时继续调用 `read_tool_result`。
- 不根据错误文本相似就猜测编号；同一记录的必要信息已经完整返回时不重复查询。
- 该工具也能读取成功执行，但不会自动附加后续失效或修复状态。
- 旧记录只用于定位，是否仍适用必须结合当前执行结果或项目状态判断。

### 第六步：读取前序任务依赖

只有错误涉及先前接口、字段、配置或设计决策时，调用：

```python
read_history_task(session_address, chat_id, task_id)
```

传入当前任务编号，只采用与本次错误有关的摘要。

当前实现返回 task_id 数值更小的任务，不包含当前任务，也不理解计划重排。计划调整后，使用 Host 提供的真实任务编号对应关系。

### 第七步：读取过去的监督检查

需要确认 supervisor 以前检查过什么、某次检查为什么失败，或避免重复无效检查时，调用：

```python
read_supervisor_history(
    session_address,
    chat_id,
    task_id=None,
    executor_seq=None,
    seq=None,
)
```

使用已知 task_id、executor_seq 或 supervisor 的 seq 缩小范围；不必把所有参数都填上。

- `executor_seq` 仅在历史记录实际含有该字段时才使用；新 Supervisor 记录通常应优先按 task_id 或 supervisor seq 查询。
- seq：supervisor 自己的检查记录编号。
- 当前工具返回监督工具调用历史，不保证有独立的 supervisor description；根据实际返回恢复检查情况，不编造未保存的分析。

### 第八步：按需补充聊天原文

前面检索后发现问题仍涉及缺失的用户目标、约束或历史决定，且已有上下文不足时，调用：

```python
read_history_chat(session_address, chat_id)
```

Host 会强制绑定当前 session_address 和 chat_id，只返回当前聊天允许读取的历史。开头已经读取且信息足够时不重复调用。

## 四、结合上下文整理结果

每次工具返回后，用 description 增量记录：新查到了什么、哪些既有工作仍有效、具体缺口、相关路径与记录编号。不要重新罗列全部历史。

supervisor 结合当前用户问题、handoff、相关聊天上下文、任务记忆和本轮 Executor 输出继续验收或调整指导。回答应直接对应当前目标，不只罗列历史摘要。当前 task 的下一 attempt 会通过 handoff 获得 previous/accepted/remaining work；跨 task 会通过 dependency_context 获得已验收 TaskOutcome，因此指导中只需写清新增目标和缺口，不必复制全部旧内容，也不能只写“按上次继续”。

只保留与当前任务相关的摘要和必要证据，不把完整历史重新塞入上下文。工具报错或返回被截断时，说明缺失内容；可用已知编号进一步缩小查询，但不添加工具没有的分页、搜索或 description_only 参数。
