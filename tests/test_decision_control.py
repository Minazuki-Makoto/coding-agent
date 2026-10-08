"""Offline regressions for s_e8dc8e49f56741fd; no live model/config access."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from AgentLoop.agent import EvidenceReference, RuntimeConfig, SupervisorHandoff, TaskOutcome, main
from AgentLoop.executor_loop import run_executor_loop
from AgentLoop.supervisor_loop import (
    _build_decision_message, _build_supervisor_evaluation_message,
    making_next_plan_loop, evaluation_loop,
)
from State.task_summary import task_summary_path
from test_agent_loop import (
    FakeRegistry, QueueChat, TEST_TEMP_ROOT, json_message, make_states,
    model_response, openai_tool, task_summary_payload,
)
from test_task_summary import decision_response, evaluation_response


def control_response(advance=False, finished=False, next_step="仅修正未确认职责，不重新取证", **changes):
    payload = dict(
        is_next_target=advance, is_finished=finished,
        next_executor_target=next_step, description="已决定下一步",
        next_execution_mode="synthesize",
        task_summary=task_summary_payload(completed=advance),
    )
    payload.update(changes)
    return model_response(json.dumps(payload, ensure_ascii=False))


class DecisionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_rejection_with_false_flag_returns_once_and_preserves_veto(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            chat = QueueChat([control_response()])
            result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertTrue(result.is_finished)
            self.assertFalse(result.is_next_target)
            self.assertEqual(len(chat.calls), 1)
            self.assertEqual(agent.now_task_id, 0)
            self.assertEqual(agent.task_outcomes, {})
            rows = [json.loads(line) for line in Path(directory, "supervisor_history.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(rows[0]["decision_finished"])
            self.assertFalse(json.loads(rows[0]["raw_model_response_excerpt"])["is_finished"])
            self.assertEqual(task_summary_path(directory, "001").read_text(encoding="utf-8").count("<!-- task-summary"), 1)

    async def test_normalization_cannot_bypass_evaluation_veto(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            chat = QueueChat([control_response(advance=True), control_response()])
            result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertFalse(result.is_next_target)
            self.assertEqual(agent.task_outcomes, {})
            self.assertIn("不允许推进", json.loads(chat.calls[1]["query"][-1]["content"])["memory_window"][0])

    async def test_normalized_rejection_cannot_create_accepted_outcome(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            chat = QueueChat([
                control_response(task_summary=task_summary_payload(completed=True)), control_response(),
            ])
            result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertFalse(result.is_next_target)
            self.assertEqual(agent.task_outcomes, {})
            self.assertEqual(len(chat.calls), 2)

    async def test_incomplete_no_tool_reply_has_independent_bounded_retries(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 10
            incomplete = control_response(task_summary=None, next_step="")
            chat = QueueChat([incomplete] * 10)
            with patch.dict("os.environ", {"CODING_AGENT_DECISION_CONTRACT_RETRIES": "3"}):
                result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertEqual(len(chat.calls), 3)
            self.assertEqual(supervisor.exit_reason, "decision_contract_exhausted")
            self.assertFalse(result.is_next_target)
            self.assertEqual(agent.task_outcomes, {})

    async def test_model_error_retains_actual_cause(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            chat = QueueChat([model_response("provider unavailable", status="error")])
            await making_next_plan_loop(agent, "chatgpt", object(), chat, [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertEqual(supervisor.exit_reason, "provider unavailable")
            self.assertEqual(len(chat.calls), 1)

    async def test_successful_retry_does_not_inherit_old_blocked_reason(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            supervisor.exit_reason = "decision_budget_exhausted"
            result = await making_next_plan_loop(agent, "chatgpt", object(), QueueChat([control_response()]), [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertTrue(result.is_finished)
            self.assertFalse(result.is_next_target)
            self.assertEqual(supervisor.exit_reason, "")

    async def test_summary_write_failure_is_not_swallowed_by_flag_repair(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            with patch("AgentLoop.supervisor_loop.append_task_summary_record", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    await making_next_plan_loop(agent, "chatgpt", object(), QueueChat([control_response()]), [], FakeRegistry(), supervisor, evaluation, executor)
            self.assertEqual(agent.now_task_id, 0)
            self.assertEqual(agent.task_summary_saved_seqs, set())

    async def test_multiple_call_protocol_error_has_independent_bounded_retries(self):
        for phase in ("decision", "evaluation"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
                agent, supervisor, executor, evaluation = make_states(directory)
                agent.max_supervision_times = 2
                chat = QueueChat([model_response(tool=[openai_tool(), openai_tool(call_id="second")])] * 5)
                registry = FakeRegistry()
                if phase == "decision":
                    await making_next_plan_loop(agent, "chatgpt", object(), chat, [], registry, supervisor, evaluation, executor)
                else:
                    await evaluation_loop(agent, "chatgpt", object(), chat, [], registry, [], supervisor, evaluation, executor)
                self.assertEqual(len(chat.calls), 3)
                self.assertEqual(registry.calls, [])

    async def test_synthesize_insufficient_result_is_returned_to_supervisor_once(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory, max_steps=10)
            agent.task_handoffs[0].execution_mode = "synthesize"
            chat = QueueChat([model_response(json_message(description="已有证据不足，需 Supervisor 核查 seq=1", is_finished=False))])
            registry = FakeRegistry()
            await run_executor_loop(agent, "chatgpt", object(), chat, [openai_tool()], registry, [], supervisor, executor)
            self.assertEqual(len(chat.calls), 1)
            self.assertEqual(chat.calls[0]["tools"], [])
            self.assertEqual(registry.calls, [])
            self.assertFalse(executor.is_finished)
            self.assertEqual(executor.exit_reason, "executor_synthesis_needs_review")

    def test_omitted_directory_facts_survive_both_supervisor_context_builders(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            evidence = EvidenceReference(7, "read_all_files_tool", status="success", paths=["/project/controller", "/project/mapper"])
            outcome = TaskOutcome(0, "收集证据", "已确认主模块目录结构", "目录浏览已验收", [evidence])
            agent.task_outcomes[0] = outcome
            agent.now_task_id = 1
            agent.now_target = "仅综合已有事实"
            handoff = SupervisorHandoff(6, 1, agent.now_target, "综合", dependency_context=[outcome], execution_mode="synthesize")
            agent.task_handoffs[1] = handoff
            executor.input_content = {"supervisor_handoff": handoff.to_dict()}
            for messages in (
                _build_supervisor_evaluation_message(executor, agent, [], "chatgpt", evaluation),
                _build_decision_message(agent, supervisor, evaluation, executor, "chatgpt"),
            ):
                context = json.loads(messages[-1]["content"])
                self.assertFalse(context["evidence_usage"]["summary_is_exhaustive"])
                self.assertIn("/project/mapper", json.dumps(context))
                self.assertIn("存在性", context["evidence_usage"]["path_metadata_scope"])
                self.assertEqual(context.get("historical_auxiliary_summary", {}).get("status", "not_loaded"), "not_loaded")

    async def test_two_task_main_rejection_handoff_rewrite_and_final_delivery(self):
        """Keep real Executor/evaluation/decision loops; fake only model, tools and initial evidence."""
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            turns = []

            async def plan(query, agent, chat_function, client, registry, provider, tools, supervisor, **kwargs):
                agent.task_list = ["最小取证", "仅综合已验收证据"]
                agent.now_target = agent.task_list[0]
                agent.task_completion_criteria[0] = "有目录和代表性源码证据"
                agent.task_handoffs[0] = SupervisorHandoff(0, 0, agent.now_target, "取证", completion_criteria=agent.task_completion_criteria[0])
                supervisor.executor_plan = "取证"

            async def execute(*args):
                agent, executor = args[0], args[8]
                turns.append(agent.now_task_id)
                if agent.now_task_id == 0:
                    executor.task_id = 0
                    executor.output_content = "目录包含 controller/mapper；代表性源码确认 SSE 推送。"
                    executor.is_finished = True
                    agent.task_evidence_references[0] = [EvidenceReference(7, "read_all_files_tool", status="success", paths=["/project/mapper"])]
                else:
                    self.assertEqual(agent.task_handoffs[1].execution_mode, "synthesize")
                    self.assertEqual(agent.task_handoffs[1].dependency_context[0].evidence_references[0].tool_result_seq, 7)
                    await run_executor_loop(*args)

            chat = QueueChat([
                evaluation_response(), control_response(advance=True, next_step="综合已验收事实"),
                model_response(json_message(description="存在 mapper 包，所以必定使用 JPA", is_finished=True)),
                evaluation_response(False), control_response(),
                model_response(json_message(description="直接事实：存在 mapper 包；源码确认 SSE 推送。未确认：mapper 实现技术。", is_finished=True)),
                evaluation_response(), control_response(advance=True, next_step="", final_answer="该后端提供 SSE 推送；mapper 包存在，但其实现技术尚未确认。"),
                model_response(json_message(description="取证与综合均已验收，保留证据边界")),
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
                result = await main("项目在做什么", directory, "001")
            self.assertEqual(result["status"], "completed")
            self.assertIn("实现技术尚未确认", result["answer"])
            self.assertEqual(turns, [0, 1, 1])
            self.assertEqual(len(chat.calls), 9)
            self.assertEqual(chat.calls[2]["tools"], [])
            self.assertEqual(chat.calls[5]["tools"], [])
            summary = task_summary_path(directory, "001").read_text(encoding="utf-8")
            self.assertEqual(summary.count("<!-- task-summary"), 3)
            self.assertEqual(summary.count("task_id=1 supervisor_seq="), 2)


if __name__ == "__main__":
    unittest.main()
