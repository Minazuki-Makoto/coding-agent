import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from AgentLoop.supervisor_loop import making_next_plan_loop
from Context.mcp_supervisor_tools import mcp_server, read_task_summary
from MCP_functions.tool_registry import AgentRole, ToolRegistry
from State.save_tool_result import read_tool_result_record, save_tool_result
from State.task_summary import append_task_summary_record
from test_agent_loop import (
    TEST_TEMP_ROOT, QueueChat, make_states, model_response, openai_tool,
)
from test_task_summary import decision_response, record
from MCP_functions.tool_registry import ToolExecutionResult


class HistoryClient:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool_name, argument):
        self.calls.append((tool_name, dict(argument)))
        if tool_name == "read_task_summary":
            result = await read_task_summary(**argument)
        else:
            result = read_tool_result_record(**argument)
        return {"structuredContent": result}


def bound_registry(directory, task_id=0, schema=None):
    client = HistoryClient()
    host = SimpleNamespace(
        tools=[
            {"type": "function", "function": {
                "name": "read_task_summary", "parameters": schema or {},
            }},
            {"type": "function", "function": {"name": "read_tool_result", "parameters": {}}},
        ],
        tool_dictionary={"read_task_summary": "supervisor_memory", "read_tool_result": "supervisor_memory"},
        session_dictionary={"supervisor_memory": client},
    )
    registry = ToolRegistry(host, context_provider=lambda: {
        "session_address": directory, "chat_id": "001", "task_id": task_id,
    })
    return registry, client


class TaskSummaryRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_registration_supervisor_permission_schema_and_host_binding(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            append_task_summary_record(directory, "001", 0, 1, record(0, 1, "真实 chat 成果"))
            registered = next(tool for tool in await mcp_server.list_tools() if tool.name == "read_task_summary")
            registry, client = bound_registry(directory, schema=registered.inputSchema)
            for provider in ["chatgpt", "claude"]:
                schemas = registry.schemas_for(AgentRole.SUPERVISOR, provider)
                summary_schema = next(item for item in schemas if (item.get("function") or item)["name"] == "read_task_summary")
                parameters = summary_schema["input_schema"] if provider == "claude" else summary_schema["function"]["parameters"]
                self.assertNotIn("session_address", parameters["properties"])
                self.assertNotIn("chat_id", parameters["properties"])
                self.assertEqual(parameters["required"], ["task_id"])
                self.assertIn("include_other_tasks", parameters["properties"])
                self.assertEqual(registry.schemas_for(AgentRole.EXECUTOR, provider), [])
            result = await registry.call(AgentRole.SUPERVISOR, "read_task_summary", {
                "session_address": "forged", "chat_id": "forged", "task_id": 0,
            })
            self.assertTrue(result.ok)
            self.assertIn("真实 chat 成果", result.output["current_task_latest"])
            self.assertEqual(client.calls[0][1]["session_address"], directory)
            self.assertEqual(client.calls[0][1]["chat_id"], "001")
            denied = await registry.call(AgentRole.EXECUTOR, "read_task_summary", {"task_id": 0})
            self.assertEqual(denied.error_type, "permission_denied")
            future = await registry.call(AgentRole.SUPERVISOR, "read_task_summary", {"task_id": 1})
            self.assertEqual(future.error_type, "invalid_history_arguments")
            self.assertEqual(len(client.calls), 1)

    async def test_exact_latest_record_optional_previous_tasks_and_not_found(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            for task_id, seq, text in [(0, 1, "旧 A"), (0, 2, "最新 A"), (1, 3, "当前 B"), (2, 4, "未来 C")]:
                append_task_summary_record(directory, "001", task_id, seq, record(task_id, seq, text))
            current = await read_task_summary(directory, "001", 1)
            self.assertIn("当前 B", current["current_task_latest"])
            self.assertEqual(current["current_task_locator"], {"task_id": 1, "supervisor_seq": 3})
            self.assertEqual(current["other_tasks_latest"], [])
            previous = await read_task_summary(directory, "001", 1, True)
            self.assertEqual(len(previous["other_tasks_latest"]), 1)
            self.assertIn("最新 A", previous["other_tasks_latest"][0]["record"])
            self.assertNotIn("旧 A", previous["other_tasks_latest"][0]["record"])
            self.assertNotIn("未来 C", json.dumps(previous, ensure_ascii=False))
            self.assertEqual((await read_task_summary(directory, "wrong-chat", 1))["status"], "not_found")
            self.assertEqual((await read_task_summary(directory, "001", 99))["status"], "not_found")

    async def test_sufficient_latest_information_does_not_load_markdown_body(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            append_task_summary_record(directory, "001", 0, 1, record(0, 1, "HIDDEN_MARKDOWN_BODY"))
            agent, supervisor, executor, evaluation = make_states(directory)
            executor.output_content = "本轮已有充分执行信息"
            evaluation.output_content = "本轮实际验收情况"
            evaluation.is_executor_passed = True
            chat = QueueChat([decision_response(completed=True)])
            registry, client = bound_registry(directory)
            with patch("Context.mcp_supervisor_tools.read_task_summary_context", side_effect=AssertionError("unexpected automatic read")):
                result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], registry, supervisor, evaluation, executor)
            self.assertTrue(result.is_next_target)
            self.assertEqual(client.calls, [])
            request = json.loads(chat.calls[0]["query"][-1]["content"])
            self.assertEqual(request["current_turn_latest_result"]["executor_summary"], executor.output_content)
            self.assertEqual(request["current_turn_supervisor_evaluation"]["description"], evaluation.output_content)
            self.assertEqual(request["historical_auxiliary_summary"]["status"], "not_loaded")
            self.assertTrue(request["historical_auxiliary_summary"]["available"])
            self.assertNotIn("HIDDEN_MARKDOWN_BODY", json.dumps(chat.calls[0]["query"], ensure_ascii=False))
            self.assertIn("没有特别准确的把握", chat.calls[0]["query"][0]["content"])

    async def test_model_can_query_summary_then_exact_evidence_without_copying_history(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            append_task_summary_record(directory, "001", 0, 1, record(0, 1, "历史累计成果 A"))
            original = ToolExecutionResult("test", "test", True, None, "", {"fact": "RAW_EVIDENCE"})
            save_tool_result(directory, "001", 0, "executor", 1, "execution", "test", {}, original, 1)
            raw_path = Path(directory, "tool_results.jsonl")
            previous_raw = raw_path.read_text(encoding="utf-8")
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 3
            evaluation.is_executor_passed = True
            chat = QueueChat([
                model_response(tool=[openai_tool("read_task_summary", {"task_id": 0})]),
                model_response(tool=[openai_tool("read_tool_result", {"tool_result_seq": 1})]),
                decision_response(completed=True, cumulative_task_summary="仍有效成果 A 和本轮 B"),
            ])
            registry, client = bound_registry(directory)
            result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], registry, supervisor, evaluation, executor)
            self.assertTrue(result.is_next_target)
            self.assertEqual([name for name, _ in client.calls], ["read_task_summary", "read_tool_result"])
            request = json.loads(chat.calls[2]["query"][-1]["content"])
            self.assertEqual(len(request["memory_query_results"]), 2)
            self.assertIn("历史累计成果 A", request["memory_query_results"][0]["result"])
            self.assertIn("RAW_EVIDENCE", request["memory_query_results"][1]["result"])
            self.assertEqual(raw_path.read_text(encoding="utf-8"), previous_raw)
            history = Path(directory, "supervisor_history.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("历史累计成果 A", history)
            self.assertNotIn("RAW_EVIDENCE", history)
            events = [json.loads(line) for line in history.splitlines() if json.loads(line)["record_type"] == "decision_tool_event"]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[-1]["referenced_tool_result_seqs"], [1])
            self.assertEqual(supervisor.tool_result_refs, [])

    async def test_duplicate_summary_query_is_not_executed_again(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            append_task_summary_record(directory, "001", 0, 1, record(0, 1, "历史成果"))
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 3
            evaluation.is_executor_passed = True
            chat = QueueChat([
                model_response(tool=[openai_tool("read_task_summary", {"task_id": 0, "session_address": "fake", "chat_id": "fake"})]),
                model_response(tool=[openai_tool("read_task_summary", {"task_id": 0, "include_other_tasks": False})]),
                decision_response(),
            ])
            registry, client = bound_registry(directory)
            await making_next_plan_loop(agent, "chatgpt", object(), chat, [], registry, supervisor, evaluation, executor)
            self.assertEqual(len(client.calls), 1)
            request = json.loads(chat.calls[2]["query"][-1]["content"])
            self.assertIn("同一历史查询已执行", "\n".join(request["memory_window"]))
            self.assertIn("历史成果", request["memory_query_results"][0]["result"])

    async def test_not_found_is_visible_and_does_not_create_an_accepted_outcome(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            chat = QueueChat([
                model_response(tool=[openai_tool("read_task_summary", {"task_id": 0})]),
                decision_response(remaining_work=["补齐无法确认的历史成果"]),
            ])
            registry, client = bound_registry(directory)
            result = await making_next_plan_loop(agent, "chatgpt", object(), chat, [], registry, supervisor, evaluation, executor)
            self.assertFalse(result.is_next_target)
            self.assertEqual(agent.task_outcomes, {})
            request = json.loads(chat.calls[1]["query"][-1]["content"])
            self.assertIn("not_found", request["memory_query_results"][0]["result"])
            self.assertEqual(len(client.calls), 1)
            self.assertFalse(Path(directory, "tool_results.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
