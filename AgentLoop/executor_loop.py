from __future__ import annotations
from terminal_render import process, show_description

import json
from pathlib import Path
from typing import TYPE_CHECKING

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
    USER_QUERY_MAX_CHARS,
    bounded_text,
    build_model_messages,
    build_tool_observation_messages,
    call_chat_function,
    normalize_tool_calls,
    parse_json_object,
    validate_description,
)
from MCP_functions.tool_registry import AgentRole, ToolExecutionResult, ToolRegistry
from State.save_executor_state_history import save_state_history
from State.save_tool_result import ToolSummary, save_tool_result

if TYPE_CHECKING:
    from AgentLoop.agent import AgentState, ExecutorState, Skill, SupervisorState


EXECUTOR_PROMPT = """
你是 coding agent 的 Executor，只完成当前 target 和 Supervisor handoff，不修改任务计划或 task_id。

上下文优先级：当前用户要求与 target > handoff 的 instruction/completion_criteria > dependency_context 中已验收事实 > recent_descriptions > latest_tool_context 元数据。冲突时服从优先级更高者，并说明冲突。

执行模式：
1. execution_mode=execute：可以调用本轮真实提供的工具。每一步最多一个结构化工具调用，只传 schema 要求的参数；不要输出 XML 工具标签，不要把旧 tool_result 拼进参数。
2. execution_mode=synthesize：不得调用工具，只能基于 dependency_context、当前 handoff 和已给摘要形成综合结果；不得因 task_id 变化重新读取已验收证据。
3. dependency_context 是前序 task 已通过 Supervisor 验收的事实与 locator；除非 handoff 明确要求因状态变化重新验证，否则直接复用。
4. latest_tool_context 只有最近一次工具的路径、状态、分页和紧凑元数据，不含正文；正文事实只能来自当前真实 observation、recent_descriptions 或 dependency_context。

输出与证据：
5. 选择工具时返回一个原生结构化工具调用即可；没有工具调用时只返回下方 JSON，不添加 Markdown、解释前缀或额外字段。
6. description 必须少于 3000 个字符，只记录本轮实际执行、关键结果、完成与验证情况及仍缺少什么。不要重复粘贴 tool_result、此前完整目录树、完整依赖列表或整个历史过程。
7. 工具总结应以本次 observation 的增量为主；若任务未完成，只指出最必要的下一步。综合模式下才输出完整的最终说明。
8. is_finished 判断整个当前 target 是否已满足 completion_criteria，不是判断单次工具是否成功。证据不足时必须为 false；工具无报错不能单独证明完成。
9. selected_skill 可为 null；若选择，必须对应 skills 中真实 id/name，且 skill 不授予额外工具权限。
10. description 必须为非空字符串，is_finished 必须是 JSON 布尔值。
11. dependency_context 中 accepted_summary 是有损概括，不是事实白名单；成功目录证据的 paths 可证明文件或包存在，但不能仅凭文件名认定职责、仅凭 Dockerfile 宣称部署可用、仅凭配置拼出已验证的完整调用链。
12. previous_work 可能是被否决的旧回答，只用于定位修正；不能照抄其错误结论。综合任务按锁定完成条件交付：直接事实、明确标注的合理推断、未确认边界。不要把合理推断在最终结论中重新写成确定事实。
13. synthesize 模式没有工具，不要无动作等待或重复生成相同说明。证据不足时用 JSON 明确缺口并 is_finished=false，交给 Supervisor 决定如何补充，而不是自行重读项目。

无工具调用时的唯一输出契约：
{
  "tool_name": null,
  "selected_skill": {"skill_name": null, "skill_id": null},
  "description": "本步新增事实、证据、完成判断和必要的未完成项",
  "is_finished": false
}
"""


REUSABLE_READ_TOOLS = frozenset({
    "read_all_files_tool",
    "read_files_content_tool",
    "judge_spring_project_tool",
    "sort_files_by_suffix_tool",
})


class SelectedSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str | None = None
    skill_id: int | None = None

    @model_validator(mode="after")
    def validate_complete_selection(self):
        if self.skill_name is not None:
            self.skill_name = self.skill_name.strip()
        has_name = bool(self.skill_name)
        has_id = self.skill_id is not None
        if has_name != has_id:
            raise ValueError(
                "skill_name 和 skill_id 必须同时提供或同时为 null"
            )
        if self.skill_id is not None and self.skill_id < 0:
            raise ValueError("skill_id 不能为负数")
        return self


class ExecutorOutPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str | None = None
    selected_skill: SelectedSkill | None = None
    description: str = Field(min_length=1)
    is_finished: bool = Field(strict=True)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value):
        return validate_description(value)

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("tool_name 必须是非空字符串或 null")
        return value

