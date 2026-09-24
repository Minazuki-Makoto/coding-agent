from __future__ import annotations

import json
from typing import TYPE_CHECKING

from AgentLoop.loop_utils import (
    DESCRIPTION_MAX_CHARS,
    USER_QUERY_MAX_CHARS,
    bounded_text,
    build_model_messages,
    call_chat_function,
    normalize_tool_calls,
    tool_result_for_context,
)
from MCP_functions.tool_registry import AgentRole,ToolRegistry

if TYPE_CHECKING:
    from AgentLoop.agent import AgentState


executor_prompt = """
你是 coding agent 的 executor，只负责完整执行当前 target，不重新规划整个任务。

每次模型请求都会提供 previous_description。第一轮可能为空；之后它是上一轮模型或 supervisor 留下的描述。跨轮判断只能依赖这个描述和当前输入，不能假设仍然拥有更早的对话上下文。

执行规则：
1. 每个内部步骤最多调用一个本轮 tools 中真实存在的工具，严格遵守名称、description 和参数 schema。
2. current_tool_observation 只包含你刚调用的工具结果，并且可能因上下文预算被截断。需要更多信息时缩小范围后再次调用工具，不能猜测省略内容。
3. 工具结果只服务于当前 executor 内部循环；它不会直接移交给 supervisor，也不会进入下一次 executor 外层回合。
4. 发起工具调用时，尽量同时在普通文本中留下简短的当前进度描述，供下一个内部步骤作为 previous_description。
5. 收到 supervisor 的 previous_description 后，执行其中要求的最小动作和验证，不扩大修改范围。
6. 工具失败时可以在本轮内根据真实错误继续处理，但相同错误没有新证据时不要重复调用。
7. 当前 target 已经完成，或继续推进需要 supervisor/用户判断时，停止调用工具并输出最终 description，说明实际动作、验证结果和剩余问题。

不得调用未提供的工具，不得伪造工具结果，不得自行宣布未验证的操作成功。
"""


async def run_executor_loop(
        now_state:"AgentState",
        executor:str,
        executor_client,
        chat_function,
        executor_tools:list[dict],
        tool_registry:ToolRegistry
):
    now_state.phase = "executor"
    now_state.tool = None
    now_state.tool_input = []
    now_state.tool_results = None
    now_state.executor_response = ""
    now_state.executor_tool_events = []
    now_state.is_error = False
    now_state.error_message = ""

    outer_description = bounded_text(
        now_state.previous_description,
        DESCRIPTION_MAX_CHARS,
        keep_tail=True
    )
    outer_executor_input = _bounded_executor_input(now_state.executor_input)
    now_state.executor_input = None
    outer_error_advice = bounded_text(
        now_state.error_review.executor_instruction,
        DESCRIPTION_MAX_CHARS,
        keep_tail=True
    )
    outer_verification_requirement = bounded_text(
        now_state.error_review.verification_requirement,
        DESCRIPTION_MAX_CHARS,
        keep_tail=True
    )
    previous_description = outer_description
    current_tool_observation = None
    tool_steps = 0

    while True:
        information = {
            "query":bounded_text(
                now_state.user_query,
                USER_QUERY_MAX_CHARS,
                keep_tail=True
            ),
            "target":now_state.now_target,
            "information":{
                "session_address":now_state.session_address,
                "chat_id":now_state.chat_id,
                "task_id":now_state.now_task_id,
                "seq":now_state.seq,
                "java":now_state.java,
                "python":now_state.python,
            },
            "previous_description":bounded_text(
                previous_description,
                DESCRIPTION_MAX_CHARS,
                keep_tail=True
            ),
            "error_advice":outer_error_advice,
            "verification_requirement":outer_verification_requirement,
            "backtrack_input":(
                outer_executor_input if tool_steps == 0 else None
            ),
            "current_tool_observation":current_tool_observation,
        }
        messages = build_model_messages(
            provider=executor,
            prompt=executor_prompt,
            information=information
        )

        try:
            response = call_chat_function(
                chat_function=chat_function,
                provider=executor,
                client=executor_client,
                model=now_state.executor_model,
                messages=messages,
                tools=executor_tools,
                temperature=now_state.temperature
            )
        except Exception as e:
            _set_executor_error(now_state,str(e))
            break

        if response.get("status") == "error":
            _set_executor_error(
                now_state,
                response.get("message","executor model error")
            )
            break

        response_description = response.get("message") or ""
        now_state.executor_response = response_description

        try:
            tool_calls = normalize_tool_calls(
                provider=executor,
                tool_calls=response.get("tool",[])
            )
        except ValueError as e:
            _set_executor_error(now_state,str(e))
            break

        if len(tool_calls) > 1:
            _set_executor_error(
                now_state,
                "executor must call at most one tool in each internal step"
            )
            break

        if not tool_calls:
            if not response_description:
                _set_executor_error(
                    now_state,
                    "executor returned neither description nor tool call"
                )
            break

        if tool_steps >= now_state.max_executor_steps:
            _set_executor_error(
                now_state,
                f"executor exceeded max tool-call steps: {now_state.max_executor_steps}"
            )
            break

        tool_call = tool_calls[0]
        tool_result = await tool_registry.call(
            role=AgentRole.EXECUTOR,
            tool_name=tool_call.name,
            arguments=tool_call.arguments
        )
        tool_steps += 1

        event = {
            "step":tool_steps,
            "call_id":tool_call.call_id,
            "tool_name":tool_call.name,
            "arguments":tool_call.arguments,
            "result":tool_result.to_dict(),
        }
        now_state.executor_tool_events.append(event)
        current_tool_observation = tool_result_for_context(tool_result)

        if response_description:
            previous_description = response_description
        else:
            previous_description = (
                f"上一内部步骤调用了 {tool_call.name}；"
                "真实结果见 current_tool_observation。"
            )

    _collect_executor_history_state(
        now_state=now_state,
        outer_description=outer_description,
        outer_executor_input=outer_executor_input,
        outer_error_advice=outer_error_advice,
        outer_verification_requirement=outer_verification_requirement
    )
    return now_state


