"""Terminal owns stdin, session selection and authorization; model owns next action."""
from __future__ import annotations
import argparse
import asyncio
import json
import shlex
import sys
import uuid
from pathlib import Path
from MCP_functions.sandbox import SandboxPolicy, SandboxRunner, safe_display
from State.session_checkpoint import SessionStore, ActiveRun, ACTIVE_RUN, rows, single_writer
from State.terminal_settings import TerminalSettings, validate_state_dir
from State.project_workspace import ProjectWorkspace, DEFAULT_EXCLUDES
from terminal_render import Renderer, RENDERER, show_description
from terminal_select import choose_yes_no, choose_option
from State.history_browser import list_chats, conversation_history, format_conversation, branch_choices, description_history

HELP = """普通文本：新请求，完成后可继续问答。空行不执行。
/help  /new  /sessions  /branches  /exit
/session [SESSION]               选择 session，自动打开旧 chat、显示历史和任务；多分支再选择
/descriptions [--actor ROLE] [--page N] [--session ID] [--chat ID] [--branch ID]
/chats SESSION                   查看旧 session 的 chat 和分支，不切换会话
/open SESSION CHAT [BRANCH]       打开旧 chat 后直接续聊；/continue 仅继续旧任务
/conversation [--session ID] [--chat ID] [--branch ID] [--page N] [--search TEXT] [--request ID]
/resume SESSION [CHAT] [BRANCH]   仅恢复状态
/continue                        用户确认继续恢复的请求
/history [--page N] [--search TEXT] [--request ID] [--branch ID] [--session ID] [--chat ID]
/tasks                           task 目标、成果和当前尝试
/tools [--actor ROLE] [--task N] [--phase NAME] [--status STATUS]
/steps [--phase NAME]             所有可恢复边界（含纯 evaluation/decision）
/show EVENT_SEQ [--page N]        展开参数/原文，每页 8000 字符
/rewind EVENT_SEQ                从事件前置状态建立新分支
/rewind task TASK_ID              从任务开始建立分支
/settings [state-dir <绝对路径>]  查看设置/修改后续新会话的存储目录
/settings exclude <glob>          添加副本排除规则；/settings exclude-reset 重置
/settings image <本地镜像>        配置后续请求；不自动拉取镜像
/settings network on|off          设置候选；on 执行前仍单独确认
/project [<绝对路径>]             查看/选择项目候选；选择不是授权
/permissions                     查看当前请求的 Host 授权与副本状态
/diff                            固定当前副本变更版本并查看差异和测试证据
/apply CHANGE_SET_ID              审阅后单独批准该固定版本写回
/export CHANGE_SET_ID             导出该版本 unified diff 到会话包目录
/apply-status                     查看应用事务与当前文件事实，不自动恢复/重放
/multiline                       多行输入，单独一行 /end 结束
打开后直接输入问题续聊；/continue 继续旧任务，/rewind 主动回退。均不回滚文件。EOF 退出。
"""

def parser():
    p = argparse.ArgumentParser(description="Supervisor–Executor terminal: run python -m backend without required options; project permissions are chosen at runtime.")
    p.add_argument("--workspace", help="compatibility: project candidate only, never an authorization")
    p.add_argument("--state-dir", help="Host state override; default from user settings (Windows D:\\coding-agent-state)")
    p.add_argument("--settings-file", help=argparse.SUPPRESS)
    p.add_argument("--config", default=str(Path(__file__).with_name("INFORMATION.json")))
    p.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], help=argparse.SUPPRESS)
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--image", help="trusted local sandbox image override; normally configured inside /settings")
    p.add_argument("--java", default="/usr/lib/jvm/java-17-openjdk-amd64/bin/java")
    p.add_argument("--javac", default="/usr/lib/jvm/java-17-openjdk-amd64/bin/javac")
    p.add_argument("--network", action="store_true")
    p.add_argument("--readable-root", action="append", default=[], help=argparse.SUPPRESS)
    p.add_argument("--writable-root", action="append", default=[], help=argparse.SUPPRESS)
    p.add_argument("--context-trigger-chars", type=int, default=60000)
    p.add_argument("--summary-max-chars", type=int, default=6000)
    p.add_argument("--context-max-chars", type=int, default=80000)
    p.add_argument("--resume", help="exact session identifier")
    p.add_argument("--chat", default="main")
    p.add_argument("--branch", default="main")
    p.add_argument("--query", help="non-interactive query; authorization never reads stdin")
    return p

def split_command(text):
    return [s[1:-1] if len(s) > 1 and s[0] == s[-1] and s[0] in "\"'" else s
            for s in shlex.split(text, posix=False)]

def filters(parts):
    positional, options = [], {}
    while parts:
        value, *parts = parts
        if value.startswith("--"):
            if not parts:
                raise ValueError("missing value: " + value)
            options[value[2:]], *parts = parts
        else:
            positional.append(value)
    return positional, options

