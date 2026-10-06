## coding harness

## Above all , I will introduce the modules I prepare to use
·1.Agent loop 

    As for the core of the agent, it gives agent a basic way to think,act,observation. And the agent need to follow the program

·2.MCP tools

    The tools agent may use in the workflow need to be packaged by mcp_server, it can give tools a standard access port.LLM can load all the tools' info 
    to choose what to use

·3.Context

    it contains:
                (1) get infos from github,Internet .....
                (2) get the history chat in the same session
    
    then clear some unuseful contents to decrease the context window,with the purpose of easing the illusory.
    After that , combined with the nearest unachieved goals, giving it to the llm

·4.Skills

    it has the combination of different kind of tools,to give model a ability to deal with one pointed assignment
    Different skills can enhance the model to deal with different assignments with less thinking-token & thinking time
    it consists of :
                (1) description of the skill
                (2) scripts(run the skill)

·5.memory

    it makes LLM to storage the history chat,containing:
                (1) state.jsonl : the thinking process and results and primary state (easy to date back to the primary state if some problems in the process) and solved plans and unsolved plans ...
                (2) history.jsonl:the history chat,and answer
        
·6.check 

    Detect execution errors and record them in state.jsonl. Use task state and LLM feedback to decide whether to retry, repair, replan, or pause.

·7.test

    it contains many  terminal command , to check the coding wether has problems, and feeding it back to the LLM , to retry

===================================================================================================================================================

·8.prompt

    different stage may have different prompt,it contains:
                (1) plan prompt : let the llm to decide plans
                (2) execute prompt : restrict the llm to run the tools strictly
                (3) check prompt :analyse the reason why the tools excute or get llm answer ... have problems
                (4) test prompt : combine the feedback to make llm decide what to do in the next step


## 2026-10-05：当前终端、恢复与执行权限

旧介绍保留作背景；下面与当前源码一致。Supervisor / Executor 分工、阶段、否决权、task_id 验收推进、原始工具事实层和按需任务摘要读取不变。INFORMATION.json 使用现有配置，不要把密钥粘贴到终端历史；本次没有改动它。

### 启动

先确认解释器。用户代码的工作区与 agent 项目/配置/状态目录分开，以下工作区需要事先存在：

~~~powershell
& "D:\anacode\python.exe" -c "import sys; print(sys.executable)"
& "D:\anacode\python.exe" "D:\pycharmcode\coding_agent\backend.py" --help
& "D:\anacode\python.exe" "D:\pycharmcode\coding_agent\backend.py" --workspace "D:\my project" --state-dir "D:\coding-agent-state" --sandbox workspace-write
~~~

启动时选 n 新建，或 r 后选择确切 SESSION [CHAT] [BRANCH]。不再执行硬编码示例请求。模型 SDK/MCP 依赖沿用原环境；--help 和本地历史查看不需要模型密钥。非交互执行使用 --query "你的要求"，不允许读取 stdin 获取授权。已有 checkpoint 可用 --resume s_实际ID --chat main --branch main；恢复本身不执行任何模型或业务工具。

### 实际命令与最小端到端示例

下面的 ID 必须从 /sessions、/tools、/show 返回值选取，不能把示例占位值当作真实身份：

~~~text
/new
请在当前工作区编写一个小型 Python 示例，并说明实际验证结果。
请依据上一问成果补充输入校验，并验证。
/history --page 1
/history --search "验证"
/tasks
/tools --actor executor
/tools --actor supervisor
/show 选中的event_seq --page 1
/exit
~~~

重新运行同一启动命令，然后：

~~~text
/sessions
/resume 选中的session_ID main main
/history
/tools --phase evaluation
/show 选中的evaluation事件编号
/rewind 选中的evaluation事件编号
/continue
/branches
/history --branch main
/history --branch 新生成的b_分支ID
/sandbox
~~~

evaluation/decision 回退保留此前 Executor 结果；工具总结边界 phase=executor_tool_summary 仅重做总结、不重执行工具。可通过 /show 确认事件 phase，再 /rewind 对应事件。/rewind task 0 从任务开始边界建立新分支，保留更早成果。工具调用事件回退到其行动选择之前，由模型重新选择，不自动照抄旧命令。所有回退都不恢复磁盘文件、不运行 Git reset；再次执行可能改变当前真实文件，仍受当前权限。

