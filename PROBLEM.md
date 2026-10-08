# Coding Agent 当前问题与下一步修复重点

更新时间：2026-10-07

## 2026-10-07：工具逐次 yes/no，Supervisor 历史免交互确认

旧实现仅按项目/阶段批准，未选项目时在工具调用中弹出“项目绝对路径”输入框，与用户只需选择 yes/no 的需求不符。
终端启用逐次 ToolPermission：Host 从工具明确绝对路径推导候选，显示实际参数与角色，默认 false，用户 yes 后才更新为 true，执行前校验并消费。参数缺失/非法反馈模型，不选择 cwd、不强迫终端填写路径。
Supervisor 历史查询按本轮用户补充免确认，继续 Host 会话绑定、角色和分支权限；Supervisor 普通文件工具仍逐次确认。
副本复制/检查、容器网络设置和固定包写回保留独立批准；普通工具批准只限本次工具/参数/环境。安装/构建工具确认包含本次必要网络，不重复旧确认。
缓存内容版本检查在批准后进行；成功工具只保存一次原始事实，格式重试只重新总结，不重执行。拒绝停止当前请求，不进入后续验收/决策推进。
tool_permissions.json 是本次请求的批准审计，不是可恢复授权；旧日志不迁移/回写，旧可信 API 保持兼容。
本轮尝试真实 Docker 测试时，引擎管道 dockerDesktopLinuxEngine 不存在，因环境不可用未完成；不自动启动引擎或下载镜像。下面 2026-10-06 的通过结果仅为历史记录。
最终离线回归 154 项：151 通过，3 项真实 Docker 测试默认跳过；新增逐次权限测试 24 项离线通过、1 项真实 Docker 尚未完成。

## 2026-10-06：运行时授权、副本和终端显示

旧终端把 workspace 直接作为真实项目容器挂载，启动依赖参数，缺少三阶段批准和固定版本应用。
改为默认无项目、独立稳定设置、Host Permission yes/no、真实项目只读、专用副本执行、固定 diff 包和冲突检查写回。
模型不能设置授权字段；拒绝停止当前请求，允许重新输入。历史/阶段输出统一可信淡色渲染，/steps 可浏览纯验收/决策边界。
模型完成且有可应用变更时自动弹出第三次 yes/no；副本结果与 Host 应用/拒绝事件分开保存。副本元数据不复制完整原始工具正文。
工具执行后副本元数据保存失败标为 unknown，停止并要求检查，不将其伪装成未执行的权限错误。
修复恢复 decision 被否决后 resume 标志不清的问题；读缓存区分源/副本/分支和内容版本。
未改 Supervisor 否决、task_id 验收推进、原始事实层或上下文压缩阈值。
新分支缺少文件快照时明确阻塞重新生成，不共享副本；纯权限/空目录变化、二进制/大文本暂不自动应用。
多文件应用不是绝对事务，备份/日志/恢复不能抵御任意外部并发写入。真实 Python/JDK 镜像、付费模型与工具链尚未验证。
最终临时项目完整回归 129 项全部通过，无失败、无跳过，含真实 nginx:alpine 副本隔离、Windows junction、最终 yes/no、冲突及失败恢复。

## 2026-10-06：跨 task 摘要白名单漂移与 decision 空转修复

复现会话：`D:\coding-agent-state\s_e8dc8e49f56741fd`。task 0 的 13 次工具调用均成功，
task 1 收到 TaskOutcome 和 1–13 的证据引用，并未丢失 task_id 或依赖。失败来自两处：
将摘要省略的目录事实误判为无证据；明确否决并给出下一步后，模型仍反复 is_finished=false，
Host 因而没有交回 Executor，最终 decision_budget_exhausted。

- evaluation、decision 和 Executor 提示词区分摘要、存在性元数据、源码行为、实测效果；
  不新增摘要白名单，推断不得在最终结论中改写成事实。有疑问由 Supervisor 按 locator 查旧事实。
