import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from AgentLoop.agent import RuntimeConfig, TaskOutcome
from AgentLoop.agent import AgentState, SupervisorState, ExecutorState, SupervisorEvaluationState
from Context.mcp_resources import read_history_chat
from MCP_functions.System_Files.Files_function.write_in import _atomic_write
from State.history_recovery import conversation_context, index_entries
from State.session_checkpoint import SessionStore, ActiveRun, ACTIVE_RUN, rows, single_writer
from terminal_cli import Terminal, parser
from test_agent_loop import TEST_TEMP_ROOT, QueueChat, model_response, json_message


class ContinueHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = []
        self.args = parser().parse_args(["--state-dir", str(self.root / "state"), "--settings-file", str(self.root / "settings.json"), "--color", "never"])
        self.terminal = Terminal(self.args, input_function=lambda _: self.fail("opening is not tool authorization"), output=self.output.append)
        self.store = SessionStore(self.terminal.state_dir / "s_legacy", "main")
        self.store.path.mkdir()
        self.config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)

    def write(self, name, records):
        _atomic_write(self.store.path / name, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    def seed(self):
        self.write("chat_history.jsonl", [
            {"chat_id": "main", "query_content": "OLD QUESTION", "answer_content": "OLD ANSWER"},
            {"chat_id": "other", "query_content": "OTHER QUESTION", "answer_content": "OTHER ANSWER"}])
        self.write("executor_history.jsonl", [
            {"chat_id": "main", "task_id": 0, "seq": 3, "record_type": "executor_turn", "target": "TASK ZERO", "description": "work zero"},
            {"chat_id": "main", "task_id": 1, "seq": 4, "record_type": "executor_turn", "target": "TASK ONE", "description": "work one"}])
        self.write("supervisor_history.jsonl", [
            {"chat_id": "main", "task_id": 0, "seq": 1, "record_type": "decision_turn", "target": "TASK ZERO",
             "description": "verified zero", "is_executor_passed": True, "is_next_target": True,
             "task_outcome": {"task_id": 0, "accepted_summary": "CONFIRMED ZERO", "verification_summary": "CHECK ZERO"}},
            {"chat_id": "main", "task_id": 1, "seq": 2, "record_type": "decision_turn", "target": "TASK ONE",
             "description": "REJECTED ONE", "is_executor_passed": False, "is_next_target": False}])
        self.write("tool_results.jsonl", [
            {"chat_id": "main", "tool_result_seq": 10, "task_id": 0, "actor": "executor",
             "tool_name": "fake_tool", "ok": True, "arguments": {}, "content": {"output": "UNIQUE RAW BODY"}}])

    def replies(self, suffix):
        return [model_response(json_message(task_list=["new task " + suffix], description="plan")),
                model_response(json_message(description="guide", executor_guidance="execute", completion_criteria="evidence")),
                model_response(json_message(description="done", is_finished=True)),
                model_response(json_message(description="checked", is_passed=True, is_finished=True)),
                model_response(json_message(description="accepted", is_next_target=True, is_finished=True, final_answer="answer " + suffix)),
                model_response(json_message(description="answer " + suffix))]

    def model_mocks(self, chat):
        from contextlib import ExitStack
        stack = ExitStack()
        for target, kwargs in [
            ("AgentLoop.agent.load_runtime_config", {"return_value": self.config}),
            ("AgentLoop.agent._register_mcp_clients", {"new": AsyncMock()}),
            ("AgentLoop.agent._build_model_client", {"return_value": object()}),
            ("AgentLoop.agent._close_model_client", {"new": AsyncMock()}),
            ("AgentLoop.agent._chat_map", {"return_value": {"chatgpt": chat}})]:
            stack.enter_context(patch(target, **kwargs))
        return stack

    async def test_legacy_open_and_followup_real_loop_retains_old_qa_and_tasks(self):
        self.seed()
        before = {p.name: p.read_bytes() for p in self.store.path.glob("*.jsonl")}
        self.terminal.command("/session s_legacy")
        agent = self.terminal.run.bundle["agent"]
        self.assertEqual(agent.task_list, ["TASK ZERO", "TASK ONE"])
        self.assertEqual(sorted(agent.task_outcomes), [0])
        self.assertEqual(self.terminal.status, "ready")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.store.path.glob("*.jsonl")})
        chat = QueueChat(self.replies("first") + self.replies("second"))
        with self.model_mocks(chat):
            self.assertEqual((await self.terminal.ask("FIRST FOLLOWUP"))["status"], "completed")
            self.assertEqual((await self.terminal.ask("SECOND FOLLOWUP"))["status"], "completed")
        for call in (chat.calls[0], chat.calls[6]):
            body = json.loads(call["query"][-1]["content"])
            text = json.dumps(body)
            self.assertIn("OLD QUESTION", text)
            self.assertIn("OLD ANSWER", text)
            self.assertNotIn("OTHER ANSWER", text)
            self.assertIn("TASK ZERO", text)
        context = self.terminal.run.bundle["agent"].conversation_context
        self.assertEqual(sum(r["content"] == "OLD ANSWER" for r in context), 1)
        self.assertEqual(self.terminal.run.bundle["agent"].request_start_task_id, 3)
        self.assertEqual(len(rows(self.store.path / "tool_results.jsonl")), 1)

    async def test_blocked_and_interrupted_accept_new_question_without_continue(self):
        for status in ("blocked", "interrupted"):
            store = SessionStore(self.terminal.state_dir / ("s_" + status))
            store.initialize()
            run = ActiveRun(store)
            agent = AgentState(str(store.path), "main", user_query="OLD FAILED QUESTION", request_id="r_old",
                               task_list=["prior target"], task_outcomes={0: TaskOutcome(0, "prior target", "prior result", "checked")})
            run.bundle = {"agent": agent, "supervisor": SupervisorState(session_address=str(store.path), chat_id="main"),
                          "executor": ExecutorState(session_address=str(store.path), chat_id="main"),
                          "evaluation": SupervisorEvaluationState(chat_id="main")}
            run.emit("user", "user", text="OLD FAILED QUESTION")
            run.emit("answer", "supervisor", answer="OLD BLOCKED ANSWER")
            run.checkpoint(status)
            self.terminal.command("/open " + store.path.name + " main")
            chat = QueueChat(self.replies(status))
            with self.model_mocks(chat):
                result = await self.terminal.ask("NEW QUESTION")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(self.terminal.run.bundle["agent"].user_query, "NEW QUESTION")
            self.assertTrue(any(r["kind"] == "request_superseded" for r in store.history("timeline.jsonl")))
            self.assertIn("OLD BLOCKED ANSWER", json.dumps(chat.calls[0]["query"]))
            self.assertIn(0, self.terminal.run.bundle["agent"].task_outcomes)

    async def test_legacy_task_rewind_keeps_prior_outcome_and_hides_future(self):
        self.seed()
        self.terminal.command("/session s_legacy")
        original = self.store.history("timeline.jsonl")
        self.terminal.command("/rewind task 1")
        self.assertNotEqual(self.terminal.run.branch, "main")
        self.assertEqual(self.terminal.run.payload["recovery_mode"], "history")
        self.assertEqual(self.terminal.run.bundle["agent"].task_list, ["TASK ZERO"])
        self.assertEqual(sorted(self.terminal.run.bundle["agent"].task_outcomes), [0])
        visible = self.store.history("supervisor_history.jsonl", self.terminal.run.branch)
        self.assertNotIn("REJECTED ONE", json.dumps(visible))
        self.assertEqual(original, self.store.history("timeline.jsonl", "main"))
        token = ACTIVE_RUN.set(self.terminal.run)
        try:
            self.assertEqual(await read_history_chat(str(self.store.path), "main"), [])
        finally:
            ACTIVE_RUN.reset(token)
        chat = QueueChat(self.replies("rewound"))
        with self.model_mocks(chat):
            result = await self.terminal.ask(continuing=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.terminal.run.bundle["agent"].request_start_task_id, 1)
        self.assertEqual(chat.calls[0]["model"], "fake")

    async def test_event_rewind_excludes_selected_chat_and_future_chat(self):
        self.write("chat_history.jsonl", [{"chat_id": "main", "query_content": "Q1", "answer_content": "A1"},
                    {"chat_id": "main", "query_content": "Q2", "answer_content": "A2"}])
        self.terminal.command("/session s_legacy")
        event = next(e for e in self.store.history("timeline.jsonl") if e.get("query_content") == "Q2")
        self.terminal.command(f"/rewind {event['event_seq']}")
        text = json.dumps(conversation_context(self.store, self.terminal.run.branch))
        self.assertIn("A1", text)
        self.assertNotIn("A2", text)
        token = ACTIVE_RUN.set(self.terminal.run)
        try:
            records = await read_history_chat(str(self.store.path), "main")
            self.assertEqual([r["answer_content"] for r in records], ["A1"])
        finally:
            ACTIVE_RUN.reset(token)

    async def test_empty_session_can_open_and_start_chat_but_cannot_invent_rewind(self):
        self.terminal.command("/session s_legacy")
        self.assertEqual(self.terminal.status, "ready")
        self.assertEqual(self.terminal.store.path, self.store.path)
        self.assertEqual(self.terminal.run.bundle["agent"].conversation_context, [])
        with self.assertRaises(ValueError):
            self.terminal.command("/rewind task 0")
        with self.assertRaisesRegex(ValueError, "no pending request"):
            await self.terminal.ask(continuing=True)
        chat = QueueChat(self.replies("empty"))
        with self.model_mocks(chat):
            self.assertEqual((await self.terminal.ask("hello"))["status"], "completed")

    async def test_restart_has_context_and_legacy_records_are_not_duplicated(self):
        self.seed()
        self.terminal.command("/session s_legacy")
        metadata = (self.store.directory / "history_index.json").read_bytes()
        checkpoint = (self.store.directory / "checkpoints" / "main.json").read_bytes()
        reopened = Terminal(self.args, output=lambda _: None)
        reopened.command("/open s_legacy main")
        self.assertEqual(metadata, (self.store.directory / "history_index.json").read_bytes())
        self.assertEqual(checkpoint, (self.store.directory / "checkpoints" / "main.json").read_bytes())
        self.assertIn("OLD ANSWER", json.dumps(reopened.run.bundle["agent"].conversation_context))
        self.assertNotIn("UNIQUE RAW BODY", metadata.decode())
        self.assertNotIn("UNIQUE RAW BODY", checkpoint.decode())

    async def test_numbering_uses_session_wide_legacy_reservations_across_chats(self):
        self.seed()
        self.terminal.command("/session s_legacy")
        maximum = self.store.latest_event_seq()
        other = SessionStore(self.store.path, "other")
        other.open_history()
        other_run = ActiveRun(other)
        self.assertGreater(other_run.event_seq, maximum)
        first = other_run.emit("user", "user", text="new")
        run = ActiveRun(self.store)
        second = run.emit("user", "user", text="new main")
        self.assertGreater(second["event_seq"], first["event_seq"])

    async def test_changed_indexed_source_is_rejected_not_silently_replayed(self):
        self.seed()
        self.terminal.command("/session s_legacy")
        self.write("chat_history.jsonl", [{"chat_id": "main", "query_content": "tampered", "answer_content": "tampered"}])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.history("chat_history.jsonl")

    async def test_recovery_failure_does_not_switch_or_claim_ready(self):
        self.seed()
        self.terminal.new()
        active = self.terminal.store
        with patch.object(SessionStore, "_write", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.terminal.command("/open s_legacy main")
        self.assertIs(self.terminal.store, active)

    async def test_model_history_query_uses_old_context_without_extra_permission(self):
        self.seed()
        self.terminal.command("/session s_legacy")
        token = ACTIVE_RUN.set(self.terminal.run)
        try:
            result = await read_history_chat(str(self.store.path), "main")
        finally:
            ACTIVE_RUN.reset(token)
        self.assertEqual([r["answer_content"] for r in result], ["OLD ANSWER"])

    async def test_rejected_task_never_becomes_completed_outcome(self):
        self.seed()
        self.terminal.command("/session s_legacy")
        self.assertNotIn(1, self.terminal.run.bundle["agent"].task_outcomes)
        self.assertFalse(self.terminal.run.bundle["agent"].task_completed_tool_calls)
        self.assertFalse(self.terminal.run.bundle["agent"].staging_root)

    async def test_single_writer_blocks_legacy_bootstrap(self):
        with single_writer(self.store.path):
            with self.assertRaises(RuntimeError):
                self.store.open_history()

    async def test_saved_chat_fills_missing_timeline_question_without_duplicate_answer(self):
        self.write("timeline.jsonl", [{"chat_id": "main", "request_id": "r1", "event_seq": 1,
                                      "kind": "answer", "answer": "A1"}])
        self.write("chat_history.jsonl", [{"chat_id": "main", "request_id": "r1", "query_content": "Q1", "answer_content": "A1"}])
        self.terminal.command("/session s_legacy")
        context = conversation_context(self.store, "main")
        self.assertEqual([r["content"] for r in context], ["Q1", "A1"])

    async def test_legacy_markdown_is_filtered_by_supervisor_identity_after_rewind(self):
        from State.task_summary import task_summary_path, read_task_summary_context
        self.seed()
        _atomic_write(task_summary_path(str(self.store.path), "main"),
            "<!-- task-summary task_id=0 supervisor_seq=1 -->\nVALID ZERO\n"
            "<!-- task-summary task_id=1 supervisor_seq=2 -->\nFUTURE ONE\n")
        self.terminal.command("/session s_legacy")
        self.terminal.command("/rewind task 1")
        token = ACTIVE_RUN.set(self.terminal.run)
        try:
            context = read_task_summary_context(str(self.store.path), "main", 1)
        finally:
            ACTIVE_RUN.reset(token)
        self.assertIn("VALID ZERO", json.dumps(context))
        self.assertNotIn("FUTURE ONE", json.dumps(context))

    async def test_raw_tool_fact_query_obeys_legacy_cutoff(self):
        from State.save_tool_result import read_tool_result_record
        self.seed()
        self.terminal.command("/session s_legacy")
        event = next(e for e in self.store.history("timeline.jsonl") if e.get("file") == "tool_results.jsonl")
        self.terminal.command(f"/rewind {event['event_seq']}")
        token = ACTIVE_RUN.set(self.terminal.run)
        try:
            self.assertEqual(read_tool_result_record(str(self.store.path), "main", 10)["status"], "not_found")
        finally:
            ACTIVE_RUN.reset(token)

        self.terminal.command("/open s_legacy main")
        token = ACTIVE_RUN.set(self.terminal.run)
        try:
            self.assertEqual(read_tool_result_record(str(self.store.path), "main", 10)["status"], "success")
        finally:
            ACTIVE_RUN.reset(token)

    async def test_task_only_history_discovers_its_actual_chat(self):
        self.write("executor_history.jsonl", [{"chat_id": "001", "task_id": 0, "seq": 1,
                   "record_type": "executor_turn", "target": "TASK WITHOUT FINAL CHAT", "description": "unfinished"}])
        self.terminal.command("/session s_legacy")
        self.assertEqual(self.terminal.store.chat_id, "001")
        self.assertEqual(self.terminal.run.bundle["agent"].task_list, ["TASK WITHOUT FINAL CHAT"])
