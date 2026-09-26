from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from pydantic import BaseModel


from AgentLoop.loop_utils import (
    build_model_messages,
    call_chat_function,
    normalize_tool_calls,
    parse_json_object
)
from MCP_functions.tool_registry import AgentRole,ToolRegistry
from State.save_supervision_state_history import save_supervisor_state_history

if TYPE_CHECKING:
    from AgentLoop.agent import AgentState,SupervisorState,ExecutorState,Skill,SupervisorEvaluationState
from Context.History_Resorce.mcp_history_error import read_history_by_seq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_EVALUATION_PROMPT = """
现在给你输出的字段有一下信息：
1.target：当前阶段的总目标
2.executor_target：前一轮executor要执行的目标
3.executor_results：前一轮executor最终执行的结果（历程）
4.executor_passed_seqs：前一轮executor操作的轮次的编号集合
5.executor_is_error：前一轮executor执行问题过程中是否出现error
6.executor_error_message:前一轮executor执行问题过程中error的信息
7.skills代表你可以选择的skill列表
8.supervisor_history代表历史你的思考记忆


你需要:
1.根据executor_results的字段信息和executor_target判断executor是否完成安排给它的任务
2.再判断是否达到了target的总目标
3.再检查executor_is_error字段是不是false,若为false，代表executor执行问题过程中没有问题，如果1，2里面的条件都满足，评估结果为通过，
准备进行下一轮问题的规划,is_passed返回true字段
4.如果executor_is_error是false,1条件达到了但2没有达到，则需要你在is_passed返回false，并在reason字段上给出你得出此判断的理由。
1，2都没达到的话同理
5.executor_is_error是true的话，锁定executor_passed_seqs里面的内容，尤其检查最后一项。可以通过read_history_by_seq来搜索result_content和
tool_name，查看这一轮到底发生了什么导致出错。当哪一轮你没有返回这个字段的时候，我默认你已经找到问题所在了，我会自动进入下一个work环节，所以，
在没有找到问题之前，一定要记得返回字段并且调用read_history_by_seq这个工具。实在找不到问题的时候，可以调用memory_retrieval.md这个skill，
按照skill里面的任务流来帮助你解决问题
6.在你执行完上述的操作后，is_finished返回true，代表supervisor的evaluation结束了，否则返回false

注意：
每轮都需要返回"description"字段，用来总结这一回合你干了什么
返回json格式要求如下：
{
    "is_passed":"",
    "description":"",
    "is_error":false,
    "reason":"",
    "needed_check":{
        "seq_num":"",
        "chat_id":"",
    },
    "skill":{
        "skill_id":"",
        "skill_name":""
    },
    "is_finished":false
}   
"""

SUPERVISOR_DECISION_PROMPT = """
输入格式为:
{
    "session_address":"",
    "chat_id":"",
    "task_list":"",
    "now_task_id":"",
    "now_target":"",
    "executor_target" : "",
    "supervisor_evaluation_result":"",
    "executor_is_passed":"",
    "executor_not_pass_reason":"",
    "executor_is_error":"",
    "executor_error_message":"",
    "memory_window":""
}

其中
task_List:总的任务清单
task_id:进行到第几个任务了
now_target:当前的任务名称
executor_target:代表上一轮executor执行的任务目标，
supervisor_evaluation_result:supervisor对executor执行完任务结果后的反馈
executor_is_passed:supervisor:评估当前回合executor是否按照要求完成任务
executor_not_pass_reason:supervisor给出的他对executor为什么没有完成任务的说明总结
executor_is_error:supervisor检查在executor执行任务的过程里面有没有出问题
executor_error_message:supervisor对于executor执行任务报错的说明
memory_window:在前几轮轮回合中supervisor已经执行了哪些操作


1.如果executor_is_passed为true，代表executor任务执行完毕，然后你需要结合now_target和supervisor_evaluation_result给出判断，
如果全部完成，进入下一步，is_next_target字段返回true,结合task_id+1对应的任务，给出下一个步骤的指导。否则返回false,然后结合now_target，
给出下一步指令。此外，如果发现task_list需要修改，将is_task_list_need_change返回为true，并在new_task_list给出新的task_list
***不到万不得已不要修改task_list****
2.如果executor_is_error为true，先结合executor_error_message的字段信息和now_target，给出解决方案
3.如果executor_is_passed为false，你可以结合当前supervisor_evaluation_result，判断需不需要回溯，需要的话need_date_back返回true，
并给出返回的seq序列号和task_id号

返回格式要求如下:

"is_finished":代表本次结束，进入executor操作轮次,结束返回true，没结束返回false
"description":""字段代表对于这轮操作你给出的总结
date_back_input:通过工具read_history_by_seq查询date_back_location里面所有的内容最后写的input的总结，改从什么地方重新开始执行任务

{
    "is_next_target":"",
    "is_task_list_need_change":"",
    "new_task_list":"",
    "next_executor_target":"",
    "need_date_back":"",
    "description":"",
    "is_finished":""
    "date_back_location":[
            {"seq":"",
        "task_id":"",
        "chat_id":"",
        "session_address":""}
        ],
    "date_back_input":""
}
"""