async def run_executor_loop(
    now_state: AgentState,
    executor: str,
    executor_client,
    chat_function,
    executor_tools: list[dict],
    tool_registry: ToolRegistry,
    skill_lists: list[Skill],
    supervisor_state: SupervisorState,
    executor_state: ExecutorState,
):
    from State.session_checkpoint import active_run, boundary
    run = active_run()
    resuming = bool(run and run.resume and run.phase in {"executor_action", "executor_tool_summary"})
    resume_payload = dict(run.payload) if resuming else {}
    if not resuming:
        _reset_executor_turn(now_state, supervisor_state, executor_state)
    handoff = executor_state.input_content["supervisor_handoff"]
    execution_mode = handoff.get("execution_mode", "execute")
    active_executor_tools = [] if execution_mode == "synthesize" else executor_tools
    prior_reusable_read_calls = {
        signature
        for outcome in handoff.get("dependency_context", [])
        if isinstance(outcome, dict)
        for signature in outcome.get("reusable_read_calls", [])
        if isinstance(signature, str)
    }
    selected_skill = executor_state.selected_skill if resuming else None
    descriptions: list[str] = list(resume_payload.get("descriptions", []))
    steps = resume_payload.get("step", 0)
    pending_summary = resume_payload if resuming and run.phase == "executor_tool_summary" else None
    if run:
        run.resume = False
    repeated_completed_calls: dict[str, int] = {}
    synthesis_tool_attempts = 0
    exit_reason = "executor_step_limit"

    process(f"进入 executor task={now_state.now_task_id} attempt={now_state.task_attempt}", actor="executor")
    if skill_lists and not resuming:
        selected_skill, selection_description = await _select_skill_before_action(
            now_state,
            executor,
            executor_client,
            chat_function,
            skill_lists,
            executor_state,
        )
        executor_state.selected_skill = selected_skill
        if selection_description:
            descriptions.append(selection_description)
            _remember(executor_state, selection_description)

    if resuming and not pending_summary and executor_state.is_finished:
        exit_reason = "executor_reported_finished"
    while steps < now_state.max_executor_steps and not (resuming and not pending_summary and executor_state.is_finished):
        steps += 1
        process(f"executor task={now_state.now_task_id} attempt={now_state.task_attempt} 第{steps}轮", actor="executor")
        now_state.executor_executing_times += 1
        try:
            if pending_summary:
                from AgentLoop.loop_utils import NormalizedToolCall
                args = pending_summary["arguments"]
                name = pending_summary["tool_name"]
                raw = {"id": "restored_observation", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                restored_call = NormalizedToolCall("restored_observation", name, args, raw)
                response = {"status": "success", "message": pending_summary.get("response_message"), "tool": []}
            else:
                boundary("executor_action", "executor", step=steps - 1, descriptions=descriptions)
                response = await _call_model(
                now_state,
                chat_function,
                executor_client,
                executor,
                _build_executor_messages(
                    steps - 1,
                    supervisor_state,
                    now_state,
                    executor_state,
                    executor,
                    skill_lists,
                    selected_skill,
                ),
                active_executor_tools,
            )
            if response["status"] != "success":
                raise RuntimeError(response.get("message") or "executor model request failed")
            executor_state.model_stage = "action_selection"
            executor_state.raw_model_response_excerpt = bounded_text(
                response.get("message"), 4000, keep_tail=True
            )
            executor_state.validation_error = ""

            tool_calls = [restored_call] if pending_summary else normalize_tool_calls(executor, response["tool"])
            if execution_mode == "synthesize" and tool_calls:
                synthesis_tool_attempts += 1
                description = (
                    "当前 handoff 为 synthesize 模式，前序已验收证据已在 "
                    "dependency_context 中提供，本轮禁止调用工具；请直接输出综合 JSON。"
                )
                descriptions.append(description)
                _remember(executor_state, description)
                executor_state.validation_error = description
                if synthesis_tool_attempts >= 2:
                    exit_reason = "synthesis_tool_call_rejected"
                    break
                continue
            if len(tool_calls) > 1:
                description = _multiple_tool_call_feedback(tool_calls)
                descriptions.append(description)
                _remember(executor_state, description)
                executor_state.validation_error = description
                # A protocol-format retry is not an executed Agent step. The
                # global model-request budget still prevents an infinite loop.
                steps -= 1
                continue

            if not tool_calls:
                try:
                    if (
                        execution_mode == "synthesize"
                        and _looks_like_tool_call_markup(response.get("message"))
                    ):
                        raise ValueError(
                            "synthesize 模式禁止返回工具调用标记；请直接综合已有证据"
                        )
                    output = ExecutorOutPut.model_validate(
                        parse_json_object(response.get("message"))
                    )
                except (ValueError, ValidationError) as exc:
                    executor_state.validation_error = str(exc)
                    feedback = _build_validation_feedback(
                        exc, response.get("message")
                    )
                    descriptions.append(feedback)
                    _remember(executor_state, feedback)
                    continue
                loaded_skill = _load_selected_skill(skill_lists, output.selected_skill)
                if selected_skill is None and loaded_skill is not None and not output.is_finished:
                    selected_skill = loaded_skill
                    executor_state.selected_skill = loaded_skill
                    description = output.description or f"已选择 skill：{loaded_skill.name}"
                    descriptions.append(description)
                    _remember(executor_state, description)
                    continue

                description = output.description
                if description:
                    descriptions.append(description)
                    _remember(executor_state, description)
                executor_state.description = description
                executor_state.is_finished = output.is_finished
                if output.is_finished:
                    exit_reason = "executor_reported_finished"
                    break
                if execution_mode == "synthesize":
                    exit_reason = "executor_synthesis_needs_review"
                    break
                continue

            tool_call = tool_calls[0]
            prepared = None
            operation = None
            prepare = getattr(tool_registry, "prepare_call", None)
            if not pending_summary and callable(prepare):
                if run:
                    import uuid
                    operation = uuid.uuid4().hex
                    run.current_operation = operation
                    run.emit("tool_intent", "executor", operation_id=operation,
                             tool_name=tool_call.name, arguments=tool_call.arguments, status="started")
                prepared = await prepare(AgentRole.EXECUTOR, tool_call.name, tool_call.arguments)
            preparation_failed = isinstance(prepared, ToolExecutionResult)
            fingerprint = _tool_call_fingerprint(
                now_state.now_task_id, tool_call.name, tool_call.arguments
            )
            reusable_signature = _tool_call_signature(
                tool_call.name, tool_call.arguments
            )
            duplicate_key = (
                reusable_signature
                if reusable_signature in prior_reusable_read_calls
                else fingerprint
            )
            if not pending_summary and not preparation_failed and (
                fingerprint in handoff.get("completed_tool_calls", [])
                or fingerprint in executor_state.completed_tool_calls
                or reusable_signature in prior_reusable_read_calls
            ) and not _repeat_is_explicitly_requested(
                handoff.get("instruction", "")
            ):
                if prepared is not None:
                    tool_registry.execution_gateway.consume_permission(
                        prepared, AgentRole.EXECUTOR.value, tool_call.name, tool_call.arguments)
                if run and operation:
                    run.emit("tool_completed", "executor", operation_id=operation,
                             tool_name=tool_call.name, status="reused", ok=True, executed=False)
                    run.current_operation = None
                repeated_completed_calls[duplicate_key] = (
                    repeated_completed_calls.get(duplicate_key, 0) + 1
                )
                description = (
                    "已存在同一工具与参数的成功证据，复用 handoff 或 "
                    "dependency_context 中的证据；请根据已有摘要和 remaining_work "
                    "选择尚未执行的下一步；"
                    "如确需重跑，Supervisor 必须明确说明重跑原因。"
                )
                descriptions.append(description)
                _remember(executor_state, description)
                if repeated_completed_calls[duplicate_key] >= 2:
                    executor_state.validation_error = (
                        "模型连续选择同一个已完成工具调用，已停止本轮以避免空转"
                    )
                    exit_reason = "executor_repeated_completed_call"
                    break
                continue
            if pending_summary:
                from State.save_tool_result import read_tool_result_record
                raw_record = read_tool_result_record(now_state.session_address, now_state.chat_id,
                                                     pending_summary["tool_result_seq"])
                if raw_record["status"] != "success":
                    raise RuntimeError("pending observation is unavailable; tool will not be replayed")
                tool_result = ToolExecutionResult(**raw_record["record"]["content"])
                locator = executor_state.last_tool_result_ref
                pending_summary = None
            else:
                if run and not operation:
                    import uuid
                    operation = uuid.uuid4().hex
                    run.current_operation = operation
                    run.emit("tool_intent", "executor", operation_id=operation,
                             tool_name=tool_call.name, arguments=tool_call.arguments, status="started")
                if preparation_failed:
                    tool_result = prepared
                else:
                    call_options = {"tool_permission": prepared} if prepared is not None else {}
                    tool_result = await tool_registry.call(
                        role=AgentRole.EXECUTOR,
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        **call_options,
                    )
                now_state.executor_seq += 1
                executor_state.executor_seq = now_state.executor_seq
                tool_event_seq = executor_state.executor_seq
                executor_state.tool_event_seqs.append(tool_event_seq)
                now_state.tool_result_seq += 1
                locator = save_tool_result(
                    session_address=now_state.session_address,
                    chat_id=now_state.chat_id,
                    task_id=now_state.now_task_id,
                    actor="executor",
                    actor_turn_seq=tool_event_seq,
                    phase="execution",
                    tool_name=tool_call.name,
                    arguments=tool_call.arguments,
                    tool_result=tool_result,
                    tool_result_seq=now_state.tool_result_seq,
                )
                executor_state.last_tool_result_ref = locator
                executor_state.tool_result_refs.append(locator)
                executor_state.latest_tool_context = _build_latest_tool_context(
                    tool_call.name,
                    tool_result,
                    locator.tool_result_seq,
                )
                executor_state.tool_name = tool_call.name
                executor_state.tool_arguments = tool_call.arguments
                executor_state.tool_ok = tool_result.ok
                executor_state.description = "工具已执行，等待观察总结。"
                executor_state.is_error = not tool_result.ok
                executor_state.error_message = (
                    "" if tool_result.ok else (tool_result.message or tool_result.error_type or "")
                )
                save_state_history(executor_state, record_type="tool_event")

            if tool_result.ok:
                fingerprint = _tool_call_fingerprint(now_state.now_task_id, tool_call.name, tool_call.arguments)
                if fingerprint not in executor_state.completed_tool_calls:
                    executor_state.completed_tool_calls.append(fingerprint)
                _remember_task_evidence(
                    now_state,
                    executor_state.latest_tool_context,
                    tool_call.name,
                    tool_call.arguments,
                )

            if run and operation:
                run.emit("tool_completed", "executor", operation_id=operation,
                         tool_name=tool_call.name, tool_result_seq=locator.tool_result_seq,
                         ok=tool_result.ok, status="success" if tool_result.ok else tool_result.error_type)
                run.current_operation = None
            boundary("executor_tool_summary", "executor", step=steps - 1,
                     descriptions=descriptions, tool_result_seq=locator.tool_result_seq,
                     tool_name=tool_call.name, arguments=tool_call.arguments,
                     response_message=response.get("message"))

            messages = _build_executor_messages(
                steps - 1,
                supervisor_state,
                now_state,
                executor_state,
                executor,
                skill_lists,
                selected_skill,
                include_latest_tool_context=False,
            )
            messages.extend(
                build_tool_observation_messages(
                    executor, response.get("message"), tool_call, tool_result
                )
            )
            summary = await _summarize_tool_once(
                now_state,
                chat_function,
                executor_client,
                executor,
                messages,
                executor_state,
            )

            description = summary.description
            descriptions.append(description)
            _remember(executor_state, description)
            executor_state.tool_summaries.append(
                ToolSummary(
                    tool_result_seq=locator.tool_result_seq,
                    tool_name=tool_call.name,
                    tool_ok=tool_result.ok,
                    description=description,
                )
            )
            executor_state.description = description
            executor_state.is_finished = summary.is_finished
            executor_state.is_error = not tool_result.ok
            boundary("executor_action", "executor", step=steps, descriptions=descriptions)
            if tool_result.error_type in {"unknown", "interrupted"}:
                exit_reason = "executor_operation_unknown"
                break
            if summary.is_finished:
                exit_reason = "executor_reported_finished"
                break

        except Exception as exc:
            executor_state.is_error = True
            executor_state.error_message = str(exc)
            if not executor_state.validation_error:
                executor_state.validation_error = str(exc)
            description = f"Executor 本轮异常：{exc}"
            locator = executor_state.last_tool_result_ref
            if locator is not None and not any(
                summary.tool_result_seq == locator.tool_result_seq
                for summary in executor_state.tool_summaries
            ):
                executor_state.tool_summaries.append(
                    ToolSummary(
                        tool_result_seq=locator.tool_result_seq,
                        tool_name=executor_state.tool_name,
                        tool_ok=bool(executor_state.tool_ok),
                        description=description,
                    )
                )
            executor_state.description = description
            descriptions.append(description)
            _remember(executor_state, description)
            exit_reason = "executor_exception"
            break

    executor_state.output_content = "\n".join(item for item in descriptions if item)
    executor_state.exit_reason = exit_reason
    task_id = now_state.now_task_id
    now_state.task_previous_work.setdefault(task_id, []).append(
        executor_state.output_content
    )
    locators = now_state.task_evidence_locators.setdefault(task_id, [])
    successful_result_seqs = {
        summary.tool_result_seq
        for summary in executor_state.tool_summaries
        if summary.tool_ok
    }
    for reference in executor_state.tool_result_refs:
        if reference.tool_result_seq not in successful_result_seqs:
            continue
        locator_data = {
            "task_id": task_id,
            "tool_result_seq": reference.tool_result_seq,
        }
        if locator_data not in locators:
            locators.append(locator_data)
    completed = now_state.task_completed_tool_calls.setdefault(task_id, [])
    for fingerprint in executor_state.completed_tool_calls:
        if fingerprint not in completed:
            completed.append(fingerprint)
    now_state.executor_seq += 1
    executor_state.executor_seq = now_state.executor_seq
    executor_state.executor_turn_seq = executor_state.executor_seq
    save_state_history(executor_state, record_type="executor_turn")
    _update_now_state(now_state, executor_state)
    return executor_state


async def _summarize_tool_once(
    now_state,
    chat_function,
    executor_client,
    executor,
    messages,
    executor_state,
):
    retry_messages = [
        *messages,
        {
            "role": "user",
            "content": (
                "这是 tool_summary 阶段：只总结刚才这一份真实 observation，禁止调用工具。"
                "description 只写本次新增事实、路径/状态/分页、它对 completion_criteria 的贡献，"
                "以及最多一个必要的下一步；不要重述累计目录树、全部历史或完整 handoff。"
                "is_finished 判断整个当前 target，而不是本次工具是否成功。若还需工具则设为 false，"
                "实际调用留到下一轮 action_selection。只返回 ExecutorOutPut JSON，"
                "不要返回 XML、工具标签、Markdown 或额外字段。"
            ),
        },
    ]
    last_error = None
    for _ in range(2):
        response = None
        try:
            response = await _call_model(
                now_state,
                chat_function,
                executor_client,
                executor,
                retry_messages,
                [],
            )

            if response["status"] != "success":
                raise RuntimeError(response.get("message") or "tool summary request failed")
            executor_state.model_stage = "tool_summary"
            executor_state.raw_model_response_excerpt = bounded_text(
                response.get("message"), 4000, keep_tail=True
            )
            if response.get("tool"):
                raise ValueError(
                    "工具观察总结阶段禁止调用工具；请只返回 Executor JSON 总结"
                )
            raw_message = response.get("message")
            if _looks_like_tool_call_markup(raw_message):
                raise ValueError(
                    "工具观察总结阶段返回了工具调用标记；请只总结当前结果，"
                    "把下一步工具调用留到 action_selection"
                )
            return ExecutorOutPut.model_validate(
                parse_json_object(raw_message)
            )
        except (ValueError, ValidationError, RuntimeError) as exc:
            last_error = exc
            raw_response = "" if response is None else response.get("message")
            executor_state.model_stage = "tool_summary"
            executor_state.raw_model_response_excerpt = bounded_text(
                raw_response, 4000, keep_tail=True
            )
            executor_state.validation_error = str(exc)
            retry_messages = [
                *retry_messages,
                {
                    "role": "user",
                    "content": _build_validation_feedback(exc, raw_response),
                },
            ]
    return _fallback_tool_summary(executor_state.latest_tool_context, last_error)


def _looks_like_tool_call_markup(content):
    if not isinstance(content, str):
        return False
    lowered = content.lower()
    return "<tool_call>" in lowered or "<arg_key>" in lowered


def _fallback_tool_summary(latest_tool_context, error):
    """Create a factual metadata-only summary when the model violates summary protocol."""
    if latest_tool_context is None:
        description = (
            "工具已经执行，但模型未返回合法的观察总结；当前没有可用的工具元数据，"
            "任务尚未完成。"
        )
    else:
        location = latest_tool_context.root or latest_tool_context.address or "未提供"
        metadata = latest_tool_context.metadata or {}
        directories = [
            str(item.get("address") or item.get("folder_name"))
            for item in metadata.get("directories", [])
            if isinstance(item, dict)
        ]
        files = [
            str(item.get("address") or item.get("file_name"))
            for item in metadata.get("files", [])
            if isinstance(item, dict)
        ]
        facts = [
            f"工具 {latest_tool_context.tool_name} 已执行",
            f"tool_result_seq={latest_tool_context.tool_result_seq}",
            f"状态={latest_tool_context.status}",
            f"路径={location}",
        ]
        if directories:
            facts.append("直接子目录=" + "、".join(directories))
        if files:
            facts.append("直接文件=" + "、".join(files))
        if latest_tool_context.pagination:
            facts.append(
                "分页="
                + json.dumps(
                    latest_tool_context.pagination,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        description = (
            "；".join(facts)
            + "。模型观察总结格式无效，已根据 LatestToolContext 生成确定性摘要；"
            "当前任务尚未宣告完成。"
        )
    return ExecutorOutPut(
        tool_name=None,
        selected_skill=None,
        description=description,
        is_finished=False,
    )


async def _select_skill_before_action(
    now_state,
    executor,
    executor_client,
    chat_function,
    skill_lists,
    executor_state,
):
    prompt = """
这是 Executor 的无工具 skill 选择阶段，尚未执行操作。
只有某个 skill 与当前 target 直接匹配且能实质改变执行方法时才选择；否则 selected_skill 为 null。
    不要根据名称勉强选择，不要调用工具，不要声称任务完成。description 必须少于 3000 个字符，只说明选择理由，
is_finished 固定为 false，tool_name 固定为 null。仅返回 ExecutorOutPut JSON。
"""
    messages = build_model_messages(
        executor,
        prompt,
        {
            "user_query": now_state.user_query,
            "target": now_state.now_target,
            "supervisor_handoff": executor_state.input_content.get(
                "supervisor_handoff", {}
            ),
            "skills": [
                {"id": item.id, "name": item.name, "description": item.description}
                for item in skill_lists
            ],
        },
    )
    response = await _call_model(
        now_state,
        chat_function,
        executor_client,
        executor,
        messages,
        [],
    )
    if response["status"] != "success" or response["tool"]:
        return None, "skill 选择阶段未形成有效选择，继续使用基础流程。"
    try:
        output = ExecutorOutPut.model_validate(
            parse_json_object(response.get("message"))
        )
    except Exception:
        return None, "skill 选择输出无效，继续使用基础流程。"
    return (
        _load_selected_skill(skill_lists, output.selected_skill),
        output.description,
    )


async def _call_model(now_state, chat_function, client, provider, messages, tools):
    from AgentLoop.context_compression import prepare_request
    async def compression_call(compression_messages, compression_tools):
        return await _counted_model_call(now_state, chat_function, client, provider,
                                         compression_messages, compression_tools)
    messages = await prepare_request(now_state, messages, tools, compression_call)
    return await _counted_model_call(now_state, chat_function, client, provider, messages, tools)


async def _counted_model_call(now_state, chat_function, client, provider, messages, tools):
    if len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)) > now_state.context_max_chars:
        raise RuntimeError("context_budget_blocked: model/compression request exceeds hard character budget")
    if now_state.model_request_count >= now_state.max_model_requests:
        raise RuntimeError("model request budget exhausted")
    now_state.model_request_count += 1
    from State.session_checkpoint import active_run
    run = active_run()
    if run:
        run.emit("model_request", "executor", model_request_count=now_state.model_request_count)
        run.checkpoint()
    response = await call_chat_function(
        chat_function=chat_function,
        provider=provider,
        client=client,
        model=now_state.executor_model,
        messages=messages,
        tools=tools,
        temperature=now_state.temperature,
    )
    now_state.total_token += response.get("total_tokens", 0)
    if run:
        run.checkpoint()
    return response


def _build_executor_messages(
    time: int,
    supervisor_state: SupervisorState,
    now_state: AgentState,
    executor_state: ExecutorState,
    executor: str,
    skill_lists: list[Skill],
    skill: Skill | None = None,
    include_latest_tool_context: bool = True,
):
    information = {
        **executor_state.input_content,
        "step": time,
        "max_steps": now_state.max_executor_steps,
        "user_query": now_state.user_query,
        "environment": {"java": now_state.java, "python": now_state.python,
            "project_root": now_state.project_root, "staging_root": now_state.staging_root,
            "workspace": now_state.staging_root or now_state.project_root,
            "permission_rule": "Host 运行时授权；真实项目只读，编辑/执行在经批准的副本，写回需单独批准固定变更包"},
        "skills": [
            {"id": item.id, "name": item.name, "description": item.description}
            for item in skill_lists
        ],
        "recent_descriptions": list(executor_state.memory_window),
    }
    if include_latest_tool_context:
        information["latest_tool_context"] = executor_state.latest_tool_context
    if supervisor_state.need_date_back:
        information["date_back"] = {
            "original_target": now_state.now_target,
            "problem": supervisor_state.date_back_input,
            "modification": supervisor_state.modify_content,
            "verification_requirement": supervisor_state.verification_requirement,
        }
    if skill is not None:
        information["selected_skill"] = {
            "name": skill.name,
            "description": skill.description,
            "content": _read_skill(skill),
        }
    return build_model_messages(executor, EXECUTOR_PROMPT, information)


def _reset_executor_turn(now_state, supervisor_state, executor_state):
    previous_task_id = executor_state.task_id
    previous_latest_tool_context = executor_state.latest_tool_context
    executor_state.task_id = now_state.now_task_id
    executor_state.now_target = now_state.now_target
    executor_state.supervisor_guidance = supervisor_state.executor_plan
    handoff = now_state.task_handoffs.get(now_state.now_task_id)
    if handoff is None:
        raise RuntimeError("Supervisor handoff is missing for the current task")
    executor_state.input_content = {
        "task_id": now_state.now_task_id,
        "task_attempt": now_state.task_attempt,
        "target": now_state.now_target,
        "supervisor_handoff": handoff.to_dict(),
    }
    executor_state.tool_event_seqs = []
    executor_state.executor_turn_seq = None
    executor_state.tool_summaries = []
    executor_state.completed_tool_calls = []
    executor_state.last_tool_result_ref = None
    executor_state.tool_result_refs = []
    executor_state.output_content = ""
    executor_state.tool_name = ""
    executor_state.tool_arguments = {}
    executor_state.tool_ok = None
    executor_state.description = ""
    executor_state.is_finished = False
    executor_state.is_error = False
    executor_state.error_message = ""
    executor_state.memory_window = []
    executor_state.selected_skill = None
    executor_state.exit_reason = ""
    executor_state.task_attempt = now_state.task_attempt
    executor_state.model_stage = ""
    executor_state.raw_model_response_excerpt = ""
    executor_state.validation_error = ""
    executor_state.latest_tool_context = (
        previous_latest_tool_context
        if previous_task_id == now_state.now_task_id
        else None
    )


def _tool_call_fingerprint(task_id, tool_name, arguments):
    return f"{task_id}:{_tool_call_signature(tool_name, arguments)}"


def _tool_call_signature(tool_name, arguments):
    from State.project_workspace import WORKSPACE, digest, packed
    workspace = WORKSPACE.get()
    scope = workspace.cache_scope(tool_name, arguments) if workspace else None
    if scope:
        arguments = scope["arguments"]
    normalized_arguments = json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"{tool_name}:{normalized_arguments}" + (f":workspace={digest(packed(scope))}" if scope else "")


def _remember_task_evidence(now_state, latest_tool_context, tool_name, arguments):
    if latest_tool_context is None or latest_tool_context.tool_result_seq is None:
        return

    from AgentLoop.agent import EvidenceReference

    paths = list(latest_tool_context.paths)
    metadata = latest_tool_context.metadata or {}
    for field_name in ("directories", "files"):
        for item in metadata.get(field_name, []):
            if not isinstance(item, dict):
                continue
            path = item.get("address")
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)
            if len(paths) >= 20:
                break
        if len(paths) >= 20:
            break

    reference = EvidenceReference(
        tool_result_seq=latest_tool_context.tool_result_seq,
        tool_name=tool_name,
        root=latest_tool_context.root,
        address=latest_tool_context.address,
        status=latest_tool_context.status,
        paths=paths[:20],
    )
    references = now_state.task_evidence_references.setdefault(
        now_state.now_task_id, []
    )
    if not any(
        item.tool_result_seq == reference.tool_result_seq for item in references
    ):
        references.append(reference)

    if tool_name in REUSABLE_READ_TOOLS:
        signature = _tool_call_signature(tool_name, arguments)
        reusable_calls = now_state.task_reusable_read_calls.setdefault(
            now_state.now_task_id, []
        )
        if signature not in reusable_calls:
            reusable_calls.append(signature)