如需要确认一次安装，可输入“使用安装工具为当前工作区安装 requests”；模型是否调用由其选择。终端展示实际规范参数、工作区、网络、目标和操作绑定。输入 y 只允许这一项；N/EOF/非交互模式拒绝且不执行。安装包目标为工作区 .agent_packages；容器 Python 通过 PYTHONPATH 引用，不安装到 Host Python。打包工具同样需要单次确认。--query 遇确认只报告受阻，不等待 stdin。

其他支持：/history --request ID --search TEXT --page N --session SESSION --branch BRANCH，/tools --task N --phase NAME --status STATUS，/show EVENT --page N，/multiline 后逐行输入、单独 /end 结束，/sandbox read-only 或 workspace-write 或 danger-full-access。查看其他会话不会切换活跃目标。空行不请求模型；EOF 退出；执行中 Ctrl+C 保存 interrupted 并清理自己拥有的容器/进程，不声称文件未变化。

### 上下文配置与去重

- --context-trigger-chars 默认60000：严格大于才 summarize，不提前删除 query、工具正文中间或第10/第6条历史。
- --summary-max-chars 默认6000：压缩输出目标。超大原材料分块送入模型，全部字符保留进入摘要输入。
- --context-max-chars 默认80000：整个 messages + tools JSON 字符上限，也检查压缩请求。字符预算不等于 token 上限。
- summarize 是显式异步入口，两次有限重试，缓存按内容/目标/分支/参数区分。失败返回可追溯 fallback 标记；truncate 单独显式使用、不调用模型。
- 模型压缩、重试、最终汇总都计入原有80次请求限制及 provider usage tokens；没有 usage 的不估算。datetime.now() 统计每次请求耗时并保存 chat_history。
- 普通工具成功同名同参本轮立即去重；不同参数、失败重试、Supervisor 明确重跑保持可用。synthesize 仍禁工具；成功后总结失败只重总结。
- description 仍是提示词要求少于3000字符，不因超长单独报错/截断；正式 TaskOutcome 的3500/1500可配置限制仍要求模型重写。

### 会话、检查点和分支

