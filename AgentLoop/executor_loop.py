from __future__ import annotations
from pathlib import Path

import json
from typing import TYPE_CHECKING
from  pydantic import BaseModel

from AgentLoop.loop_utils import (
    build_model_messages,
    call_chat_function,
    normalize_tool_calls,
    parse_json_object
)

from agent import Skill,AgentState,SupervisorState,ExecutorState
from MCP_functions.tool_registry import AgentRole,ToolRegistry

if TYPE_CHECKING:
    from AgentLoop.agent import AgentState


executor_prompt = """
你是 coding agent 的 executor，只负责完整执行当前 target，不重新规划整个任务。

每次模型请求都会提供 previous_description。第一轮可能为空；之后它是上一轮模型或 supervisor 留下的描述。跨轮判断只能依赖这个描述和当前输入，不能假设仍然拥有更早的对话上下文。

执行规则：
1. 每个内部步骤最多调用一个本轮 tools 中真实存在的工具，严格遵守名称、description 和参数 schema。
2.have_been_solved字段给你提供了你已经执行完的步骤，根据已经执行过的，严格按照supervisor_distributed_target字段的内容为你将要达到的目标，
target为总目标，进行下一个步骤的选择。selected_skill字段如果为空，代表当前状态下没有选择好skill，你可以结合情况判断选择哪个skill，不为空的话严格结合当前实际情况
判断skill有没有执行完，需不需要换skill等等。
2.如果传入字段中“have_answered_times”为0，需要你选择在传递给你的skill_List里面选择一个skill，然后按照这个skill的流程来回答问题，当然你也可以不选，返回skill的name和编号
3.输出五个字段，tool_name表示当前选择的tool工具名称，selected_skill代表你要选择的skill，也就是你工作的指导书，selected_skill
字段里还包括skill_name和传递给你的skill的id。description是结合给你的tool_result字段你给我提供的这个操作的总结。当然如果没有这个字段就返回空。
is_finished是需要根据你得到的tool_result和description，判断传入的supervisor_distributed_target字段的任务有没有被完成，
完成了返回true，没完成返回false
输出结果类型:
{
    "tool_name":"",
    "selected_skill":{
        "skill_name":"",
        "skill_id":""
    },
    "description":"",
    "is_finished":true
}

不得调用未提供的工具，不得伪造工具结果，不得自行宣布未验证的操作成功。
"""
class SelectedSkill(BaseModel):
    skill_name:str | None =None
    skill_id:int | None = None

class ExecutorOutPut(BaseModel):
    tool_name:str | None = None
    selected_skill:SelectedSkill | None = None
    description:str | None = None
    is_finished:bool | None = False


