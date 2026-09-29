from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from AgentLoop.loop_utils import (
    DESCRIPTION_MAX_CHARS,
    USER_QUERY_MAX_CHARS,
    bounded_text,
    build_model_messages,
    build_tool_observation_messages,
    call_chat_function,
    normalize_tool_calls,
    parse_json_object,
)
from MCP_functions.tool_registry import AgentRole, ToolRegistry
from State.save_executor_state_history import save_executor_review
from State.save_supervision_state_history import save_supervisor_state_history

if TYPE_CHECKING:
    from AgentLoop.agent import (
        AgentState,
        ExecutorState,
        Skill,
        SupervisorEvaluationState,
        SupervisorState,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLAN_SKILL_PATH = PROJECT_ROOT / "Skills" / "supervisor_plan.md"
DECISION_SKILL_PATH = PROJECT_ROOT / "Skills" / "supervisor_decison.md"

SUPERVISOR_EVALUATION_PROMPT = """
你是 Supervisor 的 evaluation 阶段，只验收上一轮 Executor 的委派目标，不推进 task。
必须检查真实工具结果和必要验证证据；Executor 自报完成、工具无报错都不能代替证据。
委派子目标完成可以 is_passed=true，即使整个 task 尚未完成；整个 task 是否推进由 decision 阶段判断。
需要历史时只能调用本轮提供的只读工具，精确查询使用 read_task_error。
每步最多调用一个工具。信息不足且仍需查询时 is_finished=false；形成明确验收结论时才为 true。
最终仅输出 JSON：
{
  "is_passed": false,
  "description": "本轮核验事实与证据",
  "is_error": false,
  "reason": "未通过或受阻原因",
  "needed_check": {"session_address": "", "chat_id": "", "task_id": 0, "seq": 0},
  "skill": {"skill_id": null, "skill_name": null},
  "is_finished": true
}
"""

SUPERVISOR_DECISION_PROMPT = """
你是 Supervisor 的 decision 阶段。evaluation 结论已给出。
你负责决定当前 task 是否推进、给出下一轮 Executor 指导，必要时仅调整尚未完成的计划后缀。
不得绕过 evaluation 的否决；回溯只用于重新指导，不回滚磁盘、不重放旧工具。
is_next_target=true 仅表示当前整个 task 已满足完成条件。最后一个 task 完成时给出 final_answer。
若 is_finished=false，表示本决策仍需查询；形成明确决策后必须为 true。
最终仅输出 JSON：
{
  "is_task_list_need_change": false,
  "new_task_list": [],
  "is_next_target": false,
  "next_executor_target": "下一轮具体操作与验证要求",
  "need_date_back": false,
  "description": "决策依据",
  "is_finished": true,
  "date_back_location": [],
  "date_back_input": "",
  "modify_content": "",
  "verification_requirement": "",
  "final_answer": ""
}
"""


class PlanningResult(BaseModel):
    task_list: list[str]
    description: str = ""
    executor_guidance: str | None = None

    @field_validator("task_list")
    @classmethod
    def validate_tasks(cls, value):
        tasks = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not tasks:
            raise ValueError("task_list must contain at least one non-empty task")
        return tasks


class InitialGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    executor_guidance: str
    completion_criteria: str


class NeededCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_address: str | None = None
    chat_id: str | None = None
    task_id: int | None = None
    seq: int | None = None


class SupervisorSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: int | None = None
    skill_name: str | None = None


class SupervisorEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_passed: bool = False
    description: str = ""
    is_error: bool = False
    reason: str = ""
    needed_check: NeededCheck | None = None
    skill: SupervisorSkill | None = None
    is_finished: bool = False


class DateBack(BaseModel):
    seq: int
    task_id: int
    chat_id: str
    session_address: str


class SupervisorDecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_task_list_need_change: bool = False
    new_task_list: list[str] = Field(default_factory=list)
    is_next_target: bool
    next_executor_target: str = ""
    need_date_back: bool = False
    description: str = ""
    is_finished: bool = False
    date_back_location: list[DateBack] = Field(default_factory=list)
    date_back_input: str = ""
    modify_content: str = ""
    verification_requirement: str = ""
    final_answer: str = ""


class SupervisorHistorySummary(BaseModel):
    description: str


async def supervisor_making_plan(
    query: str,
    now_state: AgentState,
    chat_function,
    client,
    tool_registry: ToolRegistry,
    supervisor: str,
    supervisor_tools: list[dict],
    supervisor_state: SupervisorState,
    executor_tools: list[dict] | None = None,
    skill_lists: list[Skill] | None = None,
):
    supervisor_state.memory_window = []

    information = {
        "user_query": bounded_text(query, USER_QUERY_MAX_CHARS),
        "session_address": now_state.session_address,
        "chat_id": now_state.chat_id,
        "executor_tools": _tool_catalog(executor_tools or []),
        "skill_info": _skill_catalog(skill_lists or []),
    }

    prompt = _load_planning_skill()
    print("==================开启supervisor指定计划轮=====================")
    for i in range(now_state.max_supervision_times):
        print(f"\n\n第{i + 1}轮")
        messages = build_model_messages(supervisor, prompt, information)
        response = await _call_supervisor_model(
            now_state, chat_function, client, supervisor, messages, supervisor_tools
        )

        if response["status"] != "success":
            supervisor_state.exit_reason = "planning_model_error"
            raise RuntimeError(response.get("message") or "planning model request failed")
        _record_supervisor_model_response(supervisor_state, "planning", response)
        calls = normalize_tool_calls(supervisor, response["tool"])
        if len(calls) > 1:
            information["last_feedback"] = (
                "一次只能调用一个工具；上一响应未执行任何工具。"
            )
            continue
        if not calls:
            result = PlanningResult.model_validate(
                parse_json_object(response.get("message"))
            )
            _apply_plan(now_state, supervisor_state, result)
            _save_supervisor_turn(
                now_state, supervisor_state, "planning", result.description
            )
            await _prepare_initial_guidance(
                now_state,
                supervisor_state,
                chat_function,
                client,
                supervisor,
            )
            return result

        call = calls[0]
        tool_result = await tool_registry.call(
            AgentRole.SUPERVISOR, call.name, call.arguments
        )
        _save_supervisor_tool_event(
            now_state, supervisor_state, call.name, call.arguments, tool_result, "planning"
        )
        messages.extend(
            build_tool_observation_messages(
                supervisor, response.get("message"), call, tool_result
            )
        )
        summary = await _call_supervisor_model(
            now_state, chat_function, client, supervisor, messages, []
        )
        if summary["status"] != "success":
            information["last_feedback"] = "历史工具已执行，但规划总结失败；不要重复调用。"
            information["latest_tool_observation"] = tool_result.to_dict()
            continue
        _record_supervisor_model_response(
            supervisor_state, "planning_summary", summary
        )
        result = PlanningResult.model_validate(
            parse_json_object(summary.get("message"))
        )
        _apply_plan(now_state, supervisor_state, result)
        _save_supervisor_turn(now_state, supervisor_state, "planning", result.description)
        await _prepare_initial_guidance(
            now_state,
            supervisor_state,
            chat_function,
            client,
            supervisor,
        )
        return result

    supervisor_state.exit_reason = "planning_limit"
    raise RuntimeError("Supervisor planning did not produce a valid non-empty task list")


async def run_supervisor_loop(
    now_state: AgentState,
    supervisor: str,
    supervisor_client,
    chat_function,
    supervisor_tools: list[dict],
    tool_registry: ToolRegistry,
    skill_lists: list[Skill],
    supervisor_state: SupervisorState,
    supervisor_evaluation_state: SupervisorEvaluationState,
    executor_state: ExecutorState,
):
    _reset_supervisor_turn(now_state, supervisor_state, supervisor_evaluation_state)
    supervisor_state.reviewed_executor_seqs = list(executor_state.passed_seq_list)
    evaluation = await evaluation_loop(
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
    )
    save_executor_review(
        supervisor_state,
        list(executor_state.passed_seq_list),
        evaluation.is_passed,
        evaluation.reason,
    )
    decision = await making_next_plan_loop(
        now_state,
        supervisor,
        supervisor_client,
        chat_function,
        supervisor_tools,
        tool_registry,
        supervisor_state,
        supervisor_evaluation_state,
        executor_state,
    )
    return decision


async def evaluation_loop(
    now_state: AgentState,
    supervisor: str,
    supervisor_client,
    chat_function,
    supervisor_tools: list[dict],
    tool_registry: ToolRegistry,
    skill_lists: list[Skill],
    supervisor_state: SupervisorState,
    supervisor_evaluation_state: SupervisorEvaluationState,
    executor_state: ExecutorState,
):
    selected_skill = None

    print("================进入supervisor_evaluation阶段=================")
    for i in range(now_state.max_supervision_times):
        print(f"\n\n第{i+1}轮")
        messages = _build_supervisor_evaluation_message(
            executor_state,
            now_state,
            skill_lists,
            supervisor,
            supervisor_evaluation_state,
            selected_skill,
        )
        response = await _call_supervisor_model(
            now_state,
            chat_function,
            supervisor_client,
            supervisor,
            messages,
            supervisor_tools,
        )
        if response["status"] != "success":
            supervisor_evaluation_state.not_pass_reason = (
                response.get("message") or "evaluation model request failed"
            )
            break
        _record_supervisor_model_response(supervisor_state, "evaluation", response)
        calls = normalize_tool_calls(supervisor, response["tool"])
        if len(calls) > 1:
            _evaluation_memory(
                supervisor_evaluation_state,
                "一次返回多个工具调用，未执行；请一次只查询一个。",
            )
            continue

        if not calls:
            result = SupervisorEvaluation.model_validate(
                parse_json_object(response.get("message"))
            )
        else:
            call = calls[0]
            tool_result = await tool_registry.call(
                AgentRole.SUPERVISOR, call.name, call.arguments
            )
            _save_supervisor_tool_event(
                now_state,
                supervisor_state,
                call.name,
                call.arguments,
                tool_result,
                "evaluation",
            )
            messages.extend(
                build_tool_observation_messages(
                    supervisor, response.get("message"), call, tool_result
                )
            )
            summary = await _call_supervisor_model(
                now_state,
                chat_function,
                supervisor_client,
                supervisor,
                messages,
                [],
            )
            if summary["status"] != "success":
                _evaluation_memory(
                    supervisor_evaluation_state,
                    "检查工具已执行，但总结失败；保留观察且不重复执行。",
                )
                continue
            _record_supervisor_model_response(
                supervisor_state, "evaluation_summary", summary
            )
            result = SupervisorEvaluation.model_validate(
                parse_json_object(summary.get("message"))
            )

        _apply_evaluation(supervisor_evaluation_state, supervisor_state, result)
        loaded = _load_supervisor_skill(skill_lists, result.skill)
        if loaded is not None:
            selected_skill = loaded
        _save_supervisor_turn(
            now_state, supervisor_state, "evaluation", result.description
        )
        if result.is_finished:
            return result

    blocked = SupervisorEvaluation(
        is_passed=False,
        description="Supervisor 验收达到上限，未形成充分结论。",
        is_error=True,
        reason=(
            supervisor_evaluation_state.not_pass_reason
            or "evaluation budget exhausted"
        ),
        is_finished=True,
    )
    _apply_evaluation(supervisor_evaluation_state, supervisor_state, blocked)
    supervisor_evaluation_state.blocked = True
    _save_supervisor_turn(now_state, supervisor_state, "evaluation_blocked", blocked.description)
    return blocked


async def making_next_plan_loop(
    now_state: AgentState,
    supervisor: str,
    supervisor_client,
    chat_function,
    supervisor_tools: list[dict],
    tool_registry: ToolRegistry,
    supervisor_state: SupervisorState,
    supervisor_evaluation_state: SupervisorEvaluationState,
    executor_state: ExecutorState,
):

    print("===============进入supervisor_making_decision阶段======================")
    for i in range(now_state.max_supervision_times):

        print(f"\n\n第{i+1}轮")
        messages = _build_decision_message(
            now_state,
            supervisor_state,
            supervisor_evaluation_state,
            executor_state,
            supervisor,
        )
        response = await _call_supervisor_model(
            now_state,
            chat_function,
            supervisor_client,
            supervisor,
            messages,
            supervisor_tools,
        )

        if response["status"] != "success":
            supervisor_state.exit_reason = (
                response.get("message") or "decision model request failed"
            )
            break
        _record_supervisor_model_response(supervisor_state, "decision", response)
        calls = normalize_tool_calls(supervisor, response["tool"])
        if len(calls) > 1:
            supervisor_state.memory_window.append(
                "一次返回多个工具调用，未执行；请一次只查询一个。"
            )
            continue
        if not calls:
            decision_response = response
        else:
            call = calls[0]
            tool_result = await tool_registry.call(
                AgentRole.SUPERVISOR, call.name, call.arguments
            )
            _save_supervisor_tool_event(
                now_state,
                supervisor_state,
                call.name,
                call.arguments,
                tool_result,
                "decision",
            )
            messages.extend(
                build_tool_observation_messages(
                    supervisor, response.get("message"), call, tool_result
                )
            )
            summary = await _call_supervisor_model(
                now_state,
                chat_function,
                supervisor_client,
                supervisor,
                messages,
                [],
            )
            if summary["status"] != "success":
                supervisor_state.memory_window.append(
                    "决策查询工具已执行，但总结失败；不重复工具。"
                )
                continue
            decision_response = summary
            _record_supervisor_model_response(
                supervisor_state, "decision_summary", summary
            )

        try:
            result = SupervisorDecisionResult.model_validate(
                parse_json_object(decision_response.get("message"))
            )
        except (ValueError, ValidationError) as exc:
            supervisor_state.validation_error = str(exc)
            feedback = (
                "decision JSON 校验失败，未应用任务推进："
                f"{bounded_text(exc, 1200)}。"
                "下一次必须返回完整 JSON，尤其必须包含 is_next_target。"
            )
            supervisor_state.memory_window.append(feedback)
            del supervisor_state.memory_window[:-10]
            _save_supervisor_turn(
                now_state,
                supervisor_state,
                "decision_validation_error",
                feedback,
            )
            continue

        _validate_decision_plan(now_state, result)
        _apply_decision(supervisor_state, result)
        _save_supervisor_turn(now_state, supervisor_state, "decision", result.description)
        if result.is_finished:
            return result

    blocked = SupervisorDecisionResult(
        is_next_target=False,
        next_executor_target="Supervisor 决策未完成，需人工检查验收记录后继续。",
        description="Supervisor 决策达到上限。",
        is_finished=True,
    )
    supervisor_state.exit_reason = "decision_budget_exhausted"
    _apply_decision(supervisor_state, blocked)
    _save_supervisor_turn(now_state, supervisor_state, "decision_blocked", blocked.description)
    return blocked


async def summarize_supervisor_descriptions(
    now_state: AgentState,
    supervisor_state: SupervisorState,
    supervisor: str,
    supervisor_client,
    chat_function,
    status: str,
    answer: str,
    block_reason: str,
):
    """Summarize bounded Supervisor turn descriptions without changing decisions."""
    prompt = """
你只负责总结本次 Agent 请求的 Supervisor 工作轨迹，不重新规划、不重新验收，也不调用工具。
根据按顺序提供的 planning、initial_guidance、evaluation、decision 描述，概括：
1. 计划与关键指导；
2. 主要验收和决策结论；
3. 最终是否完成；若受阻，说明尚缺什么。
不得把未验证操作写成完成，不得编造工具结果。仅返回 JSON：
{"description": "面向后续上下文恢复的精炼总结"}
"""
    response = await _call_supervisor_model(
        now_state,
        chat_function,
        supervisor_client,
        supervisor,
        build_model_messages(
            supervisor,
            prompt,
            {
                "user_query": bounded_text(
                    now_state.user_query, USER_QUERY_MAX_CHARS
                ),
                "status": status,
                "answer": bounded_text(answer, DESCRIPTION_MAX_CHARS),
                "block_reason": block_reason,
                "supervisor_descriptions": (
                    now_state.supervisor_descriptions.model_context()
                ),
            },
        ),
        [],
    )
    if response["status"] != "success" or response["tool"]:
        raise RuntimeError(
            response.get("message") or "Supervisor final summary failed"
        )
    summary = SupervisorHistorySummary.model_validate(
        parse_json_object(response.get("message"))
    )
    description = bounded_text(
        summary.description, DESCRIPTION_MAX_CHARS, keep_tail=True
    )
    _save_supervisor_turn(
        now_state, supervisor_state, "final_summary", description
    )
    return description


def _build_supervisor_evaluation_message(
    executor_state,
    now_state,
    skill_list,
    supervisor,
    supervisor_evaluation_state,
    selected_skill=None,
):
    information = {
        "session_address": now_state.session_address,
        "chat_id": now_state.chat_id,
        "task_id": now_state.now_task_id,
        "target": now_state.now_target,
        "executor_target": executor_state.supervisor_guidance,
        "executor_results": executor_state.output_content,
        "executor_is_finished": executor_state.is_finished,
        "executor_record_locators": [
            {
                "session_address": now_state.session_address,
                "chat_id": now_state.chat_id,
                "task_id": now_state.now_task_id,
                "seq": seq,
            }
            for seq in executor_state.passed_seq_list
        ],
        "executor_is_error": executor_state.is_error,
        "executor_error_message": executor_state.error_message,
        "skills": _skill_catalog(skill_list),
        "supervisor_history": list(supervisor_evaluation_state.memory_window)[-10:],
    }
    if selected_skill is not None:
        information["selected_skill"] = {
            "name": selected_skill.name,
            "content": _read_skill(selected_skill),
        }
    return build_model_messages(supervisor, SUPERVISOR_EVALUATION_PROMPT, information)


def _build_decision_message(
    now_state,
    supervisor_state,
    supervisor_evaluation_state,
    executor_state,
    supervisor,
):
    information = {
        "session_address": now_state.session_address,
        "chat_id": now_state.chat_id,
        "task_list": list(now_state.task_list),
        "now_task_id": now_state.now_task_id,
        "now_target": now_state.now_target,
        "executor_target": executor_state.supervisor_guidance,
        "supervisor_evaluation_result": supervisor_evaluation_state.output_content,
        "executor_is_passed": supervisor_evaluation_state.is_executor_passed,
        "executor_not_pass_reason": supervisor_evaluation_state.not_pass_reason,
        "executor_is_error": supervisor_evaluation_state.is_executor_error,
        "executor_error_message": supervisor_evaluation_state.executor_error_message,
        "memory_window": list(supervisor_state.memory_window)[-10:],
    }
    return build_model_messages(supervisor, _load_decision_prompt(), information)


async def _call_supervisor_model(
    now_state, chat_function, client, provider, messages, tools
):
    if now_state.model_request_count >= now_state.max_model_requests:
        return {"status": "error", "message": "model request budget exhausted", "tool": []}
    now_state.model_request_count += 1
    response = await call_chat_function(
        chat_function,
        provider,
        client,
        now_state.supervisor_model,
        messages,
        tools,
        now_state.temperature,
    )
    now_state.total_token += response.get("total_tokens", 0)
    return response


async def _prepare_initial_guidance(
    now_state,
    supervisor_state,
    chat_function,
    client,
    supervisor,
):
    prompt = """
规划已经完成，当前还没有任何 Executor 输出。不要进行验收。
只分析 task_list 第 0 项，为 Executor 生成具体操作指导和可核验完成条件。
仅返回 JSON：
{
  "description": "为什么这样指导",
  "executor_guidance": "具体执行步骤",
  "completion_criteria": "必须提供的完成证据"
}
"""
    messages = build_model_messages(
        supervisor,
        prompt,
        {
            "user_query": bounded_text(now_state.user_query, USER_QUERY_MAX_CHARS),
            "task_list": now_state.task_list,
            "now_task_id": 0,
            "now_target": now_state.now_target,
        },
    )
    response = await _call_supervisor_model(
        now_state, chat_function, client, supervisor, messages, []
    )
    if response["status"] != "success" or response["tool"]:
        raise RuntimeError("Supervisor failed to produce initial executor guidance")
    guidance = InitialGuidance.model_validate(
        parse_json_object(response.get("message"))
    )
    supervisor_state.executor_plan = (
        guidance.executor_guidance
        + "\n完成条件："
        + guidance.completion_criteria
    )
    supervisor_state.verification_requirement = guidance.completion_criteria
    supervisor_state.description = guidance.description
    supervisor_state.output_content = guidance.description
    _save_supervisor_turn(
        now_state, supervisor_state, "initial_guidance", guidance.description
    )


def _apply_plan(now_state, supervisor_state, result):
    now_state.task_list = list(result.task_list)
    now_state.now_task_id = 0
    now_state.now_target = now_state.task_list[0]
    supervisor_state.task_id = 0
    supervisor_state.target = now_state.now_target
    supervisor_state.description = result.description
    supervisor_state.output_content = result.description
    supervisor_state.executor_plan = (
        result.executor_guidance
        or f"执行当前任务并提供可核验结果：{now_state.now_target}"
    )


def _apply_evaluation(evaluation_state, supervisor_state, result):
    evaluation_state.is_executor_passed = result.is_passed
    evaluation_state.is_executor_error = result.is_error
    evaluation_state.executor_error_message = result.reason
    evaluation_state.not_pass_reason = result.reason
    evaluation_state.description = result.description
    evaluation_state.output_content = result.description
    evaluation_state.is_finished = result.is_finished
    _evaluation_memory(evaluation_state, result.description)
    supervisor_state.is_executor_passed = result.is_passed
    supervisor_state.is_executor_error = result.is_error
    supervisor_state.executor_error_message = result.reason
    supervisor_state.description = result.description
    supervisor_state.output_content = result.description


def _apply_decision(supervisor_state, result):
    supervisor_state.executor_plan = result.next_executor_target
    supervisor_state.need_date_back = result.need_date_back
    supervisor_state.date_back_locations = [
        item.model_dump() for item in result.date_back_location
    ]
    supervisor_state.date_back_input = result.date_back_input
    supervisor_state.modify_content = result.modify_content
    supervisor_state.verification_requirement = result.verification_requirement
    supervisor_state.description = result.description
    supervisor_state.output_content = result.description
    supervisor_state.is_next_target = result.is_next_target
    supervisor_state.proposed_task_list = (
        list(result.new_task_list) if result.is_task_list_need_change else None
    )
    supervisor_state.decision_finished = result.is_finished
    supervisor_state.final_answer = result.final_answer


def _reset_supervisor_turn(now_state, supervisor_state, evaluation_state):
    supervisor_state.task_id = now_state.now_task_id
    supervisor_state.target = now_state.now_target
    supervisor_state.memory_window = []
    supervisor_state.tool_name = ""
    supervisor_state.tool_arguments = {}
    supervisor_state.tool_result = None
    supervisor_state.tool_ok = None
    supervisor_state.is_next_target = False
    supervisor_state.proposed_task_list = None
    supervisor_state.reviewed_executor_seqs = []
    supervisor_state.decision_finished = False
    supervisor_state.exit_reason = ""
    supervisor_state.task_attempt = now_state.task_attempt
    supervisor_state.model_stage = ""
    supervisor_state.raw_model_response_excerpt = ""
    supervisor_state.validation_error = ""
    evaluation_state.supervisor_seq = now_state.supervisor_seq
    evaluation_state.chat_id = now_state.chat_id
    evaluation_state.task_id = now_state.now_task_id
    evaluation_state.target = now_state.now_target
    evaluation_state.input_content = ""
    evaluation_state.memory_window = []
    evaluation_state.output_content = ""
    evaluation_state.description = ""
    evaluation_state.is_executor_passed = False
    evaluation_state.not_pass_reason = ""
    evaluation_state.is_executor_error = False
    evaluation_state.executor_error_message = ""
    evaluation_state.tool_name = ""
    evaluation_state.tool_result = None
    evaluation_state.is_finished = False
    evaluation_state.blocked = False


def _record_supervisor_model_response(supervisor_state, stage, response):
    supervisor_state.model_stage = stage
    supervisor_state.raw_model_response_excerpt = bounded_text(
        response.get("message"), 4000, keep_tail=True
    )
    supervisor_state.validation_error = ""


def _next_supervisor_seq(now_state, supervisor_state):
    now_state.supervisor_seq += 1
    supervisor_state.supervisor_seq = now_state.supervisor_seq
    return now_state.supervisor_seq


def _save_supervisor_tool_event(
    now_state, supervisor_state, tool_name, arguments, tool_result, phase
):
    _next_supervisor_seq(now_state, supervisor_state)
    supervisor_state.tool_name = tool_name
    supervisor_state.tool_arguments = arguments
    supervisor_state.tool_result = tool_result
    supervisor_state.tool_ok = tool_result.ok
    supervisor_state.description = f"{phase} 调用 {tool_name}"
    supervisor_state.input_content = arguments
    supervisor_state.output_content = tool_result.to_dict()
    save_supervisor_state_history(supervisor_state, record_type=f"{phase}_tool_event")


def _save_supervisor_turn(now_state, supervisor_state, phase, description):
    _next_supervisor_seq(now_state, supervisor_state)
    supervisor_state.description = description or ""
    supervisor_state.output_content = description or supervisor_state.output_content
    save_supervisor_state_history(supervisor_state, record_type=f"{phase}_turn")
    now_state.supervisor_descriptions.append_turn(
        supervisor_seq=supervisor_state.supervisor_seq,
        task_id=supervisor_state.task_id,
        target=supervisor_state.target,
        phase=phase,
        description=supervisor_state.description,
    )


def _evaluation_memory(state, description):
    if description:
        state.memory_window.append(
            bounded_text(description, DESCRIPTION_MAX_CHARS, keep_tail=True)
        )
        del state.memory_window[:-10]


def _validate_decision_plan(now_state, result):
    if not result.is_task_list_need_change:
        return
    cleaned = [
        item.strip()
        for item in result.new_task_list
        if isinstance(item, str) and item.strip()
    ]
    completed_prefix = now_state.task_list[: now_state.now_task_id]
    if len(cleaned) <= now_state.now_task_id:
        raise ValueError("new_task_list removed the current task position")
    if cleaned[: now_state.now_task_id] != completed_prefix:
        raise ValueError("new_task_list cannot rewrite completed tasks")
    result.new_task_list = cleaned


def _load_planning_skill():
    try:
        return PLAN_SKILL_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"planning skill not available: {PLAN_SKILL_PATH}") from exc


