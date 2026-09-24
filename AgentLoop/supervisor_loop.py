from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from AgentLoop.loop_utils import (
    DESCRIPTION_MAX_CHARS,
    bounded_text,
    build_model_messages,
    call_chat_function,
    normalize_tool_calls,
    parse_json_object,
    tool_result_for_context,
)
from MCP_functions.tool_registry import AgentRole,ToolRegistry
from State.save_supervision_state_history import save_supervisor_state_history

if TYPE_CHECKING:
    from AgentLoop.agent import AgentState


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORRECTNESS_SKILL_PATH = PROJECT_ROOT / "Skills" / "supervisor_skill.md"
ERROR_SKILL_PATH = PROJECT_ROOT / "Skills" / "supervisor_error_skill.md"
MEMORY_RETRIEVAL_SKILL_PATH = (
    PROJECT_ROOT / "Skills" / "memory_retrieval" / "SKILL.md"
)

supervisor_runtime_policy = """

## 本轮运行时上下文规则

主循环不会把 executor_response、tool_results 或 executor_tool_events 直接放进你的输入。
当前 executor 完整回合已经保存到 executor_history.jsonl，当前 task_id 和 seq 能唯一定位它。

你必须遵守：
1. 第一轮只依据 target、标识和 previous_description 决定要读取的最小记忆，不能猜测 executor 做了什么。
2. 审查当前执行时，优先调用 read_task_error，并使用输入中精确的 session_address、chat_id、task_id、seq。
3. 只有确有需要时才读取更早任务、错误历史、监督历史或项目结构；每个内部步骤最多调用一个工具。
4. 每次继续调用工具时，尽量在普通文本中留下简短的当前证据描述。下一内部步骤只保留该 description 和最近一次工具观察，不保留完整上下文链。
5. 工具观察可能因上下文预算被截断。遇到截断时应缩小查询范围；不能根据被省略内容作出通过结论。
6. 只有完成必要查询并形成最终结论后，才输出技能规定的 JSON。最终 JSON 不调用工具。
"""


async def check_right_task(
        now_state:"AgentState",
        supervisor:str,
        supervisor_client,
        chat_function,
        supervisor_tools:list[dict],
        tool_registry:ToolRegistry
):
    now_state.phase = "correctness_review"
    information = _build_supervisor_information(
        now_state=now_state,
        previous_description=now_state.previous_description
    )

    try:
        final_results = await _run_supervisor_model_loop(
            now_state=now_state,
            supervisor=supervisor,
            supervisor_client=supervisor_client,
            chat_function=chat_function,
            supervisor_tools=supervisor_tools,
            tool_registry=tool_registry,
            skill_path=CORRECTNESS_SKILL_PATH,
            information=information
        )
        _apply_correctness_review(
            now_state=now_state,
            final_results=final_results
        )

    except Exception as e:
        review = now_state.correctness_review
        review.review_status = "blocked"
        review.supervisor_check = False
        review.supervisor_description = "正确性审查没有得到可解析的最终结果"
        review.is_task_finished = False
        review.needs_error_review = False
        review.error_review_context = str(e)

    return now_state.correctness_review


async def review_error_task(
        now_state:"AgentState",
        supervisor:str,
        supervisor_client,
        chat_function,
        supervisor_tools:list[dict],
        tool_registry:ToolRegistry
):
    now_state.phase = "error_review"
    previous_description = (
        now_state.correctness_review.supervisor_description
        or now_state.previous_description
    )
    information = _build_supervisor_information(
        now_state=now_state,
        previous_description=previous_description
    )

    try:
        final_results = await _run_supervisor_model_loop(
            now_state=now_state,
            supervisor=supervisor,
            supervisor_client=supervisor_client,
            chat_function=chat_function,
            supervisor_tools=supervisor_tools,
            tool_registry=tool_registry,
            skill_path=ERROR_SKILL_PATH,
            information=information
        )
        _apply_error_review(
            now_state=now_state,
            final_results=final_results
        )

    except Exception as e:
        review = now_state.error_review
        review.error_status = "blocked"
        review.error_type = "insufficient_evidence"
        review.root_cause = ""
        review.supervisor_description = "错误审查没有得到可解析的最终结果"
        review.executor_instruction = ""
        review.verification_requirement = ""
        review.is_blocked = True
        review.block_reason = str(e)

    return now_state.error_review


def _build_supervisor_information(now_state,previous_description):
    return {
        "target":now_state.now_target,
        "session_address":now_state.session_address,
        "chat_id":now_state.chat_id,
        "task_id":now_state.now_task_id,
        "seq":now_state.seq,
        "previous_description":bounded_text(
            previous_description,
            DESCRIPTION_MAX_CHARS,
            keep_tail=True
        ),
    }