- 完整无工具控制决策仅纠正阶段结束标志；所有否决、计划、正式摘要校验仍执行，
  不直接放行任务、不伪造 TaskOutcome。原始回复仍保存在原始模型摘录。
- 不完整 decision 默认最多 3 次契约失败；多工具回复最多连续 3 次独立协议重试。
  请求错误保留实际原因；综合模式未完成结果立即交回 Supervisor，不无工具空转。
- 独立副本完整离线回归 98 项：97 通过，1 项真实 Docker 集成默认跳过。
  对该会话 15 条原始 task 1 decision 回复逐条离线重放，每条均在一次请求后返回，
  保留否决、无工具重执行、无正式成果虚增。两 task 实际循环的模拟模型端到端完成。

未进行付费模型复跑；提示词对真实模型事实判断的改善不能视为已经验收。
历史 JSONL/checkpoint 不回写，不自动将旧 blocked 会话改成 completed。

## 2026-10-05：源码核查和本轮修复

先核对 CODEX.md 与真实代码，基线67项测试通过。确认本轮同名同参成功去重漏掉 Executor 当轮集合、旧固定窗口可能丢失摘要输入、旧 backend 是硬编码示例。CODEX 关于工具页大小/schema 与 stdio 确认存在过时描述，已按实际包装器修正。

新增显式异步高阈值压缩、整体字符预算和计费；typed checkpoint、单写者、统一 timeline、用户选择前置状态分支、连续问答CLI；Host路径/能力/单次授权和真实Docker后端。未改变角色分工、否决、任务验收推进或按需 task_summary 读取。原始工具结果仍只在 tool_results.jsonl，历史查询不递归复制原文；新增身份/分支索引不回写旧记录。

CLI恢复失败/未知操作不自动重放。成功工具但总结未完成通过locator恢复；Supervisor核查结果只保存查询动作/locator后重建。实际取消测试发现并修复 Docker 创建尚未完成时取消的时序问题：先 create 再将现存容器纳入清理，最后 start 执行代码。

离线87项中86通过、1项opt-in跳过；单独实际Docker的5项通过。工具链镜像构建因Docker Hub连接超时失败，真实Python/JDK/安装打包、付费模型和远程MCP未验收。跨文件非事务、并发路径race、SDK在途计费和长会话日志增长仍是限制。实际命令与边界见README.md；以下旧分析保留作背景，不表示全部仍存在。

## 2026-10-01：当前任务摘要实现

以下旧问题分析保留作复现背景；以当前源码为准。本次新增的任务摘要行为为：

- 每个 chat 在 session_address 下有独立的历史子目录 `chat_<编码后的 chat_id>_<摘要散列>`，其中只有一份 `task_summary.md`，所有 task 的累计记录追加到该文件。
- 原始工具事实仍在 `tool_results.jsonl`；角色 JSONL 保留摘要、状态和证据引用。Supervisor decision 另外结构化保存 `task_summary` 和最终通过时的 `task_outcome`，不复制完整工具输出。
- decision 默认接收本轮 Executor 结果、本轮验收和既有 TaskOutcome，只提示 task_summary.md 是否存在，不自动加载正文。模型对累计成果、历史依赖或推进条件没有特别准确的把握时，主动调用新增的 Supervisor-only `read_task_summary`。Host 绑定当前 session_address/chat_id，模型仅选择 task_id 和是否包含前序任务。返回各 task 的最新累计记录，排除未来任务，不把同一 task 旧轮次重复当作成果。
- 历史查询后的结果仅保留在本轮局部 `memory_query_results`，模型每次查询一个工具后再决定是否继续补取证据；相同查询避免重复。该工具复用现有历史查询的轻量事件保存，不将查询正文再次复制进 tool_results.jsonl 或角色 State。本轮有效证据优先，历史未被推翻的成果继续保留。
- decision 内生成和校验摘要，先更新 AgentState / SupervisorState；最终通过时更新 TaskOutcome。保存 decision JSONL 后，以对应 task_id / Supervisor seq 和已更新参数组装 Markdown，复用现有 `write_in` 保存。
- `write_in` 是原子覆盖写，不支持追加；因此先读取原内容、拼接完整新记录，再调用它覆盖写。按 task_id + Supervisor seq 标记避免重试重复保存；写入失败进入现有异常处理，不推进 task_id。
- description 的提示词要求少于 3000 字符；代码只检查字符串和非空，超过 3000 不报错、不重试，历史保留完整文本。兜底聊天摘要使用最近两条完整 Supervisor description，通常约在 6000 字符内，也不会因长度报错。
- TaskOutcome 的 accepted_summary / verification_summary 由最终 decision 结合多轮仍有效成果生成，分别回答累计完成成果和实际验收依据。默认上限沿用 3500 / 1500 字符，通过 `CODING_AGENT_ACCEPTED_SUMMARY_MAX_CHARS` / `CODING_AGENT_VERIFICATION_SUMMARY_MAX_CHARS` 配置；超限反馈模型重写，没有硬截断。
- 旧 JSONL 不回写；缺少新字段的历史保持可读，不自动将旧记录推断成正式 TaskOutcome。已有会话的 Supervisor seq 从该 chat 的 JSONL 最大 seq 继续。

