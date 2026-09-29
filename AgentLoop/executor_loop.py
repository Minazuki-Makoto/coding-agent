from __future__ import annotations

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
)
from MCP_functions.tool_registry import AgentRole, ToolRegistry
from State.save_executor_state_history import save_state_history

if TYPE_CHECKING:
    from AgentLoop.agent import AgentState, ExecutorState, Skill, SupervisorState


EXECUTOR_PROMPT = """
你是 coding agent 的 Executor。只执行当前 target 和 Supervisor 本轮指导，不修改总计划或 task_id。

规则：
1. 每一步最多调用一个已提供工具；不得调用不存在的工具或伪造结果。
2. 工具调用消息可以没有文本。收到真实 tool_result 后，才总结该操作。
3. is_finished 只表示本轮委派目标已完成，之后仍必须由 Supervisor 验收。
4. 信息或证据不足时返回 false，并说明下一步；不要把“没有报错”当作完成证据。
5. selected_skill 可为空。若选择，必须使用 skills 中真实的 id/name；skill 不授予额外工具权限。
6. description 必须是非空字符串，具体记录本步事实、真实证据、产出和未完成项；不得只写“已完成”“成功”等无法验收的结论。
7. is_finished 必须是 JSON 布尔值 true/false，不能使用字符串。最终仅返回 JSON：
{
  "tool_name": null,
  "selected_skill": {"skill_name": null, "skill_id": null},
  "description": "本步事实、产出、证据和未完成项",
  "is_finished": false
}
"""


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
        value = value.strip()
        if not value:
            raise ValueError("description 不能为空或只包含空白字符")
        return value

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
    _reset_executor_turn(now_state, supervisor_state, executor_state)
    selected_skill = None
    descriptions: list[str] = []
    steps = 0
    exit_reason = "executor_step_limit"

    print("====================进入executor执行轮======================")
    if skill_lists:
        selected_skill, selection_description = await _select_skill_before_action(
            now_state,
            executor,
            executor_client,
            chat_function,
            skill_lists,
        )
        executor_state.selected_skill = selected_skill
        if selection_description:
            descriptions.append(selection_description)
            _remember(executor_state, selection_description)

    while steps < now_state.max_executor_steps:
        steps += 1
        print(f"\n\n第{steps}轮")
        now_state.executor_executing_times += 1
        try:
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
                executor_tools,
            )
            if response["status"] != "success":
                raise RuntimeError(response.get("message") or "executor model request failed")
            executor_state.model_stage = "action_selection"
            executor_state.raw_model_response_excerpt = bounded_text(
                response.get("message"), 4000, keep_tail=True
            )
            executor_state.validation_error = ""

            tool_calls = normalize_tool_calls(executor, response["tool"])
            if len(tool_calls) > 1:
                description = "模型一次返回了多个工具调用；本步未执行任何工具，请一次只选择一个。"
                descriptions.append(description)
                _remember(executor_state, description)
                executor_state.is_error = True
                executor_state.error_message = description
                continue

            if not tool_calls:
                try:
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

                description = bounded_text(
                    output.description, DESCRIPTION_MAX_CHARS, keep_tail=True
                )
                if description:
                    descriptions.append(description)
                    _remember(executor_state, description)
                executor_state.description = description
                executor_state.is_finished = output.is_finished
                if output.is_finished:
                    exit_reason = "executor_reported_finished"
                    break
                continue

            tool_call = tool_calls[0]
            tool_result = await tool_registry.call(
                role=AgentRole.EXECUTOR,
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
            )
            now_state.executor_seq += 1
            executor_state.executor_seq = now_state.executor_seq
            executor_state.passed_seq_list.append(executor_state.executor_seq)
            executor_state.tool_name = tool_call.name
            executor_state.tool_arguments = tool_call.arguments
            executor_state.tool_result = tool_result
            executor_state.tool_ok = tool_result.ok
            executor_state.description = "工具已执行，等待观察总结。"
            executor_state.is_error = not tool_result.ok
            executor_state.error_message = (
                "" if tool_result.ok else (tool_result.message or tool_result.error_type or "")
            )
            save_state_history(executor_state, record_type="tool_event")

            messages = _build_executor_messages(
                steps - 1,
                supervisor_state,
                now_state,
                executor_state,
                executor,
                skill_lists,
                selected_skill,
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

            description = bounded_text(
                summary.description, DESCRIPTION_MAX_CHARS, keep_tail=True
            )
            descriptions.append(description)
            _remember(executor_state, description)
            executor_state.description = description
            executor_state.is_finished = summary.is_finished
            executor_state.is_error = not tool_result.ok
            if summary.is_finished:
                exit_reason = "executor_reported_finished"
                break

        except Exception as exc:
            executor_state.is_error = True
            executor_state.error_message = str(exc)
            if not executor_state.validation_error:
                executor_state.validation_error = str(exc)
            description = f"Executor 本轮异常：{exc}"
            executor_state.description = description
            descriptions.append(description)
            _remember(executor_state, description)
            exit_reason = "executor_exception"
            break

    executor_state.output_content = bounded_text(
        "\n".join(item for item in descriptions if item),
        DESCRIPTION_MAX_CHARS,
        keep_tail=True,
    )
    executor_state.exit_reason = exit_reason
    executor_state.input_content = {
        "target": executor_state.now_target,
        "supervisor_guidance": executor_state.supervisor_guidance,
    }
    now_state.executor_seq += 1
    executor_state.executor_seq = now_state.executor_seq
    executor_state.passed_seq_list.append(executor_state.executor_seq)
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
    retry_messages = list(messages)
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
            return ExecutorOutPut.model_validate(
                parse_json_object(response.get("message"))
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
    raise RuntimeError(f"工具已执行，但观察总结失败：{last_error}")


async def _select_skill_before_action(
    now_state,
    executor,
    executor_client,
    chat_function,
    skill_lists,
):
    prompt = """
这是 Executor 的无工具 skill 选择阶段，尚未执行任何操作。
根据当前目标从 skills 中选择一个指导后续操作，或明确不选择。
不得声称任务已经完成。仅返回 Executor JSON 格式。
"""
    messages = build_model_messages(
        executor,
        prompt,
        {
            "user_query": bounded_text(now_state.user_query, USER_QUERY_MAX_CHARS),
            "target": now_state.now_target,
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
    if now_state.model_request_count >= now_state.max_model_requests:
        raise RuntimeError("model request budget exhausted")
    now_state.model_request_count += 1
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
    return response


def _build_executor_messages(
    time: int,
    supervisor_state: SupervisorState,
    now_state: AgentState,
    executor_state: ExecutorState,
    executor: str,
    skill_lists: list[Skill],
    skill: Skill | None = None,
):
    information = {
        "have_answered_times": time,
        "user_query": bounded_text(now_state.user_query, USER_QUERY_MAX_CHARS),
        "target": now_state.now_target,
        "supervisor_distributed_target": supervisor_state.executor_plan,
        "environment": {"java": now_state.java, "python": now_state.python},
        "skills": [
            {"id": item.id, "name": item.name, "description": item.description}
            for item in skill_lists
        ],
        "recent_descriptions": list(executor_state.memory_window)[-10:],
        "latest_tool_observation": (
            executor_state.tool_result.to_dict()
            if hasattr(executor_state.tool_result, "to_dict")
            else executor_state.tool_result
        ),
    }
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
    executor_state.task_id = now_state.now_task_id
    executor_state.now_target = now_state.now_target
    executor_state.supervisor_guidance = supervisor_state.executor_plan
    executor_state.passed_seq_list = []
    executor_state.input_content = ""
    executor_state.output_content = ""
    executor_state.tool_name = ""
    executor_state.tool_arguments = {}
    executor_state.tool_result = None
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


def _remember(executor_state, description: str):
    if description:
        executor_state.memory_window.append(
            bounded_text(description, DESCRIPTION_MAX_CHARS, keep_tail=True)
        )
        del executor_state.memory_window[:-10]


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
            "description": "非空字符串，包含事实、证据、产出和未完成项",
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
        return bounded_text(
            Path(skill.address).read_text(encoding="utf-8"),
            DESCRIPTION_MAX_CHARS,
            keep_tail=True,
        )
    except OSError:
        return ""


def _load_skill_content(skill_list, skill_name, skill_id, message=None, executor=None):
    """Compatibility wrapper retained for callers outside this module."""
    selection = SelectedSkill(skill_name=skill_name, skill_id=skill_id)
    return _load_selected_skill(skill_list, selection)