class NeededCheck(BaseModel):
    seq_num:int | None =None
    chat_id:str| None =None

class SupervisorSkill(BaseModel):
    skill_id:int| None =None
    skill_name:str| None =None
class SupervisorEvaluation(BaseModel):
    is_passed: bool | None = False

    description: str | None = None
    is_error:bool | None = False
    reason: str | None = None
    needed_check: NeededCheck | None = None
    skill: SupervisorSkill | None = None
    is_finished: bool | None = False

class DateBack(BaseModel):
    seq:int
    task_id:int
    chat_id:int
    session_address:str

class SupervisorDecisionResult(BaseModel):

    is_task_list_need_change: bool | None = False
    new_task_list: list[str] | None = []
    is_next_target: bool | None = False
    next_executor_target:str | None = None
    need_date_back: bool | None = False
    description: str | None = None
    is_finished: bool | None = False
    date_back_location: DateBack | None = None
    date_back_input:str | None = None


async def supervisor_making_plan(
        query:str,
        now_state:AgentState,
        chat_function,
        client,
        tool_registry: ToolRegistry,
        supervisor:str,
        supervisor_tools:list[dict],
        supervisor_state:SupervisorState
):
    skill_content = _load_planning_skill()
    information = {}

    if query is not None:
        information["user_query"] = query
    information["session_address"] = now_state.session_address
    information["chat_id"] = now_state.chat_id
    planning_times = 0

    while planning_times < now_state.max_supervision_times:
        if len(supervisor_state.memory_window) > 0:
            information["have_been_finished"] = supervisor_state.memory_window

        message = build_model_messages(
            provider=supervisor,
            prompt=skill_content if skill_content is not None else _load_planning_prompt(),
            information=information
        )

        try:
            response =  call_chat_function(
                chat_function=chat_function,
                provider=supervisor,
                client=client,
                model = now_state.supervisor_model,
                messages=message,
                tools=supervisor_tools,
                temperature=now_state.temperature
            )

            if response.get("status") == "error":
                raise ValueError(response.get("message"))

            if len(response.get("tool") == 0):
                response_info = parse_json_object(response.get("message"))
                if "task_list" not in response_info:
                    raise ValueError("the info : task_list field not found in the message")

                task_list = response_info["task_list"]
                now_state.task_list = task_list
                now_state.supervisor_seq = planning_times + 1

                supervisor_state.output_content = task_list
                supervisor_state.executor_plan = now_state.task_list[0]
                save_supervisor_state_history(supervisor_state)
                return

            tool_calls = normalize_tool_calls(
                provider=supervisor,
                tool_calls = response.get("tool")
            )

            tool_call = tool_calls[0]

            tool_result = await tool_registry.call(
                role=AgentRole.SUPERVISOR,
                tool_name=tool_call.name,
                arguments=tool_call.arguments
            )

            message.append(
                {
                    "role":"user",
                    "content":f"tool_results:{tool_result}"
                }
            )

            try:

                second_response = call_chat_function(
                    chat_function=chat_function,
                    provider=supervisor,
                    client=client,
                    model = now_state.supervisor_model,
                    messages=message,
                    tools=supervisor_tools,
                    temperature=now_state.temperature
                )

                if second_response.get("status") == "error":
                    raise ValueError("there may occur some problems when model plan")

                second_response_info = parse_json_object(second_response.get("message"))
                supervisor_state.memory_window.append(
                    f"第{planning_times + 1}轮，已完成{second_response_info.get("description")}"
                )

                now_state.supervisor_seq += 1
                planning_times += 1
                save_supervisor_state_history(supervisor_state)
                if "task_list" in second_response_info and len(second_response_info["task_list"]) > 0:
                    now_state.task_list = second_response_info["task_list"]
                    supervisor_state.executor_plan = now_state.task_list[0]
                    supervisor_state.output_content = second_response_info["task_list"]
                    return

            except Exception as e:
                print("未得到模型回答，检查连接情况")
                raise e

        except RuntimeError as error:
            print("未得到模型回答，检查连接情况")
            raise error

        except ValueError as error:
            print("模型解析失败")
            raise error