def _load_planning_prompt():
    return _load_planning_skill()


def _load_decision_prompt():
    try:
        return DECISION_SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return SUPERVISOR_DECISION_PROMPT


def _tool_catalog(tools):
    catalog = []
    for tool in tools:
        function = tool.get("function", tool)
        catalog.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters")
                or function.get("input_schema")
                or {},
            }
        )
    return catalog


def _skill_catalog(skills):
    return [
        {"id": item.id, "name": item.name, "description": item.description}
        for item in skills
    ]


def _load_supervisor_skill(skills, selection):
    if selection is None or selection.skill_id is None or not selection.skill_name:
        return None
    selected = next((item for item in skills if item.id == selection.skill_id), None)
    if selected is None or selected.name != selection.skill_name:
        return None
    if not Path(selected.address).is_file():
        return None
    return selected


def _read_skill(skill):
    try:
        return bounded_text(
            Path(skill.address).read_text(encoding="utf-8"),
            DESCRIPTION_MAX_CHARS,
            keep_tail=True,
        )
    except OSError:
        return ""


def _update_supervisor_evaluation_state(
    supervisor_evaluation_state,
    supervisor_state,
    supervisor_results,
    tool_name="",
    tool_results=None,
    input_content=None,
    output_content="",
):
    """Compatibility helper for older callers."""
    _apply_evaluation(supervisor_evaluation_state, supervisor_state, supervisor_results)
    supervisor_evaluation_state.tool_name = tool_name
    supervisor_evaluation_state.tool_result = tool_results
    supervisor_evaluation_state.input_content = input_content or []
    if output_content:
        supervisor_evaluation_state.output_content = output_content


def _update_Agent_state(now_state, supervisor_state, message):
    now_state.supervisor_input = message
    now_state.supervisor_output = supervisor_state.output_content
    now_state.supervisor_seq = supervisor_state.supervisor_seq
