import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_agent_loop import make_states, QueueChat, FakeRegistry, model_response, json_message, openai_tool
from AgentLoop.executor_loop import run_executor_loop
from AgentLoop.agent import TaskOutcome, RuntimeConfig
from MCP_functions.tool_registry import ToolExecutionResult
from State.session_checkpoint import SessionStore, ActiveRun, ACTIVE_RUN, encode, decode, rows, single_writer
from State.save_tool_result import save_tool_result
from State.task_summary import append_task_summary_record, read_task_summary_context
from terminal_cli import Terminal, parser, split_command

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setup_run(self, directory):
        store = SessionStore(directory, "001")
        store.initialize()
        agent, supervisor, executor, evaluation = make_states(directory, max_steps=4)
        agent.request_id = "r_test"
        run = ActiveRun(store)
        run.bundle = {"agent": agent, "supervisor": supervisor, "executor": executor, "evaluation": evaluation}
        return run

    async def test_same_round_dedup_different_arguments_failure_retry_and_explicit_rerun(self):
        for failed, explicit in ((False, False), (True, False), (False, True)):
            with tempfile.TemporaryDirectory() as directory:
                agent, supervisor, executor, _ = make_states(directory, max_steps=3)
                if explicit:
                    agent.task_handoffs[0].instruction = "请重新执行并重新验证"
                chat = QueueChat([model_response(tool=[openai_tool(arguments={"page": 1})]),
                    model_response(json_message(description="真实结果已保存，尚未完成", is_finished=False)),
                    model_response(tool=[openai_tool(arguments={"page": 1})]),
                    model_response(json_message(description="完成", is_finished=True))])
                registry = FakeRegistry([ToolExecutionResult("fake_tool", "fake", not failed,
                    "failure" if failed else None, "", {"status": "error" if failed else "success"})])
                await run_executor_loop(agent, "chatgpt", object(), chat, [], registry, [], supervisor, executor)
                self.assertEqual(len(registry.calls), 2 if failed or explicit else 1)
            with tempfile.TemporaryDirectory() as directory:
                agent, supervisor, executor, _ = make_states(directory, max_steps=3)
                chat = QueueChat([model_response(tool=[openai_tool(arguments={"page": 1})]),
                    model_response(json_message(description="读取一页", is_finished=False)),
                    model_response(tool=[openai_tool(arguments={"page": 2})]),
                    model_response(json_message(description="完成", is_finished=True))])
                registry = FakeRegistry()
                await run_executor_loop(agent, "chatgpt", object(), chat, [], registry, [], supervisor, executor)
                self.assertEqual(len(registry.calls), 2)

    async def test_typed_checkpoint_branch_cutoff_and_summary_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.setup_run(directory)
            token = ACTIVE_RUN.set(run)
            try:
                run.bundle["agent"].task_summary_saved_seqs = {2}
                self.assertEqual(decode(encode(run.bundle))["agent"].task_summary_saved_seqs, {2})
                selected = run.boundary("evaluation", "supervisor")
                run.bundle["agent"].task_outcomes[0] = TaskOutcome(0, "target", "future", "checked")
                run.emit("answer", answer="future answer")
                append_task_summary_record(directory, "001", 0, 99,
                    "<!-- task-summary task_id=0 supervisor_seq=99 -->\nFUTURE")
                run.checkpoint("completed")
                branch = run.store.fork(selected["event_seq"])
                restored = run.store.load(branch)
                self.assertEqual(restored["phase"], "evaluation")
                self.assertEqual(restored["bundle"]["agent"].task_outcomes, {})
                self.assertNotIn("future answer", json.dumps(run.store.history("timeline.jsonl", branch)))
                fork = ActiveRun(run.store, branch)
                fork.bundle = restored["bundle"]
                ACTIVE_RUN.set(fork)
                self.assertEqual(read_task_summary_context(directory, "001", 0)["status"], "not_found")
                self.assertIn("future answer", json.dumps(run.store.history("timeline.jsonl", "main")))
            finally:
                ACTIVE_RUN.reset(token)

    async def test_committed_tool_with_stale_checkpoint_recovery_and_unknown_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.setup_run(directory)
            token = ACTIVE_RUN.set(run)
            try:
                run.boundary("executor_action", "executor")
                run.current_operation = "op"
                run.emit("tool_intent", "executor", operation_id="op", arguments={}, tool_name="fake_tool")
                save_tool_result(directory, "001", 0, "executor", 1, "execution", "fake_tool", {},
                    ToolExecutionResult("fake_tool", "fake", True, None, "", {"full": "result"}), 1)
                restored = run.store.load()
                self.assertEqual(restored["phase"], "executor_tool_summary")
                self.assertEqual(restored["unknown_operations"], [])
                self.assertEqual(restored["bundle"]["agent"].tool_result_seq, 1)
                self.assertTrue(restored["bundle"]["agent"].task_completed_tool_calls[0])
                run.emit("tool_intent", "executor", operation_id="unconfirmed")
                self.assertEqual(run.store.load()["unknown_operations"], ["unconfirmed"])
            finally:
                ACTIVE_RUN.reset(token)

    async def test_rewind_tool_summary_only_does_not_execute_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.setup_run(directory)
            token = ACTIVE_RUN.set(run)
            try:
                registry = FakeRegistry()
                chat = QueueChat([model_response(tool=[openai_tool()]),
                    model_response(json_message(description="工具完成", is_finished=True))])
                await run_executor_loop(run.bundle["agent"], "chatgpt", object(), chat, [], registry, [],
                                        run.bundle["supervisor"], run.bundle["executor"])
                summary = next(row for row in run.store.history("timeline.jsonl")
                               if row.get("kind") == "boundary" and row["phase"] == "executor_tool_summary")
                branch = run.store.fork(summary["event_seq"])
                saved = run.store.load(branch)
                fork = ActiveRun(run.store, branch)
                fork.bundle, fork.phase, fork.payload, fork.resume = saved["bundle"], saved["phase"], saved["payload"], True
                ACTIVE_RUN.set(fork)
                fresh_registry = FakeRegistry()
                new_chat = QueueChat([model_response(json_message(description="重新总结", is_finished=True))])
                await run_executor_loop(fork.bundle["agent"], "chatgpt", object(), new_chat, [], fresh_registry, [],
                                        fork.bundle["supervisor"], fork.bundle["executor"])
                self.assertEqual(len(registry.calls), 1)
                self.assertEqual(fresh_registry.calls, [])
                self.assertEqual(len(rows(Path(directory) / "tool_results.jsonl")), 1)
            finally:
                ACTIVE_RUN.reset(token)

    async def test_single_writer_and_legacy_precise_resume_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            store.initialize()
            with single_writer(directory):
                with self.assertRaises(RuntimeError):
                    with single_writer(directory):
                        pass
            with self.assertRaisesRegex(ValueError, "no precise checkpoint"):
                store.load()

    async def test_stale_supervisor_result_restores_query_refs_without_replay(self):
        from AgentLoop.supervisor_loop import _restore_stage_queries
        for phase in ("evaluation", "decision", "planning"):
            with tempfile.TemporaryDirectory() as directory:
                run = self.setup_run(directory)
                token = ACTIVE_RUN.set(run)
                try:
                    run.boundary(phase, "supervisor", rounds=0, queries=[])
                    save_tool_result(directory, "001", 0, "supervisor", 1, phase, "read_files_content_tool", {},
                        ToolExecutionResult("read_files_content_tool", "fake", True, None, "", {"content": "saved fact"}), 1)
                    saved = run.store.load()
                    self.assertEqual(saved["phase"], "evaluation" if phase == "evaluation" else phase + "_tool_summary")
                    self.assertEqual(saved["bundle"]["supervisor"].last_tool_result_ref.tool_result_seq, 1)
                    if phase == "evaluation":
                        run.bundle, run.phase, run.payload = saved["bundle"], saved["phase"], saved["payload"]
                        observed, _, count = await _restore_stage_queries(run.bundle["agent"], "evaluation")
                        self.assertIn("saved fact", observed[0]["result"])
                        self.assertEqual(count, 1)
                    self.assertEqual(len(rows(Path(directory) / "tool_results.jsonl")), 1)
                finally:
                    ACTIVE_RUN.reset(token)

    async def test_finished_action_resume_and_task_tool_rewind_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.setup_run(directory)
            token = ACTIVE_RUN.set(run)
            try:
                task_start = run.boundary("task_start")
                chat = QueueChat([model_response(tool=[openai_tool()]),
                    model_response(json_message(description="完成", is_finished=True))])
                registry = FakeRegistry()
                await run_executor_loop(run.bundle["agent"], "chatgpt", object(), chat, [], registry, [],
                                        run.bundle["supervisor"], run.bundle["executor"])
                events = run.store.history("timeline.jsonl")
                finished = [r for r in events if r["kind"] == "boundary" and r["phase"] == "executor_action"][-1]
                branch = run.store.fork(finished["event_seq"])
                saved = run.store.load(branch)
                fork = ActiveRun(run.store, branch)
                fork.bundle, fork.phase, fork.payload, fork.resume = saved["bundle"], saved["phase"], saved["payload"], True
                ACTIVE_RUN.set(fork)
                empty_chat = QueueChat([])
                fresh_registry = FakeRegistry()
                await run_executor_loop(fork.bundle["agent"], "chatgpt", object(), empty_chat, [], fresh_registry, [],
                                        fork.bundle["supervisor"], fork.bundle["executor"])
                self.assertEqual(empty_chat.calls, [])
                self.assertEqual(fresh_registry.calls, [])
                intent = next(r for r in events if r["kind"] == "tool_intent")
                for selected in (task_start, intent):
                    regenerated = run.store.load(run.store.fork(selected["event_seq"], "main"))
                    self.assertEqual(regenerated["bundle"]["agent"].task_completed_tool_calls, {})
                    self.assertNotIn(0, regenerated["bundle"]["agent"].task_outcomes)
            finally:
                ACTIVE_RUN.reset(token)

