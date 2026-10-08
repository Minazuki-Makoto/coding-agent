import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from AgentLoop.executor_loop import _remember
from AgentLoop.supervisor_loop import _save_supervisor_turn
from MCP_functions.System_Files.Files_function.write_in import _atomic_write
from State.history_browser import branch_choices, description_history
from State.session_checkpoint import SessionStore, ActiveRun, ACTIVE_RUN, rows
from terminal_cli import Terminal, parser
from terminal_render import Renderer, RENDERER, show_description
from terminal_select import choose_option
from AgentLoop.agent import AgentState, SupervisorState, ExecutorState, SupervisorEvaluationState

TEST_TEMP_ROOT = os.environ.get("AGENT_LOOP_TEST_TEMP")


def make_states(directory):
    agent = AgentState(session_address=str(directory), chat_id="001", user_query="test",
                       task_list=["test task"], now_target="test task")
    supervisor = SupervisorState(session_address=str(directory), chat_id="001", target="test task")
    executor = ExecutorState(session_address=str(directory), chat_id="001")
    evaluation = SupervisorEvaluationState(chat_id="001")
    return agent, supervisor, executor, evaluation


class SessionMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = []
        self.terminal = Terminal(parser().parse_args([
            "--state-dir", str(self.root / "state"), "--settings-file", str(self.root / "settings.json"),
            "--color", "never"]), input_function=lambda _: self.fail("unexpected input"), output=self.output.append)

    def seed(self, session="s_old", chat="main", branch="main", updated="2026-10-08T10:00:00"):
        store = SessionStore(self.terminal.state_dir / session, chat)
        store.initialize()
        if branch != "main":
            store._write("branches.json", {"main": {"parent": None, "cutoff": None},
                         branch: {"parent": "main", "cutoff": 1}})
        agent, supervisor, executor, evaluation = make_states(str(store.path))
        for state in (agent, supervisor, executor, evaluation):
            state.chat_id = chat
        agent.branch_id = branch
        run = ActiveRun(store, branch)
        run.bundle = dict(agent=agent, supervisor=supervisor, executor=executor, evaluation=evaluation)
        run.emit("user", "user", text="OLD QUESTION " + chat)
        run.emit("answer", "supervisor", answer="OLD ANSWER " + chat)
        run.checkpoint("completed")
        document = json.loads((store.directory / "checkpoints" / (branch + ".json")).read_text(encoding="utf-8"))
        document["updated_at"] = updated
        store._write("checkpoints/" + branch + ".json", document)
        return store

    def test_session_picker_auto_loads_history_tasks_without_chat_prompt_or_execution(self):
        store = self.seed()
        with patch("terminal_cli.choose_option", return_value=0) as choice:
            self.terminal.command("/session")
        choice.assert_called_once()
        self.assertEqual(self.terminal.store.path, store.path)
        self.assertEqual(self.terminal.status, "completed")
        body = "\n".join(self.output)
        self.assertIn("OLD QUESTION", body)
        self.assertIn("OLD ANSWER", body)
        self.assertIn("task=0", body)
        self.assertFalse(self.terminal.run.resume)

    def test_session_id_auto_chooses_latest_chat_not_a_chat_menu(self):
        self.seed(chat="main", updated="2026-10-07T10:00:00")
        self.seed(chat="second", updated="2026-10-08T10:00:00")
        with patch("terminal_cli.choose_option", side_effect=AssertionError("no chat selection")):
            self.terminal.command("/session s_old")
        self.assertEqual(self.terminal.store.chat_id, "second")

    def test_multiple_branches_have_branch_menu_and_prefix_is_respected(self):
        store = self.seed()
        self.seed(branch="b_old")
        with patch("terminal_cli.choose_option", return_value=1) as choice:
            self.terminal.command("/session s_old")
        choice.assert_called_once()
        self.assertEqual(self.terminal.run.branch, "b_old")
        self.assertEqual(self.terminal.store.path, store.path)

    def test_cancel_at_either_menu_leaves_active_session_unchanged(self):
        self.seed()
        self.terminal.new()
        active = self.terminal.store
        with patch("terminal_cli.choose_option", return_value=None):
            self.terminal.command("/session")
        self.assertIs(self.terminal.store, active)
        self.seed(branch="b_old")
        with patch("terminal_cli.choose_option", return_value=None):
            self.terminal.command("/session s_old")
        self.assertIs(self.terminal.store, active)

    def test_legacy_menu_opens_chat_for_followup_with_context(self):
        store = SessionStore(self.terminal.state_dir / "s_legacy")
        _atomic_write(store.path / "chat_history.jsonl", '{"chat_id":"main","query_content":"legacy question","answer_content":"legacy answer"}\n')
        self.terminal.command("/session s_legacy")
        self.assertEqual(self.terminal.store.path, store.path)
        self.assertEqual(self.terminal.status, "ready")
        self.assertIn("legacy answer", "\n".join(self.output))
        self.assertTrue((store.directory / "checkpoints" / "main.json").exists())

    def test_branch_enumeration_never_initializes_or_loads_state(self):
        store = self.seed()
        before = {str(p): p.stat().st_mtime_ns for p in store.path.rglob("*")}
        options = branch_choices(self.terminal.state_dir)
        self.assertEqual(options[0]["chat"], "main")
        self.assertTrue(options[0]["resumable"])
        self.assertEqual(before, {str(p): p.stat().st_mtime_ns for p in store.path.rglob("*")})

    def test_startup_restore_uses_picker_and_bad_selection_does_not_crash(self):
        self.terminal.input = Mock(side_effect=["r", "/exit"])
        with patch.object(self.terminal, "select_session", side_effect=ValueError("bad session")) as select:
            self.terminal.loop()
        select.assert_called_once_with()
        self.assertIn("agent> ", str(self.terminal.input.call_args_list))
        self.assertIn("bad session", "\n".join(self.output))


