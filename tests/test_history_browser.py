import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from MCP_functions.System_Files.Files_function.write_in import _atomic_write
from State.history_browser import list_chats, conversation_history, format_conversation
from State.session_checkpoint import SessionStore
from terminal_cli import Terminal, parser
from test_agent_loop import TEST_TEMP_ROOT
from test_agent_loop import QueueChat, model_response, json_message
from AgentLoop.agent import RuntimeConfig


class HistoryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = []
        self.terminal = Terminal(parser().parse_args([
            "--state-dir", str(self.root / "state"), "--settings-file", str(self.root / "settings.json"),
            "--color", "never"]), input_function=lambda _: self.fail("browsing must not ask permission"),
            output=self.output.append)
        self.session = self.terminal.state_dir / "s_old"
        self.session.mkdir()

    def write_rows(self, name, records):
        _atomic_write(self.session / name, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    def modern(self):
        store = SessionStore(self.session, "second")
        store.initialize()
        store._write("branches.json", {"main": {"parent": None, "cutoff": None},
                                      "b_test": {"parent": "main", "cutoff": 2}})
        self.write_rows("timeline.jsonl", [
            {"chat_id": "second", "branch_id": "main", "event_seq": 1, "request_id": "r1", "kind": "user", "text": "OLD QUESTION"},
            {"chat_id": "second", "branch_id": "main", "event_seq": 2, "request_id": "r1", "kind": "answer", "answer": "OLD ANSWER"},
            {"chat_id": "second", "branch_id": "main", "event_seq": 3, "request_id": "r2", "kind": "answer", "answer": "EXCLUDED FUTURE"},
            {"chat_id": "second", "branch_id": "b_test", "event_seq": 4, "request_id": "r3", "kind": "answer", "answer": "BRANCH ANSWER"},
            {"chat_id": "main", "branch_id": "main", "event_seq": 5, "request_id": "r4", "kind": "answer", "answer": "OTHER CHAT"}])
        return store

    def test_discover_modern_and_legacy_chats_without_writes(self):
        self.modern()
        self.write_rows("chat_history.jsonl", [{"chat_id": "001", "query_content": "legacy"}])
        before = sorted(str(p) for p in self.session.rglob("*"))
        self.assertEqual([r["chat_id"] for r in list_chats(self.session)], ["001", "main", "second"])
        self.assertEqual(before, sorted(str(p) for p in self.session.rglob("*")))

    def test_branch_prefix_and_chat_isolation(self):
        body = format_conversation(conversation_history(self.modern(), "b_test"))
        self.assertIn("OLD ANSWER", body)
        self.assertIn("BRANCH ANSWER", body)
        self.assertNotIn("EXCLUDED FUTURE", body)
        self.assertNotIn("OTHER CHAT", body)

    def test_legacy_view_without_checkpoint_or_metadata(self):
        self.write_rows("chat_history.jsonl", [{"chat_id": "001", "query_content": "OLD QUESTION", "answer_content": "OLD ANSWER"}])
        self.terminal.command("/conversation --session s_old --chat 001")
        self.assertIn("OLD QUESTION", "\n".join(self.output))
        self.assertIn("OLD ANSWER", "\n".join(self.output))
        self.assertFalse((self.session / "chats").exists())
        self.assertFalse((self.session / ".writer.lock").exists())
        with self.assertRaises(ValueError):
            conversation_history(SessionStore(self.session, "001"), "b_fake")

    def test_commands_without_active_session_and_never_switch_active_state(self):
        self.modern()
        for cmd in ("/sessions", "/chats s_old", "/history --session s_old --chat second --branch b_test",
                    "/conversation --session s_old --chat second --branch b_test"):
            self.terminal.command(cmd)
        self.assertIsNone(self.terminal.store)
        self.terminal.new()
        active = (self.terminal.store, self.terminal.run, self.terminal.status)
        self.terminal.command("/conversation --session s_old --chat second")
        self.assertEqual(active, (self.terminal.store, self.terminal.run, self.terminal.status))

    def test_current_session_chat_option_is_respected(self):
        self.modern()
        self.terminal.store = SessionStore(self.session, "main")
        self.terminal.command("/conversation --chat second --branch b_test")
        self.assertIn("OLD ANSWER", "\n".join(self.output))
        self.assertNotIn("OTHER CHAT", "\n".join(self.output))

    def test_invalid_identity_missing_session_and_page_rejected(self):
        self.modern()
        for cmd in ("/chats ..", "/chats absent", "/conversation --session s_old --chat ../other",
                    "/conversation --session s_old --chat second --page 0",
                    "/conversation --session s_old --chat second --branch absent"):
            with self.assertRaises(ValueError, msg=cmd):
                self.terminal.command(cmd)

    def test_query_filters_and_character_pagination(self):
        self.modern()
        self.terminal.command("/conversation --session s_old --chat second --request r1 --search ANSWER")
        body = "\n".join(self.output)
        self.assertIn("OLD ANSWER", body)
        self.assertNotIn("OLD QUESTION", body)
        self.output.clear()
        self.write_rows("timeline.jsonl", [{"chat_id": "second", "kind": "answer", "answer": "X" * 9000}])
        self.terminal.command("/conversation --session s_old --chat second --page 2")
        self.assertIn("page=2/2", "\n".join(self.output))

    def test_legacy_missing_chat_id_defaults_main_and_corruption_is_explicit(self):
        self.write_rows("chat_history.jsonl", [{"query_content": "old", "answer_content": "answer"}])
        self.assertEqual(list_chats(self.session)[0]["chat_id"], "main")
        self.assertIn("answer", format_conversation(conversation_history(SessionStore(self.session))))
        _atomic_write(self.session / "chat_history.jsonl", "broken\n")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            conversation_history(SessionStore(self.session))


class OpenOldChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_reopen_non_main_chat_followup_uses_old_context_and_can_rewind(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            args = parser().parse_args(["--state-dir", str(Path(directory) / "state"),
                "--settings-file", str(Path(directory) / "settings.json"), "--chat", "second", "--color", "never"])
            first = Terminal(args, output=lambda _: None)
            config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)
            def responses(n):
                return [model_response(json_message(task_list=[f"task-{n}"], description="规划")),
                    model_response(json_message(description="指导", executor_guidance="执行", completion_criteria="证据")),
                    model_response(json_message(description="执行完成", is_finished=True)),
                    model_response(json_message(description="验收", is_passed=True, is_finished=True)),
                    model_response(json_message(description="通过", is_next_target=True, is_finished=True, final_answer=f"answer-{n}")),
                    model_response(json_message(description=f"answer-{n}"))]
            chat = QueueChat(responses(0) + responses(1))
            with (patch("AgentLoop.agent.load_runtime_config", return_value=config),
                  patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                  patch("AgentLoop.agent._build_model_client", return_value=object()),
                  patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                  patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat})):
                self.assertEqual((await first.ask("FIRST QUESTION"))["status"], "completed")
                session = first.store.path.name
                second = Terminal(args, output=lambda _: None)
                before = len(chat.calls)
                second.command(f"/open {session} second main")
                self.assertEqual(len(chat.calls), before, "open must not call the model")
                self.assertEqual(second.status, "completed")
                self.assertEqual((await second.ask("FOLLOWUP QUESTION"))["status"], "completed")
                plan = json.loads(chat.calls[6]["query"][-1]["content"])
                self.assertIn("FIRST QUESTION", json.dumps(plan))
                self.assertIn("answer-0", json.dumps(plan))
                self.assertEqual(second.store.chat_id, "second")
                self.assertEqual(second.run.bundle["agent"].request_start_task_id, 1)
                self.assertEqual(sorted(second.run.bundle["agent"].task_outcomes), [0, 1])
                boundary = next(r for r in second.store.history("timeline.jsonl")
                                if r.get("kind") == "boundary" and r.get("phase") == "evaluation" and r.get("task_id") == 1)
                second.command(f"/rewind {boundary['event_seq']}")
                self.assertEqual(len(chat.calls), 12, "rewind must not execute without continue")
                self.assertNotEqual(second.run.branch, "main")
                self.assertEqual(second.store.chat_id, "second")
                self.assertEqual(sorted(second.run.bundle["agent"].task_outcomes), [0])
                self.assertNotIn("answer-1", format_conversation(conversation_history(second.store, second.run.branch)))

    async def test_open_pending_request_allows_new_question_and_legacy_can_open(self):
        from test_agent_loop import make_states
        from State.session_checkpoint import ActiveRun
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            root = Path(directory)
            args = parser().parse_args(["--state-dir", str(root / "state"), "--settings-file", str(root / "settings.json")])
            terminal = Terminal(args, output=lambda _: None)
            store = SessionStore(terminal.state_dir / "s_pending", "second")
            store.initialize()
            agent, supervisor, executor, evaluation = make_states(str(store.path))
            for state in (agent, supervisor, executor, evaluation):
                state.chat_id = "second"
            run = ActiveRun(store)
            run.bundle = dict(agent=agent, supervisor=supervisor, executor=executor, evaluation=evaluation)
            run.checkpoint("interrupted")
            terminal.command("/open s_pending second")
            config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)
            async def fake_main(*args, **kwargs):
                return {"status": "completed", "answer": "new answer"}
            with patch("AgentLoop.agent.load_runtime_config", return_value=config), patch("AgentLoop.agent.main", new=fake_main):
                await terminal.ask("NEW QUESTION")
            self.assertEqual(terminal.run.bundle["agent"].user_query, "NEW QUESTION")
            self.assertTrue(any(r["kind"] == "request_superseded" for r in terminal.store.history("timeline.jsonl")))
            legacy = terminal.state_dir / "s_legacy"
            legacy.mkdir()
            _atomic_write(legacy / "chat_history.jsonl", '{"chat_id":"main","query_content":"old"}\n')
            terminal.command("/open s_legacy main")
            self.assertEqual(terminal.store.path, legacy)
            self.assertEqual(terminal.status, "ready")
            self.assertIn("old", json.dumps(terminal.run.bundle["agent"].conversation_context))