class TerminalTests(unittest.TestCase):
    def test_local_commands_unicode_windows_paths_eof_and_view_does_not_switch(self):
        self.assertEqual(split_command('/history --search "D:\\项目 目录\\a.py"')[2], 'D:\\项目 目录\\a.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "work"
            workspace.mkdir()
            args = parser().parse_args(["--workspace", str(workspace), "--state-dir", str(root / "state")])
            output = []
            terminal = Terminal(args, output=output.append)
            terminal.command("/help")
            terminal.new()
            active = terminal.store.path
            terminal.command("/sessions")
            terminal.command('/history --search "中文 空格"')
            self.assertEqual(terminal.store.path, active)
            self.assertTrue(terminal.command("/branches"))
            self.assertFalse(terminal.command("/exit"))
            with self.assertRaises(ValueError):
                terminal.command("/unknown")
            with self.assertRaises(ValueError):
                terminal.resume("../elsewhere")
            terminal.input = lambda _: (_ for _ in ()).throw(EOFError())
            terminal.loop()


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_preserves_answer_and_interruption_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            work.mkdir()
            args = parser().parse_args(["--workspace", str(work), "--state-dir", str(Path(directory) / "state")])
            terminal = Terminal(args, output=lambda _: None)
            config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)
            with (patch("AgentLoop.agent.load_runtime_config", return_value=config),
                  patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock(side_effect=RuntimeError("test startup failed")))):
                result = await terminal.ask("完整用户要求")
            self.assertEqual(result["status"], "blocked")
            saved = terminal.store.load()
            self.assertEqual(saved["status"], "blocked")
            answers = [r for r in terminal.store.history("timeline.jsonl") if r["kind"] == "answer"]
            self.assertIn("test startup failed", answers[-1]["answer"])
            async def pending(*args, **kwargs):
                await asyncio.Event().wait()
            with (patch("AgentLoop.agent.load_runtime_config", return_value=config),
                  patch("AgentLoop.agent.main", new=pending)):
                task = asyncio.create_task(terminal.ask(continuing=True))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(terminal.store.load()["status"], "interrupted")

    async def test_two_user_requests_restart_view_and_eval_decision_rewind(self):
        from AgentLoop.agent import main
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            work.mkdir()
            args = parser().parse_args(["--workspace", str(work), "--state-dir", str(Path(directory) / "state")])
            terminal = Terminal(args, output=lambda _: None)
            config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)
            def responses(number):
                return [model_response(json_message(task_list=[f"task-{number}"], description="规划")),
                    model_response(json_message(description="指导", executor_guidance="执行", completion_criteria="证据")),
                    model_response(json_message(description="执行完成", is_finished=True)),
                    model_response(json_message(description="检查了原始条件", is_passed=True, is_finished=True)),
                    model_response(json_message(description="正式验收通过", is_next_target=True, is_finished=True,
                                                final_answer=f"回答-{number}")),
                    model_response(json_message(description="请求总结"))]
            chat = QueueChat(responses(0) + responses(1))
            with (patch("AgentLoop.agent.load_runtime_config", return_value=config),
                  patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                  patch("AgentLoop.agent._build_model_client", return_value=object()),
                  patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                  patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat})):
                first = await terminal.ask("第一问原文\n包含末尾约束")
                second = await terminal.ask("第二问，利用已有成果")
                self.assertEqual(first["status"], "completed")
                self.assertEqual(second["status"], "completed")
            agent = terminal.run.bundle["agent"]
            self.assertEqual(sorted(agent.task_outcomes), [0, 1])
            self.assertEqual(agent.request_start_task_id, 1)
            self.assertEqual(agent.model_request_count, 6)
            second_plan = json.loads(chat.calls[6]["query"][-1]["content"])
            self.assertIn("第一问原文", json.dumps(second_plan, ensure_ascii=False))
            history = terminal.store.history("timeline.jsonl")
            self.assertEqual(len([r for r in history if r["kind"] == "user"]), 2)
            self.assertEqual(len([r for r in history if r["kind"] == "answer"]), 2)
            restart = Terminal(args, output=lambda _: None)
            restart.resume(terminal.store.path.name)
            restart.command('/history --search "回答"')
            self.assertEqual(restart.run.bundle["agent"].task_outcomes[1].accepted_summary, agent.task_outcomes[1].accepted_summary)
            for phase in ("evaluation", "decision"):
                selected = [r for r in history if r["kind"] == "boundary" and r["phase"] == phase and r["task_id"] == 1][0]
                branch = terminal.store.fork(selected["event_seq"])
                saved = terminal.store.load(branch)
                run = ActiveRun(terminal.store, branch)
                run.bundle, run.phase, run.payload, run.resume = saved["bundle"], saved["phase"], saved["payload"], True
                regenerated = responses(2)[3:] if phase == "evaluation" else responses(2)[4:]
                chat2 = QueueChat(regenerated)
                with (patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                      patch("AgentLoop.agent._build_model_client", return_value=object()),
                      patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                      patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat2}),
                      patch("AgentLoop.agent.run_executor_loop", new=AsyncMock(side_effect=AssertionError("must not reexecute")))):
                    result = await main(run.bundle["agent"].user_query, str(run.store.path), "main", session_run=run,
                                        runtime_config=config, sandbox_policy=terminal.policy)
                self.assertEqual(result["status"], "completed", result)
                self.assertTrue(all(row.get("branch_id") == branch or row.get("event_seq") < selected["event_seq"]
                                    for row in terminal.store.history("timeline.jsonl", branch)))
