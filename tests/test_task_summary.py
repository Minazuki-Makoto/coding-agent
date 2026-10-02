import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from AgentLoop.agent import (
    RuntimeConfig, SupervisorDescriptionHistory, TaskOutcome, main, state_init,
)
from AgentLoop.executor_loop import ExecutorOutPut, run_executor_loop
from AgentLoop.supervisor_loop import (
    InitialGuidance, PlanningResult, SupervisorDecisionResult,
    SupervisorEvaluation, SupervisorHistorySummary,
    _persist_task_summary, making_next_plan_loop, run_supervisor_loop,
    summarize_supervisor_descriptions,
)
from State.task_summary import (
    append_task_summary_record, read_task_summary_context,
    render_task_summary_record, task_summary_path,
)
from State.save_supervision_state_history import save_supervisor_state_history
from Context.mcp_supervisor_tools import read_task_summary
from MCP_functions.tool_registry import ToolExecutionResult
from test_agent_loop import (
    FakeRegistry, QueueChat, TEST_TEMP_ROOT, json_message, make_states,
    model_response, openai_tool, task_summary_payload,
)


def record(task_id, seq, cumulative):
    return render_task_summary_record(
        task_id=task_id, supervisor_seq=seq, target=f"target-{task_id}",
        current_turn_summary="本轮已检查", cumulative_task_summary=cumulative,
        current_verification_summary="按原始条件检查了真实证据",
        corrected_or_invalidated=[], remaining_work=[], decision_summary="继续",
        next_step="检查下一步", evidence_references=[], task_outcome=None,
    )


def decision_response(completed=False, **draft_changes):
    draft = task_summary_payload(completed=completed)
    draft.update(draft_changes)
    return model_response(json_message(
        is_next_target=completed, is_finished=True, description="已作出本轮决定",
        next_executor_target="继续必要检查" if not completed else "",
        task_summary=draft,
    ))


def evaluation_response(passed=True):
    return model_response(json_message(
        is_passed=passed, description="检查实际测试结果及原始完成条件",
        is_error=False, reason="" if passed else "本轮测试失败", is_finished=True,
    ))


class SummaryRegistry(FakeRegistry):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory

    async def call(self, role, tool_name, arguments):
        if tool_name != "read_task_summary":
            return await super().call(role, tool_name, arguments)
        self.calls.append((role, tool_name, arguments))
        output = await read_task_summary(
            self.directory, "001", arguments["task_id"],
            arguments.get("include_other_tasks", False),
        )
        return ToolExecutionResult(tool_name, "supervisor_memory", True, None, "", output)


