from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from AgentLoop.loop_utils import (
    DESCRIPTION_MAX_CHARS,
    TOOL_RESULT_CONTEXT_MAX_CHARS,
    USER_QUERY_MAX_CHARS,
    bounded_text,
    build_model_messages,
    build_tool_observation_messages,
    call_chat_function,
    normalize_tool_calls,
    parse_json_object,
)
from MCP_functions.tool_registry import (
    SUPERVISOR_ONLY_TOOLS,
    AgentRole,
    ToolRegistry,
)
from State.save_executor_state_history import save_executor_review
from State.save_supervision_state_history import save_supervisor_state_history
from State.save_tool_result import ToolSummary, save_tool_result

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
MEMORY_RETRIEVAL_SKILL_NAME = "memory_retrieval.md"

SUPERVISOR_EVALUATION_PROMPT = """
你是 Supervisor 的 evaluation 阶段，只验收上一轮 Executor 的委派目标，不推进 task、不改变完成条件。

验收顺序：
1. 先用 executor_results、tool_summaries、tool_result locator、错误字段、completion_criteria 和 supervisor_handoff。description 是可验收摘要，locator 是可追溯引用；不要为了“更放心”无条件展开原始结果。
2. 只有摘要存在具体缺口、相互矛盾、工具失败或必须核对某个正文事实时，才调用一个只读记忆工具精确查询；查询到足够证据立即停止，不重复读取同一 locator。
3. 工具成功但模型总结失败时，不能判成工具失败；结合 locator、tool_ok 和可见元数据判断。输出截断也不等于操作失败，只能说明被截断部分尚未核验。
4. execution_mode=synthesize 时，允许依据 dependency_context 中已验收的 TaskOutcome 验证综合结果；不要要求综合任务重新产生工具调用。
5. 只按锁定的 completion_criteria 判断，不临时增加“必须看到更多文件/原文”的要求。达到最小充分证据即可通过；不得用穷举所有文件代替验收。
6. is_passed 只表示本轮委派目标通过；整个 task 是否推进由 decision 决定。is_error 只表示真实执行/工具错误，证据不足但无错误时保持 false。
7. description 写简洁、可复用的核验事实和依据；reason 只写未通过或受阻的具体缺口。

每步最多调用一个工具。需要继续查询时 is_finished=false；形成明确结论时为 true。无工具调用时仅返回 JSON：
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
你是 Supervisor 的 decision 阶段。evaluation 已完成事实验收；你只做控制决策，不重复验收、不执行任务。

规则：
1. evaluation 未通过时不得推进。下一轮指导只针对明确缺口，复用已接受工作和 locator，不要求重做已验证部分。
2. evaluation 通过后，对照当前 task 的原始 completion_criteria 判断整个 task；不得在 decision 阶段新增验收条件。
3. is_next_target=true 只表示当前整个 task 已完成。若仍留在当前 task，next_executor_target 必须是一项具体、最小、可验证的下一步。
4. 推进下一 task 时，next_executor_target 说明如何利用前序 TaskOutcome。下一项仍需新外部证据时 next_execution_mode=execute；只需总结、归纳或形成最终回答时必须为 synthesize。
5. synthesize 会禁用 Executor 工具，因此只有 dependency_context 已足够支撑下一目标时才选择；不要让综合任务重新读取相同文件。
6. 最后一个 task 完成时，final_answer 必须是可直接交付用户的实际答案，不是“任务已完成”等状态说明；不要再生成不存在的下一步。
7. 只有确实缺少决策信息时才调用一个只读记忆工具；回溯只用于重新指导，不回滚磁盘、不重放旧工具。
8. description 简洁说明控制决策依据，不重复粘贴 Executor 全部产出。

需要继续查询时 is_finished=false；形成明确决策后为 true。无工具调用时仅返回 JSON：
{
  "is_task_list_need_change": false,
  "new_task_list": [],
  "is_next_target": false,
  "next_executor_target": "下一轮具体操作与验证要求",
  "next_execution_mode": "execute",
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
    model_config = ConfigDict(extra="forbid")

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
    next_execution_mode: Literal["execute", "synthesize"] = "execute"
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
    rounds = 0
    while rounds < now_state.max_supervision_times:
        print(f"\n\n第{rounds + 1}轮")
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
            information["last_feedback"] = _multiple_tool_call_feedback(calls)
            continue
        rounds += 1

        if not calls:
            try:
                result = PlanningResult.model_validate(
                    parse_json_object(response.get("message"))
                )
            except (ValueError, ValidationError) as exc:
                _save_validation_error(
                    now_state, supervisor_state, "planning", exc, information
                )
                continue
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
        summary, result = await _validated_tool_summary(
            now_state, chat_function, client, supervisor, messages,
            PlanningResult, supervisor_state, "planning_summary"
        )
        if result is None:
            supervisor_state.exit_reason = "planning_summary_validation_exhausted"
            raise RuntimeError(
                "Supervisor planning tool succeeded but summary validation exhausted"
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
    supervisor_state.reviewed_executor_seqs = [
        *executor_state.tool_event_seqs,
        *(
            [executor_state.executor_turn_seq]
            if executor_state.executor_turn_seq is not None
            else []
        ),
    ]
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
        list(executor_state.tool_event_seqs),
        executor_state.executor_turn_seq,
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
    # Memory retrieval is not an optional evaluation technique: every evaluation
    # must know the retrieval order, while still avoiding queries when the
    # Executor feedback is already sufficient.
    selected_skill = _find_supervisor_skill(
        skill_lists, MEMORY_RETRIEVAL_SKILL_NAME
    )
    memory_query_results = []
    queried_memory_calls = set()

    print("================进入supervisor_evaluation阶段=================")
    rounds = 0
    while rounds < now_state.max_supervision_times:
        print(f"\n\n第{rounds + 1}轮")
        messages = _build_supervisor_evaluation_message(
            executor_state,
            now_state,
            skill_lists,
            supervisor,
            supervisor_evaluation_state,
            selected_skill,
            memory_query_results,
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
                _multiple_tool_call_feedback(calls),
            )
            continue
        rounds += 1

        if not calls:
            try:
                result = SupervisorEvaluation.model_validate(
                    parse_json_object(response.get("message"))
                )
            except (ValueError, ValidationError) as exc:
                _save_evaluation_validation_error(
                    now_state, supervisor_state,
                    supervisor_evaluation_state, exc
                )
                continue
        else:
            call = calls[0]
            query_fingerprint = _memory_query_fingerprint(
                call.name, call.arguments
            )
            if query_fingerprint in queried_memory_calls:
                _evaluation_memory(
                    supervisor_evaluation_state,
                    (
                        f"同一记忆查询已执行，不再重复：{call.name} "
                        f"{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}。"
                        "请使用已有查询结果，选择下一层查询或形成验收结论。"
                    ),
                )
                continue
            tool_result = await tool_registry.call(
                AgentRole.SUPERVISOR, call.name, call.arguments
            )
            queried_memory_calls.add(query_fingerprint)
            _save_supervisor_tool_event(
                now_state,
                supervisor_state,
                call.name,
                call.arguments,
                tool_result,
                "evaluation",
            )
            memory_query_results.append(
                _memory_query_observation(
                    call.name,
                    call.arguments,
                    tool_result,
                    supervisor_state.last_tool_result_ref,
                )
            )
            del memory_query_results[:-6]
            _evaluation_memory(
                supervisor_evaluation_state,
                (
                    f"已完成记忆查询 {call.name}；结果已放入 memory_query_results。"
                    "请先判断现有信息是否足够，不足时按 memory_retrieval 规则查询下一层。"
                ),
            )
            continue

        _apply_evaluation(supervisor_evaluation_state, supervisor_state, result)
        _append_supervisor_tool_summaries(
            supervisor_state, memory_query_results, result.description
        )
        loaded = _load_supervisor_skill(skill_lists, result.skill)
        if loaded is not None:
            selected_skill = loaded
        supervisor_state.evaluation_seq = _save_supervisor_turn(
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
    rounds = 0
    while rounds < now_state.max_supervision_times:

        print(f"\n\n第{rounds + 1}轮")
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
                _multiple_tool_call_feedback(calls)
            )
            del supervisor_state.memory_window[:-10]
            continue
        rounds += 1
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
            summary, parsed_result = await _validated_tool_summary(
                now_state, chat_function, supervisor_client, supervisor, messages,
                SupervisorDecisionResult, supervisor_state, "decision_summary",
            )
            if parsed_result is None:
                supervisor_state.exit_reason = (
                    "decision_summary_validation_exhausted"
                )
                break
            decision_response = summary

        try:
            result = SupervisorDecisionResult.model_validate(
                parse_json_object(decision_response.get("message"))
            )
            _validate_decision_plan(now_state, result)
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

        _apply_decision(
            now_state, supervisor_state, supervisor_evaluation_state,
            executor_state, result
        )
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
    _apply_decision(
        now_state, supervisor_state, supervisor_evaluation_state,
        executor_state, blocked
    )
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
你只生成本次请求的持久化工作摘要，不重新规划、验收或调用工具。
优先依据 status、answer、block_reason、已验收 task_outcomes 和 Supervisor 顺序记录，概括：
1. 已完成并验收的 task 及关键证据；
2. 重要决策与仍有效的约束；
3. 最终交付状态；若受阻，只写直接阻塞点和仍缺内容。
不要逐轮复述、复制完整工具输出或把未验收操作写成完成。若 answer 已是完整交付，只概括其依据，
不另造结论。仅返回 JSON：
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
                "task_outcomes": [
                    outcome.to_dict()
                    for _, outcome in sorted(now_state.task_outcomes.items())
                ],
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
    memory_query_results=None,
):
    information = {
        "session_address": now_state.session_address,
        "chat_id": now_state.chat_id,
        "task_id": now_state.now_task_id,
        "target": now_state.now_target,
        "executor_target": executor_state.supervisor_guidance,
        "executor_results": executor_state.output_content,
        "supervisor_handoff": executor_state.input_content.get(
            "supervisor_handoff", {}
        ) if isinstance(executor_state.input_content, dict) else {},
        "original_completion_criteria": now_state.task_completion_criteria.get(
            now_state.now_task_id, ""
        ),
        "executor_is_finished": executor_state.is_finished,
        "executor_record_locators": [
            {
                "session_address": now_state.session_address,
                "chat_id": now_state.chat_id,
                "task_id": now_state.now_task_id,
                "seq": seq,
            }
            for seq in executor_state.tool_event_seqs
        ],
        "executor_tool_result_locators": list(executor_state.tool_result_refs),
        "tool_summaries": list(executor_state.tool_summaries),
        "memory_query_results": list(memory_query_results or []),
        "executor_turn_locator": {
            "session_address": now_state.session_address,
            "chat_id": now_state.chat_id,
            "task_id": now_state.now_task_id,
            "seq": executor_state.executor_turn_seq,
        },
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
        "original_completion_criteria": now_state.task_completion_criteria.get(
            now_state.now_task_id, ""
        ),
        "executor_tool_locators": list(
            now_state.task_evidence_locators.get(now_state.now_task_id, [])
        ),
        "executor_turn_locator": {
            "task_id": now_state.now_task_id,
            "seq": executor_state.executor_turn_seq,
        },
        "evaluation_locator": {
            "task_id": now_state.now_task_id,
            "seq": supervisor_state.evaluation_seq,
        },
        "accepted_work": list(
            now_state.task_accepted_work.get(now_state.now_task_id, [])
        ),
        "remaining_work": list(
            now_state.task_remaining_work.get(now_state.now_task_id, [])
        ),
        "completed_task_outcomes": [
            outcome.to_dict()
            for task_id, outcome in sorted(now_state.task_outcomes.items())
            if task_id <= now_state.now_task_id
        ],
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
规划已经完成，当前没有 Executor 输出。这里只为 task_list[0] 建立第一次 handoff，不进行验收。
executor_guidance 应说明当前 task 的最小执行范围、优先工具/路径、停止条件和必要异常处理；
不要要求一次穷举整个项目，也不要提前执行后续 task。completion_criteria 必须是稳定、最小充分、
可由真实结果核验的证据标准，后续不得因为“更放心”随意扩大。仅返回 JSON：
{
  "description": "指导依据与范围边界",
  "executor_guidance": "当前 task 的具体步骤、停止条件和证据记录方式",
  "completion_criteria": "锁定的最小充分完成证据"
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
    guidance = None
    retry_messages = list(messages)
    for _ in range(now_state.max_supervision_times):
        response = await _call_supervisor_model(
            now_state, chat_function, client, supervisor, retry_messages, []
        )
        if response["status"] != "success" or response["tool"]:
            feedback = "initial guidance 必须返回无工具调用的完整 JSON。"
        else:
            try:
                guidance = InitialGuidance.model_validate(
                    parse_json_object(response.get("message"))
                )
                break
            except (ValueError, ValidationError) as exc:
                feedback = _validation_feedback("initial guidance", exc)
                supervisor_state.validation_error = str(exc)
        retry_messages = [
            *retry_messages, {"role": "user", "content": feedback}
        ]
        _save_supervisor_turn(
            now_state, supervisor_state, "initial_guidance_validation_error", feedback
        )
    if guidance is None:
        supervisor_state.exit_reason = "initial_guidance_budget_exhausted"
        raise RuntimeError("Supervisor initial guidance did not produce valid JSON")
    supervisor_state.executor_plan = (
        guidance.executor_guidance
        + "\n完成条件："
        + guidance.completion_criteria
    )
    supervisor_state.verification_requirement = guidance.completion_criteria
    now_state.task_completion_criteria[0] = guidance.completion_criteria
    supervisor_state.original_completion_criteria = guidance.completion_criteria
    from AgentLoop.agent import SupervisorHandoff
    handoff = SupervisorHandoff(
        supervisor_seq=now_state.supervisor_seq,
        task_id=0,
        target=now_state.now_target,
        instruction=guidance.executor_guidance,
        completion_criteria=guidance.completion_criteria,
    )
    now_state.task_handoffs[0] = handoff
    supervisor_state.handoff = handoff
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


def _apply_decision(now_state, supervisor_state, evaluation_state, executor_state, result):
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
    task_id = now_state.now_task_id
    previous_work = list(now_state.task_previous_work.get(task_id, []))
    if executor_state.output_content and executor_state.output_content not in previous_work:
        previous_work.append(executor_state.output_content)
    accepted_work = list(now_state.task_accepted_work.get(task_id, []))
    if evaluation_state.is_executor_passed and executor_state.output_content:
        if executor_state.output_content not in accepted_work:
            accepted_work.append(executor_state.output_content)
    remaining_work = [] if result.is_next_target else [
        item for item in (
            result.next_executor_target,
            result.verification_requirement,
            evaluation_state.not_pass_reason,
        ) if item
    ]
    now_state.task_accepted_work[task_id] = accepted_work
    now_state.task_remaining_work[task_id] = remaining_work
    current_handoff = now_state.task_handoffs.get(task_id)
    if result.is_next_target and evaluation_state.is_executor_passed:
        from AgentLoop.agent import TaskOutcome

        accepted_source = (
            accepted_work[-1] if accepted_work else evaluation_state.description
        )
        now_state.task_outcomes[task_id] = TaskOutcome(
            task_id=task_id,
            target=now_state.now_target,
            accepted_summary=bounded_text(
                accepted_source, 3500, keep_tail=True
            ),
            verification_summary=bounded_text(
                evaluation_state.description, 1500, keep_tail=True
            ),
            evidence_references=list(
                now_state.task_evidence_references.get(task_id, [])
            ),
            reusable_read_calls=list(
                now_state.task_reusable_read_calls.get(task_id, [])
            ),
        )

    from AgentLoop.agent import SupervisorHandoff
    handoff = SupervisorHandoff(
        supervisor_seq=now_state.supervisor_seq,
        task_id=task_id,
        target=now_state.now_target,
        instruction=result.next_executor_target,
        previous_work=previous_work,
        accepted_work=accepted_work,
        remaining_work=remaining_work,
        completion_criteria=now_state.task_completion_criteria.get(task_id, ""),
        evidence_locators=list(now_state.task_evidence_locators.get(task_id, [])),
        completed_tool_calls=list(
            now_state.task_completed_tool_calls.get(task_id, [])
        ),
        dependency_context=(
            list(current_handoff.dependency_context) if current_handoff else []
        ),
        execution_mode=(
            current_handoff.execution_mode if current_handoff else "execute"
        ),
    )
    now_state.task_handoffs[task_id] = handoff
    supervisor_state.handoff = handoff
    supervisor_state.original_completion_criteria = handoff.completion_criteria
    supervisor_state.accepted_work = accepted_work
    supervisor_state.remaining_work = remaining_work


def _reset_supervisor_turn(now_state, supervisor_state, evaluation_state):
    supervisor_state.task_id = now_state.now_task_id
    supervisor_state.target = now_state.now_target
    supervisor_state.memory_window = []
    supervisor_state.tool_name = ""
    supervisor_state.tool_arguments = {}
    supervisor_state.last_tool_result_ref = None
    supervisor_state.tool_result_refs = []
    supervisor_state.tool_summaries = []
    supervisor_state.referenced_tool_result_seqs = []
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
    supervisor_state.handoff = now_state.task_handoffs.get(now_state.now_task_id)
    supervisor_state.original_completion_criteria = (
        now_state.task_completion_criteria.get(now_state.now_task_id, "")
    )
    supervisor_state.accepted_work = list(
        now_state.task_accepted_work.get(now_state.now_task_id, [])
    )
    supervisor_state.remaining_work = list(
        now_state.task_remaining_work.get(now_state.now_task_id, [])
    )
    supervisor_state.tool_event_seq = None
    supervisor_state.evaluation_seq = None
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
    evaluation_state.last_tool_result_ref = None
    evaluation_state.tool_result_refs = []
    evaluation_state.tool_summaries = []
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
    actor_turn_seq = _next_supervisor_seq(now_state, supervisor_state)
    supervisor_state.tool_name = tool_name
    supervisor_state.tool_arguments = arguments
    supervisor_state.tool_ok = tool_result.ok
    supervisor_state.last_tool_result_ref = None
    supervisor_state.referenced_tool_result_seqs = []
    if tool_name in SUPERVISOR_ONLY_TOOLS:
        supervisor_state.referenced_tool_result_seqs = (
            _collect_tool_result_seqs(tool_result.output)
        )
    else:
        now_state.tool_result_seq += 1
        locator = save_tool_result(
            session_address=now_state.session_address,
            chat_id=now_state.chat_id,
            task_id=now_state.now_task_id,
            actor="supervisor",
            actor_turn_seq=actor_turn_seq,
            phase=phase,
            tool_name=tool_name,
            arguments=arguments,
            tool_result=tool_result,
            tool_result_seq=now_state.tool_result_seq,
        )
        supervisor_state.last_tool_result_ref = locator
        supervisor_state.tool_result_refs.append(locator)
    supervisor_state.description = f"{phase} 调用 {tool_name}"
    supervisor_state.input_content = arguments
    supervisor_state.output_content = supervisor_state.description
    save_supervisor_state_history(supervisor_state, record_type=f"{phase}_tool_event")
    supervisor_state.tool_event_seq = supervisor_state.supervisor_seq
    return supervisor_state.supervisor_seq


def _collect_tool_result_seqs(value):
    found = set()
    if isinstance(value, dict):
        seq = value.get("tool_result_seq")
        if type(seq) is int:
            found.add(seq)
        for item in value.values():
            found.update(_collect_tool_result_seqs(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect_tool_result_seqs(item))
    return sorted(found)


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
    return supervisor_state.supervisor_seq


def _validation_feedback(stage, exc):
    return (
        f"{stage} JSON 或语义校验失败：{bounded_text(exc, 1200)}。"
        "请修正后只返回契约要求的完整 JSON，不要添加额外字段或 Markdown。"
    )


def _save_validation_error(now_state, supervisor_state, stage, exc, information):
    feedback = _validation_feedback(stage, exc)
    supervisor_state.validation_error = str(exc)
    information["last_feedback"] = feedback
    _save_supervisor_turn(
        now_state, supervisor_state, f"{stage}_validation_error", feedback
    )


def _save_evaluation_validation_error(
    now_state, supervisor_state, evaluation_state, exc
):
    feedback = _validation_feedback("evaluation", exc)
    supervisor_state.validation_error = str(exc)
    _evaluation_memory(evaluation_state, feedback)
    _save_supervisor_turn(
        now_state, supervisor_state, "evaluation_validation_error", feedback
    )


async def _validated_tool_summary(
    now_state,
    chat_function,
    client,
    supervisor,
    messages,
    model_type,
    supervisor_state,
    stage,
    memory_state=None,
):
    retry_messages = list(messages)
    for _ in range(now_state.max_supervision_times):
        response = await _call_supervisor_model(
            now_state, chat_function, client, supervisor, retry_messages, []
        )
        if response["status"] != "success" or response["tool"]:
            feedback = f"{stage} 必须返回无工具调用的完整 JSON。"
        else:
            _record_supervisor_model_response(supervisor_state, stage, response)
            try:
                result = model_type.model_validate(
                    parse_json_object(response.get("message"))
                )
                if isinstance(result, SupervisorDecisionResult):
                    _validate_decision_plan(now_state, result)
                locator = supervisor_state.last_tool_result_ref
                if locator is not None:
                    supervisor_state.tool_summaries.append(
                        ToolSummary(
                            tool_result_seq=locator.tool_result_seq,
                            tool_name=supervisor_state.tool_name,
                            tool_ok=bool(supervisor_state.tool_ok),
                            description=getattr(result, "description", ""),
                        )
                    )
                return response, result
            except (ValueError, ValidationError) as exc:
                supervisor_state.validation_error = str(exc)
                feedback = _validation_feedback(stage, exc)
        if memory_state is not None:
            _evaluation_memory(memory_state, feedback)
        else:
            supervisor_state.memory_window.append(feedback)
            del supervisor_state.memory_window[:-10]
        retry_messages = [
            *retry_messages, {"role": "user", "content": feedback}
        ]
        _save_supervisor_turn(
            now_state, supervisor_state, f"{stage}_validation_error", feedback
        )
    return None, None


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


def _find_supervisor_skill(skills, skill_name):
    selected = next((item for item in skills if item.name == skill_name), None)
    if selected is None or not Path(selected.address).is_file():
        return None
    return selected


def _memory_query_fingerprint(tool_name, arguments):
    return json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _memory_query_observation(tool_name, arguments, tool_result, locator=None):
    observation = {
        "tool_name": tool_name,
        "arguments": arguments,
        "ok": tool_result.ok,
        "result": bounded_text(
            tool_result.to_model_content(),
            TOOL_RESULT_CONTEXT_MAX_CHARS,
            keep_tail=True,
        ),
    }
    if locator is not None:
        observation["tool_result_ref"] = locator
    return observation


def _append_supervisor_tool_summaries(
    supervisor_state, memory_query_results, description
):
    summarized = {
        summary.tool_result_seq for summary in supervisor_state.tool_summaries
    }
    for observation in memory_query_results:
        locator = observation.get("tool_result_ref")
        if locator is None:
            continue
        if locator.tool_result_seq in summarized:
            continue
        supervisor_state.tool_summaries.append(
            ToolSummary(
                tool_result_seq=locator.tool_result_seq,
                tool_name=observation["tool_name"],
                tool_ok=bool(observation["ok"]),
                description=description or "Supervisor 已核查该工具结果。",
            )
        )
        summarized.add(locator.tool_result_seq)


def _multiple_tool_call_feedback(calls):
    returned_calls = [
        {"tool_name": call.name, "arguments": call.arguments}
        for call in calls
    ]
    return (
        "工具调用协议错误：模型一次返回了多个工具调用，本次未执行任何工具。"
        "请根据当前阶段、handoff、已有查询结果和明确缺口，重新选择当前最应该执行的一个工具；"
        "下一次只能返回一个工具调用。"
        f"本次返回：{bounded_text(json.dumps(returned_calls, ensure_ascii=False), 1600)}"
    )


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
    supervisor_evaluation_state.last_tool_result_ref = None
    supervisor_evaluation_state.input_content = input_content or []
    if output_content:
        supervisor_evaluation_state.output_content = output_content


def _update_Agent_state(now_state, supervisor_state, message):
    now_state.supervisor_input = message
    now_state.supervisor_output = supervisor_state.output_content
    now_state.supervisor_seq = supervisor_state.supervisor_seq
