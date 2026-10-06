import json
import unittest
from unittest.mock import AsyncMock
from AgentLoop.context_compression import compress_context, prepare_request, ContextBudgetError
from AgentLoop.agent import AgentState
from AgentLoop.loop_utils import build_model_messages, build_tool_observation_messages, normalize_tool_calls
from MCP_functions.tool_registry import ToolExecutionResult

class CompressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_inputs_complete_and_counted_without_recursive_compression(self):
        from AgentLoop.executor_loop import _call_model
        from test_agent_loop import QueueChat, model_response
        original = "甲" * 70000 + "MIDDLE" + "乙" * 70000
        responses = [dict(model_response('{"summary":"依据"}'), total_tokens=7) for _ in range(3)]
        responses.append(dict(model_response("final"), total_tokens=11))
        chat = QueueChat(responses)
        agent = AgentState("unused", "main", user_query="全部要求")
        await _call_model(agent, chat, object(), "chatgpt", build_model_messages("chatgpt", "RULES", {
            "user_query": agent.user_query, "previous_work": original}), [])
        materials = [json.loads(call["query"][0]["content"])["material"] for call in chat.calls[:-1]]
        self.assertEqual("".join(materials), original)
        self.assertEqual(agent.model_request_count, 4)
        self.assertEqual(agent.total_token, 32)
        for call in chat.calls:
            self.assertLessEqual(len(json.dumps({"messages": call["query"], "tools": call["tools"]}, ensure_ascii=False)), 80000)
        agent = AgentState("unused", "main", user_query="全部要求", max_model_requests=1)
        chat = QueueChat([dict(model_response('{"summary":"依据"}'), total_tokens=7)])
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            await _call_model(agent, chat, object(), "chatgpt", build_model_messages("chatgpt", "RULES", {
                "previous_work": "x" * 61000}), [])
        self.assertEqual(len(chat.calls), 1)

    async def test_history_entries_are_not_preclipped(self):
        from AgentLoop.executor_loop import _call_model
        from test_agent_loop import QueueChat, model_response
        agent = AgentState("unused", "main", user_query="完整要求")
        payload = {"recent_descriptions": [f"description-{i}" for i in range(15)],
                   "memory_query_results": [{"result": f"history-{i}"} for i in range(9)]}
        chat = QueueChat([model_response("okay")])
        await _call_model(agent, chat, object(), "chatgpt", build_model_messages("chatgpt", "RULES", payload), [])
        received = json.loads(chat.calls[0]["query"][-1]["content"])
        self.assertEqual(received, payload)
        self.assertEqual(len(chat.calls), 1)

    async def test_strict_threshold_truncate_cache_goal_branch_and_failures(self):
        call = AsyncMock(return_value={"status": "success", "message": '{"summary":"kept evidence"}', "tool": []})
        for size in (9, 10):
            result = await compress_context("x" * size, trigger_chars=10, summary_max_chars=20, call_model=call)
            self.assertEqual(result.content, "x" * size)
        self.assertEqual(call.await_count, 0)
        cache = {}
        for _ in range(2):
            await compress_context("x" * 11, trigger_chars=10, summary_max_chars=20, call_model=call, cache=cache)
        self.assertEqual(call.await_count, 1)
        await compress_context("x" * 11, trigger_chars=10, summary_max_chars=20, call_model=call, cache=cache, goal="new")
        await compress_context("x" * 11, trigger_chars=10, summary_max_chars=20, call_model=call, cache=cache, branch_scope="fork")
        self.assertEqual(call.await_count, 3)
        result = await compress_context("x" * 11, trigger_chars=10, summary_max_chars=5, strategy="truncate", call_model=call)
        self.assertTrue(result.truncated)
        self.assertEqual(call.await_count, 3)
        bad = AsyncMock(return_value={"status": "success", "message": '{"summary":"this is much too long"}', "tool": []})
        result = await compress_context("x" * 11, trigger_chars=10, summary_max_chars=5, call_model=bad, locator={"seq": 1})
        self.assertEqual(bad.await_count, 2)
        self.assertTrue(result.truncated)
        self.assertTrue(result.failure_reason)
        self.assertEqual(result.locator, {"seq": 1})

    async def test_complete_middle_fact_and_protected_metadata_for_both_providers(self):
        body = "a" * 31000 + "CRITICAL_MIDDLE_FACT" + "z" * 31000
        for provider in ("chatgpt", "glm", "claude"):
            agent = AgentState("unused", "main", user_query="preserve requirements")
            agent.compression_cache = {}
            raw = {"name": "read", "id": "id", "input": {}} if provider == "claude" else {
                "id": "id", "function": {"name": "read", "arguments": "{}"}}
            call = normalize_tool_calls(provider, [raw])[0]
            result = ToolExecutionResult("read", "test", True, None, "unchanged error/message", {
                "content": body, "address": "D:/work/file.py", "has_more": True, "next_start_char": 42})
            messages = build_model_messages(provider, "RULES MUST STAY", {"user_query": agent.user_query})
            messages += build_tool_observation_messages(provider, "", call, result)
            compression = AsyncMock(return_value={"status": "success", "message": '{"summary":"CRITICAL_MIDDLE_FACT"}', "tool": []})
            prepared = await prepare_request(agent, messages, [], compression)
            sent = json.dumps(compression.call_args.args[0], ensure_ascii=False)
            self.assertIn("CRITICAL_MIDDLE_FACT", sent)
            self.assertIn("a" * 20000, sent)
            self.assertEqual(compression.call_args.args[1], [])
            self.assertIn("RULES MUST STAY", json.dumps(prepared))
            self.assertIn("next_start_char", json.dumps(prepared))
            self.assertIn("unchanged error/message", json.dumps(prepared))
            self.assertEqual(result.output["content"], body)
            self.assertEqual(compression.await_count, 1)

    async def test_whole_request_budget_retains_user_and_does_not_summarize_early(self):
        agent = AgentState("unused", "main", user_query="完整约束" * 200)
        agent.context_trigger_chars = 60000
        agent.context_max_chars = 100
        call = AsyncMock()
        with self.assertRaises(ContextBudgetError):
            await prepare_request(agent, build_model_messages("chatgpt", "RULES", {"user_query": agent.user_query}), [], call)
        call.assert_not_awaited()