~~~text
STATE_DIR/s_会话ID/
  executor_history.jsonl / supervisor_history.jsonl / chat_history.jsonl
  tool_results.jsonl                  # 完整原始工具结果唯一事实来源
  timeline.jsonl                     # 统一 event_seq，引用原日志，不复制工具正文
  .writer.lock
  chats/CHAT/
    branches.json
    checkpoints/main.json / b_*.json
    snapshots/*.json                 # 可回退阶段的前置状态
  chat_<既有编码与哈希>/task_summary.md
~~~

同 session 所有 chat 共用原始编号空间；角色 seq 按 chat 全部历史继续。每个新用户请求有独立 request_id、模型预算/token计数，task_id 接续旧任务列表，不覆盖旧 outcomes。chat、branch 和 request 身份可追溯，统一 event_seq 不等同于任何角色 seq。

恢复 typed dataclass，重建 int 键/set/handoff/TaskOutcome，SDK/API 对象不持久化。checkpoint 是临时文件+fsync+原子替换，复用 write_in._atomic_write；task_summary 继续复用 write_in 覆盖写前拼接旧内容。CLI 持有 session 单写者锁，禁止两个终端交错推进，同步检查 stale writer。

工具已落盘但 checkpoint 滞后：从原始事实补齐编号、成功集合和 pending summary，仅重建 observation。未落盘的 tool_intent 或 unknown/interrupted 不自动重放，先查看真实效果，再选安全回退边界。Supervisor 核查结果通过 locator 或已有历史查询视图重建，返回正文不再复制落盘。

分支 parent/cutoff 限定有效前缀；memory、tool_result、task_summary、依赖 TaskOutcome 和缓存遵守范围。旧分支保留可查看。旧无 branch 记录按 main 处理，不回写/删原日志；缺少可靠 checkpoint/snapshot 的旧会话只可看历史，精确 resume/rewind 明确拒绝，不伪造重建。

直接使用旧 main(query, session, chat) 仍兼容，但 CLI 单写者/精确 checkpoint 需传入运行上下文；不要将无上下文的并发调用当成安全持续会话。

### Docker 后端与权限边界

Windows 使用 Docker Desktop 的 Linux 容器引擎。后端参数参考 [Docker 官方说明](https://docs.docker.com/engine/containers/run/)。需要用户自己启用 Docker 并准备可信工具链镜像；程序不自动安装 Docker、不自动 pull、不自动退回 Host 运行。

构建已提供 Dockerfile 的命令（涉及下载，用户确认网络后自行执行）：

~~~powershell
docker build -t coding-agent-sandbox:local -f "D:\pycharmcode\coding_agent\sandbox\Dockerfile" "D:\pycharmcode\coding_agent\sandbox"
~~~

该镜像包含 Python、OpenJDK17、Maven、Gradle。--image 可选可信本地镜像；--java/--javac 指定容器内路径，不是 Windows 宿主路径。本次构建因 Docker Hub 网络失败，尚未验证专用镜像；镜像缺失时运行工具明确受阻。可以继续看文件/历史，不自动降级。不要仅为了绕过镜像缺失就启用 full-access。

- 默认 workspace-write；read-only 禁止用户工作区写入，Host 仍可写独立状态目录。
- --readable-root / --writable-root 是可信文件工具额外根；不能填盘符根或整个用户家目录。不自动挂进容器。
- Host 路径规范化，拒绝 ..、根外、链接逃逸以及配置/状态访问；目录遍历逐项检查。静态检查不是抵御恶意并发链接替换的完整 OS 保证。
- 所有 Python/Java/pip/Maven/Gradle/启动进程经过 SandboxRunner。受限容器只挂选定工作区，不挂 Host 家目录、配置、状态、Docker socket，不继承模型 API key；如选定工作区包含 protected 配置/状态，明确拒绝进程运行。
- 容器采用只读 rootfs、普通用户、去 capabilities、no-new-privileges、资源限制和专用 /tmp。默认网络关闭；--network 用户显式全局开网络，或某次依赖安装批准只对该操作开启；仅支持 on/off，不宣称域名级过滤。
- danger-full-access 仅用户明确设置，显示无进程隔离且网络不限制。当前策略由 CLI 决定，恢复旧日志不恢复旧批准或旧权限。
- 外部 MCP 不自动受容器约束，未知 capability 默认拒绝。可信 INFORMATION.json 的可选 tool_capabilities 映射配置明确工具名及能力列表（read/write/execute/network/external），配置写入/执行/外部能力必须由可信用户审核；本次不改你的配置文件。
- 安装/构建确认由 terminal Host 完成。模型的 confirmed/approved/trusted_roots 等字段不能批准操作；授权绑定不可变参数、session/chat/branch 和策略版本。参数或分支变化无效。
- 超时/取消记 unknown/interrupted，不推断零副作用、不自动重试。限制输出缓冲/超时并清理自有进程树/容器。长驻 Spring 启动超时不作为“验证成功”。

### 验证记录和已知限制

~~~powershell
Set-Location "D:\pycharmcode\coding_agent"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONIOENCODING = "utf-8"
& "D:\anacode\python.exe" -m unittest discover -s tests -v
~~~

离线87项：86通过、1项真实Docker需 opt-in 跳过。原有67项通过。包含两轮模拟问答、重启恢复、evaluation/decision与工具总结重新生成、checkpoint滞后、分支未来事实隔离、单写者、完整压缩与计费预算、失败和授权路径。

真实 Docker 测试使用本机已有 nginx:alpine（不代表Python/JDK镜像），在临时工作区运行5项全部通过：

~~~powershell
$env:CODING_AGENT_DOCKER_INTEGRATION = "1"
$env:CODING_AGENT_DOCKER_TEST_IMAGE = "nginx:alpine"
& "D:\anacode\python.exe" -m unittest discover -s tests -p test_sandbox.py -v
Remove-Item Env:\CODING_AGENT_DOCKER_INTEGRATION
Remove-Item Env:\CODING_AGENT_DOCKER_TEST_IMAGE
~~~

实际覆盖容器内允许写、未挂载Host文件不可读写、只读写失败、网络关闭外连失败、伪测试API key不继承、取消后无残留自有容器。未运行真实付费模型、专用Python/JDK/实际安装打包、远程MCP或Windows junction专项验证；不得把mock通过当成这些验收通过。

持久化是多个独立文件，不是跨文件原子事务/绝对 exactly-once。SDK在途请求取消不保证服务端零计费；输出上限会截断进程stdout/stderr并保留状态，原始工具记录保存的是该规范化输出。长会话扫描/snapshot缓存增长无自动清理。真实模型摘要质量、OS并发路径race和额外ACL仍需部署验收。

最终原项目验收开启实际 Docker 后端：87项全部通过，--help正常；git diff --check 无空白错误。