class GeneralChoiceTests(unittest.TestCase):
    def test_arrow_tab_confirm_and_cancel(self):
        for keys, expected in ((["\r"], 0), (["down", "\r"], 1), (["up", "\r"], 2),
                               (["\t", "\t", "\r"], 2), (["down", "\x1b"], None)):
            reader = iter(keys)
            result = choose_option(Renderer(lambda _: None, "never"), ["a", "b", "c"],
                                   key_reader=lambda: next(reader), stream=io.StringIO())
            self.assertEqual(result, expected)

    def test_fallback_invalid_number_retries_and_blank_eof_cancel(self):
        for answers, expected in ((["/chats abc", "99", "2"], 1), ([""], None)):
            reader = iter(answers)
            self.assertEqual(choose_option(Renderer(lambda _: None, "never"), ["a", "b"],
                            input_function=lambda _: next(reader), stream=io.StringIO()), expected)
        self.assertIsNone(choose_option(Renderer(lambda _: None, "never"), ["a"],
                          input_function=Mock(side_effect=EOFError()), stream=io.StringIO()))


class DescriptionDisplayTests(unittest.TestCase):
    def test_live_descriptions_role_styles_sanitization_and_plain_final_answer(self):
        output = []
        renderer = Renderer(output.append, "always")
        token = RENDERER.set(renderer)
        try:
            show_description("executor", "执行\x1b[31m正文", task_id=0)
            show_description("supervisor", "监督历史正文", phase="decision", seq=2)
            renderer.say("FINAL ANSWER")
        finally:
            RENDERER.reset(token)
        if renderer.color:
            self.assertTrue(output[0].startswith("\x1b[2;37m"))
            self.assertTrue(output[1].startswith("\x1b[2;32m"))
        self.assertIn("\\x1b[31m", output[0])
        self.assertEqual(output[2], "FINAL ANSWER")

    def test_validated_executor_and_supervisor_descriptions_are_displayed_without_changing_records(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory)
            output = []
            token = RENDERER.set(Renderer(output.append, "never"))
            try:
                _remember(executor, "EXEC DESCRIPTION")
                _save_supervisor_turn(agent, supervisor, "evaluation", "SUP DESCRIPTION")
            finally:
                RENDERER.reset(token)
            self.assertEqual(executor.memory_window, ["EXEC DESCRIPTION"])
            self.assertIn("EXEC DESCRIPTION", "\n".join(output))
            self.assertIn("SUP DESCRIPTION", "\n".join(output))
            self.assertEqual(rows(Path(directory) / "supervisor_history.jsonl")[0]["description"], "SUP DESCRIPTION")

    def test_description_history_filters_chat_branch_events_and_retains_executor_rounds(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            store = SessionStore(directory, "main")
            store.initialize()
            store._write("branches.json", {"main": {"parent": None, "cutoff": None}, "b": {"parent": "main", "cutoff": 2}})
            records = [dict(chat_id="main", branch_id="main", event_seq=1, seq=1, record_type="executor_turn",
                            description="round 2", output_content="round 1\nround 2")]
            _atomic_write(store.path / "executor_history.jsonl", json.dumps(records[0]) + "\n")
            records = [dict(chat_id="main", branch_id="main", event_seq=2, description="GOOD", record_type="evaluation_turn"),
                       dict(chat_id="main", branch_id="main", event_seq=3, description="FUTURE", record_type="decision_turn"),
                       dict(chat_id="other", branch_id="main", event_seq=1, description="OTHER", record_type="decision_turn"),
                       dict(chat_id="main", branch_id="b", event_seq=4, description="DUP EVENT", record_type="decision_tool_event")]
            _atomic_write(store.path / "supervisor_history.jsonl", "".join(json.dumps(r) + "\n" for r in records))
            history = description_history(store, "b")
            self.assertEqual([r["description"] for r in history], ["round 1\nround 2", "GOOD"])
            self.assertEqual([r["actor"] for r in history], ["executor", "supervisor"])

    def test_description_command_uses_role_colors_without_switching(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            root = Path(directory)
            output = []
            terminal = Terminal(parser().parse_args(["--state-dir", str(root / "state"),
                                "--settings-file", str(root / "settings.json"), "--color", "always"]), output=output.append)
            terminal.new()
            active = terminal.store
            _atomic_write(active.path / "supervisor_history.jsonl", '{"chat_id":"main","seq":1,"record_type":"decision_turn","description":"HISTORY"}\n')
            terminal.command("/descriptions --actor supervisor")
            self.assertIs(terminal.store, active)
            self.assertIn("HISTORY", "\n".join(output))