class TaskSummaryPersistenceTests(unittest.TestCase):
    def test_chat_paths_are_isolated_for_case_whitespace_and_path_characters(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            identifiers = ["Chat", "chat", "chat ", "chat/child", "../chat"]
            paths = [task_summary_path(directory, chat_id) for chat_id in identifiers]
            self.assertEqual(len({str(path).lower() for path in paths}), len(paths))
            for path in paths:
                self.assertTrue(path.is_relative_to(Path(directory).resolve()))

    def test_shared_file_append_chat_isolation_latest_record_and_dedup(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            first = record(0, 1, "旧成果 A")
            second = record(0, 2, "累计有效成果 A 和 B")
            third = record(1, 3, "其他 task 成果 C")
            append_task_summary_record(directory, "001", 0, 1, first)
            append_task_summary_record(directory, "001", 0, 2, second)
            append_task_summary_record(directory, "001", 1, 3, third)
            duplicate = append_task_summary_record(directory, "001", 0, 2, second)
            append_task_summary_record(directory, "002", 0, 1, record(0, 1, "独立成果 D"))
            saved = task_summary_path(directory, "001").read_text(encoding="utf-8")
            self.assertIn(first, saved)
            self.assertIn(second, saved)
            self.assertIn(third, saved)
            self.assertEqual(saved.count("supervisor_seq=2 -->"), 1)
            self.assertEqual(duplicate["operation"], "skip_duplicate")
            self.assertNotIn("独立成果 D", saved)
            self.assertNotEqual(task_summary_path(directory, "001"), task_summary_path(directory, "002"))
            context = read_task_summary_context(directory, "001", 0)
            self.assertIn("累计有效成果 A 和 B", context["current_task_latest"])
            self.assertNotIn("旧成果 A", context["current_task_latest"])
            self.assertEqual(context["other_tasks_latest"], [])
            context = read_task_summary_context(directory, "001", 1)
            self.assertEqual(len(context["other_tasks_latest"]), 1)
            self.assertEqual(context["other_tasks_latest"][0]["task_id"], 0)
            self.assertEqual(context["other_tasks_latest"][0]["supervisor_seq"], 2)

    def test_save_failure_preserves_previous_summary_and_retry_can_append(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            append_task_summary_record(directory, "001", 0, 1, record(0, 1, "A"))
            path = task_summary_path(directory, "001")
            previous = path.read_text(encoding="utf-8")
            with patch("State.task_summary.write_in", return_value={"status": "error", "message": "disk full"}):
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    append_task_summary_record(directory, "001", 0, 2, record(0, 2, "A+B"))
            self.assertEqual(path.read_text(encoding="utf-8"), previous)
            append_task_summary_record(directory, "001", 0, 2, record(0, 2, "A+B"))
            self.assertIn(previous.rstrip(), path.read_text(encoding="utf-8"))

    def test_resume_continues_supervisor_seq_for_the_correct_chat(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            _, supervisor, _, _ = make_states(directory)
            supervisor.supervisor_seq = 12
            save_supervisor_state_history(supervisor, "decision_turn")
            supervisor.chat_id = "002"
            supervisor.supervisor_seq = 99
            save_supervisor_state_history(supervisor, "decision_turn")
            config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)
            self.assertEqual(state_init(directory, "001", config=config).supervisor_seq, 12)
            self.assertEqual(state_init(directory, "003", config=config).supervisor_seq, 0)

    def test_all_description_models_accept_overlength_without_truncation(self):
        description = "长" * 7001
        models = [
            (ExecutorOutPut, {"is_finished": True}),
            (PlanningResult, {"task_list": ["task"]}),
            (InitialGuidance, {"executor_guidance": "execute", "completion_criteria": "check"}),
            (SupervisorEvaluation, {}),
            (SupervisorDecisionResult, {"is_next_target": False}),
            (SupervisorHistorySummary, {}),
        ]
        for model, extra in models:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.model_validate({"description": description, **extra}).description, description)
                with self.assertRaises(ValueError):
                    model.model_validate({"description": "   ", **extra})

    def test_fallback_can_exceed_3000_and_keeps_two_complete_recent_entries(self):
        history = SupervisorDescriptionHistory()
        for seq in range(3):
            history.append_turn(seq, 0, "task", "decision", str(seq) * 2800)
        summary = history.fallback_summary()
        self.assertGreater(len(summary), 3000)
        self.assertLessEqual(len(summary), 6000)
        self.assertIn("1" * 2800, summary)
        self.assertIn("2" * 2800, summary)
        self.assertNotIn("0" * 2800, summary)


class TaskSummaryLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_tasks_in_main_share_one_markdown_with_original_task_ids(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            async def plan(query, agent, chat_function, client, registry, provider, tools, supervisor, **kwargs):
                agent.task_list = ["task-0", "task-1"]
                agent.now_target = "task-0"
                supervisor.executor_plan = "execute task-0"

            async def execute(*args):
                args[8].output_content = f"实际结果-{args[0].now_task_id}"
                args[8].is_finished = True

            chat = QueueChat([
                evaluation_response(), decision_response(completed=True, cumulative_task_summary="任务0累计成果"),
                evaluation_response(), decision_response(completed=True, cumulative_task_summary="任务1累计成果"),
                model_response(json_message(description="两个任务已经分别验收并保存摘要")),
            ])
            config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)
            with (
                patch("AgentLoop.agent.load_runtime_config", return_value=config),
                patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                patch("AgentLoop.agent._build_model_client", return_value=object()),
                patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat}),
                patch("AgentLoop.agent.supervisor_making_plan", new=plan),
                patch("AgentLoop.agent.run_executor_loop", new=execute),
            ):
                result = await main("query", directory, "001")
            self.assertEqual(result["status"], "completed")
            paths = list(Path(directory).rglob("task_summary.md"))
            self.assertEqual(paths, [task_summary_path(directory, "001")])
            saved = paths[0].read_text(encoding="utf-8")
            self.assertIn("task_id=0 supervisor_seq=2", saved)
            self.assertIn("task_id=1 supervisor_seq=4", saved)
            self.assertNotIn("task_id=2", saved)
            self.assertIn("任务0累计成果", saved)
            self.assertIn("任务1累计成果", saved)
            second_request = json.loads(chat.calls[3]["query"][-1]["content"])
            self.assertEqual(second_request["historical_auxiliary_summary"]["status"], "not_loaded")
            self.assertTrue(second_request["historical_auxiliary_summary"]["available"])
            self.assertNotIn("任务0累计成果", json.dumps(second_request, ensure_ascii=False))
            self.assertEqual(second_request["completed_task_outcomes"][0]["task_id"], 0)

    async def test_semantically_invalid_summary_retries_without_reexecuting_supervisor_tool(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            evaluation.is_executor_passed = True
            # A valid JSON with a completed outcome while still continuing the task is invalid.
            chat = QueueChat([
                model_response(tool=[openai_tool("read_files_content_tool", {"file_address": "target"})]),
                decision_response(accepted_summary="尚未最终通过却声称完成"),
                decision_response(),
            ])
            registry = FakeRegistry()
            result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], registry, supervisor, evaluation, executor)
            self.assertFalse(result.is_next_target)
            self.assertEqual(len(registry.calls), 1)
            self.assertEqual(len(chat.calls), 3)
            self.assertNotIn(0, agent.task_outcomes)
            self.assertEqual(len(Path(directory, "tool_results.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            self.assertIn("accepted_summary", chat.calls[2]["query"][-1]["content"])

    async def test_two_round_cumulative_outcome_and_update_render_write_order(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            executor.output_content = "本轮确认 A"
            first = QueueChat([evaluation_response(), decision_response(cumulative_task_summary="已确认 A；B 待检查")])
            await run_supervisor_loop(agent, "chatgpt", object(), first, [], FakeRegistry(), [], supervisor, evaluation, executor)
            self.assertNotIn(0, agent.task_outcomes)
            prior = task_summary_path(directory, "001").read_text(encoding="utf-8")
            executor.output_content = "本轮只检查并确认 B"
            second = QueueChat([
                evaluation_response(),
                model_response(tool=[openai_tool("read_task_summary", {"task_id": 0})]),
                decision_response(
                completed=True, cumulative_task_summary="累计确认 A 和 B",
                accepted_summary="多轮累计确认 A 和 B", verification_summary="检查 A 与 B 的原始条件和真实证据均满足",
            )])
            events = []

            def inspect_render(**values):
                events.append("render")
                self.assertEqual(agent.now_task_id, 0)
                self.assertEqual(agent.task_cumulative_summaries[0], "累计确认 A 和 B")
                self.assertEqual(agent.task_outcomes[0].accepted_summary, "多轮累计确认 A 和 B")
                self.assertEqual(supervisor.task_summary["cumulative_task_summary"], "累计确认 A 和 B")
                self.assertEqual(values["task_id"], 0)
                self.assertEqual(values["supervisor_seq"], supervisor.supervisor_seq)
                return render_task_summary_record(**values)

            def inspect_append(**values):
                events.append("write")
                self.assertEqual(events, ["render", "write"])
                self.assertEqual(values["task_id"], 0)
                return append_task_summary_record(**values)

            with patch("AgentLoop.supervisor_loop.render_task_summary_record", side_effect=inspect_render), patch("AgentLoop.supervisor_loop.append_task_summary_record", side_effect=inspect_append):
                result = await run_supervisor_loop(agent, "chatgpt", object(), second, [], SummaryRegistry(directory), [], supervisor, evaluation, executor)
            saved = task_summary_path(directory, "001").read_text(encoding="utf-8")
            self.assertIn(prior.rstrip(), saved)
            self.assertTrue(result.is_next_target)
            self.assertEqual(agent.now_task_id, 0)  # advancement belongs to AgentLoop
            latest = read_task_summary_context(directory, "001", 0)["current_task_latest"]
            self.assertIn("累计确认 A 和 B", latest)
            self.assertIn("多轮累计确认 A 和 B", latest)
            self.assertNotEqual(agent.task_outcomes[0].accepted_summary, executor.output_content)
            request = json.loads(second.calls[1]["query"][-1]["content"])
            self.assertEqual(request["current_turn_latest_result"]["executor_summary"], executor.output_content)
            self.assertEqual(request["historical_auxiliary_summary"]["status"], "not_loaded")
            self.assertNotIn("已确认 A；B 待检查", json.dumps(request, ensure_ascii=False))
            queried_request = json.loads(second.calls[2]["query"][-1]["content"])
            self.assertIn("已确认 A；B 待检查", queried_request["memory_query_results"][0]["result"])
            decisions = [json.loads(line) for line in Path(directory, "supervisor_history.jsonl").read_text(encoding="utf-8").splitlines() if json.loads(line)["record_type"] == "decision_turn"]
            self.assertIsNone(decisions[0]["task_outcome"])
            self.assertEqual(decisions[-1]["task_outcome"]["accepted_summary"], "多轮累计确认 A 和 B")
            self.assertEqual(decisions[-1]["task_summary"]["cumulative_task_summary"], "累计确认 A 和 B")
            seq = supervisor.supervisor_seq
            agent.task_summary_saved_seqs.clear()  # simulate retry after losing in-memory acknowledgement
            _persist_task_summary(agent, supervisor, evaluation, executor, result, seq)
            self.assertEqual(task_summary_path(directory, "001").read_text(encoding="utf-8"), saved)

    async def test_latest_regression_corrects_old_summary_preserves_unaffected_work_and_vetoes(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.now_task_id = 1
            agent.now_target = "当前任务"
            agent.task_list = ["前序任务", "当前任务"]
            agent.max_supervision_times = 3
            append_task_summary_record(directory, "001", 0, 1, record(0, 1, "依赖成果 C"))
            append_task_summary_record(directory, "001", 1, 2, record(1, 2, "A 有效，B 验收通过"))
            agent.supervisor_seq = 2
            executor.output_content = "本轮真实测试 B 失败，未修改 A"
            executor.is_finished = True
            chat = QueueChat([
                evaluation_response(False),
                model_response(tool=[openai_tool("read_task_summary", {"task_id": 1, "include_other_tasks": True})]),
                decision_response(completed=True),  # must not override evaluation veto
                decision_response(
                    cumulative_task_summary="A 成果仍有效；B 回归失败，需要修复",
                    corrected_or_invalidated=["旧的 B 验收通过结论被本轮失败证据推翻"],
                    current_verification_summary="本轮 B 测试失败，不能最终通过",
                    remaining_work=["修复并重新验证 B"],
                ),
            ])
            result = await run_supervisor_loop(agent, "chatgpt", object(), chat, [], SummaryRegistry(directory), [], supervisor, evaluation, executor)
            self.assertFalse(result.is_next_target)
            self.assertNotIn(1, agent.task_outcomes)
            self.assertEqual(agent.now_task_id, 1)
            request = json.loads(chat.calls[1]["query"][-1]["content"])
            self.assertIn("B 失败", request["current_turn_latest_result"]["executor_summary"])
            self.assertFalse(request["current_turn_supervisor_evaluation"]["is_passed"])
            self.assertEqual(request["historical_auxiliary_summary"]["status"], "not_loaded")
            queried_request = json.loads(chat.calls[2]["query"][-1]["content"])
            self.assertIn("B 验收通过", queried_request["memory_query_results"][0]["result"])
            self.assertIn("依赖成果 C", queried_request["memory_query_results"][0]["result"])
            latest = read_task_summary_context(directory, "001", 1)["current_task_latest"]
            self.assertIn("A 成果仍有效", latest)
            self.assertIn("被本轮失败证据推翻", latest)
            self.assertNotIn("已完成 TaskOutcome", latest)
            self.assertIn("evaluation 未通过", json.dumps(chat.calls[3]["query"], ensure_ascii=False))

    async def test_task_outcome_lengths_retry_without_truncating_or_premature_outcome(self):
        for field, constant in [("accepted_summary", "TASK_OUTCOME_ACCEPTED_MAX_CHARS"), ("verification_summary", "TASK_OUTCOME_VERIFICATION_MAX_CHARS")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                agent, supervisor, executor, evaluation = make_states(directory)
                evaluation.is_executor_passed = True
                chat = QueueChat([decision_response(completed=True, **{field: "x" * 51}), decision_response(completed=True, **{field: "关键事实保留后的完整重写"})])
                with patch("AgentLoop.supervisor_loop." + constant, 50):
                    result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
                self.assertTrue(result.is_next_target)
                self.assertEqual(getattr(agent.task_outcomes[0], field), "关键事实保留后的完整重写")
                self.assertEqual(len(chat.calls), 2)
                request = json.loads(chat.calls[1]["query"][-1]["content"])
                self.assertIsNone(request["current_task_outcome"])
                self.assertIn(field, "\n".join(request["memory_window"]))
                self.assertEqual(request["task_outcome_length_limits"][field], 50)

    async def test_outcome_retry_exhaustion_keeps_old_cumulative_work_without_formal_outcome(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.task_cumulative_summaries[0] = "既有已确认成果 A"
            evaluation.is_executor_passed = True
            chat = QueueChat([decision_response(completed=True, accepted_summary="x" * 51)] * 2)
            with patch("AgentLoop.supervisor_loop.TASK_OUTCOME_ACCEPTED_MAX_CHARS", 50):
                result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertFalse(result.is_next_target)
            self.assertNotIn(0, agent.task_outcomes)
            latest = read_task_summary_context(directory, "001", 0)["current_task_latest"]
            self.assertIn("既有已确认成果 A", latest)
            self.assertNotIn("x" * 51, latest)

    async def test_long_executor_tool_summary_is_saved_completely_without_retry(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory)
            description = "完整实际结果" * 1300
            chat = QueueChat([model_response(tool=[openai_tool()]), model_response(json_message(description=description, is_finished=True))])
            registry = FakeRegistry()
            await run_executor_loop(agent, "chatgpt", object(), chat, [], registry, [], supervisor, executor)
            self.assertEqual(len(chat.calls), 2)
            self.assertEqual(len(registry.calls), 1)
            self.assertFalse(executor.is_error)
            self.assertEqual(executor.description, description)
            self.assertEqual(executor.output_content, description)
            self.assertEqual(executor.tool_summaries[0].description, description)
            saved = json.loads(Path(directory, "executor_history.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(saved["description"], description)
            self.assertEqual(saved["output_content"], description)

    async def test_long_supervisor_decision_and_final_description_do_not_retry(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            executor.output_content = "实际结果"
            description = "验收描述" * 1100
            chat = QueueChat([evaluation_response(), model_response(json_message(is_next_target=False, is_finished=True, description=description)), model_response(json_message(description=description))])
            await run_supervisor_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), [], supervisor, evaluation, executor)
            summary = await summarize_supervisor_descriptions(agent, supervisor, "chatgpt", object(), chat, "blocked", "", "仍需继续")
            self.assertEqual(summary, description)
            self.assertEqual(len(chat.calls), 3)
            self.assertIn(description, task_summary_path(directory, "001").read_text(encoding="utf-8"))

    async def test_decision_receives_existing_task_outcomes_as_auxiliary_context(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.task_outcomes[0] = TaskOutcome(0, "task", "旧有效成果", "旧验证证据")
            executor.output_content = "新的实际检查结果"
            evaluation.output_content = "新的验收说明"
            evaluation.is_executor_passed = True
            chat = QueueChat([decision_response()])
            await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
            request = json.loads(chat.calls[0]["query"][-1]["content"])
            self.assertEqual(request["current_task_outcome"]["accepted_summary"], "旧有效成果")
            self.assertEqual(request["completed_task_outcomes"][0]["verification_summary"], "旧验证证据")
            self.assertEqual(request["current_turn_supervisor_evaluation"]["description"], "新的验收说明")

    async def test_failed_markdown_write_blocks_main_before_next_task(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            captured = []
            executions = []

            async def plan(query, agent, chat_function, client, registry, provider, tools, supervisor, **kwargs):
                captured.append(agent)
                agent.task_list = ["task-0", "task-1"]
                agent.now_target = "task-0"
                supervisor.executor_plan = "execute task-0"

            async def execute(*args):
                executions.append(args[0].now_task_id)
                args[8].output_content = "本轮有效执行结果"
                args[8].is_finished = True

            chat = QueueChat([evaluation_response(), decision_response(completed=True), model_response(json_message(description="摘要文件保存失败，当前任务没有推进"))])
            config = RuntimeConfig("chatgpt", "chatgpt", None, None, "fake", "fake", 0.0, None, None)
            with (
                patch("AgentLoop.agent.load_runtime_config", return_value=config),
                patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                patch("AgentLoop.agent._build_model_client", return_value=object()),
                patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                patch("AgentLoop.agent._chat_map", return_value={"chatgpt": chat}),
                patch("AgentLoop.agent.supervisor_making_plan", new=plan),
                patch("AgentLoop.agent.run_executor_loop", new=execute),
                patch("State.task_summary.write_in", return_value={"status": "error", "message": "disk full"}),
            ):
                result = await main("query", directory, "001")
            self.assertEqual(result["status"], "blocked")
            self.assertIn("disk full", result["block_reason"])
            self.assertEqual(executions, [0])
            self.assertEqual(captured[0].now_task_id, 0)
            self.assertEqual(captured[0].task_summary_saved_seqs, set())
            self.assertFalse(task_summary_path(directory, "001").exists())


if __name__ == "__main__":
    unittest.main()