def _build_latest_tool_context(tool_name, tool_result, tool_result_seq):
    """Keep deterministic navigation metadata, never tool body content."""
    from AgentLoop.agent import LatestToolContext

    output = tool_result.output if isinstance(tool_result.output, dict) else {}
    root = output.get("root")
    address = output.get("address")
    paths = []

    def remember_path(value):
        if isinstance(value, str) and value and value not in paths:
            paths.append(value)

    def collect_paths(value):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key).lower()
                if (
                    normalized_key in {"root", "address", "path"}
                    or normalized_key.endswith("_path")
                    or normalized_key.endswith("_address")
                ):
                    remember_path(item)
                if normalized_key not in {
                    "content", "stdout", "stderr", "logs", "raw_result",
                    "source_code", "document_text",
                }:
                    collect_paths(item)
        elif isinstance(value, list):
            for item in value:
                collect_paths(item)

    remember_path(root)
    remember_path(address)
    collect_paths(output)
    for key in ("file_address", "project_path", "jar_path", "pom_path", "code_path"):
        remember_path(output.get(key))

    file_record = output.get("file")
    if isinstance(file_record, dict):
        remember_path(file_record.get("address"))
    for collection_name in ("directories", "files"):
        records = output.get(collection_name)
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict):
                remember_path(record.get("address"))

    summary = output.get("summary") if isinstance(output.get("summary"), dict) else {}
    pagination = {}
    for source in (summary, file_record if isinstance(file_record, dict) else {}):
        for key in (
            "start_index", "next_start_index", "start_char", "end_char",
            "next_start_char", "has_more",
        ):
            if key in source:
                pagination[key] = source[key]

    metadata = {}
    for source in (output, summary):
        for key in (
            "scope", "total_entries", "returned_entries", "total_directories",
            "total_files", "returned_files", "content_chars", "operation",
            "created", "changed", "bytes_written", "replacements", "returncode",
            "jdk_version", "is_spring_project", "is_spring_boot_project",
            "build_tools",
        ):
            if key in source:
                metadata[key] = source[key]

    if tool_name == "read_all_files_tool":
        directories = output.get("directories") or []
        files = output.get("files") or []
        metadata["directories"] = [
            {
                "folder_name": item.get("folder_name"),
                "address": item.get("address"),
            }
            for item in directories if isinstance(item, dict)
        ]
        metadata["files"] = [
            {
                "file_name": item.get("file_name"),
                "address": item.get("address"),
                "suffix": item.get("suffix"),
            }
            for item in files if isinstance(item, dict)
        ]
        paths = []
    elif tool_name == "read_files_content_tool":
        records = output.get("files") or []
        is_single_file = isinstance(file_record, dict)
        if isinstance(file_record, dict):
            records = [file_record]
        metadata["files"] = [
            {
                "file_name": item.get("file_name"),
                **(
                    {}
                    if is_single_file
                    else {"address": item.get("address")}
                ),
                "suffix": item.get("suffix"),
                "content_status": item.get("content_status"),
                "content_chars": item.get("content_chars"),
            }
            for item in records if isinstance(item, dict)
        ]
        paths = []
    elif tool_name == "sort_files_by_suffix_tool":
        sorted_files = output.get("sorted")
        if isinstance(sorted_files, dict):
            metadata["suffix_counts"] = {
                str(suffix): len(records) if isinstance(records, list) else 0
                for suffix, records in sorted_files.items()
            }

    return LatestToolContext(
        tool_name=tool_name,
        tool_ok=tool_result.ok,
        tool_result_seq=tool_result_seq,
        root=root if isinstance(root, str) else None,
        address=address if isinstance(address, str) else None,
        paths=paths,
        status=str(output.get("status") or ("success" if tool_result.ok else "error")),
        message=bounded_text(tool_result.message or output.get("message"), 1000),
        metadata=metadata,
        pagination=pagination,
    )