#supervisor轮一共要干两件事情：1.评估executor这一轮干的活 2.判断下一回合的目标，task_id移动不移动

async def run_supervisor_loop(
        now_state: AgentState,
        supervisor: str,
        supervisor_client,
        chat_function,
        supervisor_tools: list[dict],
        tool_registry: ToolRegistry,
        skill_lists: list[Skill],
        supervisor_state: SupervisorState,
        supervisor_evaluation_state:SupervisorEvaluationState,
        executor_state: ExecutorState
):

    supervisor_run_times = 0

    supervisor_state.memory_window = []

    await evaluation_loop(
        now_state=now_state,
        supervisor=supervisor,
        supervisor_client=supervisor_client,
        chat_function=chat_function,
        supervisor_tools=supervisor_tools,
        tool_registry=tool_registry,
        skill_lists=skill_lists,
        supervisor_state=supervisor_state,
        supervisor_evaluation_state=supervisor_evaluation_state,
        executor_state=executor_state
    )

    await making_next_plan_loop(
        now_state = now_state,
        supervisor=supervisor,
        supervisor_client=supervisor_client,
        chat_function=chat_function,
        supervisor_tools=supervisor_tools,
        tool_registry=tool_registry,
        supervisor_state=supervisor_state,
        supervisor_evaluation_state=supervisor_evaluation_state,
        executor_state=executor_state
    )

    return

async def making_next_plan_loop(
        now_state: AgentState,
        supervisor: str,
        supervisor_client,
        chat_function,
        supervisor_tools: list[dict],
        tool_registry: ToolRegistry,
        supervisor_state: SupervisorState,
        supervisor_evaluation_state: SupervisorEvaluationState,
        executor_state: ExecutorState
):
    supervisor_evaluation_state.memory_window = []

    primary_message = _build_decision_message(
        now_state=now_state,
        supervisor_state=supervisor_state,
        supervisor_evaluation_state=supervisor_evaluation_state,
        executor_state=executor_state,
        supervisor=supervisor
    )

    output_content = ""
    supervisor_executing_times = 0
    while supervisor_executing_times < now_state.max_supervision_times:
        message = _build_decision_message(
            now_state=now_state,
            supervisor_state=supervisor_state,
            supervisor_evaluation_state=supervisor_evaluation_state,
            executor_state=executor_state,
            supervisor=supervisor
        )

        try:
            response = call_chat_function(
                chat_function=chat_function,
                provider=supervisor,
                client=supervisor_client,
                model = now_state.supervisor_model,
                messages=message,
                tools=supervisor_tools,
                temperature=now_state.temperature
            )

            if response.get("status") == "error":
                raise ValueError("there may occur some problems when model deals with queries")

            tools = response.get("tool", {})
            if tools == {}:

                content = SupervisorDecisionResult.model_validate(
                    parse_json_object(
                        response.get("message")
                    )
                )
                supervisor_state.supervisor_seq += 1
                supervisor_state.executor_plan = content.next_executor_target
                supervisor_state.need_date_back = content.need_date_back
                supervisor_state.date_back_input = content.date_back_input

                now_state.supervisor_seq = supervisor_state.supervisor_seq
                if content.is_task_list_need_change:
                    now_state.task_list = content.new_task_list

                if content.is_next_target:
                    now_state.now_task_id += 1
                    now_state.now_target = now_state.task_list[now_state.now_task_id]

                now_state.supervisor_input = primary_message
                now_state.supervisor_output = output_content

                return

            tool_calls = normalize_tool_calls(
                provider=supervisor,
                tool_calls=tools
            )

            tool_call = tool_calls[0]

            tool_result = await tool_registry.call(
                role=AgentRole.SUPERVISOR,
                tool_name=tool_call.name,
                arguments=tool_call.arguments
            )

            message.append(
                {
                    "role": "user",
                    "content": f"tool_results:{tool_result}"
                }
            )
            try:
                second_response = call_chat_function(
                    chat_function=chat_function,
                    provider=supervisor,
                    client=supervisor_client,
                    model = now_state.supervisor_model,
                    messages=message,
                    tools=[],
                    temperature=now_state.temperature
                )
                supervisor_executing_times += 1
                supervisor_state.supervisor_seq += 1

                if second_response.get("status") == "error":
                    raise ValueError("there may occur some problems when model deals with queries")

                response_content = parse_json_object(second_response.get("message"))
                content = SupervisorDecisionResult.model_validate(response_content)

                supervisor_state.memory_window.append(content.description)
                output_content += f"第{supervisor_executing_times}轮已经完成{content.description}"

                supervisor_state.output_content = output_content
                supervisor_state.tool_name = tool_call.name
                supervisor_state.tool_result = tool_result
                supervisor_state.input_content = message

                save_supervisor_state_history(supervisor_state)

                if content.is_finished:
                    supervisor_state.executor_plan = content.next_executor_target
                    supervisor_state.need_date_back = content.need_date_back
                    supervisor_state.date_back_input = content.date_back_input

                    now_state.supervisor_seq = supervisor_state.supervisor_seq
                    if content.is_task_list_need_change :
                        now_state.task_list = content.new_task_list

                    if content.is_next_target :
                        now_state.now_task_id +=1
                        now_state.now_target = now_state.task_list[now_state.now_task_id]


                    now_state.supervisor_input = primary_message
                    now_state.supervisor_output = output_content

                    return

            except Exception as e:
                raise e

        except Exception as e:
            raise e
