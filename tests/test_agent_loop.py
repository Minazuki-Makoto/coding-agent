import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from AgentLoop.agent import (
    AgentState,
    ExecutorState,
    SupervisorEvaluationState,
    SupervisorState,
    _load_skills,
    RuntimeConfig,
    SupervisorDescriptionHistory,
    main,
)
from AgentLoop.executor_loop import SelectedSkill, _load_selected_skill, run_executor_loop
from AgentLoop.supervisor_loop import (
    DateBack,
    SupervisorDecisionResult,
    _validate_decision_plan,
    run_supervisor_loop,
    summarize_supervisor_descriptions,
    supervisor_making_plan,
)
from Context.History_Resorce.mcp_history_error import read_question_task
from Context.History_Resorce.mcp_history_resource_service import read_task_all_history
from MCP_functions.tool_registry import (
    AgentRole,
    ToolExecutionResult,
    ToolRegistry,
)
from State.save_chat_history import save_chat_history

TEST_TEMP_ROOT = os.environ.get("AGENT_LOOP_TEST_TEMP")


def model_response(message=None, tool=None, status="success"):
    return {"status": status, "message": message, "tool": tool or []}


def json_message(**values):
    return json.dumps(values, ensure_ascii=False)


def openai_tool(name="fake_tool", arguments=None, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {}, ensure_ascii=False),
        },
    }


class QueueChat:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected model call")
        return self.responses.pop(0)


class FakeRegistry:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def call(self, role, tool_name, arguments):
        self.calls.append((role, tool_name, arguments))
        if self.results:
            return self.results.pop(0)
        return ToolExecutionResult(
            tool_name=tool_name,
            client_name="fake",
            ok=True,
            error_type=None,
            message="",
            output={"done": True},
        )


def make_states(directory, max_steps=3):
    agent = AgentState(
        session_address=str(directory),
        chat_id="001",
        user_query="完成测试任务",
        task_list=["测试任务"],
        now_target="测试任务",
        executor_model="fake",
        supervisor_model="fake",
        max_executor_steps=max_steps,
        max_supervision_times=2,
        max_model_requests=30,
    )
    supervisor = SupervisorState(
        session_address=str(directory),
        chat_id="001",
        target="测试任务",
        executor_plan="执行并验证测试任务",
    )
    executor = ExecutorState(session_address=str(directory), chat_id="001")
    evaluation = SupervisorEvaluationState(chat_id="001")
    return agent, supervisor, executor, evaluation


class ExecutorLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_runs_once_and_bool_is_used_directly(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory)
            chat = QueueChat(
                [
                    model_response(tool=[openai_tool(arguments={"path": "x"})]),
                    model_response(
                        json_message(
                            tool_name="fake_tool",
                            selected_skill=None,
                            description="工具验证成功",
                            is_finished=True,
                        )
                    ),
                ]
            )
            registry = FakeRegistry()
            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                registry,
                [],
                supervisor,
                executor,
            )
            self.assertEqual(len(registry.calls), 1)
            self.assertTrue(executor.is_finished)
            self.assertEqual(executor.passed_seq_list, [1, 2])
            records = [
                json.loads(line)
                for line in Path(directory, "executor_history.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(records[0]["arguments"], {"path": "x"})
            self.assertTrue(records[0]["tool_ok"])

    async def test_bad_summary_retries_without_reexecuting_tool(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory)
            chat = QueueChat(
                [
                    model_response(tool=[openai_tool()]),
                    model_response("not json"),
                    model_response(
                        json_message(
                            description="第二次总结成功",
                            is_finished=True,
                            selected_skill=None,
                        )
                    ),
                ]
            )
            registry = FakeRegistry()
            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                registry,
                [],
                supervisor,
                executor,
            )
            self.assertEqual(len(registry.calls), 1)
            self.assertEqual(len(chat.calls), 3)
            self.assertIn("第二次总结成功", executor.output_content)

    async def test_plain_json_and_window_are_bounded(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=11)
            chat = QueueChat(
                [
                    model_response(
                        json_message(
                            description=f"description-{index}",
                            is_finished=index == 10,
                            selected_skill=None,
                        )
                    )
                    for index in range(11)
                ]
            )
            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                FakeRegistry(),
                [],
                supervisor,
                executor,
            )
            self.assertEqual(len(executor.memory_window), 10)
            self.assertNotIn("description-0", executor.memory_window)
            self.assertTrue(executor.is_finished)

    async def test_tool_failure_is_saved_and_step_limit_one_exits(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=1)
            chat = QueueChat(
                [
                    model_response(tool=[openai_tool()]),
                    model_response(
                        json_message(
                            description="工具失败，任务未完成",
                            is_finished=False,
                            selected_skill=None,
                        )
                    ),
                ]
            )
            failed = ToolExecutionResult(
                tool_name="fake_tool",
                client_name="fake",
                ok=False,
                error_type="tool_error",
                message="boom",
                output={"status": "error"},
            )
            registry = FakeRegistry([failed])
            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                registry,
                [],
                supervisor,
                executor,
            )
            self.assertEqual(executor.exit_reason, "executor_step_limit")
            self.assertTrue(executor.is_error)
            records = [
                json.loads(line)
                for line in Path(directory, "executor_history.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertFalse(records[0]["tool_ok"])
            self.assertEqual(records[0]["error_message"], "boom")

    def test_invalid_skill_selection_is_finite(self):
        self.assertIsNone(
            _load_selected_skill(
                [],
                SelectedSkill(skill_name="missing.md", skill_id=-1),
            )
        )

    async def test_multiple_tools_are_not_silently_executed(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=1)
            chat = QueueChat(
                [
                    model_response(
                        tool=[
                            openai_tool(call_id="one"),
                            openai_tool(call_id="two"),
                        ]
                    )
                ]
            )
            registry = FakeRegistry()
            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                registry,
                [],
                supervisor,
                executor,
            )
            self.assertEqual(registry.calls, [])
            self.assertTrue(executor.is_error)
            self.assertIn("多个工具调用", executor.error_message)

    async def test_reentry_clears_transient_window_but_preserves_sequence(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=1)
            first = QueueChat(
                [
                    model_response(
                        json_message(
                            description="old-description",
                            is_finished=True,
                            selected_skill=None,
                        )
                    )
                ]
            )
            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                first,
                [],
                FakeRegistry(),
                [],
                supervisor,
                executor,
            )
            first_seq = agent.executor_seq
            second = QueueChat(
                [
                    model_response(
                        json_message(
                            description="new-description",
                            is_finished=True,
                            selected_skill=None,
                        )
                    )
                ]
            )
            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                second,
                [],
                FakeRegistry(),
                [],
                supervisor,
                executor,
            )
            self.assertGreater(agent.executor_seq, first_seq)
            self.assertEqual(executor.memory_window, ["new-description"])
            second_request = json.dumps(second.calls[0]["query"], ensure_ascii=False)
            self.assertNotIn("old-description", second_request)


class SupervisorLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_descriptions_are_summarized_and_persistable(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, _, _ = make_states(directory)
            agent.supervisor_descriptions = SupervisorDescriptionHistory()
            agent.supervisor_descriptions.append_turn(
                1, 0, "inspect", "planning", "制定了两步计划"
            )
            agent.supervisor_descriptions.append_turn(
                2, 0, "inspect", "evaluation", "检查证据后通过"
            )
            chat = QueueChat(
                [
                    model_response(
                        json_message(description="已规划并核验，任务完成。")
                    )
                ]
            )
            description = await summarize_supervisor_descriptions(
                now_state=agent,
                supervisor_state=supervisor,
                supervisor="chatgpt",
                supervisor_client=object(),
                chat_function=chat,
                status="completed",
                answer="artifact",
                block_reason="",
            )
            self.assertEqual(description, "已规划并核验，任务完成。")
            self.assertEqual(
                [entry.phase for entry in agent.supervisor_descriptions.entries],
                ["planning", "evaluation", "final_summary"],
            )
            save_chat_history(
                chat_id="001",
                session_address=directory,
                query_content="query",
                answer_content="artifact",
                description=description,
                task_number=1,
                seq_number=supervisor.supervisor_seq,
                supervisor_descriptions=(
                    agent.supervisor_descriptions.to_dicts()
                ),
            )
            record = json.loads(
                Path(directory, "chat_history.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(record["description"], description)
            self.assertEqual(len(record["supervisor_descriptions"]), 3)

    async def test_first_request_plans_then_guides_task_zero_without_evaluation(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, _, _ = make_states(directory)
            agent.task_list = []
            agent.now_target = ""
            chat = QueueChat(
                [
                    model_response(
                        json_message(
                            task_list=["inspect", "repair"],
                            description="计划完成",
                        )
                    ),
                    model_response(
                        json_message(
                            description="先检查真实代码",
                            executor_guidance="读取相关源码并定位问题",
                            completion_criteria="提供文件路径和实际证据",
                        )
                    ),
                ]
            )
            registry = FakeRegistry()
            await supervisor_making_plan(
                "repair project",
                agent,
                chat,
                object(),
                registry,
                "chatgpt",
                [],
                supervisor,
                executor_tools=[],
                skill_lists=[],
            )
            self.assertEqual(agent.now_task_id, 0)
            self.assertEqual(agent.now_target, "inspect")
            self.assertIn("提供文件路径和实际证据", supervisor.executor_plan)
            self.assertEqual(registry.calls, [])
            self.assertEqual(len(chat.calls), 2)

    async def test_executor_self_report_does_not_skip_rejection(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            executor.is_finished = True
            executor.output_content = "声称完成，但没有验证证据"
            executor.passed_seq_list = [1]
            chat = QueueChat(
                [
                    model_response(
                        json_message(
                            is_passed=False,
                            description="缺少验证证据",
                            is_error=False,
                            reason="必须补充测试结果",
                            is_finished=True,
                        )
                    ),
                    model_response(
                        json_message(
                            is_next_target=False,
                            next_executor_target="运行测试并提供输出",
                            description="保持当前任务",
                            is_finished=True,
                        )
                    ),
                ]
            )
            decision = await run_supervisor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                FakeRegistry(),
                [],
                supervisor,
                evaluation,
                executor,
            )
            self.assertFalse(decision.is_next_target)
            self.assertEqual(agent.now_task_id, 0)
            self.assertEqual(supervisor.executor_plan, "运行测试并提供输出")

    async def test_accepted_subgoal_can_continue_same_task(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            executor.output_content = "完成了当前子目标"
            executor.passed_seq_list = [1]
            chat = QueueChat(
                [
                    model_response(
                        json_message(
                            is_passed=True,
                            description="子目标证据充分",
                            is_error=False,
                            reason="",
                            is_finished=True,
                        )
                    ),
                    model_response(
                        json_message(
                            is_next_target=False,
                            next_executor_target="继续当前 task 的下一部分",
                            description="总任务尚未完成",
                            is_finished=True,
                        )
                    ),
                ]
            )
            decision = await run_supervisor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                FakeRegistry(),
                [],
                supervisor,
                evaluation,
                executor,
            )
            self.assertTrue(evaluation.is_executor_passed)
            self.assertFalse(decision.is_next_target)
            self.assertEqual(agent.now_task_id, 0)

    async def test_evaluation_limit_one_produces_blocked_result(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 1
            executor.passed_seq_list = [1]
            chat = QueueChat(
                [
                    model_response(
                        json_message(
                            is_passed=False,
                            description="仍需检查",
                            is_error=False,
                            reason="信息不足",
                            is_finished=False,
                        )
                    ),
                    model_response(
                        json_message(
                            is_next_target=False,
                            next_executor_target="补充验证",
                            description="验收受阻后保持当前任务",
                            is_finished=True,
                        )
                    ),
                ]
            )
            await run_supervisor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [],
                FakeRegistry(),
                [],
                supervisor,
                evaluation,
                executor,
            )
            self.assertTrue(evaluation.blocked)
            self.assertFalse(evaluation.is_executor_passed)
            self.assertIn("达到上限", evaluation.description)

    def test_plan_reorder_preserves_completed_prefix_and_chat_id_string(self):
        agent = AgentState(
            session_address="x",
            chat_id="001",
            task_list=["done", "current", "later"],
            now_task_id=1,
        )
        valid = SupervisorDecisionResult(
            is_task_list_need_change=True,
            new_task_list=["done", "current revised", "new later"],
            date_back_location=[
                DateBack(
                    seq=2,
                    task_id=1,
                    chat_id="001",
                    session_address="x",
                )
            ],
        )
        _validate_decision_plan(agent, valid)
        self.assertEqual(valid.date_back_location[0].chat_id, "001")
        invalid = SupervisorDecisionResult(
            is_task_list_need_change=True,
            new_task_list=["rewritten", "current"],
        )
        with self.assertRaises(ValueError):
            _validate_decision_plan(agent, invalid)


class MainSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tasks_finish_without_extra_executor_or_index_error(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            events = []

            async def fake_plan(
                query,
                now_state,
                chat_function,
                client,
                tool_registry,
                supervisor,
                supervisor_tools,
                supervisor_state,
                executor_tools=None,
                skill_lists=None,
            ):
                events.append("plan")
                now_state.task_list = ["task-0", "task-1"]
                now_state.now_task_id = 0
                now_state.now_target = "task-0"
                supervisor_state.executor_plan = "execute task-0"

            async def fake_executor(
                now_state,
                executor,
                executor_client,
                chat_function,
                executor_tools,
                tool_registry,
                skill_lists,
                supervisor_state,
                executor_state,
            ):
                events.append(f"execute-{now_state.now_task_id}")
                executor_state.output_content = f"artifact-{now_state.now_task_id}"
                executor_state.description = executor_state.output_content
                executor_state.passed_seq_list = [now_state.now_task_id + 1]
                executor_state.is_finished = True

            decisions = [
                SupervisorDecisionResult(
                    is_next_target=True,
                    next_executor_target="execute task-1",
                    description="task-0 accepted",
                    is_finished=True,
                ),
                SupervisorDecisionResult(
                    is_next_target=True,
                    description="task-1 accepted",
                    final_answer="final artifact",
                    is_finished=True,
                ),
            ]

            async def fake_supervisor(
                now_state,
                supervisor,
                supervisor_client,
                chat_function,
                supervisor_tools,
                tool_registry,
                skill_lists,
                supervisor_state,
                supervisor_evaluation_state,
                executor_state,
            ):
                events.append(f"supervise-{now_state.now_task_id}")
                result = decisions.pop(0)
                supervisor_state.executor_plan = result.next_executor_target
                supervisor_state.final_answer = result.final_answer
                return result

            config = RuntimeConfig(
                supervisor="chatgpt",
                executor="chatgpt",
                supervisor_api="test",
                executor_api="test",
                supervisor_model="fake",
                executor_model="fake",
                temperature=0.0,
                java=None,
                python=None,
            )
            with (
                patch("AgentLoop.agent.load_runtime_config", return_value=config),
                patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                patch("AgentLoop.agent._build_model_client", return_value=object()),
                patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                patch("AgentLoop.agent.supervisor_making_plan", new=fake_plan),
                patch("AgentLoop.agent.run_executor_loop", new=fake_executor),
                patch("AgentLoop.agent.run_supervisor_loop", new=fake_supervisor),
            ):
                result = await main("query", directory, "001")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["answer"], "final artifact")
            self.assertEqual(
                events,
                ["plan", "execute-0", "supervise-0", "execute-1", "supervise-1"],
            )

    async def test_same_task_attempt_limit_returns_blocked(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            executor_calls = 0

            async def fake_plan(*args, **kwargs):
                now_state = args[1]
                supervisor_state = args[7]
                now_state.task_list = ["stuck"]
                now_state.now_target = "stuck"
                supervisor_state.executor_plan = "try"

            async def fake_executor(*args, **kwargs):
                nonlocal executor_calls
                executor_calls += 1
                args[8].passed_seq_list = [executor_calls]

            async def fake_supervisor(*args, **kwargs):
                return SupervisorDecisionResult(
                    is_next_target=False,
                    next_executor_target="retry with evidence",
                    description="not complete",
                    is_finished=True,
                )

            config = RuntimeConfig(
                supervisor="chatgpt",
                executor="chatgpt",
                supervisor_api="test",
                executor_api="test",
                supervisor_model="fake",
                executor_model="fake",
                temperature=0.0,
                java=None,
                python=None,
            )
            with (
                patch("AgentLoop.agent.load_runtime_config", return_value=config),
                patch("AgentLoop.agent._register_mcp_clients", new=AsyncMock()),
                patch("AgentLoop.agent._build_model_client", return_value=object()),
                patch("AgentLoop.agent._close_model_client", new=AsyncMock()),
                patch("AgentLoop.agent.supervisor_making_plan", new=fake_plan),
                patch("AgentLoop.agent.run_executor_loop", new=fake_executor),
                patch("AgentLoop.agent.run_supervisor_loop", new=fake_supervisor),
            ):
                result = await main("query", directory, "001")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(executor_calls, 3)
            self.assertIn("exceeded 3 attempts", result["block_reason"])


class HistoryAndPermissionTests(unittest.TestCase):
    def test_review_changes_pending_record_to_valid_summary(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            history = Path(directory, "executor_history.jsonl")
            history.write_text(
                json.dumps(
                    {
                        "record_type": "executor_turn",
                        "chat_id": "001",
                        "task_id": 0,
                        "seq": 1,
                        "description": "实际完成摘要",
                        "is_error": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(len(read_question_task(directory, "001", 0)), 1)
            with history.open("a", encoding="utf-8") as history_file:
                history_file.write(
                    json.dumps(
                        {
                            "record_type": "supervisor_review",
                            "chat_id": "001",
                            "task_id": 0,
                            "supervisor_seq": 1,
                            "reviewed_executor_seqs": [1],
                            "is_passed": False,
                            "reason": "缺少验证证据",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                history_file.write(
                    json.dumps(
                        {
                            "record_type": "supervisor_review",
                            "chat_id": "001",
                            "task_id": 0,
                            "supervisor_seq": 2,
                            "reviewed_executor_seqs": [1],
                            "is_passed": True,
                            "reason": "证据充分",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            rejected_lines = history.read_text(encoding="utf-8").splitlines()
            history.write_text(
                "\n".join(rejected_lines[:-1]) + "\n", encoding="utf-8"
            )
            pending = read_question_task(directory, "001", 0)
            self.assertEqual(pending[0]["update_advice"], "缺少验证证据")
            with history.open("a", encoding="utf-8") as history_file:
                history_file.write(rejected_lines[-1] + "\n")
            self.assertEqual(read_question_task(directory, "001", 0), [])
            summaries = read_task_all_history(directory, "001", 0)
            self.assertEqual(summaries[0]["description"], "实际完成摘要")

    def test_permissions_and_skill_paths(self):
        class Host:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "read_history_chat",
                        "description": "",
                        "parameters": {},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "write_in_tool",
                        "description": "",
                        "parameters": {},
                    },
                },
            ]
            tool_dictionary = {
                "read_history_chat": "memory",
                "write_in_tool": "system",
            }
            session_dictionary = {}

        registry = ToolRegistry(Host())
        supervisor_names = {
            tool["function"]["name"]
            for tool in registry.schemas_for(AgentRole.SUPERVISOR, "chatgpt")
        }
        executor_names = {
            tool["function"]["name"]
            for tool in registry.schemas_for(AgentRole.EXECUTOR, "chatgpt")
        }
        self.assertIn("read_history_chat", supervisor_names)
        self.assertNotIn("write_in_tool", supervisor_names)
        self.assertIn("write_in_tool", executor_names)
        self.assertNotIn("read_history_chat", executor_names)

        skills = _load_skills(
            Path(__file__).resolve().parents[1] / "Skills", AgentRole.SUPERVISOR
        )
        self.assertTrue(skills)
        self.assertTrue(all(Path(skill.address).is_file() for skill in skills))
        self.assertEqual(
            _load_skills(
                Path(__file__).resolve().parents[1] / "Skills", AgentRole.EXECUTOR
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