async def run_executor_loop(
        now_state:AgentState,
        executor:str,
        executor_client,
        chat_function,
        executor_tools:list[dict],
        tool_registry:ToolRegistry,
        skill_lists:list[Skill],
        supervisor_state:SupervisorState,
        executor_state:ExecutorState
):
    executing_times = 0

    output_content = ""
    skill = None

    #清空上轮记忆窗
    executor_state.memory_window = []
    while(executing_times < now_state.max_executor_steps):

        message = _build_executor_messages(
            time = executing_times,
            supervisor_state=supervisor_state,
            now_state=now_state,
            executor_state=executor_state,
            executor=executor,
            skill = skill
        )

        try:
            response =  call_chat_function(
                chat_function=chat_function,
                client=executor_client,
                model=now_state.executor_model,
                provider=executor,
                messages=message,
                tools=executor_tools,
                temperature=now_state.temperature
            )

            if response.get("status") == "error":
                raise ValueError("there may be some problems when model dealing with tasks")

            tools = response.get("tools",{})

            #没工具直接结束
            if not tools or tools == {}:

                executor_input = (
                        f"supervisor安排的任务:{executor_state.supervisor_guidance}" +
                        f"\n\n当前目标task:{executor_state.now_target}"
                )
                _update_now_state(now_state, executor_state, executor_input, output_content)

                return

            tool_calls = normalize_tool_calls(
                provider=executor,
                tool_calls = tools
            )

            tool_call = tool_calls[0]

            tool_result = tool_registry.call(
                role=AgentRole.EXECUTOR,
                tool_name=tool_call.name,
                arguments=tool_call.arguments
            )

            message.append(
                {
                    "role":"user",
                    "content":f"tool_result:{tool_result}"
                }
            )
            first_information = parse_json_object(response.get(message))
            executor_output = ExecutorOutPut.model_validate(first_information)

            selected_skill = executor_output.selected_skill.skill_name
            selected_skill_id = executor_output.selected_skill.skill_id

            #加载skill
            _load_skill_content(
                skill_list=skill_lists,
                skill_name=selected_skill,
                skill_id=selected_skill_id,
                message=message,
                executor=executor
            )

            #第二次不传工具
            response = await call_chat_function(
                chat_function=chat_function,
                client=executor_client,
                model=now_state.executor_model,
                provider=executor,
                messages=message,
                tools=[],
                temperature=now_state.temperature
            )

            information = parse_json_object(response.get("message"))

            if response.get("status") == "success":
                output_content += (
                        f"\n\n第{executing_times + 1}次执行任务，完成了" +
                        f"{information.get("description")}"
                )

            else:
                output_content += (
                    f"\n\n第{executing_times+1}次执行任务失败了，失败原因:"
                    f"{information.get("message")}"
                )

            executor_state.memory_window.append(f"第{executing_times + 1}轮已完成:{information.get("description")}")

            executing_times +=1
            executor_state.executor_seq += 1
            executor_state.passed_seq_list.append(executor_state.executor_seq)

            _update_executor_state(
                executor_state,
                is_finished=information.get("is_finished").lower() == "true",
                output_content=output_content,
                is_error=response.get("status") == "error",
                error_message=response.get("message") if "message" in response else None,
                input_content=json.dumps(information)
            )

            if (
                    information.get("is_finished").lower() == "true" or
                    response.get("state")=="error" or
                    executing_times ==  now_state.max_executor_steps - 1
            ):
                executor_input = (
                    f"supervisor安排的任务:{executor_state.supervisor_guidance}"+
                    f"\n\n当前目标task:{executor_state.now_target}"
                )
                _update_now_state(now_state,executor_state,executor_input,output_content)

                return

        except Exception as e:
            executor_input = (
                    f"supervisor安排的任务:{executor_state.supervisor_guidance}" +
                    f"\n\n当前目标task:{executor_state.now_target}"
            )

            _update_now_state(now_state, executor_state, executor_input, output_content)



def _build_executor_messages(
        time:int,
        supervisor_state:SupervisorState,
        now_state:AgentState,
        executor_state:ExecutorState,
        executor:str,
        skill:Skill=None
):
    now_target = now_state.now_target
    supervisor_distributed_target = supervisor_state.executor_plan

    queries = {
        "have_answered_times":time,
        "target":now_target,
        "supervisor_distributed_target":supervisor_distributed_target,
    }


    if supervisor_state.need_date_back and supervisor_state.date_back_input:
        queries["date_back"] = (
                "你应该回答这个问题:" +
                f"\n{supervisor_state.date_back_input}"+
                f"\n\n修改意见:"+
                f"\n{supervisor_state.date_back_input}"
        )

    if skill is not None:
        queries["selected_skill"] = (
            f"已选择的skill:{skill.name}"+
            f"\n\n描述:{skill.description}"
        )

    if len(executor_state.memory_window) != 0 and time > 0:
        queries["have_been_solved"] = executor_state.memory_window

    message = build_model_messages(
        executor,
        prompt=executor_prompt,
        information=queries
    )

    return message


def _update_executor_state(

        executor_state:ExecutorState,
        is_finished:bool,
        output_content,
        is_error:bool,
        error_message:str,
        input_content:str,

):

    executor_state.is_finished = is_finished
    executor_state.is_error = is_error
    executor_state.error_message = error_message
    executor_state.is_finished = False
    executor_state.output_content = output_content
    executor_state.input_content = input_content

def _update_now_state(
        now_state:AgentState,
        executor_state:ExecutorState,
        executor_input_content:str,
        executor_output:str
):
    now_state.executor_seq = executor_state.executor_seq
    now_state.executor_input = executor_input_content
    now_state.executor_output = executor_output

def _load_skill_content(
        skill_list:list[Skill],
        skill_name:str,
        skill_id:int,
        message:list[dict],
        executor:str
):
    if skill_list[skill_id] != skill_name:
        return

    selected_skill = skill_list[skill_id]
    address = selected_skill.address

    address = Path(address)
    if not address.exists():
        return

    with open(address,"r",encoding="utf-8") as f:
        skill_content = f.read()

    if executor == "claude":
        message.append(
            {
                "role":"user",
                "content":f"skill:{skill_content}"
            }
        )

    else:
        message.append(
            {
                "role":"system",
                "content":f"skill:{skill_content}"
            }
        )
        return selected_skill