def _multiple_tool_call_feedback(tool_calls):
    returned_calls = [
        {"tool_name": call.name, "arguments": call.arguments}
        for call in tool_calls
    ]
    return (
        "工具调用协议错误：模型一次返回了多个工具调用，本次未执行任何工具。"
        "请结合当前目标、dependency_context、已有 locator 和 recent_descriptions，重新选择当前最应该执行的"
        "一个工具；下一次只能返回一个工具调用，不要重复返回整组调用。"
        f"本次返回：{bounded_text(json.dumps(returned_calls, ensure_ascii=False), 1600)}"
    )


def _repeat_is_explicitly_requested(instruction):
    lowered = str(instruction).lower()
    if any(
        marker in lowered
        for marker in (
            "不要重新",
            "不得重新",
            "无需重新",
            "不需要重新",
            "禁止重新",
            "不要再次",
            "不得再次",
            "do not rerun",
            "do not repeat",
        )
    ):
        return False
    return any(
        marker in lowered
        for marker in (
            "请重新",
            "需要重新",
            "必须重新",
            "重新执行",
            "重试",
            "再次执行",
            "rerun",
            "re-run",
            "repeat",
        )
    )


def _remember(executor_state, description: str):
    show_description("executor", description, task_id=executor_state.task_id)
    if description:
        executor_state.memory_window.append(description)