async def _run_supervisor_model_loop(
        now_state,
        supervisor,
        supervisor_client,
        chat_function,
        supervisor_tools,
        tool_registry,
        skill_path,
        information
):
    with skill_path.open("r",encoding="utf-8") as f:
        skill_content = f.read()

    with MEMORY_RETRIEVAL_SKILL_PATH.open("r",encoding="utf-8") as f:
        memory_retrieval_skill = f.read()

    prompt = "\n\n".join(
        [skill_content,memory_retrieval_skill,supervisor_runtime_policy]
    )
    previous_description = information.get("previous_description","")
    current_memory_observation = None
    tool_steps = 0

    while True:
        current_information = dict(information)
        current_information["previous_description"] = bounded_text(
            previous_description,
            DESCRIPTION_MAX_CHARS,
            keep_tail=True
        )
        current_information["current_memory_observation"] = (
            current_memory_observation
        )
        messages = build_model_messages(
            provider=supervisor,
            prompt=prompt,
            information=current_information
        )

        response = call_chat_function(
            chat_function=chat_function,
            provider=supervisor,
            client=supervisor_client,
            model=now_state.supervisor_model,
            messages=messages,
            tools=supervisor_tools,
            temperature=now_state.temperature
        )

        if response.get("status") == "error":
            raise RuntimeError(response.get("message","supervisor model error"))

        tool_calls = normalize_tool_calls(
            provider=supervisor,
            tool_calls=response.get("tool",[])
        )

        if len(tool_calls) > 1:
            raise ValueError(
                "supervisor must call at most one tool in each internal step"
            )

        if not tool_calls:
            return parse_json_object(response.get("message"))

        if tool_steps >= now_state.max_supervision_times:
            raise RuntimeError(
                "supervisor exceeded max tool-call steps: "
                f"{now_state.max_supervision_times}"
            )

        tool_call = tool_calls[0]
        tool_result = await tool_registry.call(
            role=AgentRole.SUPERVISOR,
            tool_name=tool_call.name,
            arguments=tool_call.arguments
        )
        tool_steps += 1

        _save_supervisor_tool_event(
            now_state=now_state,
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            tool_result=tool_result
        )
        current_memory_observation = tool_result_for_context(tool_result)

        response_description = response.get("message") or ""
        if response_description:
            previous_description = response_description
        else:
            previous_description = (
                f"上一内部步骤调用了 {tool_call.name}；"
                "真实结果见 current_memory_observation。"
            )


def _save_supervisor_tool_event(
        now_state,
        tool_name,
        arguments,
        tool_result
):
    history_state = SimpleNamespace(
        session_address=now_state.session_address,
        chat_id=now_state.chat_id,
        seq=now_state.seq,
        supervisor_seq=now_state.supervisor_seq,
        now_task_id=now_state.now_task_id,
        now_target=now_state.now_target,
        tool=tool_name,
        is_error=not tool_result.ok,
        error_message=tool_result.message,
        tool_input=arguments,
        tool_results=tool_result.to_dict(),
        user_query="",
    )
    save_supervisor_state_history(history_state)
    now_state.supervisor_seq += 1


def _apply_correctness_review(now_state,final_results):
    valid_status = {
        "passed",
        "failed",
        "insufficient_evidence",
        "blocked",
    }

    review_status = final_results.get("review_status")
    if review_status not in valid_status:
        raise ValueError(f"invalid review_status: {review_status}")

    supervisor_check = _get_boolean(final_results,"supervisor_check")
    is_task_finished = _get_boolean(final_results,"is_task_finished")
    needs_error_review = _get_boolean(final_results,"needs_error_review")

    if supervisor_check and review_status != "passed":
        raise ValueError("supervisor_check=true requires review_status=passed")

    if is_task_finished and not supervisor_check:
        raise ValueError("is_task_finished=true requires supervisor_check=true")

    review = now_state.correctness_review
    review.review_status = review_status
    review.supervisor_check = supervisor_check
    review.supervisor_description = _get_string(
        final_results,"supervisor_description"
    )
    review.is_task_finished = is_task_finished
    review.needs_error_review = needs_error_review
    review.error_review_context = _get_string(
        final_results,"error_review_context"
    )
    review.repairs = _get_references(final_results.get("repairs",[]))
    review.invalidates = _get_references(final_results.get("invalidates",[]))

    if not supervisor_check and review.repairs:
        raise ValueError("failed review cannot contain repairs")


def _apply_error_review(now_state,final_results):
    valid_status = {"confirmed","unconfirmed","blocked"}
    valid_type = {
        "implementation_error",
        "tool_protocol_error",
        "environment_error",
        "insufficient_evidence",
        "historical_conflict",
    }

    error_status = final_results.get("error_status")
    error_type = final_results.get("error_type")

    if error_status not in valid_status:
        raise ValueError(f"invalid error_status: {error_status}")

    if error_type not in valid_type:
        raise ValueError(f"invalid error_type: {error_type}")

    backtrack_to = final_results.get("backtrack_to")
    if backtrack_to is not None:
        references = _get_references([backtrack_to])
        backtrack_to = references[0]

    review = now_state.error_review
    review.error_status = error_status
    review.error_type = error_type
    review.root_cause = _get_string(final_results,"root_cause")
    review.supervisor_description = _get_string(
        final_results,"supervisor_description"
    )
    review.executor_instruction = _get_string(
        final_results,"executor_instruction"
    )
    review.verification_requirement = _get_string(
        final_results,"verification_requirement"
    )
    review.invalidates = _get_references(
        final_results.get("invalidates",[])
    )
    review.backtrack_to = backtrack_to
    review.backtrack_reason = _get_string(
        final_results,"backtrack_reason"
    )
    review.is_blocked = _get_boolean(final_results,"is_blocked")
    review.block_reason = _get_string(final_results,"block_reason")


def _get_boolean(values,name):
    value = values.get(name)
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _get_string(values,name):
    value = values.get(name,"")
    if not isinstance(value,str):
        raise ValueError(f"{name} must be a string")
    return value


def _get_references(references):
    if not isinstance(references,list):
        raise ValueError("history references must be a list")

    normalized = []

    for reference in references:
        if not isinstance(reference,dict):
            raise ValueError("history reference must be a dictionary")

        task_id = reference.get("task_id")
        seq = reference.get("seq")

        if type(task_id) is not int or type(seq) is not int:
            raise ValueError("history reference task_id and seq must be integers")

        normalized.append(
            {
                "task_id":task_id,
                "seq":seq
            }
        )

    return normalized
