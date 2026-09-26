from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass,field
from pathlib import Path
from typing import Any,Literal

from anthropic import Anthropic
from openai import OpenAI
from zai import ZhipuAiClient
from mcp import StdioServerParameters

from AgentChat.Chatgpt_chat import chatGpt_chat
from AgentChat.Claude_chat import claude_chat
from AgentChat.DeepSeek_chat import deepseek_chat
from AgentChat.GLM_chat import zhipu_chat
from AgentChat.Qwen_chat import qwen_chat
from AgentLoop.executor_loop import run_executor_loop
from AgentLoop.loop_utils import (
    USER_QUERY_MAX_CHARS,
    bounded_text,
    build_model_messages,
    call_chat_function,
    normalize_tool_calls,
    parse_json_object,
)

from MCP_functions.MCP_hosts import mcp_host
from MCP_functions.Search.mcp_search_server import get_remote_mcp_link
from MCP_functions.tool_registry import AgentRole,ToolRegistry
from State.save_chat_history import save_chat_history
from State.save_executor_state_history import save_state_history
from MCP_functions.tool_registry import AgentRole


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INFORMATION_PATH = PROJECT_ROOT / "INFORMATION.json"
PLAN_SKILL_PATH = PROJECT_ROOT / "Skills" / "executor_plan_skill.md"

@dataclass
class Skill:
    id:int
    name:str
    description:str
    address:str
    belong:AgentRole


@dataclass
class Checkpoint:
    checkpoint_id: str
    chat_id: str
    task_id: int
    executor_seq: int
    target: str
    user_query: str
    input_content: Any
    previous_description: str
    error_advice: str
    verification_requirement: str


@dataclass
class SupervisorState:

    supervisor_seq:int
    session_address:str

    chat_id:str
    task_id:int
    target:str
    input_content:str
    output_content:str

    memory_window:list[str]
    executor_plan:str

    is_executor_error:bool
    executor_error_message:str

    tool_name:str
    tool_result:str
    need_date_back:bool
    date_back_input:str
    modify_content:str

class SupervisorEvaluationState:
    supervisor_seq:int
    chat_id:str
    task_id:int
    target:str

    input_content:str
    memory_window:list[str]
    output_content:str
    is_executor_passed:bool
    not_pass_reason:str
    is_executor_error:bool
    executor_error_message:str

    tool_name:str
    tool_result:str

@dataclass
class ExecutorState:

    executor_seq:int
    chat_id:str
    task_id:int
    session_address:str
    passed_seq_list:list[int]

    now_target:str
    supervisor_guidance:str
    input_content:str
    tool_name: str
    output_content:str
    tool_result:str

    is_finished:bool
    is_error:bool
    error_message:str

    memory_window:list[str]


@dataclass
class AgentState:
    session_address: str
    chat_id: str
    user_query: str
    task_list: list[str]
    now_task_id: int
    now_target: str

    executor_seq: int
    supervisor_seq: int
    task_attempt: int
    executor_model: str
    supervisor_model: str
    temperature: float
    max_executor_steps: int
    max_supervision_times: int
    max_task_attempts: int
    java: dict[str,Any] | None
    python: dict[str,Any] | None

    supervisor_input: str
    supervisor_output: str
    supervisor_executing_times: int

    executor_input: str
    executor_output: str
    executor_executing_times: int

# 兼容仍从 agent.py 导入 state 的旧模块。
state = AgentState


with INFORMATION_PATH.open("r",encoding="utf-8") as f:
    infos = json.loads(f.read())

supervisor = (
    infos.get("supervisor")
    if (infos.get(infos.get("supervisor")) or {}).get("mode") == "on"
    else None
)
executor = (
    infos.get("executor")
    if (infos.get(infos.get("executor")) or {}).get("mode") == "on"
    else None
)
temperature = infos.get("temperature",0.3)

if executor is None:
    executor = supervisor
if supervisor is None:
    supervisor = executor
if executor is None or supervisor is None:
    raise ValueError("please set at least one model")

executor_api = (infos.get(executor) or {}).get("api") or None
executor_model = (infos.get(executor) or {}).get("model") or None
supervisor_api = (infos.get(supervisor) or {}).get("api") or None
supervisor_model = (infos.get(supervisor) or {}).get("model") or None

if executor_model is None:
    raise ValueError("please select the executor model name")
if supervisor_model is None:
    raise ValueError("please select the supervisor model name")


def state_init(session_address:str,chat_id:str):
    return AgentState(
        session_address=str(session_address),
        chat_id=str(chat_id),
        executor_model=executor_model,
        supervisor_model=supervisor_model,
        temperature=float(temperature),
        java=infos.get("java"),
        python=infos.get("python"),
    )


async def main(query:str,session_address:str,chat_id:str):






async def _register_mcp_clients(host):
    required_parameters = {
        "memory":StdioServerParameters(
            command=sys.executable,
            args=["-m","Context.mcp_resources"],
            cwd=str(PROJECT_ROOT),
        ),
        "supervisor_memory":StdioServerParameters(
            command=sys.executable,
            args=["-m","Context.mcp_supervisor_tools"],
            cwd=str(PROJECT_ROOT),
        ),
        "system":StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "MCP_functions.System_Files.Files_server.mcp_system_server",
            ],
            cwd=str(PROJECT_ROOT),
        ),
    }

    for client_name,parameters in required_parameters.items():
        await host.run(parameters,client_name)

    try:
        optional_parameters = get_remote_mcp_link()
    except Exception:
        optional_parameters = {}

    for client_name,parameters in optional_parameters.items():
        try:
            await host.run(parameters,client_name)
        except Exception:
            continue


#description和skill_name的对应逻辑
SKILL_GUIDE = {
    "memory_retrieval.md":"",

}

SKILL_BELONG = {
    "memory_retrieval.md":(AgentRole.SUPERVISOR),

}
def _load_skills(skills_route:str,agentRole:AgentRole):
    skills_route = Path(skills_route)
    if not skills_route.exists():
        raise FileExistsError(f"{skills_route} does not exist,please provide a valid path")

    if not skills_route.is_dir():
        raise NotADirectoryError(f"{skills_route} is not a directory")
    skill_gather = []
    start = 0
    for item in skills_route.iterdir():
        if item.is_file():
            if agentRole in SKILL_BELONG.get(item.name):
                skill = Skill(
                    id = start,
                    name = item.name,
                    description=SKILL_GUIDE.get(item.name),
                    address=item.root,
                    belong=agentRole,
                )
                start += 1
                skill_gather.append(skill)

    return skill_gather


def _build_model_client(provider,api_key):
    client_map = {
        "glm":ZhipuAiClient,
        "chatgpt":OpenAI,
        "deepseek":OpenAI,
        "qwen":OpenAI,
        "claude":Anthropic,
    }
    base_urls = {
        "deepseek":"https://api.deepseek.com",
        "qwen":"https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    client_type = client_map.get(provider)
    if client_type is None:
        raise ValueError(f"unsupported model provider: {provider}")

    arguments = {"api_key":api_key}
    if provider in base_urls:
        arguments["base_url"] = base_urls[provider]
    return client_type(**arguments)


def _close_model_client(client):
    close = getattr(client,"close",None)
    if callable(close):
        close()


def _chat_map():
    return {
        "glm":zhipu_chat,
        "chatgpt":chatGpt_chat,
        "qwen":qwen_chat,
        "claude":claude_chat,
        "deepseek":deepseek_chat,
    }
