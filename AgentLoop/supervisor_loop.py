from __future__ import annotations
from terminal_render import process, show_description

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

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
    validate_description,
)
from MCP_functions.tool_registry import (
    SUPERVISOR_ONLY_TOOLS,
    AgentRole,
    ToolRegistry,
)
from State.save_executor_state_history import save_executor_review
from State.save_supervision_state_history import save_supervisor_state_history
from State.save_tool_result import ToolSummary, save_tool_result
from State.task_summary import (
    append_task_summary_record,
    render_task_summary_record,
    task_summary_path,
)

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


def _positive_limit_from_env(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


TASK_OUTCOME_ACCEPTED_MAX_CHARS = _positive_limit_from_env(
    "CODING_AGENT_ACCEPTED_SUMMARY_MAX_CHARS", 3500
)
TASK_OUTCOME_VERIFICATION_MAX_CHARS = _positive_limit_from_env(
    "CODING_AGENT_VERIFICATION_SUMMARY_MAX_CHARS", 1500
)

SUPERVISOR_EVALUATION_PROMPT = """
你是 Supervisor 的 evaluation 阶段，只验收上一轮 Executor 的委派目标，不推进 task、不改变完成条件。

验收顺序：
1. 先用 executor_results、tool_summaries、tool_result locator、错误字段、completion_criteria 和 supervisor_handoff。description 是可验收摘要，locator 是可追溯引用；不要为了“更放心”无条件展开原始结果。
2. 只有摘要存在具体缺口、相互矛盾、工具失败或必须核对某个正文事实时，才调用一个只读记忆工具精确查询；查询到足够证据立即停止，不重复读取同一 locator。
3. 工具成功但模型总结失败时，不能判成工具失败；结合 locator、tool_ok 和可见元数据判断。输出截断也不等于操作失败，只能说明被截断部分尚未核验。
4. execution_mode=synthesize 时，允许依据 dependency_context 中已验收的 TaskOutcome 验证综合结果；不要要求综合任务重新产生工具调用。
5. 只按锁定的 completion_criteria 判断，不临时增加“必须看到更多文件/原文”的要求。达到最小充分证据即可通过；不得用穷举所有文件代替验收。
6. is_passed 只表示本轮委派目标通过；整个 task 是否推进由 decision 决定。is_error 只表示真实执行/工具错误，证据不足但无错误时保持 false。
7. description 必须少于 3000 个字符，只写本轮实际检查、关键结果、验收结论和剩余缺口；不得复制原始工具输出或整段历史。reason 只写未通过或受阻的具体缺口。
8. accepted_summary/verification_summary 是有损摘要，不是事实白名单。dependency_context 中成功目录工具的 evidence_references.paths 可以证明对应文件/目录存在；摘要没逐项列出不代表失效。文件存在不能证明源码职责、构建成功或部署可用；依赖/配置存在不能证明完整业务链路已运行。必须区分存在性、声明、源码行为和实际验证。
9. 综合任务以原始 completion_criteria 为准。不得新增“只能写摘要明确列出的事实”的条件；旧失败回答 previous_work 只供修正，不能当成已验收事实。遇到证据引用与摘要不一致，先按 locator 查询已有历史，不要求 Executor 重读项目。

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
7. 默认只提供历史摘要是否存在，不自动加载 task_summary.md 正文。对累计成果、旧结论是否仍有效、历史依赖或整个 task 的推进条件没有特别准确的把握时，先调用 read_task_summary，再作决定。最新执行、验收和已有 TaskOutcome 足够充分时可直接决策。需要具体原始字段时再按 locator 查询；回溯只用于重新指导，不回滚磁盘、不重放旧工具。
8. 本轮最新结果和实际验证是主要依据；按需查到的历史辅助摘要只补充仍有效成果、依赖和遗留问题。新证据推翻旧结论时必须修正，未涉及的有效旧成果继续保留。Executor 声称完成但无必要验证时不得通过。memory_query_results 是本轮实际查询结果；已返回且足够时不要重复查询，not_found 或查询失败不代表验收通过。
9. description 必须少于 3000 个字符，只说明本轮决策依据、关键结果和剩余工作，不复制原始工具输出或完整历史。
10. task_summary 是本轮更新后的累计任务状态；每次完整 decision 都必须生成。只有当前 task 最终通过时，accepted_summary 和 verification_summary 才能写正式完成成果，否则必须为空。
11. is_finished 表示本次 decision 已完成，不表示当前 task 已通过。否决并给出下一步也是完整决策：必须 is_finished=true、is_next_target=false，立即交回 Executor。只有需要具体历史查询时才继续查询；不得无工具地反复输出相同修改意见。
12. 前序 TaskOutcome 的摘要不是事实白名单。成功证据引用中的路径元数据仍可用于存在性判断，不得因摘要省略而删除事实或新增验收条件。未读正文不得推断职责，未运行不得声称验证成功。

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
  "final_answer": "",
  "task_summary": {
    "current_turn_summary": "本轮实际执行与关键结果",
    "cumulative_task_summary": "结合仍有效历史成果后的累计状态",
    "current_verification_summary": "本轮检查、结果与证据依据",
    "corrected_or_invalidated": [],
    "remaining_work": [],
    "next_step": "Supervisor 最新决定和下一步",
    "accepted_summary": "仅最终通过时填写累计完成成果",
    "verification_summary": "仅最终通过时填写通过依据"
  }
}
"""


class PlanningResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_list: list[str]
    description: str
    executor_guidance: str | None = None

    _validate_description = field_validator("description")(validate_description)

    @field_validator("task_list")
    @classmethod
    def validate_tasks(cls, value):
        tasks = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if not tasks:
            raise ValueError("task_list must contain at least one non-empty task")
        return tasks


class InitialGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    executor_guidance: str
    completion_criteria: str

    _validate_description = field_validator("description")(validate_description)


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
    description: str
    is_error: bool = False
    reason: str = ""
    needed_check: NeededCheck | None = None
    skill: SupervisorSkill | None = None
    is_finished: bool = False

    _validate_description = field_validator("description")(validate_description)


class DateBack(BaseModel):
    seq: int
    task_id: int
    chat_id: str
    session_address: str


class TaskSummaryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_turn_summary: str
    cumulative_task_summary: str
    current_verification_summary: str
    corrected_or_invalidated: list[str] = Field(default_factory=list)
    remaining_work: list[str] = Field(default_factory=list)
    next_step: str
    accepted_summary: str = ""
    verification_summary: str = ""

    @field_validator(
        "current_turn_summary",
        "cumulative_task_summary",
        "current_verification_summary",
        "next_step",
    )
    @classmethod
    def validate_summary_descriptions(cls, value):
        return validate_description(value)

    @field_validator("corrected_or_invalidated", "remaining_work")
    @classmethod
    def validate_summary_lists(cls, value):
        cleaned = []
        for item in value:
            cleaned.append(validate_description(item))
        return cleaned

    @field_validator("accepted_summary")
    @classmethod
    def validate_accepted_summary(cls, value):
        value = value.strip()
        if len(value) > TASK_OUTCOME_ACCEPTED_MAX_CHARS:
            raise ValueError(
                "accepted_summary 超过配置上限 "
                f"{TASK_OUTCOME_ACCEPTED_MAX_CHARS}；请保留最终累计成果并重写"
            )
        return value

    @field_validator("verification_summary")
    @classmethod
    def validate_verification_summary(cls, value):
        value = value.strip()
        if len(value) > TASK_OUTCOME_VERIFICATION_MAX_CHARS:
            raise ValueError(
                "verification_summary 超过配置上限 "
                f"{TASK_OUTCOME_VERIFICATION_MAX_CHARS}；请保留验收条件、检查结果和证据定位并重写"
            )
        return value


class SupervisorDecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_task_list_need_change: bool = False
    new_task_list: list[str] = Field(default_factory=list)
    is_next_target: bool
    next_executor_target: str = ""
    next_execution_mode: Literal["execute", "synthesize"] = "execute"
    need_date_back: bool = False
    description: str
    is_finished: bool = False
    date_back_location: list[DateBack] = Field(default_factory=list)
    date_back_input: str = ""
    modify_content: str = ""
    verification_requirement: str = ""
    final_answer: str = ""
    task_summary: TaskSummaryDraft | None = None

    _validate_description = field_validator("description")(validate_description)

    @model_validator(mode="after")
    def require_task_summary_for_finished_decision(self):
        if self.is_finished and self.task_summary is None:
            raise ValueError("完整 decision 必须包含 task_summary")
        return self


class SupervisorHistorySummary(BaseModel):
    description: str

    _validate_description = field_validator("description")(validate_description)


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
        "user_query": query,
        "session_address": now_state.session_address,
        "chat_id": now_state.chat_id,
        "executor_tools": _tool_catalog(executor_tools or []),
        "conversation_context": now_state.conversation_context,
        "historical_tasks": [{"task_id": index, "target": target,
            "accepted": index in now_state.task_outcomes,
            "accepted_summary": now_state.task_outcomes[index].accepted_summary if index in now_state.task_outcomes else ""}
            for index, target in enumerate(now_state.task_list)],
        "skill_info": _skill_catalog(skill_lists or []),
    }

    prompt = _load_planning_skill()
    process(f"进入 supervisor planning task={now_state.now_task_id}", actor="supervisor")
    rounds = 0
    while rounds < now_state.max_supervision_times:
        process(f"supervisor planning task={now_state.now_task_id} 第{rounds + 1}轮", actor="supervisor")
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
        from State.session_checkpoint import boundary
        boundary("planning_tool_summary", "supervisor", tool_name=call.name, arguments=call.arguments,
                 response_message=response.get("message"), tool_result_seq=(
                     supervisor_state.last_tool_result_ref.tool_result_seq if call.name not in SUPERVISOR_ONLY_TOOLS and supervisor_state.last_tool_result_ref else None))
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
    from State.session_checkpoint import active_run, boundary
    run = active_run()
    if run and run.resume and run.phase == "decision_tool_summary":
        restored = await resume_supervisor_tool_summary(now_state, supervisor_state, supervisor_evaluation_state,
            executor_state, supervisor, supervisor_client, chat_function)
        if restored.is_finished:
            return restored
        boundary("decision", "supervisor", rounds=1, queries=[])
        return await making_next_plan_loop(now_state, supervisor, supervisor_client, chat_function,
            supervisor_tools, tool_registry, supervisor_state, supervisor_evaluation_state, executor_state)
    resume_decision = bool(run and run.resume and run.phase in {"decision", "decision_tool_summary"})
    if not (run and run.resume):
        _reset_supervisor_turn(now_state, supervisor_state, supervisor_evaluation_state)
    supervisor_state.reviewed_executor_seqs = [
        *executor_state.tool_event_seqs,
        *(
            [executor_state.executor_turn_seq]
            if executor_state.executor_turn_seq is not None
            else []
        ),
    ]
    if not resume_decision:
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
    memory_query_results, queried_memory_calls, rounds = await _restore_stage_queries(now_state, "evaluation")
    protocol_failures = 0

    process(f"进入 supervisor evaluation task={now_state.now_task_id} attempt={now_state.task_attempt}", actor="supervisor")
    while rounds < now_state.max_supervision_times:
        from State.session_checkpoint import boundary
        boundary("evaluation", "supervisor", rounds=rounds, queries=_query_descriptors(memory_query_results))
        process(f"supervisor evaluation task={now_state.now_task_id} attempt={now_state.task_attempt} 第{rounds + 1}轮", actor="supervisor")
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
            protocol_failures += 1
            _evaluation_memory(
                supervisor_evaluation_state,
                _multiple_tool_call_feedback(calls),
            )
            if protocol_failures >= 3:
                supervisor_evaluation_state.not_pass_reason = "evaluation tool-call protocol retries exhausted"
                break
            continue
        protocol_failures = 0
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

    process(f"进入 supervisor decision task={now_state.now_task_id} attempt={now_state.task_attempt}", actor="supervisor")
    # /continue retries this stage; a previous blocked checkpoint's reason must
    # not terminate a newly successful decision. Historical records stay intact.
    supervisor_state.exit_reason = ""
    memory_query_results, queried_memory_calls, rounds = await _restore_stage_queries(now_state, "decision")
    contract_failures = 0
    contract_limit = _positive_limit_from_env("CODING_AGENT_DECISION_CONTRACT_RETRIES", 3)
    protocol_failures = 0
    while rounds < now_state.max_supervision_times:
        from State.session_checkpoint import boundary
        boundary("decision", "supervisor", rounds=rounds, queries=_query_descriptors(memory_query_results))

        process(f"supervisor decision task={now_state.now_task_id} attempt={now_state.task_attempt} 第{rounds + 1}轮", actor="supervisor")
        messages = _build_decision_message(
            now_state,
            supervisor_state,
            supervisor_evaluation_state,
            executor_state,
            supervisor,
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
            supervisor_state.exit_reason = (
                response.get("message") or "decision model request failed"
            )
            break

        _record_supervisor_model_response(supervisor_state, "decision", response)
        calls = normalize_tool_calls(supervisor, response["tool"])
        if len(calls) > 1:
            protocol_failures += 1
            supervisor_state.memory_window.append(
                _multiple_tool_call_feedback(calls)
            )
            if protocol_failures >= 3:
                supervisor_state.exit_reason = "decision_protocol_exhausted"
                break
            continue
        protocol_failures = 0
        rounds += 1
        if not calls:
            decision_response = response
        else:
            call = calls[0]
            query_arguments = {
                key: value for key, value in call.arguments.items()
                if key not in {"session_address", "chat_id"}
            }
            if call.name == "read_task_summary":
                query_arguments.setdefault("include_other_tasks", False)
            query_fingerprint = _memory_query_fingerprint(
                call.name, query_arguments,
            )
            if call.name in SUPERVISOR_ONLY_TOOLS and query_fingerprint in queried_memory_calls:
                supervisor_state.memory_window.append(
                    f"同一历史查询已执行：{call.name}。请使用 memory_query_results 中的结果；"
                    "若仍无准确把握，缩小缺口后选择其他 task 或证据 locator，不重复同一查询。"
                )
                continue
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

            if call.name in SUPERVISOR_ONLY_TOOLS:
                queried_memory_calls.add(query_fingerprint)
                memory_query_results.append(
                    _memory_query_observation(call.name, call.arguments, tool_result)
                )
                # Query facts stay local to this decision, never in long-lived State or JSONL.
                continue

            messages.extend(
                build_tool_observation_messages(
                    supervisor, response.get("message"), call, tool_result
                )
            )
            boundary("decision_tool_summary", "supervisor", tool_name=call.name, arguments=call.arguments,
                     response_message=response.get("message"), tool_result_seq=(
                         supervisor_state.last_tool_result_ref.tool_result_seq if supervisor_state.last_tool_result_ref else None))

            summary, parsed_result = await _validated_tool_summary(
                now_state, chat_function, supervisor_client, supervisor, messages,
                SupervisorDecisionResult, supervisor_state, "decision_summary",
                evaluation_state=supervisor_evaluation_state,
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
            result = _complete_control_decision(result)
            _validate_decision_plan(now_state, result)
            _validate_task_summary_for_decision(
                result, supervisor_evaluation_state
            )
        except (ValueError, ValidationError) as exc:
            contract_failures += 1
            supervisor_state.validation_error = str(exc)
            feedback = (
                "decision JSON 校验失败，未应用任务推进："
                f"{bounded_text(exc, 1200)}。"
                "下一次返回完整 JSON 和 task_summary。is_finished 表示本次决策完成，"
                "不是任务通过；明确要求 Executor 修改时返回 is_finished=true、"
                "is_next_target=false。如确需历史证据，选择一个真实查询工具。"
            )
            supervisor_state.memory_window.append(feedback)
            _save_supervisor_turn(
                now_state,
                supervisor_state,
                "decision_validation_error",
                feedback,
            )
            if contract_failures >= contract_limit:
                supervisor_state.exit_reason = "decision_contract_exhausted"
                break
            continue

        _apply_decision(
            now_state, supervisor_state, supervisor_evaluation_state,
            executor_state, result
        )
        decision_seq = _save_supervisor_turn(
            now_state, supervisor_state, "decision", result.description
        )
        if result.is_finished:
            _persist_task_summary(
                now_state,
                supervisor_state,
                supervisor_evaluation_state,
                executor_state,
                result,
                decision_seq,
            )
            return result

    blocked = SupervisorDecisionResult(
        is_next_target=False,
        next_executor_target="Supervisor 决策未完成，需人工检查验收记录后继续。",
        description="Supervisor 决策达到上限。",
        is_finished=True,
        task_summary=TaskSummaryDraft(
            current_turn_summary="本轮未形成有效的 Supervisor 控制决策。",
            cumulative_task_summary=(
                now_state.task_cumulative_summaries.get(now_state.now_task_id, "")
                + "\n本轮决策未完成，没有新增可确认成果；此前仍有效成果予以保留。"
            ).strip(),
            current_verification_summary="Decision 重试达到上限，未产生新的通过结论。",
            remaining_work=["人工检查本轮验收记录并重新形成决策。"],
            next_step="保持当前 task，不推进 task_id。",
        ),
    )
    supervisor_state.exit_reason = supervisor_state.exit_reason or "decision_budget_exhausted"
    _apply_decision(
        now_state, supervisor_state, supervisor_evaluation_state,
        executor_state, blocked
    )
    decision_seq = _save_supervisor_turn(
        now_state, supervisor_state, "decision_blocked", blocked.description
    )
    _persist_task_summary(
        now_state,
        supervisor_state,
        supervisor_evaluation_state,
        executor_state,
        blocked,
        decision_seq,
    )
    return blocked


async def resume_supervisor_tool_summary(now_state, supervisor_state, evaluation_state, executor_state,
                                         provider, client, chat_function):
    """Rebuild a pending observation from its locator, never call the original tool."""
    from State.session_checkpoint import active_run
    from State.save_tool_result import read_tool_result_record
    from MCP_functions.tool_registry import ToolExecutionResult
    from AgentLoop.loop_utils import NormalizedToolCall
    run = active_run()
    payload = dict(run.payload)
    phase = run.phase
    tool_result = await _restore_query_result(now_state, payload)
    call = NormalizedToolCall("restored_supervisor_observation", payload["tool_name"], payload["arguments"], {
        "id": "restored_supervisor_observation", "type": "function", "function": {
            "name": payload["tool_name"], "arguments": json.dumps(payload["arguments"], ensure_ascii=False)}})
    model_type = PlanningResult if phase == "planning_tool_summary" else SupervisorDecisionResult
    messages = (build_model_messages(provider, _load_planning_skill(), {
        "user_query": now_state.user_query, "conversation_context": now_state.conversation_context})
        if model_type is PlanningResult else _build_decision_message(now_state, supervisor_state, evaluation_state, executor_state, provider))
    messages.extend(build_tool_observation_messages(provider, payload.get("response_message"), call, tool_result))
    run.resume = False
    _, result = await _validated_tool_summary(now_state, chat_function, client, provider, messages,
        model_type, supervisor_state, phase, evaluation_state=evaluation_state)
    if result is None:
        raise RuntimeError("pending Supervisor observation summary failed; original tool remains executed")
    if model_type is PlanningResult:
        _apply_plan(now_state, supervisor_state, result)
        _save_supervisor_turn(now_state, supervisor_state, "planning", result.description)
        await _prepare_initial_guidance(now_state, supervisor_state, chat_function, client, provider)
    else:
        _apply_decision(now_state, supervisor_state, evaluation_state, executor_state, result)
        seq = _save_supervisor_turn(now_state, supervisor_state, "decision", result.description)
        if result.is_finished:
            _persist_task_summary(now_state, supervisor_state, evaluation_state, executor_state, result, seq)
    return result


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
    不另造结论。description 必须少于 3000 个字符，不复制完整历史或工具输出。仅返回 JSON：
{"description": "面向后续上下文恢复的精炼总结"}
"""
    messages = build_model_messages(
        supervisor,
        prompt,
        {
            "user_query": now_state.user_query,
            "status": status,
            "answer": answer,
            "block_reason": block_reason,
            "supervisor_descriptions": (
                {"entries": now_state.supervisor_descriptions.to_dicts(), "omitted_earlier_entries": 0}
            ),
            "task_outcomes": [
                outcome.to_dict()
                for _, outcome in sorted(now_state.task_outcomes.items())
            ],
        },
    )
    summary = None
    retry_messages = list(messages)
    for _ in range(now_state.max_supervision_times):
        response = await _call_supervisor_model(
            now_state,
            chat_function,
            supervisor_client,
            supervisor,
            retry_messages,
            [],
        )
        if response["status"] != "success" or response["tool"]:
            feedback = "final summary 必须返回无工具调用的完整 JSON。"
        else:
            try:
                summary = SupervisorHistorySummary.model_validate(
                    parse_json_object(response.get("message"))
                )
                break
            except (ValueError, ValidationError) as exc:
                feedback = _validation_feedback("final summary", exc)
        retry_messages = [
            *retry_messages,
            {"role": "user", "content": feedback},
        ]
    if summary is None:
        raise RuntimeError("Supervisor final summary validation exhausted")
    description = summary.description
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
        "evidence_usage": _evidence_usage_context(),
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
        "supervisor_history": list(supervisor_evaluation_state.memory_window),
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
    memory_query_results=None,
):
    current_outcome = now_state.task_outcomes.get(now_state.now_task_id)
    information = {
        "session_address": now_state.session_address,
        "chat_id": now_state.chat_id,
        "task_list": list(now_state.task_list),
        "now_task_id": now_state.now_task_id,
        "now_target": now_state.now_target,
        "current_turn_latest_result": {
            "executor_target": executor_state.supervisor_guidance,
            "executor_summary": executor_state.output_content,
            "executor_is_finished": executor_state.is_finished,
            "executor_tool_result_locators": list(executor_state.tool_result_refs),
            "tool_summaries": list(executor_state.tool_summaries),
        },
        "decision_control_contract": {
            "is_finished": "本次决策完成；否决并委派修改时也为 true，与任务是否完成无关",
            "is_next_target": "整个 task 验收通过才为 true，必须尊重本轮 evaluation 否决",
            "no_tool_reply": "完整 task_summary 加明确控制决定即可交回 Host，不重复同一决定",
        },
        "evidence_usage": _evidence_usage_context(),
        "current_turn_supervisor_evaluation": {
            "description": supervisor_evaluation_state.output_content,
            "is_passed": supervisor_evaluation_state.is_executor_passed,
            "reason": supervisor_evaluation_state.not_pass_reason,
            "is_error": supervisor_evaluation_state.is_executor_error,
            "error_message": supervisor_evaluation_state.executor_error_message,
        },
        "historical_auxiliary_summary": {
            "status": (
                "queried" if any(
                    item.get("tool_name") == "read_task_summary"
                    for item in memory_query_results or []
                ) else "not_loaded"
            ),
            "available": task_summary_path(
                now_state.session_address, now_state.chat_id
            ).is_file(),
            "tool_name": "read_task_summary",
            "task_id": now_state.now_task_id,
        },
        "memory_query_results": list(memory_query_results or []),
        "current_task_outcome": (
            current_outcome.to_dict() if current_outcome is not None else None
        ),
        "task_outcome_length_limits": {
            "accepted_summary": TASK_OUTCOME_ACCEPTED_MAX_CHARS,
            "verification_summary": TASK_OUTCOME_VERIFICATION_MAX_CHARS,
        },
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
        "memory_window": list(supervisor_state.memory_window),
    }
    return build_model_messages(supervisor, _load_decision_prompt(), information)


async def _call_supervisor_model(
    now_state, chat_function, client, provider, messages, tools
):
    from AgentLoop.context_compression import prepare_request, ContextBudgetError
    async def compression_call(compression_messages, compression_tools):
        return await _counted_supervisor_call(now_state, chat_function, client, provider,
                                              compression_messages, compression_tools)
    try:
        messages = await prepare_request(now_state, messages, tools, compression_call)
    except ContextBudgetError as exc:
        return {"status": "error", "message": str(exc), "tool": []}
    return await _counted_supervisor_call(now_state, chat_function, client, provider, messages, tools)


async def _counted_supervisor_call(now_state, chat_function, client, provider, messages, tools):
    if len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)) > now_state.context_max_chars:
        return {"status": "error", "message": "context_budget_blocked: model/compression request exceeds hard character budget", "tool": []}
    if now_state.model_request_count >= now_state.max_model_requests:
        return {"status": "error", "message": "model request budget exhausted", "tool": []}
    now_state.model_request_count += 1
    from State.session_checkpoint import active_run
    run = active_run()
    if run:
        run.emit("model_request", "supervisor", model_request_count=now_state.model_request_count)
        run.checkpoint()
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
    if run:
        run.checkpoint()
    return response


async def _prepare_initial_guidance(
    now_state,
    supervisor_state,
    chat_function,
    client,
    supervisor,
):
    from State.session_checkpoint import boundary
    boundary("initial_guidance", "supervisor")
    prompt = """
规划已经完成，当前没有 Executor 输出。这里只为 task_list[0] 建立第一次 handoff，不进行验收。
executor_guidance 应说明当前 task 的最小执行范围、优先工具/路径、停止条件和必要异常处理；
不要要求一次穷举整个项目，也不要提前执行后续 task。completion_criteria 必须是稳定、最小充分、
    可由真实结果核验的证据标准，后续不得因为“更放心”随意扩大。description 必须少于 3000 个字符，
    不复制历史或工具原文。仅返回 JSON：
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
            "user_query": now_state.user_query,
            "task_list": now_state.task_list,
            "now_task_id": now_state.now_task_id,
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
    now_state.task_completion_criteria[now_state.now_task_id] = guidance.completion_criteria
    supervisor_state.original_completion_criteria = guidance.completion_criteria
    from AgentLoop.agent import SupervisorHandoff
    handoff = SupervisorHandoff(
        supervisor_seq=now_state.supervisor_seq,
        task_id=now_state.now_task_id,
        target=now_state.now_target,
        instruction=guidance.executor_guidance,
        completion_criteria=guidance.completion_criteria,
        dependency_context=[value for key, value in sorted(now_state.task_outcomes.items()) if key < now_state.now_task_id],
    )
    now_state.task_handoffs[now_state.now_task_id] = handoff
    supervisor_state.handoff = handoff
    supervisor_state.description = guidance.description
    supervisor_state.output_content = guidance.description
    _save_supervisor_turn(
        now_state, supervisor_state, "initial_guidance", guidance.description
    )


def _apply_plan(now_state, supervisor_state, result):
    start = getattr(now_state, "request_start_task_id", 0)
    now_state.task_list = list(now_state.task_list[:start]) + list(result.task_list)
    now_state.now_task_id = start
    now_state.now_target = now_state.task_list[start]
    supervisor_state.task_id = start
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
    if result.task_summary is not None:
        now_state.task_cumulative_summaries[task_id] = (
            result.task_summary.cumulative_task_summary
        )
        supervisor_state.task_summary = result.task_summary.model_dump()
    else:
        supervisor_state.task_summary = None
    supervisor_state.task_outcome = None

    current_handoff = now_state.task_handoffs.get(task_id)
    if result.is_next_target and evaluation_state.is_executor_passed:
        from AgentLoop.agent import TaskOutcome

        now_state.task_outcomes[task_id] = TaskOutcome(
            task_id=task_id,
            target=now_state.now_target,
            accepted_summary=result.task_summary.accepted_summary,
            verification_summary=result.task_summary.verification_summary,
            evidence_references=list(
                now_state.task_evidence_references.get(task_id, [])
            ),
            reusable_read_calls=list(
                now_state.task_reusable_read_calls.get(task_id, [])
            ),
        )
        supervisor_state.task_outcome = now_state.task_outcomes[task_id].to_dict()

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


def _complete_control_decision(result):
    """A complete no-tool control reply ends decision, not necessarily the task.

    Only repair the phase-completion flag. All task advancement, veto, summary
    and outcome checks still run afterwards. Never infer acceptance from prose.
    """
    if not result.is_finished:
        if result.task_summary is None or (
            not result.is_next_target and not result.next_executor_target.strip()
        ):
            raise ValueError(
                "无工具回复未形成完整决定：必须包含 task_summary 与具体下一步；"
                "需要历史时调用查询工具，而不是重复 is_finished=false"
            )
        result = result.model_copy(update={"is_finished": True})
    return result


def _evidence_usage_context():
    from State.project_workspace import WORKSPACE
    workspace = WORKSPACE.get()
    return {
        "project_execution_scope": {
            "project_root": workspace.project_root,
            "staging_root": workspace.staging_root,
            "real_project_applied": False,
            "rule": "副本成果与真实项目回写分开；模型完成/验收不代表用户批准回写，应用仅由终端 Host 决定。",
        } if workspace else None,
        "summary_is_exhaustive": False,
        "source_priority": ["current_valid_evidence", "accepted_dependency_evidence", "summaries"],
        "path_metadata_scope": "成功目录工具的 evidence_references.paths 证明存在性，不证明职责或运行效果。",
        "verification_scope": "构建依赖/配置声明、源码行为、实际运行验证必须分开；不得把推断写成实测。",
        "rejected_previous_work": "previous_work 中未验收内容仅供修改，不是成果或证据。",
        "missing_summary_item": "摘要省略不等于证据失效；有具体疑问按 tool_result_seq 查询已有结果，不重读项目。",
        "acceptance": "只按锁定 completion_criteria 验收，不新增摘要白名单或穷举要求。",
    }


def _validate_task_summary_for_decision(result, evaluation_state):
    if result.is_next_target and not evaluation_state.is_executor_passed:
        raise ValueError("Supervisor evaluation 未通过，不允许推进 task")
    if result.is_next_target and not result.is_finished:
        raise ValueError("is_next_target=true 时 decision 必须同时 is_finished=true")
    if not result.is_finished:
        return
    draft = result.task_summary
    if draft is None:
        raise ValueError("完整 decision 必须包含 task_summary")
    task_is_accepted = result.is_next_target and evaluation_state.is_executor_passed
    if task_is_accepted:
        if not draft.accepted_summary:
            raise ValueError("最终通过的 task 必须生成 accepted_summary")
        if not draft.verification_summary:
            raise ValueError("最终通过的 task 必须生成 verification_summary")
    elif draft.accepted_summary or draft.verification_summary:
        raise ValueError(
            "当前 task 未最终通过，accepted_summary 和 verification_summary 必须为空"
        )


def _persist_task_summary(
    now_state,
    supervisor_state,
    evaluation_state,
    executor_state,
    result,
    decision_seq,
):
    task_id = now_state.now_task_id
    if decision_seq in now_state.task_summary_saved_seqs:
        return
    if supervisor_state.task_summary is None:
        raise ValueError("cannot persist task summary before state has been updated")
    if supervisor_state.task_id != task_id or supervisor_state.supervisor_seq != decision_seq:
        raise ValueError("task summary state does not match the current task and seq")
    draft = TaskSummaryDraft.model_validate(supervisor_state.task_summary)
    evidence = [
        reference.to_dict()
        for reference in now_state.task_evidence_references.get(task_id, [])
    ]
    known_result_seqs = {
        item.get("tool_result_seq") for item in evidence if isinstance(item, dict)
    }
    for locator in now_state.task_evidence_locators.get(task_id, []):
        if locator.get("tool_result_seq") in known_result_seqs:
            continue
        evidence.append(dict(locator))
    record = render_task_summary_record(
        task_id=task_id,
        supervisor_seq=decision_seq,
        target=supervisor_state.target,
        current_turn_summary=draft.current_turn_summary,
        cumulative_task_summary=draft.cumulative_task_summary,
        current_verification_summary=draft.current_verification_summary,
        corrected_or_invalidated=list(draft.corrected_or_invalidated),
        remaining_work=list(draft.remaining_work),
        decision_summary=supervisor_state.description,
        next_step=draft.next_step,
        evidence_references=evidence,
        task_outcome=supervisor_state.task_outcome,
    )
    append_task_summary_record(
        session_address=now_state.session_address,
        chat_id=now_state.chat_id,
        task_id=task_id,
        supervisor_seq=decision_seq,
        record=record,
    )
    now_state.task_summary_saved_seqs.add(decision_seq)


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
    supervisor_state.task_summary = None
    supervisor_state.task_outcome = None
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
    from State.session_checkpoint import active_run
    run = active_run()
    if run and run.current_operation:
        run.emit("tool_completed", "supervisor", operation_id=run.current_operation,
                 tool_name=tool_name, tool_result_seq=(supervisor_state.last_tool_result_ref.tool_result_seq
                    if supervisor_state.last_tool_result_ref else None), ok=tool_result.ok,
                 status="success" if tool_result.ok else tool_result.error_type)
        run.current_operation = None
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
    show_description("supervisor", supervisor_state.description,
                     task_id=supervisor_state.task_id, phase=phase,
                     seq=supervisor_state.supervisor_seq)
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
    evaluation_state=None,
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
                    result = _complete_control_decision(result)
                    _validate_decision_plan(now_state, result)
                    if evaluation_state is not None:
                        _validate_task_summary_for_decision(result, evaluation_state)
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
        retry_messages = [
            *retry_messages, {"role": "user", "content": feedback}
        ]
        _save_supervisor_turn(
            now_state, supervisor_state, f"{stage}_validation_error", feedback
        )
    return None, None


def _evaluation_memory(state, description):
    if description:
        state.memory_window.append(description)


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


def _query_descriptors(observations):
    """Checkpoint locators/query actions, never the already-saved return bodies."""
    result = []
    for item in observations:
        ref = item.get("tool_result_ref")
        result.append({"tool_name": item["tool_name"], "arguments": dict(item["arguments"]),
                       "tool_result_seq": ref.tool_result_seq if ref else None})
    return result


async def _restore_query_result(now_state, descriptor):
    from State.save_tool_result import read_tool_result_record
    from MCP_functions.tool_registry import ToolExecutionResult
    if descriptor.get("tool_result_seq") is not None:
        record = read_tool_result_record(now_state.session_address, now_state.chat_id, descriptor["tool_result_seq"])
        if record["status"] != "success":
            raise RuntimeError("pending Supervisor result unavailable; original tool will not be replayed")
        return ToolExecutionResult(**record["record"]["content"])
    name = descriptor["tool_name"]
    if name not in SUPERVISOR_ONLY_TOOLS:
        raise RuntimeError("ordinary tool lacks persisted locator; automatic replay refused")
    from Context import mcp_resources, mcp_supervisor_tools
    arguments = {key: value for key, value in descriptor["arguments"].items()
                 if key not in {"session_address", "chat_id", "branch_id"}}
    arguments.update(session_address=now_state.session_address, chat_id=now_state.chat_id)
    function = getattr(mcp_resources, name, None) or getattr(mcp_supervisor_tools, name)
    # Historical reads are safe views, not new side effects or new raw facts.
    return ToolExecutionResult(name, "host_history", True, None, "", await function(**arguments))


async def _restore_stage_queries(now_state, phase):
    from State.session_checkpoint import active_run
    from State.save_tool_result import ToolResultLocator
    run = active_run()
    descriptors = run.payload.get("queries", []) if run and run.phase == phase else []
    observations, fingerprints = [], set()
    for descriptor in descriptors:
        tool_result = await _restore_query_result(now_state, descriptor)
        seq = descriptor.get("tool_result_seq")
        locator = ToolResultLocator(now_state.session_address, now_state.chat_id, seq) if seq is not None else None
        observations.append(_memory_query_observation(descriptor["tool_name"], descriptor["arguments"], tool_result, locator))
        arguments = {key: value for key, value in descriptor["arguments"].items()
                     if phase == "evaluation" or key not in {"session_address", "chat_id"}}
        fingerprints.add(_memory_query_fingerprint(descriptor["tool_name"], arguments))
    return observations, fingerprints, (run.payload.get("rounds", 0) if run and run.phase == phase else 0)


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
        "result": tool_result.to_model_content(),
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
        return Path(skill.address).read_text(encoding="utf-8")
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