def _build_decision_message(
        now_state:AgentState,
        supervisor_state: SupervisorState,
        supervisor_evaluation_state:SupervisorEvaluationState,
        executor_state:ExecutorState,
        supervisor:str
):
    task_id = now_state.now_task_id
    task_list = now_state.task_list

    executor_target = executor_state.input_content
    supervisor_evaluation_result = supervisor_evaluation_state.output_content
    supervisor_evaluation_executor_is_error = supervisor_evaluation_state.is_executor_error
    executor_is_passed = supervisor_evaluation_state.is_executor_passed
    executor_error_message = supervisor_evaluation_state.executor_error_message
    executor_not_pass_reason = supervisor_evaluation_state.not_pass_reason
    infos = {}

    infos["session_address"] = now_state.session_address
    infos["chat_id"] = now_state.chat_id
    infos["target_list"] = task_list
    infos["now_task_id"] = task_id
    infos["now_task"] = task_list[task_id]
    infos["executor_target"] = executor_target if executor_target else None
    infos["supervisor_evaluation_result"] = supervisor_evaluation_result if supervisor_evaluation_result else None
    infos["executor_is_passed"] = executor_is_passed if executor_is_passed else False
    infos["executor_not_pass_reason"] = executor_not_pass_reason if executor_not_pass_reason else None
    infos["executor_is_error"] = supervisor_evaluation_executor_is_error if executor_is_passed else False
    infos["executor_error_message"] = executor_error_message if executor_error_message else None
    infos["memory_window"] = supervisor_state.memory_window

    return build_model_messages(
        provider=supervisor,
        prompt=SUPERVISOR_DECISION_PROMPT,
        information=infos
    )