def _build_validation_feedback(exc, raw_response):
    if isinstance(exc, ValidationError):
        error_details = exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    else:
        error_details = str(exc)
    feedback = {
        "error_type": type(exc).__name__,
        "validation_errors": error_details,
        "raw_response_excerpt": bounded_text(
            raw_response, 2000, keep_tail=True
        ),
        "required_contract": {
            "tool_name": None,
            "selected_skill": None,
            "description": "非空字符串；只写新增事实、证据、完成判断和必要未完成项",
            "is_finished": False,
        },
    }
    return (
        "上一条 Executor 响应校验失败。请根据错误修正，"
        "只返回完整 JSON，不要添加解释、前缀或 Markdown：\n"
        + json.dumps(feedback, ensure_ascii=False)
    )


def _update_now_state(now_state, executor_state):
    now_state.executor_input = executor_state.input_content
    now_state.executor_output = executor_state.output_content


def _load_selected_skill(skill_list, selection: SelectedSkill | None):
    if selection is None or selection.skill_id is None or not selection.skill_name:
        return None
    if selection.skill_id < 0:
        return None
    selected = next((item for item in skill_list if item.id == selection.skill_id), None)
    if selected is None or selected.name != selection.skill_name:
        return None
    if not Path(selected.address).is_file():
        return None
    return selected


def _read_skill(skill) -> str:
    try:
        return Path(skill.address).read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_skill_content(skill_list, skill_name, skill_id, message=None, executor=None):
    """Compatibility wrapper retained for callers outside this module."""
    selection = SelectedSkill(skill_name=skill_name, skill_id=skill_id)
    return _load_selected_skill(skill_list, selection)
