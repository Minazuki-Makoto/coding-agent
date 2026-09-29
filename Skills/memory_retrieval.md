# Memory Retrieval

supervisor 在每次重新接手任务后，通过本技能恢复当前任务所需的 executor 执行记忆和 supervisor 检查记忆。先判断是否需要联系历史上下文，再按正常或异常流程查询；得到足够信息就停止。

## 一、先判断是否需要联系上下文

先检查本轮输入是否足以明确用户目标、当前任务、约束和已确认的决定。

- 输入已经包含所需背景：直接使用，不重复读取聊天记录。
- 用户要求“继续”“修改之前的内容”，或目标、约束依赖当前输入缺失的历史信息：先调用 `read_history_chat(session_address)`，恢复相关上下文后再检索任务记忆。
- 工具读取整个会话目录且不自动按 chat_id 过滤；只采用当前聊天对应的相关记录，提取必要背景，不复制整段历史。
- 历史要求与当前用户明确要求冲突时，以当前要求为准；无法恢复的关键信息应说明缺失，不猜测。
- 若只需恢复执行进度、失败原因或监督结论，直接进入下面的任务记忆流程，不必先读聊天原文。

## 二、正常情况：读取同一任务的 description

### 1. 读取当前任务摘要

调用：
```python
read_now_task(session_address, chat_id, task_id)
```

读取同一 chat_id、同一 task_id 的有效成功摘要和未解决的失效提示。返回结果包含 description 和 locator；普通失败尝试不在此列表中。

### 2. 根据 description 选择相关记忆

逐条阅读 description，找出与当前 target 有关的已完成工作、已有结论和限制。

- 摘要足够时直接采用，不展开全部原始记录。
- 需要核对某条记录时，将其 locator 中的 session_address、chat_id、task_id、seq 原样传给 `read_task_error`，核对实际动作和结果。
- 根据 description 的内容选择记录，不根据编号相近或路径相似猜测相关性。

### 3. 按需要补充背景

- 需要恢复 supervisor 以前的检查情况：调用 `read_supervisor_history`，优先按当前 task_id 筛选。
- 当前任务依赖前序结果：调用 `read_history_task`，传入当前 task_id。
- 发现失败、失效或相互冲突的结论：进入异常流程。
- 新任务没有历史：使用本轮提供的目标和指导，不强行查出一条记忆。

## 三、出现问题：按六步逐渐扩大检索范围

executor 报错、未完成、达到执行次数上限，或 supervisor 验收发现问题时，按下面的顺序检索。每步读取后判断是否还缺信息；后续步骤按需执行。

### 第一步：读取当前任务未解决问题

调用：

```python
read_task_history_error(session_address, chat_id, task_id)
```

- 找出当前仍未解决的失败尝试和被判为失效的旧执行。
- 区分原始失败、后来失效和无效修复。
- 空结果只表示没有恢复出历史待处理项，不代表当前错误不存在。

### 第二步：按编号读取原始执行

当第一步或当前输入给出明确的 task_id 和 executor seq 时，调用：

```python
read_task_error(session_address, chat_id, task_id, seq)
```

- 核对当时的工具输入、输出、错误和 supervisor 总结。
- 不根据错误文本相似就猜测编号；同一记录的必要信息已经完整返回时不重复查询。
- 该工具也能读取成功执行，但不会自动附加后续失效或修复状态。
- 旧记录只用于定位，是否仍适用必须结合当前执行结果或项目状态判断。

### 第三步：读取当前任务有效摘要

调用：

```python
read_now_task(session_address, chat_id, task_id)
```

确认哪些工作仍然有效，避免让 executor 重做已经验证的部分。结合待处理记录和修复关系，检查旧问题是否已有有效修复；不能只凭后来一条成功 description 判定问题消失。

### 第四步：读取前序任务依赖

只有错误涉及先前接口、字段、配置或设计决策时，调用：

```python
read_history_task(session_address, chat_id, task_id)
```

传入当前任务编号，只采用与本次错误有关的摘要。

当前实现返回 task_id 数值更小的任务，不包含当前任务，也不理解计划重排。计划调整后，使用 Host 提供的真实任务编号对应关系。

### 第五步：读取过去的监督检查

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

- executor_seq：关联的 executor 执行编号。
- seq：supervisor 自己的检查记录编号。
- 当前工具返回监督工具调用历史，不保证有独立的 supervisor description；根据实际返回恢复检查情况，不编造未保存的分析。

### 第六步：按需补充聊天原文

前面检索后发现问题仍涉及缺失的用户目标、约束或历史决定，且已有上下文不足时，调用：

```python
read_history_chat(session_address)
```

它读取整个会话目录的聊天记录，不自动按 chat_id 过滤。只采用当前聊天对应的记录；开头已经读取且信息足够时，不重复调用。

## 四、结合上下文整理结果

每次工具返回后，用 description 记录：查到了什么、哪些工作仍有效、哪些问题未解决、相关路径与记录编号，以及下一步缺少什么信息。

supervisor 结合当前用户问题、相关聊天上下文、任务记忆和本轮 ExecutorAnswer，回答本次问题、继续验收或调整指导。回答应直接对应当前目标，不只罗列历史摘要；历史中的成功结论不能代替本轮验证。由于 executor 下一回合会清空之前的临时上下文，继续执行所需的背景必须写进新的 SupervisorAnswer，不能只说“按上次继续”。

只保留与当前任务相关的摘要和必要证据，不把完整历史重新塞入上下文。工具报错或返回被截断时，说明缺失内容；可用已知编号进一步缩小查询，但不添加工具没有的分页、搜索或 description_only 参数。