async def evaluation_loop(
        now_state: AgentState,
        supervisor: str,
        supervisor_client,
        chat_function,
        supervisor_tools: list[dict],
        tool_registry: ToolRegistry,
        skill_lists: list[Skill],
        supervisor_state: SupervisorState,
        supervisor_evaluation_state:SupervisorEvaluationState,
        executor_state: ExecutorState
):
    supervisor_results = None
    supervisor_run_times = 0

    output_content = ""
    supervisor_state.memory_window = []
    primary_message = _build_supervisor_evaluation_message(
            executor_state,
            now_state=now_state,
            skill_list=skill_lists,
            supervisor=supervisor,
            supervisor_evaluation_state=supervisor_evaluation_state
        )
    while (supervisor_run_times < now_state.max_supervision_times):
        message = _build_supervisor_evaluation_message(
            executor_state,
            now_state=now_state,
            skill_list=skill_lists,
            supervisor=supervisor,
            supervisor_evaluation_state=supervisor_evaluation_state
        )

        try:
            response =  call_chat_function(
                chat_function=chat_function,
                provider=supervisor,
                client=supervisor_client,
                model = now_state.supervisor_model,
                messages=message,
                tools=supervisor_tools,
                temperature=now_state.temperature
            )

            if response.get("status") == "error":
                raise ValueError("there may occur some problems when model deals with queries")

            tools = response.get("tool",{})
            if tools == {}:
                supervisor_state.supervisor_seq += 1

                _update_supervisor_evaluation_state(
                    supervisor_evaluation_state=supervisor_evaluation_state,
                    supervisor_state=supervisor_state,
                    supervisor_results=supervisor_results,
                    tool_name="",
                    tool_results="",
                    input_content=message,
                    output_content=output_content
                )
                _update_Agent_state(
                    now_state=now_state,
                    SupervisorState=supervisor_state,
                    message=primary_message
                )
                save_supervisor_state_history(supervisor_state)

                return
            tool_calls = normalize_tool_calls(
                provider=supervisor,
                tool_calls = tools
            )

            tool_call = tool_calls[0]

            tool_result = await tool_registry.call(
                role=AgentRole.SUPERVISOR,
                tool_name=tool_call.name,
                arguments=tool_call.arguments
            )

            message.append(
                {
                    "role":"user",
                    "content":f"tool_results:{tool_result}"
                }
            )

            try:
                second_response = call_chat_function(
                chat_function=chat_function,
                provider=supervisor,
                client=supervisor_client,
                model = now_state.supervisor_model,
                messages=message,
                tools=supervisor_tools,
                temperature=now_state.temperature
            )

                supervisor_run_times += 1
                supervisor_state.supervisor_seq += 1

                if second_response.get("status") == "error":
                    raise ValueError("there may occur some problems when model plan")

                second_response_info = parse_json_object(second_response.get("message"))
                try:

                    supervisor_results = SupervisorEvaluation.model_validate(second_response_info)
                    supervisor_evaluation_state.memory_window.append(
                        f"第{supervisor_run_times + 1}次，执行{supervisor_results.description}"
                    )

                    output_content += (
                        f"第{supervisor_run_times+1}轮"+
                        f"完成{supervisor_results.description}+\n\n"
                    )

                    _update_supervisor_evaluation_state(
                        supervisor_evaluation_state=supervisor_evaluation_state,
                        supervisor_state=supervisor_state,
                        supervisor_results=supervisor_results,
                        tool_name=tool_call.name,
                        tool_results=tool_result,
                        input_content=message,
                        output_content=output_content
                    )

                    save_supervisor_state_history(supervisor_state)

                    if supervisor_results.is_finished or supervisor_run_times == now_state.max_supervision_times-1:
                        _update_Agent_state(
                            now_state=now_state,
                            SupervisorState=supervisor_state,
                            message=primary_message
                        )

                        return
                except Exception as e:
                    raise e

            except Exception as e:
                raise e

        except RuntimeError as error:
            raise error
        except ValueError as error:
            raise error

def _load_planning_skill():
    skill_address = Path(r"D:\pycharmcode\coding_agent\Skills\memory_retrieval.md")

    if not skill_address.exists():
        return

    with open(skill_address, "r",encoding="utf-8") as skill_file:
        skill_content = skill_file.read()

    return skill_content