def _set_executor_error(now_state,message):
    now_state.is_error = True
    now_state.error_message = str(message)


def _collect_executor_history_state(
        now_state,
        outer_description,
        outer_executor_input,
        outer_error_advice,
        outer_verification_requirement
):
    events = now_state.executor_tool_events
    input_content = {
        "previous_description":outer_description,
        "error_advice":outer_error_advice,
        "verification_requirement":outer_verification_requirement,
        "backtrack_input":outer_executor_input,
    }

    if events:
        now_state.tool = [event["tool_name"] for event in events]
        input_content["events"] = [
                {
                    "step":event["step"],
                    "tool_name":event["tool_name"],
                    "arguments":event["arguments"],
                }
                for event in events
        ]
        now_state.tool_input = input_content
        now_state.tool_results = {
            "executor_description":now_state.executor_response,
            "events":[
                {
                    "step":event["step"],
                    "tool_name":event["tool_name"],
                    "result":event["result"],
                }
                for event in events
            ],
        }
        now_state.is_error = any(
            not event["result"].get("ok",False)
            for event in events
        ) or now_state.is_error

        if now_state.is_error and not now_state.error_message:
            failed_messages = [
                event["result"].get("message","")
                for event in events
                if not event["result"].get("ok",False)
            ]
            now_state.error_message = "\n".join(
                message for message in failed_messages if message
            )
        return

    now_state.tool = "executor_response"
    now_state.tool_input = input_content
    now_state.tool_results = {
        "status":"error" if now_state.is_error else "success",
        "message":now_state.executor_response,
        "error_message":now_state.error_message,
    }


def _bounded_executor_input(value):
    if value is None:
        return None

    content = json.dumps(value,ensure_ascii=False,default=str)
    if len(content) <= DESCRIPTION_MAX_CHARS:
        return value

    return {
        "truncated":True,
        "original_chars":len(content),
        "content_preview":bounded_text(
            content,
            DESCRIPTION_MAX_CHARS,
            keep_tail=True
        ),
    }