验证覆盖摘要追加、chat 隔离、多轮累计、旧结论修正、否决权、长度行为、状态先更新后保存、重试去重和写入失败阻止推进。使用模拟模型与工具的离线测试；真实模型生成质量和同一 chat 多进程同时执行未验证。

## 1. 当前最大问题：跨轮记忆不是“可复用证据”

当前系统已经会把 Executor、Supervisor 和最终聊天写入 JSONL，也提供历史查询工具。但上次真实运行证明：保存记录不等于下一轮 Agent 能稳定使用记录。

复现会话：

```text
D:\coding-agent\192744494437_20260927_192047
```

任务是读取并分析一个 Spring Boot 项目。该会话最终一直停留在 `task_id=0`，并在同一 task 三次尝试后阻塞。

### 1.1 复现事实

- Executor 三次成功调用同一个 `read_all_files_tool`。
- 三次工具事件都返回成功，Executor 最终 JSON 也没有字段缺失。
- Supervisor decision 每次都明确保存 `is_next_target=false`，不是字段漏解析导致未推进。
- `executor_history.jsonl` 有 9 条记录：3 个 tool_event、3 个 executor_turn、3 个 supervisor_review，文件约 1.15 MB。
- `supervisor_history.jsonl` 有 13 条记录，文件约 2.89 MB。
- Supervisor 可以查到“工具成功”，但关键证据在工具结果裁剪、description 压缩和重复历史中不稳定可见。
- evaluation/decision 逐轮要求更多原文摘录，验收标准从“完成项目材料读取”漂移成“必须展示指定类别文件的原文片段”。

当时的递归大结果问题已通过新的文件工具拆分缓解：`read_all_files_tool` 现在只看单层结构，`read_files_content_tool` 定向分页读取正文，POM 由专用工具完整解析。但这只能减少新结果大小，不能修复跨轮记忆机制本身。

## 2. 当前代码中的直接根因

### 2.1 Executor 重新进入时清空临时记忆

`_reset_executor_turn()` 每次都会清空：

- `memory_window`
- `tool_result`
- `tool_arguments`
- `description`
- `input_content/output_content`

因此同一 task 的第二次尝试虽然知道 Supervisor 新指导，却拿不到第一次尝试的结构化证据。Executor 没有历史查询权限，只能相信简短指导、重新调用工具或重新猜测。

### 2.2 没有 task 级 evidence_context

当前跨角色传递主要依赖：

- Executor 的最多 6000 字符 `output_content`；
- Supervisor 主动调用历史工具；
- Supervisor 再用自然语言生成下一轮 `executor_plan`。