def _load_planning_prompt():
    return  """
你是 coding agent 的 supervisor，负责理解用户需求，结合历史上下文和 executor 的实际工具能力，制定可执行、可验收的任务计划。
在没有确定最终的task_list计划之前，都只输出[]，否则默认你已经完成task_list的决定
输入信息：
- user_query：当前用户需求。
- session_address、chat_id：当前会话位置和聊天编号。
- executor_tools：executor 当前已注册的工具名称、描述和参数。
- skill_info：可用 skill 的名称和简介。
- 当前计划与执行摘要：重新规划时提供。

工作流程：

1. 判断是否需要历史上下文
   - 先判断当前输入是否足以明确目标、对象、约束和预期结果。
   - 如果用户提到“继续”“之前的方案”“修改刚才的内容”等历史信息，且当前输入没有提供完整内容，调用 read_history_chat_resource(session_address)。
   - 该工具读取整个会话目录，必须筛选当前 chat_id 对应的记录，只提取与本次需求有关的信息。
   - 当前输入已经足够时，不重复读取历史。
   - 历史要求与当前用户明确要求冲突时，以当前要求为准。
   - 历史记录不足以确定关键需求时，不自行猜测，将缺失信息作为后续任务的前置条件。

2. 明确本次目标
   - 提取用户要完成的工作、涉及的项目或路径、必须遵守的约束以及预期交付物。
   - 区分已经完成的内容与本次需要完成的内容。
   - 不添加与用户目标无关的工作。

3. 核对 executor 的能力
   - 根据 executor_tools 判断哪些操作可以执行，以及需要哪些输入。
   - skill_info 只提供工作方法，不代表额外的工具权限。
   - 下面的工具说明仅用于理解能力；只有出现在当前 executor_tools 中的工具才能安排调用。
   - 不把 MCP 服务名称当作工具名称，不虚构工具、参数或执行结果。
   - 所需能力不可用时，明确阻塞原因和恢复执行的前置条件，不将其描述成当前可以直接执行的步骤。

4. 按依赖关系拆分任务
   - 先安排必要的信息收集，再安排实现、验证和结果交付。
   - 每个任务围绕一个明确目标，可以包含多次相关工具调用。
   - 不把每次工具调用都拆成独立任务，不固定任务数量。
   - 每个任务说明：执行什么、操作对象、必要时使用什么工具、达到什么条件算完成。
   - 工具的具体参数由 executor 根据实际情况填写，不编造未知路径或配置。

5. 检查并输出计划
   - 检查计划是否覆盖用户目标，任务之间的依赖是否合理。
   - 完成条件必须能够通过实际结果判断，不能仅写“完成修改”“确保正确”。
   - 验证范围应与用户目标相符；启动成功不能代替功能验证。
   - 重新规划时保留仍然有效的已完成工作，只调整受影响或尚未完成的任务。
   - 只制定计划，不声称计划中的操作已经执行。

executor 工具说明：

1. read_all_files_tool(home_address)
   读取指定目录下的文件信息和可解码的文本内容。
   当前实现传入单个文件时只返回文件信息，不返回正文。
   读取时选择必要的项目目录或子目录。

2. sort_files_by_suffix_tool(files)
   按后缀分组 read_all_files_tool 返回的完整结果。
   不会压缩或摘要文件内容。

3. sort_files_by_mother_tool(files)
   当前实现实际按后缀分组，不能用于判断父目录结构。

4. judge_spring_project_tool(sorted_files_by_suffix)
   根据分组后的配置和源码，静态判断是否为 Spring / Spring Boot 项目，并提取构建工具和 JDK 版本线索。
   不能证明项目可以构建或启动。

5. find_java_exe_tool(home_address)
   在 Windows 指定目录下查找并检查 java.exe、javac.exe。
   优先指定合理的搜索目录。

6. run_java_get_feedback(code_address, jdk_location)
   编译并运行不含 package 声明的独立 Java 源文件，返回执行反馈。
   不用于构建完整 Spring Boot 项目。

7. package_spring_boot_with_confirmation_tool(project_path, temp_path)
   使用 Maven 或 Gradle 打包 Spring Boot 项目，并将 JAR 复制到目标目录。
   当前实现使用交互式确认，与 stdio MCP 的输入存在冲突；接入方修复确认流程前，视为受阻能力。
   打包成功不代表测试已经通过。

8. check_spring_boot_startup_tool(java_path, jar_path, timeout)
   启动 JAR，在限定时间内根据日志检查启动情况。
   只能提供启动观察结果，不能代替接口或业务测试，也不用于持续运行服务。

9. write_in_tool(file_address, code)
   创建必要的父目录，并向目标文件追加文本。
   当前实现是追加写入，不能作为覆盖或替换已有代码的工具。

10. find_the_python_editor_tool(home_address)
    在 Windows 指定目录下查找并检查 Python 解释器。
    优先指定合理的搜索目录。

11. run_code_get_feedback_tool(editor_address, code_path)
    使用指定 Python 解释器运行脚本，返回输出、错误或超时信息。
    当前执行超时为 10 秒，不适合直接运行长期服务。

12. download_package_with_confirmation_tool(editor_address, package_name)
    使用指定 Python 解释器安装依赖。
    当前实现使用交互式确认，与 stdio MCP 的输入存在冲突；接入方修复确认流程前，视为受阻能力。

13. get_needed_info_tool(needed)
    当前实现仅返回传入的 needed，不会实际检索配置或补充信息。
    不得依赖它获取缺失信息。

外部 MCP 工具：
- GitHub、浏览器和 Docker 的具体能力，以 executor_tools 实际列出的工具及参数为准。
- 只有服务配置但没有已注册工具时，不能安排调用。
- supervisor 可以依据 executor 的能力制定计划，但不能因此获得 executor 的执行权限。

输出要求：
- 需要读取历史时，先发起工具调用，收到结果后再完成规划。
- 最终仅输出合法 JSON，不附加解释或 Markdown 代码块。
- 只包含 task_list 字段，值为非空字符串列表。
- 每个字符串是一项具体任务，按执行顺序排列。
- `description`每轮都需要带上，负责对于你这一轮执行的操作，以及为什么选择这个操作等等方面进行总结，不能失去重点，但最好精炼
输出格式：
{
  "task_list": [
    "任务描述，包含操作对象、必要的工具和完成条件",
    "下一项任务描述"
  ],
  "description":""
}
"""


