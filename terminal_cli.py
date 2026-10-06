"""Terminal owns stdin, session selection and authorization; model owns next action."""
from __future__ import annotations
import argparse
import asyncio
import json
import shlex
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from MCP_functions.sandbox import SandboxPolicy, SandboxRunner, safe_display
from State.session_checkpoint import SessionStore, ActiveRun, rows, single_writer

HELP = """普通文本：新请求，完成后可继续问答。空行不执行。
/help  /new  /sessions  /branches  /exit
/resume SESSION [CHAT] [BRANCH]   仅恢复状态
/continue                        用户确认继续恢复的请求
/history [--page N] [--search TEXT] [--request ID] [--branch ID] [--session ID]
/tasks                           task 目标、成果和当前尝试
/tools [--actor ROLE] [--task N] [--phase NAME] [--status STATUS]
/show EVENT_SEQ [--page N]        展开参数/原文，每页 8000 字符
/rewind EVENT_SEQ                从事件前置状态建立新分支
/rewind task TASK_ID              从任务开始建立分支
/sandbox [read-only|workspace-write|danger-full-access]
/multiline                       多行输入，单独一行 /end 结束
恢复/回退不回滚文件、不自动重放命令；/continue 才进入模型循环。EOF 退出。
"""

def parser():
    p = argparse.ArgumentParser(description="Supervisor–Executor coding agent terminal")
    p.add_argument("--workspace", default=str(Path.cwd()))
    p.add_argument("--state-dir", help="dedicated Host state directory; default next to workspace")
    p.add_argument("--config", default=str(Path(__file__).with_name("INFORMATION.json")))
    p.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    p.add_argument("--image", default="coding-agent-sandbox:local")
    p.add_argument("--java", default="/usr/lib/jvm/java-17-openjdk-amd64/bin/java")
    p.add_argument("--javac", default="/usr/lib/jvm/java-17-openjdk-amd64/bin/javac")
    p.add_argument("--network", action="store_true")
    p.add_argument("--readable-root", action="append", default=[])
    p.add_argument("--writable-root", action="append", default=[])
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
        root = Path(args.workspace).resolve()
        self.state_dir = Path(args.state_dir).resolve() if args.state_dir else root.parent / ".coding-agent-state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.policy = SandboxPolicy(str(root), str(self.state_dir), args.sandbox,
            tuple(args.readable_root), tuple(args.writable_root), (str(Path(args.config).resolve()),),
            args.network, args.image, toolchains={"python": "/usr/local/bin/python", "java": args.java,
                "javac": args.javac, "mvn": "/usr/bin/mvn", "gradle": "/usr/bin/gradle"})
        if min(args.context_trigger_chars, args.summary_max_chars, args.context_max_chars) < 1:
            raise ValueError("context limits must be positive")
        self.store, self.run, self.status, self.unknown = None, None, "new", []

    def say(self, value):
        self.output(safe_display(value))

    def event(self, row):
        if row["kind"] in {"boundary", "tool_intent", "tool_completed", "authorization", "authorization_waiting", "interrupted"} or str(row.get("record_type", "")).endswith("_tool_event"):
            self.say(f"[{row['event_seq']}] {row['actor']} task={row.get('task_id')} phase={row['phase']} "
                     f"{row['kind']} {row.get('tool_name', '')} {row.get('status', '')}")

    def new(self):
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
        with single_writer(store.path):
            saved = store.load(branch)
        run = ActiveRun(store, branch, self.event)
        run.bundle, run.phase, run.payload = saved["bundle"], saved["phase"], saved["payload"]
        run.resume = saved["status"] != "completed"
        self.store, self.run, self.status = store, run, saved["status"]
        self.unknown = saved.get("unknown_operations", [])
        self.say(f"已恢复 {session}/{chat}/{branch} phase={run.phase} status={self.status}；未执行工具。")
        if self.unknown:
            self.say("未知操作需人工核对，不自动重放：" + str(self.unknown))

    def approval(self, request):
        if self.args.query is not None or not sys.stdin.isatty():
            return False
        self.say("仅本次授权：\n" + json.dumps(request, ensure_ascii=False, indent=2))
        try:
            return self.input("允许？[y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    async def ask(self, query=None, continuing=False):
        from AgentLoop.agent import main, state_init, load_runtime_config, SupervisorState, ExecutorState, SupervisorEvaluationState
        if not self.store:
            self.new()
        if continuing:
            if self.status == "completed" or not self.run.bundle:
                raise ValueError("no pending request")
            if self.unknown:
                raise ValueError("unknown operation: inspect it and select a safe rewind boundary")
            query = self.run.bundle["agent"].user_query
            self.run.resume = True
        elif self.status not in {"new", "completed"}:
            raise ValueError("unfinished request: /continue or /rewind; new text was not merged")
        if not query or not query.strip():
            return
        config = load_runtime_config(Path(self.args.config))
        with single_writer(self.store.path):
            latest_seq = max((row.get("event_seq", 0) for row in rows(self.store.path / "timeline.jsonl")), default=0)
            if latest_seq != self.run.event_seq:
                raise ValueError("session changed in another writer; explicitly /resume before continuing")
            if not continuing:
                old = self.run.bundle["agent"] if self.run.bundle else None
                agent = state_init(str(self.store.path), self.store.chat_id, query, config)
                agent.request_id, agent.branch_id = "r_" + uuid.uuid4().hex, self.run.branch
                agent.context_trigger_chars, agent.context_summary_max_chars = self.args.context_trigger_chars, self.args.summary_max_chars
                agent.context_max_chars = self.args.context_max_chars
                if old:
                    agent.task_list, agent.task_outcomes = list(old.task_list), dict(old.task_outcomes)
                    agent.now_task_id = agent.request_start_task_id = len(old.task_list)
                agent.conversation_context = [{"role": "user" if r["kind"] == "user" else "assistant",
                    "request_id": r.get("request_id"), "content": r.get("text", r.get("answer", ""))}
                    for r in self.store.history("timeline.jsonl", self.run.branch) if r.get("kind") in {"user", "answer"}]
                self.run.bundle = {"agent": agent, "supervisor": SupervisorState(session_address=str(self.store.path), chat_id=self.store.chat_id),
                    "executor": ExecutorState(session_address=str(self.store.path), chat_id=self.store.chat_id),
                    "evaluation": SupervisorEvaluationState(chat_id=self.store.chat_id)}
                self.run.phase, self.run.payload, self.run.resume = "planning", {}, False
                self.run.emit("user", "user", text=query)
                self.run.checkpoint()
            self.status = "running"
            try:
                result = await main(query, str(self.store.path), self.store.chat_id, session_run=self.run,
                                    runtime_config=config, sandbox_policy=self.policy, approval=self.approval)
                self.status = result["status"]
                self.say(result["answer"])
                return result
            except asyncio.CancelledError:
                self.status = "interrupted"
                self.run.checkpoint("interrupted")
                raise

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
        elif command == "/sessions":
            for directory in sorted(self.state_dir.iterdir()):
                if directory.is_dir():
                    checkpoints = list(directory.glob("chats/*/checkpoints/*.json"))
                    if not checkpoints:
                        self.say(directory.name + " legacy/new: history viewable, precise resume unavailable")
                    for path in checkpoints:
                        r = json.loads(path.read_text(encoding="utf-8"))
                        self.say(f"{directory.name}/{r['chat_id']}/{r['branch_id']} {r['status']} {r['updated_at']}")
        elif command == "/resume":
            if not 1 <= len(positional) <= 3:
                raise ValueError("/resume SESSION [CHAT] [BRANCH]")
            self.resume(*positional)
        elif command in {"/sandbox", "/permissions"}:
            if positional:
                self.policy = replace(self.policy, mode=positional[0], version=uuid.uuid4().hex)
            self.say(json.dumps({**self.policy.describe(), **SandboxRunner(self.policy).availability()}, ensure_ascii=False, indent=2))
        else:
            if not self.store:
                raise ValueError("no active session")
            if command == "/branches":
                self.say(json.dumps(self.store.branches(), ensure_ascii=False, indent=2))
                return True
            if command == "/rewind":
                if len(positional) == 2 and positional[0] == "task":
                    candidates = [r for r in self.store.history("timeline.jsonl", self.run.branch)
                        if r.get("task_id") == int(positional[1]) and r.get("phase") == "task_start" and r["kind"] == "boundary"]
                    if not candidates:
                        raise ValueError("task start boundary unavailable")
                    seq = candidates[0]["event_seq"]
                elif len(positional) == 1:
                    seq = int(positional[0])
                else:
                    raise ValueError("/rewind EVENT_SEQ or /rewind task TASK_ID")
                branch = self.store.fork(seq, self.run.branch)
                self.resume(self.store.path.name, self.store.chat_id, branch)
                self.say("新分支已建立；重新生成对话步骤，当前文件保持现状。/continue 才执行。")
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
            if command == "/history":
                events = [r for r in events if r.get("kind") in {"user", "answer", "interrupted"}]
                if not events:
                    events = store.history("chat_history.jsonl", branch)
            elif command == "/tools":
                events = [r for r in events if r.get("kind") in {"tool_intent", "tool_completed", "authorization_waiting", "authorization"}
                          or str(r.get("record_type", "")).endswith("_tool_event")]
            elif command == "/show":
                if len(positional) != 1:
                    raise ValueError("/show EVENT_SEQ [--page N]")
                matches = [r for r in events if r["event_seq"] == int(positional[0])]
                if len(matches) != 1:
                    raise ValueError("event not found")
                event = dict(matches[0])
                if event.get("file"):
                    event["record"] = next((r for r in rows(store.path / event["file"]) if r.get("event_seq") == event["event_seq"]), None)
                if event.get("tool_result_seq"):
                    event["raw_tool_record"] = next((r for r in store.history("tool_results.jsonl", branch) if r["tool_result_seq"] == event["tool_result_seq"]), None)
                body = json.dumps(event, ensure_ascii=False, indent=2)
                page = int(options.get("page", 1))
                if page < 1:
                    raise ValueError("page must be positive")
                self.say(body[(page - 1) * 8000:page * 8000])
                self.say(f"原始记录：{store.path}; page={page}, chars={len(body)}")
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
                self.say(json.dumps(r, ensure_ascii=False))
            self.say(f"page={page}, matched={len(events)}; 活跃会话未因查看而切换。")
        return True

    def loop(self):
        self.say(HELP)
        try:
            if self.args.resume:
                self.resume(self.args.resume, self.args.chat, self.args.branch)
            elif self.input("新建 [n] / 恢复 [r]：").strip().lower() == "r":
                self.command("/sessions")
                self.command("/resume " + self.input("SESSION [CHAT] [BRANCH]："))
            else:
                self.new()
        except EOFError:
            return
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
                self.say("已中断，请检查 /tools 与恢复状态。")
            except (ValueError, RuntimeError, OSError, IndexError) as exc:
                self.say("受阻：" + str(exc))

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