系统没有一个稳定字段保存“本 task 已获得哪些仍有效证据”。路径、工具名、seq、关键发现、分页位置、是否完整和验收状态没有作为统一对象传递。

### 2.3 完整工具结果被重复写入

Executor 路径：

1. `tool_event` 保存完整 `tool_result`；
2. 工具后生成总结；
3. 保存 `executor_turn` 前没有清掉 `tool_result`；
4. 同一结果再次写入 turn。

Supervisor 路径：

1. 历史查询的 tool event 保存完整查询结果；
2. `_save_supervisor_turn()` 保存 evaluation/decision 时没有清掉 `tool_result`；
3. 同一查询结果可能继续进入多个后续 turn。

这就是 Supervisor 历史比 Executor 历史更快膨胀的直接原因。历史越大，精确查询再次进入模型上下文时越容易被 12000 字符 observation 上限裁剪。

### 2.4 原始完成条件没有独立锁定

初始 guidance 会生成 `completion_criteria`，但它主要被拼接进 `executor_plan`，同时写入可被后续决策覆盖的 `verification_requirement`。当前没有不可漂移的 task-level 字段区分：

- 原始验收条件；
- 本轮补充操作；
- 已满足条件；
- 尚未满足条件。

当 decision 重写下一轮指导后，evaluation 容易按新措辞提高标准，而不是始终对照 task 创建时的完成条件。

### 2.5 摘要、事实和控制状态混在一起

description 同时承担人类说明、模型短期记忆、历史语义检索和验收依据。自然语言摘要适合检索，不适合作为唯一事实存储。`is_finished`、`is_passed`、`is_next_target` 已经分离，但证据本身尚未结构化分离。

## 3. 修复目标

记忆层应形成三层，而不是在每轮重复塞入全部历史：

```text
原始事实层：tool_event，只保存一次完整工具结果
    ↓ 引用
task 证据层：有界 evidence_context，保存可验收事实和 locator
    ↓ 使用
对话层：description，只负责说明和语义检索
```

原始事实层采用独立的 `tool_results.jsonl`：每次普通工具调用只在该文件
保存一次完整参数和规范化结果，并由独立 `tool_result_seq` 标识。
`executor_history.jsonl` 与 `supervisor_history.jsonl` 只保存
`ToolResultLocator`、`ToolSummary`、状态和控制字段。历史查询工具返回的是
既有历史视图，其返回正文不得再次写入 `tool_results.jsonl`，避免查询历史时
递归复制旧事实。旧会话中内联的 `tool_result` 仍保持只读兼容；新记录不再
写入旧内联格式。

### 3.1 建议新增 TaskEvidence

建议在 AgentState 中增加按 task_id 管理的证据状态，示意结构：

```json
{
  "task_id": 0,
  "target": "当前任务",
  "completion_criteria": ["创建 task 时锁定的条件"],
  "evidence": [
    {
      "evidence_id": "task-0-executor-2",
      "tool_name": "read_all_files_tool",
      "tool_ok": true,
      "locator": {
        "session_address": "...",
        "chat_id": "...",
        "task_id": 0,
        "seq": 2
      },
      "facts": ["发现 pom.xml", "发现 src 目录"],
      "paths": ["D:\\project\\pom.xml"],
      "completeness": "complete",
      "continuation": null,
      "accepted": true
    }
  ],
  "satisfied_criteria": [],
  "missing_criteria": []
}
```

约束：

- `facts` 必须来自真实工具结果，不能由模型凭空补充；
- 单条证据保持有界，不复制完整正文；
- locator 指向唯一原始 tool_event；
- 文件分页读取必须保存 `next_start_char/next_start_index`；
- Supervisor review 更新 accepted 和条件满足状态，不改写原始事实。

## 4. 建议实施顺序

### P0：消除历史重复