def _build_supervisor_evaluation_message(
        executor_state:ExecutorState,
        now_state:AgentState,
        skill_list:list[Skill],
        supervisor:str,
        supervisor_evaluation_state:SupervisorEvaluationState,
):
    now_target = now_state.now_target
    executor_input = executor_state.input_content
    executor_output = executor_state.output_content

    executor_last_seqs = executor_state.passed_seq_list
    executor_is_error = executor_state.is_error
    executor_error_message = executor_state.error_message

    infos = {}

    infos["target"] = now_target
    infos["executor_target"] = executor_input
    infos["executor_results"] = executor_output
    infos["executor_passed_seqs"] = executor_last_seqs
    infos["executor_is_error"] = executor_is_error
    infos["executor_error_message"] = executor_error_message
    infos["skills"] = skill_list

    infos["supervisor_history"] = supervisor_evaluation_state.memory_window if len( supervisor_evaluation_state.memory_window) > 0 else None
    return build_model_messages(
        provider=supervisor,
        prompt=SUPERVISOR_EVALUATION_PROMPT,
        information=infos
    )

def _update_supervisor_evaluation_state(
        supervisor_evaluation_state:SupervisorEvaluationState,
        supervisor_state:SupervisorState,
        supervisor_results:SupervisorEvaluation,
        tool_name:str,
        tool_results:str,
        input_content:list[dict],
        output_content:str
):
    supervisor_evaluation_state.input_content = input_content
    supervisor_evaluation_state.is_executor_error = supervisor_results.is_error
    supervisor_evaluation_state.executor_error_message = supervisor_results.reason
    supervisor_evaluation_state.is_executor_passed = supervisor_results.is_passed
    supervisor_evaluation_state.not_pass_reason = supervisor_results.reason
    supervisor_evaluation_state.tool_name = tool_name
    supervisor_evaluation_state.tool_result = tool_results
    supervisor_evaluation_state.output_content = output_content


    supervisor_state.supervisor_seq = supervisor_evaluation_state.supervisor_seq
    supervisor_state.memory_window = supervisor_evaluation_state.memory_window
    supervisor_state.is_executor_error = supervisor_evaluation_state.is_executor_error
    supervisor_state.executor_error_message = supervisor_evaluation_state.executor_error_message
    supervisor_state.input_content = supervisor_evaluation_state.input_content
    supervisor_state.output_content = supervisor_evaluation_state.output_content


def _update_Agent_state(
        now_state:AgentState,
        SupervisorState:SupervisorState,
        message:list[dict]
):
    now_state.supervisor_input = message
    now_state.supervisor_output = SupervisorState.output_content
    now_state.supervisor_seq = now_state.supervisor_seq