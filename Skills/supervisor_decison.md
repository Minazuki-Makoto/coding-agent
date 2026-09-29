# Supervisor Decision Skill

## 目标

本技能用于 Supervisor 的 decision 阶段。evaluation 已经完成对上一轮 Executor 的验收；decision 只负责根据验收事实决定：

1. 当前 task 是否满足推进条件；
2. 若不推进，下一轮 Executor 应执行什么以及如何验证；
3. 是否需要调整尚未完成的计划后缀；
4. 是否需要依据历史记录重新指导，但不回滚磁盘、不重放旧工具；
5. 最后一个 task 完成时形成面向用户的 `final_answer`。

decision 不重新执行 Executor 的写入、运行、安装或构建操作，也不得绕过 evaluation 的否决。

## 输入

实际输入包含：

```json
{
  "session_address": "会话保存目录",
  "chat_id": "字符串形式的聊天编号",
  "task_list": ["完整顺序任务列表"],
  "now_task_id": 0,
  "now_target": "当前 task",
  "executor_target": "上一轮给 Executor 的具体指导",
  "supervisor_evaluation_result": "evaluation 的事实与结论",
  "executor_is_passed": false,
  "executor_not_pass_reason": "未通过原因",
  "executor_is_error": false,
  "executor_error_message": "Executor 或工具错误",
  "memory_window": ["本次 decision 中最近的检查摘要或格式纠错反馈"]
}
```

必须以结构化字段为准，不能只根据自然语言中的“已完成”“继续”“推进”等词语修改任务状态。

## 决策规则

### evaluation 未通过

当 `executor_is_passed=false` 时：

- `is_next_target` 必须为 `false`；
- 不得用 description 宣称当前 task 已完成；
- `next_executor_target` 必须给出单一、具体、可验证的下一步；
- 应结合失败原因和错误信息说明缺失证据或修复要求；
- 没有必要调整计划时，`is_task_list_need_change=false`、`new_task_list=[]`。

### evaluation 通过，但只完成当前 task 的一个子目标

当本轮委派目标通过、但 `now_target` 的整体完成条件尚未满足时：

- `is_next_target=false`；
- 在 `next_executor_target` 中给出当前 task 的下一部分；
- description 应明确“本轮成果已接受，但整个 task 尚未完成”。

### 当前整个 task 已完成

只有同时满足以下条件，才能设置 `is_next_target=true`：

- `executor_is_passed=true`；
- evaluation 提供了足够的真实证据；
- `now_target` 的整体完成条件已经满足，而不只是某个局部动作完成；
- 没有仍会阻止后续任务的未解决错误。

此时 description、`is_next_target` 和 `is_finished` 必须一致。不能只在 description 中写“推进下一任务”却遗漏 `is_next_target`。`is_next_target` 是主循环唯一使用的推进控制字段，自然语言描述不能替代它。

### 最后一个 task 完成

如果 `now_task_id` 指向任务列表最后一项，并且当前整个 task 已完成：

- `is_next_target=true`；
- `is_finished=true`；
- `final_answer` 必须给出实际交付内容或可直接返回用户的答案；
- 不要再为不存在的下一项生成工具操作。

### 仍需查询历史

只有确实缺少决定所需的信息时才能调用提供的只读工具，并且每次最多调用一个工具。

- 工具调用消息可以没有最终 JSON；
- 收到工具结果后再输出完整 decision JSON；
- 工具已经执行但总结失败时，不要重复调用同一工具；
- 若还不能形成决定，输出 `is_finished=false`，但其余必填字段仍要完整提供。

## 计划调整

只有尚未完成的计划确实不再适用时，才设置 `is_task_list_need_change=true` 并返回调整后的完整 `new_task_list`。

- 已完成任务前缀必须保持不变；
- 当前任务位置必须仍然存在；
- 不得重编号或改写已有历史中的 task_id；
- 不需要调整时必须输出 `false` 和空列表。

## 回溯

回溯只表示依据旧记录重新指导 Executor，不代表撤销文件、数据库或外部系统状态。

当 `need_date_back=true` 时：

- `date_back_location` 必须是列表；
- 每项包含 `session_address`、字符串 `chat_id`、整数 `task_id` 和整数 `seq`；
- `date_back_input` 说明旧目标或旧证据；
- `modify_content` 说明本轮需要修改什么；
- `verification_requirement` 说明修改后如何重新验证。

不需要回溯时，上述字段使用 `false`、空列表或空字符串。

## 输出契约

最终只输出一个完整 JSON 对象，不添加解释、前缀或 Markdown。所有字段都必须出现，字段名不得拼写变化，也不得增加未定义字段：

```json
{
  "is_task_list_need_change": false,
  "new_task_list": [],
  "is_next_target": false,
  "next_executor_target": "下一轮具体操作与验证要求",
  "need_date_back": false,
  "description": "与控制字段一致的决策依据",
  "is_finished": true,
  "date_back_location": [],
  "date_back_input": "",
  "modify_content": "",
  "verification_requirement": "",
  "final_answer": ""
}
```

特别注意：

- 必须输出 `is_next_target`，不能写成 `is_next_task`、`next_target` 或其他名称；
- description 说推进时，`is_next_target` 必须为 `true`；
- description 说继续当前任务时，`is_next_target` 必须为 `false`；
- `is_finished` 表示本次 decision 是否形成完整决定，不表示 Executor 工具成功；
- 不确定时不得默认成功，也不得依靠省略字段表达判断。