1. 完整 `tool_result` 只允许出现在对应的 tool_event。
2. 保存 Executor turn 前清除工具结果，改存 `tool_event_seq` 或 `evidence_ids`。
3. 保存 Supervisor evaluation/decision turn 前清除工具查询结果，改存 `tool_event_seq`。
4. review 继续只保存被审查 seq、结论和原因。

这是最小、确定性最高的第一步，不需要先改变模型提示词。

### P0：锁定 task 完成条件

1. planning/initial guidance 后把 `completion_criteria` 独立保存到 task 状态。
2. evaluation 每轮都接收同一份原始条件。
3. decision 可以提出下一轮操作，但不能静默改写原始条件。
4. 如果用户需求变化，需要显式生成条件变更记录，而不是用 description 覆盖。

### P0：生成并传递有界 evidence_context

1. 工具总结成功后，从规范化结果生成 TaskEvidence。
2. Supervisor evaluation 直接接收当前 task 的证据摘要和 locators。
3. 下一次 Executor 进入同一 task 时接收已接受证据、未满足条件和精确续读位置。
4. 只有需要核对原文时才调用 `read_task_error`，不得默认加载整条巨大记录。

### P1：重复调用保护

建立工具调用指纹，例如：

```text
task_id + tool_name + 规范化 arguments
```

若同一指纹已有成功且仍有效的证据：

- 默认复用 locator 和 evidence；
- 只有路径内容已变、上次读取不完整、分页尚未结束或 Supervisor 明确说明新增证据时才允许重调；
- 记录重调原因。

### P1：历史读取瘦身

- `read_now_task` 默认返回 evidence 摘要和 locator；
- `read_task_error` 支持选择性返回字段，而不是总返回完整 tool_result；
- Supervisor 历史查询默认不返回重复工具正文；
- description 继续用于语义选择，但不能代替事实字段。

## 5. 必须增加的回归测试

1. 同一 task 第二次进入时仍能拿到第一次已保存的 evidence，不重复工具。
2. `tool_result` 只存在于 tool_event，Executor/Supervisor turn 不复制。
3. 初始完成条件在多轮 decision 后保持不变。
4. evaluation 只能对照锁定条件，不得临时增加条件阻止推进。
5. 同一工具与参数已有成功证据时，默认复用；参数或分页位置改变时允许调用。
6. evidence locator 能精确回查原始记录。
7. 大型工具结果不会让 JSONL 因相同内容在多个 turn 中成倍增长。
8. 下一轮 Executor 能根据 `next_start_char/next_start_index` 继续，而不是从头读取。

## 6. 完成验收标准

- 上述复现任务不会因为同一读取动作重复三次而达到 task 尝试上限。
- 同一 task 跨 Executor 尝试保留已接受证据和未满足条件。
- Supervisor evaluation 始终能看到原始完成条件、当前 evidence 和缺口。
- JSONL 中每个完整工具结果只保存一次，其他记录只保存引用。
- 模型上下文即使裁剪，也不会丢失工具成功状态、关键事实、locator 和续读位置。
- task 满足锁定条件时，decision 能稳定输出 `is_next_target=true`。

## 7. 已完成但不要误认为解决了记忆问题的工作

- Executor/Supervisor JSON 字段使用 Pydantic 严格校验；
- 缺少 `is_finished`、拼错 `is_next_target`、额外字段都会失败并反馈重试；
- 工具后总结失败不会重复执行工具；
- 文件读取已拆为单层结构浏览与定向分页内容读取；
- 大型 POM 由专用 Spring 判断工具完整解析；
- Executor、Supervisor 保存字段已增加模型阶段、原始响应摘要和校验错误。

这些改动提高了单轮可靠性和可诊断性，但尚未建立 task 级可复用证据记忆。

## 8. 独立安全问题

`INFORMATION.json` 当前包含明文 API 凭据。它不是本次记忆阻塞的根因，但属于高优先级安全风险。不得打印或提交这些值；应迁移到环境变量，并轮换已经暴露或共享过的密钥。