class Terminal:
    def __init__(self, args, input_function=input, output=print):
        self.args, self.input, self.output = args, input_function, output
        self.renderer = Renderer(output, args.color)
        self.settings = TerminalSettings(args.settings_file)
        self.state_dir = self._initialize_storage(args.state_dir or self.settings.state_dir())
        self.next_state_dir = self.state_dir
        self.project_root = str(Path(args.workspace).resolve()) if args.workspace else None
        self.policy, self.workspace = None, None
        self.image = args.image or self.settings.values.get("image", "coding-agent-sandbox:local")
        self.network = args.network or self.settings.values.get("network", False)
        self.toolchains = {"python": "/usr/local/bin/python", "java": args.java,
            "javac": args.javac, "mvn": "/usr/bin/mvn", "gradle": "/usr/bin/gradle", "sh": "/bin/sh"}
        if args.sandbox == "danger-full-access" or args.readable_root or args.writable_root:
            raise ValueError("终端副本流程不接受启动参数扩权；请在运行时授权，不能绕过副本写入真实项目")
        if min(args.context_trigger_chars, args.summary_max_chars, args.context_max_chars) < 1:
            raise ValueError("context limits must be positive")
        self.store, self.run, self.status, self.unknown = None, None, "new", []

    def _initialize_storage(self, value):
        initial_value = value
        while True:
            try:
                return validate_state_dir(value)
            except (ValueError, OSError) as exc:
                self.say("状态目录不可用：" + str(exc), "error")
                if self.args.query is not None or not sys.stdin.isatty():
                    raise ValueError("请通过交互设置可写绝对目录；未静默切换存储位置") from exc
                try:
                    value = self.input("输入新的状态存储绝对路径（空行取消启动）：").strip().strip('"')
                except (EOFError, KeyboardInterrupt):
                    raise ValueError("状态目录设置已取消") from exc
                if not value:
                    raise ValueError("状态目录设置已取消") from exc
                # Persist a successfully selected replacement in stable settings.
                try:
                    return self.settings.set_state_dir(value)
                except (ValueError, OSError) as error:
                    self.say("存储设置未保存：" + str(error), "error")
                    value = initial_value

    def say(self, value, style="normal"):
        self.renderer.say(value, style)

    @staticmethod
    def event_style(row):
        status = str(row.get("status", ""))
        if row.get("kind") in {"authorization", "authorization_waiting", "interrupted"}:
            return "warning"
        if row.get("ok") is False or status in {"error", "failed", "unknown", "interrupted", "denied", "authorization_denied", "permission_denied", "blocked"}:
            return "error"
        if row.get("kind") in {"user", "answer"}:
            return "normal"
        actor = row.get("actor")
        return actor if actor in {"executor", "supervisor"} else "dim"

    def event(self, row):
        if row["kind"] in {"boundary", "tool_intent", "tool_completed", "authorization", "authorization_waiting", "interrupted"} or str(row.get("record_type", "")).endswith("_tool_event"):
            self.say(f"[{row['event_seq']}] {row['actor']} task={row.get('task_id')} phase={row['phase']} "
                     f"{row['kind']} {row.get('tool_name', '')} {row.get('status', '')}", self.event_style(row))

    def new(self):
        self.state_dir = self.next_state_dir
        self.policy, self.workspace = None, None
        self.store = SessionStore(self.state_dir / ("s_" + uuid.uuid4().hex[:16]), self.args.chat)
        self.store.initialize()
        self.run = ActiveRun(self.store, callback=self.event)
        self.status, self.unknown = "new", []
        self.say(f"新建 {self.store.path.name}/{self.store.chat_id}/main")

    def resolve(self, session):
        if not session or Path(session).name != session or session in {".", ".."}:
            raise ValueError("use an exact session identifier, not a path")
        target = (self.state_dir / session).resolve()
        if not target.is_relative_to(self.state_dir) or not target.is_dir():
            raise ValueError("session not found")
        return target

    def resume(self, session, chat="main", branch="main"):
        store = SessionStore(self.resolve(session), chat)
        if not (store.directory / "checkpoints" / f"{branch}.json").is_file():
            store.open_history(branch)
        with single_writer(store.path):
            saved = store.load(branch)
        run = ActiveRun(store, branch, self.event)
        run.bundle, run.phase, run.payload = saved["bundle"], saved["phase"], saved["payload"]
        run.resume = saved["status"] not in {"completed", "denied", "ready"}
        self.store, self.run, self.status = store, run, saved["status"]
        self.unknown = saved.get("unknown_operations", [])
        self.project_root = run.bundle["agent"].project_root or self.project_root
        self.policy, self.workspace = None, None
        self.say(f"已恢复 {session}/{chat}/{branch} phase={run.phase} status={self.status}；未执行工具。")
        self.say("可直接输入新问题承接历史；/continue 仅继续原中断任务，/rewind 才回退。", "dim")
        if saved.get("payload", {}).get("recovery_mode") == "history":
            self.say("历史上下文已加载；没有原执行快照，不恢复旧工具或文件。", "dim")
        if self.unknown:
            self.say("未知操作需人工核对，不自动重放：" + str(self.unknown))

    def select_session(self, session=None):
        choices = branch_choices(self.state_dir, self.resolve(session) if session else None)
        if not choices:
            self.say("暂无可打开的历史记录；空 session 还没有问答或任务。", "dim")
            return
        if session is None:
            sessions = sorted({c["session"] for c in choices})
            labels = []
            for identifier in sessions:
                available = [c for c in choices if c["session"] == identifier]
                latest = max(available, key=lambda c: c["updated_at"])
                labels.append(f"{identifier}  {latest['status']}  {latest['updated_at']}")
            self.say("选择 session：", "dim")
            index = choose_option(self.renderer, labels, self.input)
            if index is None:
                self.say("已取消，当前会话未切换。", "dim")
                return
            session = sessions[index]
        candidates = [c for c in choices if c["session"] == session]
        resumable = [c for c in candidates if c["resumable"]]
        latest = max(resumable or candidates, key=lambda c: (c["updated_at"], c["chat"] == "main"))
        candidates = [c for c in candidates if c["chat"] == latest["chat"]]
        selected = latest
        if len(candidates) > 1:
            self.say("此 session 有多个分支，选择要打开的分支：", "dim")
            index = choose_option(self.renderer, [f"{c['branch']}  {c['status']}  {c['updated_at']}" for c in candidates], self.input)
            if index is None:
                self.say("已取消，当前会话未切换。", "dim")
                return
            selected = candidates[index]
        self.command(f"/open {session} {selected['chat']} {selected['branch']}")
        store = self.store
        self.say(f"自动打开：{session}，chat={selected['chat']}，branch={selected['branch']}", "dim")
        body = format_conversation(conversation_history(store, selected["branch"]))
        if not body and not self.run.bundle["agent"].task_list:
            self.say("此 session 尚无历史，可直接开始聊天；没有过去的 task/event 可回退。", "dim")
        page = max(1, (len(body) + 7999) // 8000)
        self.command(f"/conversation --session {session} --chat {selected['chat']} --branch {selected['branch']} --page {page}")
        self.say("以上为对话最后一页；用 /conversation --page N 查看更早内容。", "dim")
        self.command("/tasks")
        history = description_history(store, selected["branch"])
        if history:
            self.command(f"/descriptions --session {session} --chat {selected['chat']} --branch {selected['branch']} --page {max(1, (len(history) + 19) // 20)}")

    def approval(self, request):
        if self.args.query is not None or not sys.stdin.isatty():
            return False
        action = request.get("action")
        if action == "tool_call":
            operation = request["operation"]
            label = {"read": "访问/查看", "write": "写入/修改副本", "execute": "在 Docker 副本中执行",
                     "information": "调用信息工具"}[operation]
            arguments = dict(request["arguments"])
            # Show text length/hash, not pages of source code or terminal control bytes.
            import hashlib
            for key in ("code", "content", "old_text", "new_text"):
                if isinstance(arguments.get(key), str):
                    value = arguments[key]
                    arguments[key] = {"characters": len(value), "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
            execution = request["actual_execution"]
            question = (f"{request['actor']} 请求{label}：{request['tool_name']}\n"
                        + "本次实际参数：" + json.dumps(arguments, ensure_ascii=False, indent=2))
            if execution.get("backend") == "docker":
                question += "\n执行环境：Docker；仅挂载已批准的副本，不写真实项目。"
                question += "\n镜像：" + execution["policy"]["image"]
                question += "\n网络：" + ("开启（本次安装/构建需要联网）" if execution.get("network_required") or execution["policy"]["network"] else "关闭")
            yes, no = "仅允许这一次工具及所示参数；不批准下一次调用或写回原项目", "不执行该工具，停止当前请求，返回输入界面重新说明需求"
        elif action == "read_project":
            question = "是否允许智能体读取以下项目目录？\n" + request["project_root"]
            yes, no = "允许本次请求只读分析，不修改原文件、不执行项目代码", "拒绝读取，停止本次请求，回到输入界面"
        elif action == "create_staging":
            question = ("是否允许读取并复制此项目（或检查已恢复的副本），供后续 Docker 工具使用？\n原项目：" + request["project_root"]
                        + "\n临时工作区：" + request["staging_root"])
            yes, no = "仅允许复制/检查副本；每次工具仍需批准，不授权写回或联网", "不复制/检查副本，停止当前请求，可重新提出只读需求"
        elif action == "apply_changes":
            question = "是否批准这份固定版本的改动写回真实项目？\n" + request["change_set_id"] + "\n" + request["project_root"]
            yes, no = "只应用已展示的固定变更包，冲突时停止；不在 Host 执行代码", "不写回，可保留副本，回到输入界面"
        elif action == "enable_network":
            question = "是否允许本次请求的副本容器联网？\n项目：" + request["project_root"]
            yes, no = "允许本次请求容器外联（仅开/关，不是域名白名单）", "拒绝联网，停止当前请求，可重新提出离线需求"
        else:
            question = "是否允许这项具体操作？\n" + json.dumps(request, ensure_ascii=False, indent=2)
            yes, no = "仅批准展示的操作及必要网络范围", "不执行，停止当前请求，回到输入界面"
        self.say(question + "\n  yes：" + yes + "\n  no ：" + no, "warning")
        return choose_yes_no(self.renderer, self.input)

    def current_workspace(self, restoring=False):
        if not self.run or not self.run.bundle:
            raise ValueError("没有当前请求")
        agent = self.run.bundle["agent"]
        if not self.workspace:
            self.workspace = ProjectWorkspace(str(self.store.path), self.store.chat_id, self.run.branch,
                agent.request_id, self.state_dir, self.project_root, self.approval,
                protected=(str(Path(self.args.config).resolve()), str(self.settings.path.resolve())),
                image=self.image, toolchains=self.toolchains, network=self.network,
                excludes=(*DEFAULT_EXCLUDES, *self.settings.values.get("extra_excludes", [])),
                per_tool_permissions=True)
            if restoring:
                self.workspace.restore(agent)
        return self.workspace

    def show_package(self, package):
        body = package["body"]
        self.say(f"固定变更版本：{package['change_set_id']}\n原项目：{body['project_root']}\n副本：{body['staging_root']}")
        for change in body["changes"]:
            self.say(f"{change['action']} {change['path']}\n{change['diff']}")
        self.say("排除规则/文件：" + json.dumps(body["excluded"], ensure_ascii=False), "dim")
        if body["skipped"]:
            self.say("未自动应用的文件：" + str(body["skipped"]), "warning")
        verification_tools = {"run_code_get_feedback_tool", "run_java_get_feedback", "check_spring_boot_startup_tool",
                              "package_spring_boot_with_confirmation_tool"}
        if not any(item.get("tool_name") in verification_tools for item in body["tests"]):
            self.say("没有执行构建/测试/进程检查，不能声称已验证。", "warning")
        for evidence in body["tests"]:
            self.say(json.dumps(evidence, ensure_ascii=False), "error" if evidence["result"].get("status") != "success" else "dim")

    def review_and_apply(self, package):
        return asyncio.run(self.review_and_apply_async(package))

    async def review_and_apply_async(self, package):
        self.show_package(package)
        workspace = self.current_workspace()
        approved = await workspace._confirm("apply_changes", project_root=workspace.project_root,
            change_set_id=package["change_set_id"], file_list=[c["path"] for c in package["body"]["changes"]])
        if not approved:
            message = "未写回真实项目；副本和固定变更包保留。可重新说明需求。"
            self.run.emit("application_declined", "host", change_set_id=package["change_set_id"],
                          status="denied", answer=message, project_root=workspace.project_root)
            self.status = "denied"
            self.run.checkpoint("denied")
            self.say(message, "warning")
            return None
        result = workspace.apply(package, approved=True)
        message = "已应用指定变更包。未在 Host 运行项目代码。"
        self.run.emit("application", "host", change_set_id=package["change_set_id"], status="applied",
                      answer=message, project_root=workspace.project_root)
        self.say(message)
        return result

    async def ask(self, query=None, continuing=False):
        from AgentLoop.agent import main, state_init, load_runtime_config, SupervisorState, ExecutorState, SupervisorEvaluationState
        if not self.store:
            self.new()
        if continuing:
            if self.status in {"completed", "denied", "ready", "new"} or not self.run.bundle:
                raise ValueError("no pending request")
            if self.unknown:
                raise ValueError("unknown operation: inspect it and select a safe rewind boundary")
            query = self.run.bundle["agent"].user_query
            self.run.resume = True
        if not query or not query.strip():
            return
        config = load_runtime_config(Path(self.args.config))
        if continuing and self.run.payload.get("recovery_mode") == "history":
            # Historical reconstruction deliberately stores no provider config.
            recovered = self.run.bundle["agent"]
            recovered.executor_model, recovered.supervisor_model = config.executor_model, config.supervisor_model
            recovered.temperature, recovered.java, recovered.python = config.temperature, config.java, config.python
            # No executable phase was restored: explicitly run planning again,
            # retaining the verified task prefix instead of skipping to its end.
            self.run.resume = False
        with single_writer(self.store.path):
            latest_seq = self.store.latest_event_seq()
            if latest_seq != self.run.event_seq:
                raise ValueError("session changed in another writer; explicitly /resume before continuing")
            if not continuing:
                old = self.run.bundle["agent"] if self.run.bundle else None
                if old and self.status not in {"new", "completed", "denied", "ready"}:
                    document = self.run.checkpoint(self.status)
                    snapshot = f"snapshots/{self.run.branch}_superseded_{self.run.event_seq + 1}.json"
                    self.store._write(snapshot, document)
                    self.run.current_snapshot = snapshot
                    self.run.emit("request_superseded", "host", previous_status=self.status,
                                  message="用户输入新问题；保留旧历史，不自动继续旧任务。")
                agent = state_init(str(self.store.path), self.store.chat_id, query, config)
                agent.request_id, agent.branch_id = "r_" + uuid.uuid4().hex, self.run.branch
                agent.project_root = self.project_root
                self.workspace, self.policy = None, None
                agent.context_trigger_chars, agent.context_summary_max_chars = self.args.context_trigger_chars, self.args.summary_max_chars
                agent.context_max_chars = self.args.context_max_chars
                if old:
                    agent.task_list, agent.task_outcomes = list(old.task_list), dict(old.task_outcomes)
                    agent.now_task_id = agent.request_start_task_id = len(old.task_list)
                from State.history_recovery import conversation_context
                agent.conversation_context = conversation_context(self.store, self.run.branch)
                self.run.bundle = {"agent": agent, "supervisor": SupervisorState(session_address=str(self.store.path), chat_id=self.store.chat_id),
                    "executor": ExecutorState(session_address=str(self.store.path), chat_id=self.store.chat_id),
                    "evaluation": SupervisorEvaluationState(chat_id=self.store.chat_id)}
                self.run.phase, self.run.payload, self.run.resume = "planning", {}, False
                self.run.emit("user", "user", text=query)
                self.run.checkpoint()
            self.status = "running"
            workspace = self.current_workspace(restoring=continuing)
            render_token = RENDERER.set(self.renderer)
            run_token = ACTIVE_RUN.set(self.run)
            try:
                result = await main(query, str(self.store.path), self.store.chat_id, session_run=self.run,
                                    runtime_config=config, approval=self.approval, project_workflow=workspace)
                self.project_root = workspace.project_root
                self.policy = workspace.policy() if workspace.read_allowed else None
                self.status = "denied" if workspace.denied else result["status"]
                if workspace.denied:
                    self.run.checkpoint("denied")
                self.say(result["answer"])
                if workspace.staging_root:
                    package = workspace.make_package()
                    result["project_status"] = "staged"
                    if result["status"] == "completed" and package["body"]["changes"] and not package["body"]["skipped"]:
                        applied = await self.review_and_apply_async(package)
                        result["project_status"] = "applied" if applied else "not_applied"
                        if not applied:
                            result["status"] = "denied"
                    else:
                        self.say("以上执行发生在隔离副本；真实项目未写回。可使用 /diff、/apply 审阅固定版本。", "warning")
                        self.show_package(package)
                return result
            except asyncio.CancelledError:
                self.status = "interrupted"
                self.run.checkpoint("interrupted")
                raise
            finally:
                ACTIVE_RUN.reset(run_token)
                RENDERER.reset(render_token)

    def command(self, text):
        parts = split_command(text)
        command = parts[0]
        positional, options = filters(parts[1:])
        if command == "/exit":
            return False
        if command == "/help":
            self.say(HELP)
        elif command == "/new":
            self.new()
        elif command == "/settings":
            if positional:
                if positional[0] == "state-dir" and len(positional) == 2:
                    self.next_state_dir = self.settings.set_state_dir(positional[1])
                    self.say("设置已保存；后续 /new 或重启生效。当前会话仍在：" + str(self.state_dir))
                elif positional[0] == "exclude" and len(positional) == 2:
                    from MCP_functions.System_Files.Files_function.write_in import _atomic_write
                    values = {**self.settings.values, "extra_excludes": [*self.settings.values.get("extra_excludes", []), positional[1]]}
                    _atomic_write(self.settings.path, json.dumps(values, ensure_ascii=False, indent=2))
                    self.settings.values = values
                    self.say("排除规则已保存，后续请求生效；默认凭据/缓存排除仍保留。")
                elif positional == ["exclude-reset"]:
                    from MCP_functions.System_Files.Files_function.write_in import _atomic_write
                    values = {**self.settings.values, "extra_excludes": []}
                    _atomic_write(self.settings.path, json.dumps(values, ensure_ascii=False, indent=2))
                    self.settings.values = values
                elif positional[0] in {"image", "network"} and len(positional) == 2:
                    key, value = positional
                    if key == "network":
                        if value not in {"on", "off"}:
                            raise ValueError("/settings network on|off；on 仅是候选，执行前仍需 yes/no")
                        value = value == "on"
                    elif not value.strip() or any(c.isspace() for c in value):
                        raise ValueError("镜像名不能包含空白")
                    from MCP_functions.System_Files.Files_function.write_in import _atomic_write
                    values = {**self.settings.values, key: value}
                    _atomic_write(self.settings.path, json.dumps(values, ensure_ascii=False, indent=2))
                    self.settings.values = values
                    if key == "image":
                        self.image = value
                    else:
                        self.network = value
                    self.say("设置已保存，下一个请求生效；配置联网不等于已经授权网络。")
                else:
                    raise ValueError("/settings state-dir <绝对路径> / exclude <glob> / image <镜像> / network on|off")
            self.say(json.dumps({"active_state_dir": str(self.state_dir), "next_state_dir": str(self.next_state_dir),
                "settings_file": str(self.settings.path), "project_root": self.project_root,
                "staging_root": self.workspace.staging_root if self.workspace else None,
                "image": self.image, "network_requested": self.network,
                "excludes": [*DEFAULT_EXCLUDES, *self.settings.values.get("extra_excludes", [])]}, ensure_ascii=False, indent=2))
        elif command == "/project":
            if positional:
                if len(positional) != 1 or self.status not in {"new", "completed", "denied", "ready"}:
                    raise ValueError("/project <绝对路径>；未完成请求中不能切换项目，请 /new 或先完成当前请求")
                candidate = Path(positional[0])
                if not candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError("项目路径必须是绝对路径，不包含 ..")
                self.project_root = str(candidate.resolve())
                self.workspace, self.policy = None, None
            self.say("项目候选：" + str(self.project_root) + "；首次项目工具调用前会确认读取，不自动授权。")
        elif command == "/session":
            if len(positional) > 1 or options:
                raise ValueError("/session 或 /session 实际session_ID")
            self.select_session(positional[0] if positional else None)
        elif command == "/descriptions":
            if positional:
                raise ValueError("/descriptions [--actor executor|supervisor] [--page N]")
            if "session" in options:
                store = SessionStore(self.resolve(options["session"]), options.get("chat", "main"))
            elif self.store:
                store = SessionStore(self.store.path, options["chat"]) if "chat" in options else self.store
            else:
                raise ValueError("请先 /session 选择会话")
            branch = options.get("branch", self.run.branch if store is self.store else "main")
            events = description_history(store, branch)
            if "actor" in options:
                if options["actor"] not in {"executor", "supervisor"}:
                    raise ValueError("actor 必须是 executor 或 supervisor")
                events = [r for r in events if r["actor"] == options["actor"]]
            page = int(options.get("page", 1))
            if page < 1:
                raise ValueError("page must be positive")
            token = RENDERER.set(self.renderer)
            try:
                for row in events[(page - 1) * 20:page * 20]:
                    show_description(row["actor"], row["description"], task_id=row.get("task_id"),
                                     phase=row.get("record_type"), seq=row.get("seq"))
            finally:
                RENDERER.reset(token)
            self.say(f"历史 description page={page}, matched={len(events)}；/descriptions --page N 翻页。", "dim")
        elif command == "/sessions":
            self.say("历史存储目录：" + str(self.state_dir), "dim")
            count = 0
            for directory in sorted(self.state_dir.iterdir()):
                if directory.is_dir() and directory.resolve().is_relative_to(self.state_dir.resolve()):
                    count += 1
                    checkpoints = list(directory.glob("chats/*/checkpoints/*.json"))
                    if not checkpoints:
                        self.say(directory.name + " 可续聊（无执行快照；选中后加载历史）")
                    for path in checkpoints:
                        r = json.loads(path.read_text(encoding="utf-8"))
                        self.say(f"{directory.name}/{r['chat_id']}/{r['branch_id']} {r['status']} {r['updated_at']}")
            if not count:
                self.say("此存储目录暂无 session；旧记录在其他目录时，请用 --state-dir 指定旧目录。")
            self.say("直接选择并打开历史：/session；手动列表与 /open 仍可用。", "dim")
        elif command == "/chats":
            if len(positional) > 1 or (not positional and not self.store):
                raise ValueError("/chats SESSION")
            session = self.resolve(positional[0]) if positional else self.store.path
            chats = list_chats(session)
            for chat in chats:
                self.say(f"{session.name}/{chat['chat_id']} branches={','.join(chat['branches']) or 'main (旧日志，仅查看)'}")
            if not chats:
                self.say("此 session 暂无 chat 记录。")
            self.say("仅查看列表，活跃会话未切换。", "dim")
            self.say("继续该对话：/open SESSION CHAT [BRANCH]；回退：打开后 /rewind EVENT_SEQ", "dim")
        elif command in {"/history", "/conversation"}:
            if positional:
                raise ValueError(command + " 使用 --session ID --chat ID 选择历史")
            if "session" in options:
                store = SessionStore(self.resolve(options["session"]), options.get("chat", "main"))
            elif self.store:
                store = SessionStore(self.store.path, options["chat"]) if "chat" in options else self.store
            else:
                raise ValueError("请指定 --session ID；可先 /sessions 和 /chats SESSION")
            branch = options.get("branch", self.run.branch if store is self.store else "main")
            events = conversation_history(store, branch)
            if "request" in options:
                events = [r for r in events if str(r.get("request_id")) == options["request"]]
            if "search" in options:
                events = [r for r in events if options["search"] in json.dumps(r, ensure_ascii=False)]
            page = int(options.get("page", 1))
            if page < 1:
                raise ValueError("page must be positive")
            self.say(f"历史：{store.path.name}/{store.chat_id}/{branch}", "dim")
            if command == "/conversation":
                body = format_conversation(events)
                self.say(body[(page - 1) * 8000:page * 8000])
                self.say(f"page={page}/{max(1, (len(body) + 7999) // 8000)}, matched={len(events)}; 每页8000字符。", "dim")
            else:
                for r in events[(page - 1) * 20:page * 20]:
                    self.say(json.dumps(r, ensure_ascii=False), self.event_style(r))
                self.say(f"page={page}, matched={len(events)}", "dim")
            if not events:
                self.say("未找到匹配对话；用 /chats SESSION 确认 chat ID。")
            self.say("只读展示：未改变当前会话；续聊将使用当前分支的历史上下文。", "dim")
        elif command in {"/resume", "/open"}:
            if command == "/open" and not 2 <= len(positional) <= 3:
                raise ValueError("/open SESSION CHAT [BRANCH]")
            if not 1 <= len(positional) <= 3:
                raise ValueError("/resume SESSION [CHAT] [BRANCH]")
            self.resume(*positional)
            self.say("已打开旧 chat，可直接输入新问题沿用历史；需要继续旧任务才用 /continue。")
        elif command in {"/sandbox", "/permissions"}:
            if positional:
                raise ValueError("权限由运行时 yes/no 建立；不能通过模式名称把真实项目升级成可写")
            self.say(json.dumps({"project_root": self.project_root,
                "staging_root": self.workspace.staging_root if self.workspace else None,
                "policy": self.policy.describe() if self.policy else None,
                "permissions": [p.__dict__ for p in self.workspace.permissions] if self.workspace else [],
                "tool_permissions": [p.__dict__ for p in self.workspace.tool_permissions] if self.workspace else []}, ensure_ascii=False, indent=2))
        else:
            if not self.store:
                raise ValueError("no active session")
            if command == "/branches":
                self.say(json.dumps(self.store.branches(), ensure_ascii=False, indent=2))
                return True
            if command in {"/diff", "/apply", "/apply-status", "/export"}:
                workspace = self.current_workspace(restoring=True)
                if command == "/apply-status":
                    self.say(json.dumps(workspace.transaction_status(), ensure_ascii=False, indent=2), "warning")
                    return True
                with single_writer(self.store.path):
                    latest = self.store.latest_event_seq()
                    if latest != self.run.event_seq:
                        raise ValueError("会话已被其他写者改变，请先 /resume")
                    token = ACTIVE_RUN.set(self.run)
                    try:
                        if command == "/diff":
                            self.show_package(workspace.make_package())
                        elif command == "/export":
                            if len(positional) != 1:
                                raise ValueError("/export CHANGE_SET_ID")
                            from MCP_functions.System_Files.Files_function.write_in import _atomic_write
                            package = workspace.load_package(positional[0])
                            target = workspace.metadata / "packages" / (package["change_set_id"] + ".patch")
                            _atomic_write(target, "".join(c["diff"] for c in package["body"]["changes"]))
                            self.say("补丁已导出（未应用）：" + str(target))
                        else:
                            if len(positional) != 1:
                                raise ValueError("/apply CHANGE_SET_ID")
                            self.review_and_apply(workspace.load_package(positional[0]))
                    finally:
                        ACTIVE_RUN.reset(token)
                return True
            if command == "/rewind":
                if len(positional) == 2 and positional[0] == "task":
                    candidates = [r for r in self.store.history("timeline.jsonl", self.run.branch)
                        if r.get("task_id") == int(positional[1]) and r.get("phase") == "task_start" and r["kind"] == "boundary"]
                    if not candidates:
                        candidates = [r for r in self.store.history("timeline.jsonl", self.run.branch)
                                      if r.get("task_id") == int(positional[1])]
                    if not candidates:
                        raise ValueError("task start boundary unavailable")
                    seq = candidates[0]["event_seq"]
                elif len(positional) == 1:
                    seq = int(positional[0])
                else:
                    raise ValueError("/rewind EVENT_SEQ or /rewind task TASK_ID")
                branch = self.store.fork(seq, self.run.branch, allow_history=True)
                self.resume(self.store.path.name, self.store.chat_id, branch)
                self.say("新分支已建立；重新生成对话步骤，当前文件保持现状。/continue 才执行。")
                if self.run.payload.get("recovery_mode") == "history":
                    self.say("此边界无原执行快照，按可见历史重新规划；旧记录无时间编号时采用明确标注的重建顺序。", "dim")
                return True
            store = self.store if "session" not in options else SessionStore(self.resolve(options["session"]), options.get("chat", "main"))
            branch = options.get("branch", self.run.branch if store is self.store else "main")
            if command == "/tasks":
                a = store.load(branch)["bundle"]["agent"]
                for task, target in enumerate(a.task_list):
                    self.say(f"task={task} target={target} accepted={task in a.task_outcomes} current={task == a.now_task_id} "
                             f"attempts={a.task_attempt if task == a.now_task_id else 'see timeline'}")
                return True
            events = store.history("timeline.jsonl", branch)
            if command == "/tools":
                events = [r for r in events if r.get("kind") in {"tool_intent", "tool_completed", "authorization_waiting", "authorization"}
                          or str(r.get("record_type", "")).endswith("_tool_event")]
            elif command in {"/steps", "/events"}:
                events = [r for r in events if r.get("kind") == "boundary"]
            elif command == "/show":
                if len(positional) != 1:
                    raise ValueError("/show EVENT_SEQ [--page N]")
                matches = [r for r in events if r["event_seq"] == int(positional[0])]
                if len(matches) != 1:
                    raise ValueError("event not found")
                event = dict(matches[0])
                if event.get("file"):
                    event["record"] = next((r for r in store.history(event["file"], branch) if r.get("event_seq") == event["event_seq"]), None)
                if event.get("tool_result_seq"):
                    event["raw_tool_record"] = next((r for r in store.history("tool_results.jsonl", branch) if r["tool_result_seq"] == event["tool_result_seq"]), None)
                body = json.dumps(event, ensure_ascii=False, indent=2)
                page = int(options.get("page", 1))
                if page < 1:
                    raise ValueError("page must be positive")
                self.say(body[(page - 1) * 8000:page * 8000], self.event_style(event))
                self.say(f"原始记录：{store.path}; page={page}, chars={len(body)}", "dim")
                return True
            else:
                raise ValueError("unknown command: " + command)
            for name in ("actor", "phase", "status", "request", "task"):
                if name in options:
                    key = "request_id" if name == "request" else "task_id" if name == "task" else name
                    events = [r for r in events if str(r.get(key)) == options[name]]
            if "search" in options:
                events = [r for r in events if options["search"] in json.dumps(r, ensure_ascii=False)]
            page = int(options.get("page", 1))
            if page < 1:
                raise ValueError("page must be positive")
            for r in events[(page - 1) * 20:page * 20]:
                self.say(json.dumps(r, ensure_ascii=False), self.event_style(r))
            self.say(f"page={page}, matched={len(events)}; 活跃会话未因查看而切换。", "dim")
        return True

    def loop(self):
        self.say(HELP)
        try:
            if self.args.resume:
                self.resume(self.args.resume, self.args.chat, self.args.branch)
            elif self.input("新建 [n] / 恢复 [r]：").strip().lower() == "r":
                self.select_session()
            else:
                self.new()
        except EOFError:
            return
        except (ValueError, RuntimeError, OSError, IndexError) as exc:
            self.say("受阻：" + str(exc) + "；可在 agent> 输入 /session 重新选择。", "error")
        while True:
            try:
                text = self.input("agent> ")
                if not text.strip():
                    continue
                if text.strip() == "/multiline":
                    lines = []
                    while True:
                        line = self.input("... ")
                        if line == "/end":
                            break
                        lines.append(line)
                    text = "\n".join(lines)
                    if text.strip():
                        asyncio.run(self.ask(text))
                    continue
                if text.strip() == "/continue":
                    asyncio.run(self.ask(continuing=True))
                elif text.startswith("/"):
                    if not self.command(text):
                        return
                elif text.strip():
                    asyncio.run(self.ask(text))
            except EOFError:
                self.say("EOF：退出，已落盘检查点保留。")
                return
            except KeyboardInterrupt:
                self.say("已中断，请检查 /tools 与恢复状态。", "warning")
            except (ValueError, RuntimeError, OSError, IndexError) as exc:
                self.say("受阻：" + str(exc), "error")

def main(argv=None):
    args = parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    terminal = Terminal(args)
    if args.query is not None:
        if args.resume:
            terminal.resume(args.resume, args.chat, args.branch)
        return asyncio.run(terminal.ask(args.query))
    terminal.loop()

if __name__ == "__main__":
    main()
