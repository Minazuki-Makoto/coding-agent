import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from AgentLoop.agent import (
    AgentState,
    EvidenceReference,
    ExecutorState,
    SupervisorEvaluationState,
    SupervisorState,
    _load_skills,
    RuntimeConfig,
    SupervisorDescriptionHistory,
    SupervisorHandoff,
    TaskOutcome,
    main,
)
from AgentLoop.executor_loop import (
    ExecutorOutPut,
    SelectedSkill,
    _load_selected_skill,
    run_executor_loop,
)
from AgentLoop.supervisor_loop import (
    DateBack,
    SupervisorDecisionResult,
    TaskSummaryDraft,
    _apply_decision,
    _validate_decision_plan,
    _save_supervisor_tool_event,
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
from State.save_tool_result import read_tool_result_record
from MCP_functions.System_Files.Files_function.write_in import str_replace, write_in

TEST_TEMP_ROOT = os.environ.get("AGENT_LOOP_TEST_TEMP")


def model_response(message=None, tool=None, status="success"):
    return {"status": status, "message": message, "tool": tool or []}


def json_message(**values):
    if "is_next_target" in values and "task_summary" not in values:
        values["task_summary"] = task_summary_payload(
            completed=bool(values["is_next_target"])
        )
    return json.dumps(values, ensure_ascii=False)


def task_summary_payload(completed=False, cumulative="累计有效成果"):
    return {
        "current_turn_summary": "本轮完成了当前委派并获得关键结果",
        "cumulative_task_summary": cumulative,
        "current_verification_summary": "Supervisor 对照完成条件检查了本轮证据",
        "corrected_or_invalidated": [],
        "remaining_work": [] if completed else ["继续当前任务"],
        "next_step": "推进下一任务" if completed else "继续当前任务",
        "accepted_summary": "任务累计成果已经确认" if completed else "",
        "verification_summary": "完成条件与实际证据均已检查通过" if completed else "",
    }


def task_summary_draft(completed=False, cumulative="累计有效成果"):
    return TaskSummaryDraft.model_validate(
        task_summary_payload(completed=completed, cumulative=cumulative)
    )


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
    handoff = SupervisorHandoff(
        supervisor_seq=0,
        task_id=0,
        target="测试任务",
        instruction="执行并验证测试任务",
        completion_criteria="提供可核验证据",
    )
    agent.task_completion_criteria[0] = handoff.completion_criteria
    agent.task_handoffs[0] = handoff
    supervisor.handoff = handoff
    executor = ExecutorState(session_address=str(directory), chat_id="001")
    evaluation = SupervisorEvaluationState(chat_id="001")
    return agent, supervisor, executor, evaluation


class ExecutorLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_tool_context_keeps_metadata_but_not_content(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory)
            chat = QueueChat([
                model_response(tool=[openai_tool(name="read_files_content_tool")]),
                model_response(json_message(
                    description="已阅读文件并概括正文", is_finished=False,
                    selected_skill=None,
                )),
                model_response(json_message(
                    description="基于摘要完成下一步", is_finished=True,
                    selected_skill=None,
                )),
            ])
            result = ToolExecutionResult(
                tool_name="read_files_content_tool",
                client_name="system",
                ok=True,
                error_type=None,
                message="",
                output={
                    "status": "success",
                    "root": r"D:\project\Main.py",
                    "scope": "single_file_content",
                    "file": {
                        "file_name": "Main.py",
                        "address": r"D:\project\Main.py",
                        "suffix": ".py",
                        "content": "SECRET_SOURCE_BODY",
                        "content_status": "content_complete",
                        "content_chars": 18,
                        "start_char": 0,
                        "end_char": 18,
                        "has_more": False,
                        "next_start_char": None,
                    },
                },
            )
            await run_executor_loop(
                agent, "chatgpt", object(), chat, [], FakeRegistry([result]), [],
                supervisor, executor,
            )

            summary_request = json.dumps(chat.calls[1]["query"], ensure_ascii=False)
            next_action_request = json.dumps(chat.calls[2]["query"], ensure_ascii=False)
            next_action_information = json.loads(
                chat.calls[2]["query"][-1]["content"]
            )
            self.assertIn("SECRET_SOURCE_BODY", summary_request)
            self.assertNotIn("SECRET_SOURCE_BODY", next_action_request)
            self.assertEqual(
                next_action_information["latest_tool_context"]["root"],
                r"D:\project\Main.py",
            )
            self.assertEqual(executor.latest_tool_context.tool_result_seq, 1)
            self.assertEqual(executor.latest_tool_context.root, r"D:\project\Main.py")
            self.assertEqual(executor.latest_tool_context.paths, [])
            self.assertTrue(
                all(
                    "content" not in item
                    for item in executor.latest_tool_context.metadata["files"]
                )
            )
            self.assertEqual(
                agent.task_evidence_references[0][0].tool_result_seq, 1
            )
            self.assertEqual(
                len(agent.task_reusable_read_calls[0]), 1
            )
            saved_result = Path(directory, "tool_results.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertIn("SECRET_SOURCE_BODY", saved_result)

    async def test_tool_result_save_failure_creates_no_locator(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=1)
            chat = QueueChat([model_response(tool=[openai_tool()])])
            with patch(
                "AgentLoop.executor_loop.save_tool_result",
                side_effect=OSError("disk full"),
            ):
                await run_executor_loop(
                    agent, "chatgpt", object(), chat, [], FakeRegistry(), [],
                    supervisor, executor
                )
            self.assertEqual(executor.tool_result_refs, [])
            records = Path(directory, "executor_history.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0])["record_type"], "executor_turn")

    async def test_multiple_tool_results_match_summaries(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=3)
            chat = QueueChat([
                model_response(tool=[openai_tool(call_id="one", arguments={"n": 1})]),
                model_response(json_message(
                    tool_name="fake_tool", selected_skill=None,
                    description="first", is_finished=False
                )),
                model_response(tool=[openai_tool(call_id="two", arguments={"n": 2})]),
                model_response(json_message(
                    tool_name="fake_tool", selected_skill=None,
                    description="second", is_finished=True
                )),
            ])
            await run_executor_loop(
                agent, "chatgpt", object(), chat, [], FakeRegistry(), [],
                supervisor, executor
            )
            raw = Path(directory, "tool_results.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(raw), 2)
            self.assertEqual(
                [summary.tool_result_seq for summary in executor.tool_summaries],
                [1, 2],
            )

    def test_executor_output_requires_is_finished(self):
        with self.assertRaises(ValueError):
            ExecutorOutPut.model_validate(
                {
                    "description": "GLM 漏掉了完成状态",
                }
            )

    def test_executor_output_rejects_blank_description_and_string_bool(self):
        with self.assertRaises(ValueError):
            ExecutorOutPut.model_validate(
                {
                    "description": "   ",
                    "is_finished": False,
                }
            )
        with self.assertRaises(ValueError):
            ExecutorOutPut.model_validate(
                {
                    "description": "字段类型错误",
                    "is_finished": "false",
                }
            )

    def test_selected_skill_requires_name_and_id_together(self):
        with self.assertRaises(ValueError):
            SelectedSkill.model_validate(
                {
                    "skill_name": "example.md",
                    "skill_id": None,
                }
            )

    async def test_plain_json_validation_error_is_fed_back_and_retried(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory)
            chat = QueueChat(
                [
                    model_response(
                        json_message(
                            description="   ",
                            is_finished=False,
                        )
                    ),
                    model_response(
                        json_message(
                            description="纠正后返回完整有效结果",
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
            self.assertEqual(registry.calls, [])
            self.assertEqual(len(chat.calls), 2)
            self.assertTrue(executor.is_finished)
            second_information = chat.calls[1]["query"][-1]["content"]
            self.assertIn("Executor 响应校验失败", second_information)
            self.assertIn("description", second_information)

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
            self.assertEqual(
                json.dumps(chat.calls[1]["query"], ensure_ascii=False).count("done"),
                1,
            )
            self.assertEqual(executor.tool_event_seqs, [1])
            self.assertEqual(executor.executor_turn_seq, 2)
            records = [
                json.loads(line)
                for line in Path(directory, "executor_history.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertNotIn("arguments", records[0])
            self.assertNotIn("tool_result", records[0])
            self.assertTrue(records[0]["tool_ok"])
            self.assertIn("model_stage", records[0])
            self.assertIn("raw_model_response_excerpt", records[0])
            self.assertIn("validation_error", records[0])
            self.assertEqual(records[-1]["model_stage"], "tool_summary")
            self.assertEqual(records[-1]["task_attempt"], 0)
            self.assertEqual(records[-1]["input_content"], executor.input_content)
            self.assertNotIn("tool_result", records[-1])
            self.assertEqual(records[-1]["tool_summaries"][0]["tool_result_seq"], 1)
            tool_results = [
                json.loads(line)
                for line in Path(directory, "tool_results.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(tool_results), 1)
            self.assertEqual(tool_results[0]["arguments"], {"path": "x"})
            self.assertEqual(
                read_tool_result_record(directory, "001", 1)["status"], "success"
            )
            self.assertEqual(
                read_tool_result_record(directory, "wrong", 1)["status"], "not_found"
            )

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
            correction = chat.calls[2]["query"][-1]["content"]
            self.assertIn("validation_errors", correction)
            self.assertIn("is_finished", correction)
            records = [
                json.loads(line)
                for line in Path(directory, "executor_history.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertIn("Expecting value", records[-1]["validation_error"])
            self.assertIn("第二次总结成功", records[-1]["raw_model_response_excerpt"])

    async def test_tool_call_markup_during_summary_falls_back_and_continues(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=2)
            markup = (
                "<tool_call>fake_tool<arg_key>path</arg_key>"
                "<arg_value>next</arg_value></tool_call>"
            )
            chat = QueueChat([
                model_response(tool=[openai_tool(arguments={"path": "root"})]),
                model_response(markup),
                model_response(markup),
                model_response(tool=[openai_tool(arguments={"path": "next"})]),
                model_response(json_message(
                    description="第二个工具总结完成",
                    is_finished=True,
                    selected_skill=None,
                )),
            ])
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

            self.assertEqual(len(registry.calls), 2)
            self.assertTrue(executor.is_finished)
            self.assertFalse(executor.is_error)
            self.assertIn("确定性摘要", executor.output_content)
            self.assertIn("第二个工具总结完成", executor.output_content)
            summary_request = json.dumps(chat.calls[1]["query"], ensure_ascii=False)
            self.assertIn("只总结刚才这一份真实 observation", summary_request)
            next_action_request = json.dumps(chat.calls[3]["query"], ensure_ascii=False)
            self.assertIn("确定性摘要", next_action_request)

    async def test_latest_tool_context_survives_same_task_reentry(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=1)
            result = ToolExecutionResult(
                tool_name="fake_tool",
                client_name="fake",
                ok=True,
                error_type=None,
                message="",
                output={
                    "status": "success",
                    "root": r"D:\project\src",
                    "directories": [
                        {
                            "folder_name": "main",
                            "address": r"D:\project\src\main",
                        }
                    ],
                    "files": [],
                },
            )
            first = QueueChat([
                model_response(tool=[openai_tool()]),
                model_response(json_message(
                    description="src 已浏览，后续继续 main",
                    is_finished=False,
                    selected_skill=None,
                )),
            ])
            await run_executor_loop(
                agent, "chatgpt", object(), first, [], FakeRegistry([result]), [],
                supervisor, executor,
            )

            agent.task_attempt += 1
            agent.task_handoffs[0] = SupervisorHandoff(
                supervisor_seq=1,
                task_id=0,
                target="测试任务",
                instruction="继续浏览尚未完成的下一层目录",
                completion_criteria="提供可核验证据",
            )
            second = QueueChat([
                model_response(json_message(
                    description="已读取跨 attempt 上下文",
                    is_finished=True,
                    selected_skill=None,
                ))
            ])
            await run_executor_loop(
                agent, "chatgpt", object(), second, [], FakeRegistry(), [],
                supervisor, executor,
            )

            request_information = json.loads(second.calls[0]["query"][-1]["content"])
            self.assertEqual(
                request_information["latest_tool_context"]["root"],
                r"D:\project\src",
            )
            self.assertEqual(executor.latest_tool_context.tool_result_seq, 1)

    async def test_synthesize_handoff_receives_dependencies_and_disables_tools(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=2)
            outcome = TaskOutcome(
                task_id=0,
                target="读取项目证据",
                accepted_summary="已确认 controller 和 service 职责",
                verification_summary="Supervisor 已验收",
                evidence_references=[
                    EvidenceReference(
                        tool_result_seq=7,
                        tool_name="read_files_content_tool",
                        root=r"D:\project\src",
                        status="success",
                    )
                ],
                reusable_read_calls=[
                    'read_files_content_tool:{"home_address":"D:\\\\project\\\\src"}'
                ],
            )
            agent.task_handoffs[0] = SupervisorHandoff(
                supervisor_seq=1,
                task_id=0,
                target="综合总结",
                instruction="仅综合已验收证据",
                completion_criteria="给出最终说明",
                dependency_context=[outcome],
                execution_mode="synthesize",
            )
            chat = QueueChat([
                model_response(tool=[openai_tool(name="read_files_content_tool")]),
                model_response(json_message(
                    description="根据前序证据完成综合总结",
                    is_finished=True,
                    selected_skill=None,
                )),
            ])
            registry = FakeRegistry()

            await run_executor_loop(
                agent,
                "chatgpt",
                object(),
                chat,
                [{"type": "function", "function": {"name": "fake"}}],
                registry,
                [],
                supervisor,
                executor,
            )

            self.assertEqual(registry.calls, [])
            self.assertEqual(chat.calls[0]["tools"], [])
            self.assertTrue(executor.is_finished)
            request = json.loads(chat.calls[0]["query"][-1]["content"])
            handoff = request["supervisor_handoff"]
            self.assertEqual(handoff["execution_mode"], "synthesize")
            self.assertEqual(
                handoff["dependency_context"][0]["accepted_summary"],
                "已确认 controller 和 service 职责",
            )

    async def test_repeated_completed_call_stops_before_step_limit(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=10)
            agent.task_handoffs[0].completed_tool_calls = ["0:fake_tool:{}"]
            chat = QueueChat([
                model_response(tool=[openai_tool()]),
                model_response(tool=[openai_tool()]),
            ])

            await run_executor_loop(
                agent, "chatgpt", object(), chat, [], FakeRegistry(), [],
                supervisor, executor,
            )

            self.assertEqual(len(chat.calls), 2)
            self.assertEqual(executor.exit_reason, "executor_repeated_completed_call")
            self.assertIn("连续选择同一个已完成工具调用", executor.validation_error)

    async def test_execute_mode_reuses_prior_task_read_call(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=10)
            arguments = {"home_address": r"D:\project"}
            signature = "read_all_files_tool:" + json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            agent.task_handoffs[0] = SupervisorHandoff(
                supervisor_seq=1,
                task_id=0,
                target="继续分析",
                instruction="复用前序证据并继续尚未完成的分析",
                dependency_context=[
                    TaskOutcome(
                        task_id=-1,
                        target="读取目录",
                        accepted_summary="目录已经读取",
                        verification_summary="证据通过",
                        reusable_read_calls=[signature],
                    )
                ],
                execution_mode="execute",
            )
            repeated_call = model_response(tool=[openai_tool(
                name="read_all_files_tool", arguments=arguments
            )])
            chat = QueueChat([repeated_call, repeated_call])
            registry = FakeRegistry()

            await run_executor_loop(
                agent, "chatgpt", object(), chat, [], registry, [],
                supervisor, executor,
            )

            self.assertEqual(registry.calls, [])
            self.assertEqual(executor.exit_reason, "executor_repeated_completed_call")

    async def test_plain_json_preserves_history_before_high_threshold_compression(self):
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
            self.assertEqual(len(executor.memory_window), 11)
            self.assertIn("description-0", executor.memory_window)
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
        with self.assertRaises(ValueError):
            SelectedSkill(skill_name="missing.md", skill_id=-1)
        self.assertIsNone(
            _load_selected_skill(
                [],
                SelectedSkill(skill_name="missing.md", skill_id=99),
            )
        )

    async def test_multiple_tools_are_protocol_retried_then_one_tool_executes(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, _ = make_states(directory, max_steps=1)
            chat = QueueChat(
                [
                    model_response(
                        tool=[
                            openai_tool(call_id="one"),
                            openai_tool(call_id="two"),
                        ]
                    ),
                    model_response(
                        tool=[openai_tool(
                            name="fake_tool",
                            arguments={"selected": True},
                            call_id="selected",
                        )]
                    ),
                    model_response(
                        json_message(
                            description="单个工具执行完成",
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
            self.assertEqual(registry.calls[0][2], {"selected": True})
            self.assertFalse(executor.is_error)
            self.assertTrue(executor.is_finished)
            self.assertEqual(len(chat.calls), 3)
            retry_request = json.dumps(chat.calls[1]["query"], ensure_ascii=False)
            self.assertIn("工具调用协议错误", retry_request)
            self.assertIn("下一次只能返回一个工具调用", retry_request)

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
            agent.task_handoffs[0] = SupervisorHandoff(
                supervisor_seq=1,
                task_id=0,
                target="测试任务",
                instruction="继续验证",
                previous_work=["old-description"],
                accepted_work=["old-description"],
                remaining_work=["补充验证"],
                completion_criteria="提供可核验证据",
            )
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
            self.assertIn("old-description", second_request)
            self.assertEqual(
                executor.input_content["supervisor_handoff"]["instruction"],
                "继续验证",
            )


class SupervisorLoopTests(unittest.IsolatedAsyncioTestCase):
    def test_passed_task_is_frozen_as_dependency_outcome(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            executor.output_content = "Executor 已读取并总结关键源码"
            evaluation.is_executor_passed = True
            evaluation.description = "已核验 controller、service 和 mapper 证据"
            agent.task_evidence_references[0] = [
                EvidenceReference(
                    tool_result_seq=3,
                    tool_name="read_files_content_tool",
                    root=r"D:\project\src",
                    status="success",
                )
            ]
            agent.task_reusable_read_calls[0] = [
                'read_files_content_tool:{"home_address":"D:\\\\project\\\\src"}'
            ]
            decision = SupervisorDecisionResult(
                is_next_target=True,
                next_executor_target="综合此前证据",
                next_execution_mode="synthesize",
                description="当前 task 已完成",
                is_finished=True,
                task_summary=task_summary_draft(
                    completed=True,
                    cumulative="已确认 controller、service 和 mapper 的累计成果",
                ),
            )

            _apply_decision(agent, supervisor, evaluation, executor, decision)

            outcome = agent.task_outcomes[0]
            self.assertEqual(outcome.accepted_summary, "任务累计成果已经确认")
            self.assertIn("完成条件", outcome.verification_summary)
            self.assertEqual(outcome.evidence_references[0].tool_result_seq, 3)
            self.assertEqual(len(outcome.reusable_read_calls), 1)

    async def test_evaluation_loads_memory_skill_and_can_query_multiple_results(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 3
            executor.output_content = "Executor 摘要不足，需要补充上下文"
            executor.executor_turn_seq = 1
            skills = _load_skills(
                Path(__file__).resolve().parents[1] / "Skills",
                AgentRole.SUPERVISOR,
            )
            chat = QueueChat([
                model_response(tool=[openai_tool(
                    name="read_now_task", arguments={"task_id": 0}, call_id="memory-1"
                )]),
                model_response(tool=[openai_tool(
                    name="read_tool_result",
                    arguments={"tool_result_seq": 7},
                    call_id="memory-2",
                )]),
                model_response(json_message(
                    is_passed=True,
                    description="结合当前摘要和两次记忆查询后证据充分",
                    is_error=False,
                    reason="",
                    is_finished=True,
                )),
                model_response(json_message(
                    is_next_target=False,
                    next_executor_target="继续当前任务",
                    description="子目标通过但总任务未结束",
                    is_finished=True,
                )),
            ])
            registry = FakeRegistry([
                ToolExecutionResult(
                    tool_name="read_now_task", client_name="memory", ok=True,
                    error_type=None, message="",
                    output={"records": [{"description": "已有任务摘要"}]},
                ),
                ToolExecutionResult(
                    tool_name="read_tool_result", client_name="memory", ok=True,
                    error_type=None, message="",
                    output={"record": {"tool_result_seq": 7, "content": "原始证据"}},
                ),
            ])
            await run_supervisor_loop(
                agent, "chatgpt", object(), chat, [], registry, skills,
                supervisor, evaluation, executor
            )
            self.assertTrue(evaluation.is_executor_passed)
            self.assertEqual(
                [call[1] for call in registry.calls],
                ["read_now_task", "read_tool_result"],
            )
            first_request = json.dumps(chat.calls[0]["query"], ensure_ascii=False)
            self.assertIn("Memory Retrieval", first_request)
            self.assertIn("优先使用 Executor 本轮反馈", first_request)
            final_evaluation_request = json.dumps(
                chat.calls[2]["query"], ensure_ascii=False
            )
            self.assertIn("已有任务摘要", final_evaluation_request)
            self.assertIn("原始证据", final_evaluation_request)
            self.assertFalse(Path(directory, "tool_results.jsonl").exists())

    async def test_supervisor_multiple_tools_are_protocol_retried(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 1
            executor.output_content = "本轮证据"
            executor.executor_turn_seq = 1
            chat = QueueChat([
                model_response(tool=[
                    openai_tool(name="read_now_task", call_id="one"),
                    openai_tool(name="read_history_task", call_id="two"),
                ]),
                model_response(json_message(
                    is_passed=True,
                    description="重新选择后直接使用现有证据验收",
                    is_error=False,
                    reason="",
                    is_finished=True,
                )),
                model_response(json_message(
                    is_next_target=False,
                    next_executor_target="继续当前任务",
                    description="保持当前任务",
                    is_finished=True,
                )),
            ])
            registry = FakeRegistry()
            await run_supervisor_loop(
                agent, "chatgpt", object(), chat, [], registry, [],
                supervisor, evaluation, executor
            )
            self.assertTrue(evaluation.is_executor_passed)
            self.assertEqual(registry.calls, [])
            retry_request = json.dumps(chat.calls[1]["query"], ensure_ascii=False)
            self.assertIn("工具调用协议错误", retry_request)
            self.assertIn("read_now_task", retry_request)
            self.assertIn("read_history_task", retry_request)

    async def test_planning_and_initial_guidance_validation_retry(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, _, _ = make_states(directory)
            agent.max_supervision_times = 3
            agent.task_list = []
            chat = QueueChat([
                model_response("not-json"),
                model_response(json_message(task_list=["inspect"], extra="bad")),
                model_response(json_message(task_list=["inspect"], description="ok")),
                model_response("not-json"),
                model_response(json_message(
                    description="guide", executor_guidance="inspect",
                    completion_criteria="evidence", extra="bad"
                )),
                model_response(json_message(
                    description="guide", executor_guidance="inspect",
                    completion_criteria="evidence"
                )),
            ])
            await supervisor_making_plan(
                "inspect", agent, chat, object(), FakeRegistry(), "chatgpt", [],
                supervisor, executor_tools=[], skill_lists=[]
            )
            self.assertEqual(agent.task_completion_criteria[0], "evidence")
            self.assertEqual(len(chat.calls), 6)

    async def test_evaluation_validation_retries_before_decision(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 3
            executor.output_content = "evidence"
            executor.executor_turn_seq = 1
            chat = QueueChat([
                model_response("not-json"),
                model_response(json_message(
                    is_passed=True, description="accepted", is_error=False,
                    reason="", is_finished=True, extra="bad"
                )),
                model_response(json_message(
                    is_passed=True, description="accepted", is_error=False,
                    reason="", is_finished=True
                )),
                model_response(json_message(
                    is_next_target=False, next_executor_target="continue",
                    description="continue", is_finished=True
                )),
            ])
            await run_supervisor_loop(
                agent, "chatgpt", object(), chat, [], FakeRegistry(), [],
                supervisor, evaluation, executor
            )
            self.assertTrue(evaluation.is_executor_passed)
            self.assertEqual(len(chat.calls), 4)

    def test_decision_rejects_misspelled_is_next_target(self):
        with self.assertRaises(ValueError):
            SupervisorDecisionResult.model_validate(
                {
                    "is_next_targte": True,
                    "description": "推进下一任务",
                    "is_finished": True,
                }
            )

    def test_decision_cannot_advance_by_description_without_control_field(self):
        with self.assertRaises(ValueError):
            SupervisorDecisionResult.model_validate(
                {
                    "description": "当前任务已经完成，推进下一任务",
                    "is_finished": True,
                }
            )

    async def test_invalid_decision_is_saved_then_retried_without_advancing(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            executor.output_content = "执行证据"
            executor.tool_event_seqs = [1]
            executor.executor_turn_seq = 2
            chat = QueueChat(
                [
                    model_response(
                        json_message(
                            is_passed=True,
                            description="证据充分",
                            is_error=False,
                            reason="",
                            is_finished=True,
                        )
                    ),
                    model_response(
                        json_message(
                            description="当前任务完成，应推进下一任务",
                            is_finished=True,
                        )
                    ),
                    model_response(
                        json_message(
                            is_next_target=True,
                            description="补全控制字段后允许推进",
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
            self.assertTrue(decision.is_next_target)
            records = [
                json.loads(line)
                for line in Path(directory, "supervisor_history.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            parse_error = next(
                item
                for item in records
                if item["record_type"] == "decision_validation_error_turn"
            )
            self.assertFalse(parse_error["is_next_target"])
            self.assertIn("is_next_target", parse_error["validation_error"])
            self.assertIn(
                "应推进下一任务", parse_error["raw_model_response_excerpt"]
            )
            final_decision = records[-1]
            self.assertTrue(final_decision["is_next_target"])
            self.assertTrue(final_decision["decision_finished"])

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
                total_tokens=321,
                elapsed_time_seconds=4.25,
                started_at="2026-09-29T10:00:00",
                finished_at="2026-09-29T10:00:04",
            )
            record = json.loads(
                Path(directory, "chat_history.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(record["description"], description)
            self.assertEqual(len(record["supervisor_descriptions"]), 3)
            self.assertEqual(record["total_tokens"], 321)
            self.assertEqual(record["elapsed_time_seconds"], 4.25)
            self.assertEqual(record["started_at"], "2026-09-29T10:00:00")
            self.assertEqual(record["finished_at"], "2026-09-29T10:00:04")

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
            executor.tool_event_seqs = [1]
            executor.executor_turn_seq = 2
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
            executor.tool_event_seqs = [1]
            executor.executor_turn_seq = 2
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
            handoff = agent.task_handoffs[0]
            self.assertIn("完成了当前子目标", handoff.previous_work)
            self.assertIn("完成了当前子目标", handoff.accepted_work)
            self.assertIn("继续当前 task 的下一部分", handoff.remaining_work)
            self.assertEqual(
                handoff.completion_criteria, "提供可核验证据"
            )

    async def test_evaluation_limit_one_produces_blocked_result(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, executor, evaluation = make_states(directory)
            agent.max_supervision_times = 1
            executor.tool_event_seqs = [1]
            executor.executor_turn_seq = 2
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
            is_next_target=False,
            description="调整未完成计划后缀",
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
            is_next_target=False,
            description="非法改写已完成前缀",
        )
        with self.assertRaises(ValueError):
            _validate_decision_plan(agent, invalid)


class MainSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tasks_finish_without_extra_executor_or_index_error(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            events = []
            observed_handoffs = []

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
                if now_state.now_task_id == 1:
                    observed_handoffs.append(now_state.task_handoffs[1])
                executor_state.output_content = f"artifact-{now_state.now_task_id}"
                executor_state.description = executor_state.output_content
                executor_state.tool_event_seqs = [now_state.now_task_id + 1]
                executor_state.executor_turn_seq = now_state.now_task_id + 10
                executor_state.is_finished = True

            decisions = [
                SupervisorDecisionResult(
                    is_next_target=True,
                    next_executor_target="execute task-1",
                    next_execution_mode="synthesize",
                    description="task-0 accepted",
                    is_finished=True,
                    task_summary=task_summary_draft(completed=True),
                ),
                SupervisorDecisionResult(
                    is_next_target=True,
                    description="task-1 accepted",
                    final_answer="final artifact",
                    is_finished=True,
                    task_summary=task_summary_draft(completed=True),
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
                if now_state.now_task_id == 0:
                    now_state.task_outcomes[0] = TaskOutcome(
                        task_id=0,
                        target="task-0",
                        accepted_summary="artifact-0",
                        verification_summary="task-0 accepted",
                    )
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
            self.assertEqual(observed_handoffs[0].execution_mode, "synthesize")
            self.assertEqual(
                observed_handoffs[0].dependency_context[0].accepted_summary,
                "artifact-0",
            )
            history_record = json.loads(
                Path(directory, "chat_history.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(history_record["total_tokens"], 0)
            self.assertGreaterEqual(history_record["elapsed_time_seconds"], 0)
            self.assertTrue(history_record["started_at"])
            self.assertTrue(history_record["finished_at"])

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
                args[8].tool_event_seqs = [executor_calls]
                args[8].executor_turn_seq = executor_calls + 10

            async def fake_supervisor(*args, **kwargs):
                return SupervisorDecisionResult(
                    is_next_target=False,
                    next_executor_target="retry with evidence",
                    description="not complete",
                    is_finished=True,
                    task_summary=task_summary_draft(completed=False),
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
    def test_atomic_write_replace_and_boundary(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            target = Path(directory, "sample.txt")
            with patch.dict(
                os.environ, {"CODING_AGENT_WRITABLE_ROOTS": directory}
            ):
                first = write_in(str(target), "first")
                second = write_in(str(target), "second")
                self.assertEqual(first["status"], "success")
                self.assertEqual(second["operation"], "overwrite")
                self.assertEqual(target.read_text(encoding="utf-8"), "second")
                replaced = str_replace(str(target), "second", "third")
                self.assertEqual(replaced["replacements"], 1)
                rejected = str_replace(str(target), "missing", "x")
                self.assertEqual(rejected["status"], "error")
                self.assertEqual(target.read_text(encoding="utf-8"), "third")
                outside = Path(directory).parent / "outside-write-test.txt"
                denied = write_in(str(outside), "blocked")
                self.assertEqual(denied["status"], "error")
                self.assertFalse(outside.exists())

    def test_supervisor_result_is_stored_once_and_history_query_is_not_copied(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            agent, supervisor, _, _ = make_states(directory)
            result = ToolExecutionResult(
                tool_name="read_files_content_tool",
                client_name="system",
                ok=True,
                error_type=None,
                message="",
                output={"large": "original"},
            )
            _save_supervisor_tool_event(
                agent, supervisor, "read_files_content_tool",
                {"file_address": "x"}, result, "evaluation"
            )
            raw_records = Path(directory, "tool_results.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(raw_records), 1)
            history = json.loads(
                Path(directory, "supervisor_history.jsonl").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("tool_result", history)
            historical = ToolExecutionResult(
                tool_name="read_tool_result", client_name="memory", ok=True,
                error_type=None, message="",
                output={"record": {"tool_result_seq": 1, "content": "original"}},
            )
            _save_supervisor_tool_event(
                agent, supervisor, "read_tool_result", {"tool_result_seq": 1},
                historical, "evaluation"
            )
            self.assertEqual(
                len(Path(directory, "tool_results.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()), 1
            )

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
                {
                    "type": "function",
                    "function": {
                        "name": "read_files_content_tool",
                        "description": "",
                        "parameters": {},
                    },
                },
            ]
            tool_dictionary = {
                "read_history_chat": "memory",
                "write_in_tool": "system",
                "read_files_content_tool": "system",
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
        self.assertIn("read_files_content_tool", supervisor_names)
        self.assertIn("read_files_content_tool", executor_names)

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


class HistoryBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_overrides_history_session_and_chat(self):
        class Client:
            def __init__(self):
                self.arguments = None

            async def call_tool(self, tool_name, argument):
                self.arguments = argument
                return {"structuredContent": {"status": "not_found"}}

        client = Client()

        class Host:
            tools = [{
                "type": "function",
                "function": {
                    "name": "read_tool_result",
                    "description": "",
                    "parameters": {},
                },
            }]
            tool_dictionary = {"read_tool_result": "memory"}
            session_dictionary = {"memory": client}

        registry = ToolRegistry(
            Host(),
            context_provider=lambda: {
                "session_address": "trusted-session",
                "chat_id": "trusted-chat",
                "task_id": 2,
            },
        )
        await registry.call(
            AgentRole.SUPERVISOR,
            "read_tool_result",
            {
                "session_address": "forged-session",
                "chat_id": "forged-chat",
                "tool_result_seq": 1,
            },
        )
        self.assertEqual(client.arguments["session_address"], "trusted-session")
        self.assertEqual(client.arguments["chat_id"], "trusted-chat")


if __name__ == "__main__":
    unittest.main()
