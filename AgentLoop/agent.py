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
from AgentLoop.supervisor_loop import check_right_task,review_error_task
from MCP_functions.MCP_hosts import mcp_host
from MCP_functions.Search.mcp_search_server import get_remote_mcp_link
from MCP_functions.tool_registry import AgentRole,ToolRegistry
from State.save_chat_history import save_chat_history
from State.save_executor_state_history import save_state_history


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INFORMATION_PATH = PROJECT_ROOT / "INFORMATION.json"
PLAN_SKILL_PATH = PROJECT_ROOT / "Skills" / "executor_plan_skill.md"


@dataclass
class Checkpoint:
    checkpoint_id: str
    chat_id: str
    task_id: int
    seq: int
    target: str
    user_query: str
    input_content: Any
    previous_description: str
    error_advice: str
    verification_requirement: str


def create_checkpoint(state:"AgentState",history_record,checkpoint_id):
    return Checkpoint(
        checkpoint_id=checkpoint_id,
        chat_id=state.chat_id,
        task_id=history_record["task_id"],
        seq=history_record["seq"],
        target=history_record.get("target") or state.now_target,
        user_query=state.user_query,
        input_content=history_record.get("input_content"),
        previous_description=_history_previous_description(history_record),
        error_advice=state.error_review.executor_instruction,
        verification_requirement=(
            state.error_review.verification_requirement
        ),
    )


@dataclass
class CorrectnessReviewState:
    review_status: Literal[
        "pending",
        "passed",
        "failed",
        "insufficient_evidence",
        "blocked",
    ] = "pending"
    supervisor_check: bool = False
    supervisor_description: str = ""
    is_task_finished: bool = False
    needs_error_review: bool = False
    error_review_context: str = ""
    repairs: list[dict[str,int]] = field(default_factory=list)
    invalidates: list[dict[str,int]] = field(default_factory=list)


@dataclass
class ErrorReviewState:
    error_status: Literal[
        "pending",
        "confirmed",
        "unconfirmed",
        "blocked",
    ] = "pending"
    error_type: str = ""
    root_cause: str = ""
    supervisor_description: str = ""
    executor_instruction: str = ""
    verification_requirement: str = ""
    invalidates: list[dict[str,int]] = field(default_factory=list)
    backtrack_to: dict[str,int] | None = None
    backtrack_reason: str = ""
    is_blocked: bool = False
    block_reason: str = ""


@dataclass
class AgentState:
    session_address: str
    chat_id: str
    user_query: str = ""
    task_list: list[str] = field(default_factory=list)
    now_task_id: int = 0
    now_target: str = ""

    seq: int = 1
    supervisor_seq: int = 1
    task_attempt: int = 0
    executor_model: str = ""
    supervisor_model: str = ""
    temperature: float = 0.3
    max_executor_steps: int = 10
    max_supervision_times: int = 3
    max_task_attempts: int = 5
    phase: Literal[
        "planning",
        "executor",
        "correctness_review",
        "error_review",
        "finished",
        "blocked",
    ] = "planning"

    previous_description: str = ""
    finished_task_descriptions: list[str] = field(default_factory=list)
    executor_input: Any = None
    tool: Any = None
    tool_results: Any = None
    tool_input: Any = None
    executor_response: str = ""
    executor_tool_events: list[dict] = field(default_factory=list)
    is_error: bool = False
    error_message: str = ""

    correctness_review: CorrectnessReviewState = field(
        default_factory=CorrectnessReviewState
    )
    error_review: ErrorReviewState = field(default_factory=ErrorReviewState)

    java: dict[str,Any] | None = None
    python: dict[str,Any] | None = None

    @property
    def executor_seq(self) -> int:
        return self.seq

    @executor_seq.setter
    def executor_seq(self,value:int):
        self.seq = value

    @property
    def supervisor_check(self) -> bool:
        return self.correctness_review.supervisor_check

    @supervisor_check.setter
    def supervisor_check(self,value:bool):
        self.correctness_review.supervisor_check = bool(value)

    @property
    def supervisor_description(self) -> str:
        return self.correctness_review.supervisor_description

    @supervisor_description.setter
    def supervisor_description(self,value:str | None):
        self.correctness_review.supervisor_description = value or ""

    @property
    def supervisor_error_advice(self) -> str:
        return self.error_review.executor_instruction

    @supervisor_error_advice.setter
    def supervisor_error_advice(self,value:str | None):
        self.error_review.executor_instruction = value or ""

    @property
    def is_task_end(self) -> bool:
        return self.correctness_review.is_task_finished

    @is_task_end.setter
    def is_task_end(self,value:bool):
        self.correctness_review.is_task_finished = bool(value)

    @property
    def repaired(self) -> list[dict[str,int]]:
        return self.correctness_review.repairs

    @repaired.setter
    def repaired(self,value:list[dict[str,int]] | None):
        self.correctness_review.repairs = value or []

    @property
    def invalid(self) -> list[dict[str,int]]:
        combined = self.correctness_review.invalidates + self.error_review.invalidates
        unique = []
        for reference in combined:
            if reference not in unique:
                unique.append(reference)
        return unique

    @invalid.setter
    def invalid(self,value:list[dict[str,int]] | None):
        self.correctness_review.invalidates = value or []

    @property
    def backtrack_to(self) -> dict[str,int] | None:
        return self.error_review.backtrack_to

    @backtrack_to.setter
    def backtrack_to(self,value:dict[str,int] | None):
        self.error_review.backtrack_to = value

    @property
    def backtrack_reason(self) -> str:
        return self.error_review.backtrack_reason

    @backtrack_reason.setter
    def backtrack_reason(self,value:str | None):
        self.error_review.backtrack_reason = value or ""


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
    now_state = state_init(session_address,chat_id)
    now_state.user_query = query

    executor_client = _build_model_client(executor,executor_api)
    supervisor_client = _build_model_client(supervisor,supervisor_api)

    try:
        async with AsyncExitStack() as stack:
            host = mcp_host(stack)
            await _register_mcp_clients(host)

            tool_registry = ToolRegistry(host)
            executor_tools = tool_registry.schemas_for(
                AgentRole.EXECUTOR,
                executor
            )
            supervisor_tools = tool_registry.schemas_for(
                AgentRole.SUPERVISOR,
                supervisor
            )

            now_state.task_list = _build_task_plan(
                query=query,
                executor_client=executor_client,
                executor_tools=executor_tools
            )

            await run_agent_loop(
                now_state=now_state,
                executor_client=executor_client,
                supervisor_client=supervisor_client,
                executor_chat=_chat_map()[executor],
                supervisor_chat=_chat_map()[supervisor],
                executor_tools=executor_tools,
                supervisor_tools=supervisor_tools,
                tool_registry=tool_registry,
                executor_provider=executor,
                supervisor_provider=supervisor,
            )

    except Exception as e:
        now_state.phase = "blocked"
        now_state.error_message = str(e)
        _save_main_chat_history(now_state)
        raise

    finally:
        _close_model_client(executor_client)
        if supervisor_client is not executor_client:
            _close_model_client(supervisor_client)

    _save_main_chat_history(now_state)
    return now_state


def _save_main_chat_history(now_state):
    descriptions = []
    if now_state.previous_description:
        descriptions.append(now_state.previous_description)
    if now_state.error_message:
        descriptions.append("error_message: " + now_state.error_message)
    descriptions.append("phase: " + now_state.phase)

    save_chat_history(
        chat_id=now_state.chat_id,
        session_address=now_state.session_address,
        query_content=now_state.user_query,
        answer_content="\n\n".join(now_state.finished_task_descriptions),
        description="\n".join(descriptions),
        task_number=now_state.now_task_id,
        seq_number=now_state.seq,
    )


async def run_agent_loop(
        now_state:AgentState,
        executor_client,
        supervisor_client,
        executor_chat,
        supervisor_chat,
        executor_tools,
        supervisor_tools,
        tool_registry,
        executor_provider,
        supervisor_provider
):
    task_id = 0
    task_attempts = {}
    checkpoint = None

    while task_id < len(now_state.task_list):
        target = now_state.task_list[task_id]
        now_state.now_task_id = task_id
        now_state.now_target = target
        task_attempts[task_id] = task_attempts.get(task_id,0) + 1
        now_state.task_attempt = task_attempts[task_id]

        if now_state.task_attempt > now_state.max_task_attempts:
            now_state.phase = "blocked"
            now_state.previous_description = (
                f"task {task_id} exceeded max attempts: "
                f"{now_state.max_task_attempts}"
            )
            return now_state

        # Executor 内部完全结束后，才会进入 Supervisor。
        await run_executor_loop(
            now_state=now_state,
            executor=executor_provider,
            executor_client=executor_client,
            chat_function=executor_chat,
            executor_tools=executor_tools,
            tool_registry=tool_registry
        )
        checkpoint = None
        now_state.correctness_review = CorrectnessReviewState()
        now_state.error_review = ErrorReviewState()

        # 当前完整执行先落历史，Supervisor 再通过私有记忆工具按编号读取。
        save_state_history(now_state)

        # 正确性 Supervisor 内部完全结束后，才决定是否进入错误 Supervisor。
        await check_right_task(
            now_state=now_state,
            supervisor=supervisor_provider,
            supervisor_client=supervisor_client,
            chat_function=supervisor_chat,
            supervisor_tools=supervisor_tools,
            tool_registry=tool_registry
        )

        if (
                not now_state.correctness_review.supervisor_check
                and (
                    now_state.correctness_review.needs_error_review
                    or now_state.is_error
                )
                and now_state.correctness_review.review_status != "blocked"
        ):
            await review_error_task(
                now_state=now_state,
                supervisor=supervisor_provider,
                supervisor_client=supervisor_client,
                chat_function=supervisor_chat,
                supervisor_tools=supervisor_tools,
                tool_registry=tool_registry
            )

        _finalize_executor_history(now_state)
        next_description = _build_next_description(now_state)
        task_finished = (
            now_state.correctness_review.supervisor_check
            and now_state.correctness_review.is_task_finished
        )
        blocked = (
            now_state.correctness_review.review_status == "blocked"
            or now_state.error_review.is_blocked
        )
        backtrack_to = now_state.error_review.backtrack_to

        now_state.seq += 1

        if blocked:
            now_state.phase = "blocked"
            now_state.previous_description = next_description
            return now_state

        if task_finished:
            now_state.finished_task_descriptions.append(
                now_state.correctness_review.supervisor_description
            )

        if backtrack_to is not None:
            try:
                task_id,checkpoint,next_description = _resolve_backtrack(
                    now_state=now_state,
                    backtrack_to=backtrack_to,
                    next_description=next_description
                )
            except ValueError as e:
                now_state.phase = "blocked"
                now_state.error_review.is_blocked = True
                now_state.error_review.block_reason = str(e)
                now_state.previous_description = (
                    next_description + "\nbacktrack blocked: " + str(e)
                ).strip()
                return now_state

            now_state.executor_input = checkpoint.input_content
            now_state.previous_description = next_description
            continue

        now_state.previous_description = next_description

        if task_finished:
            task_id += 1

    now_state.phase = "finished"
    return now_state


def _resolve_backtrack(
        now_state,
        backtrack_to,
        next_description
):
    task_id = backtrack_to.get("task_id")
    seq = backtrack_to.get("seq")
    current_executor_seq = now_state.seq - 1

    if type(task_id) is not int or type(seq) is not int:
        raise ValueError("backtrack_to must contain integer task_id and seq")

    if task_id < 0 or task_id >= len(now_state.task_list):
        raise ValueError(f"backtrack task_id is outside current plan: {task_id}")

    if task_id > now_state.now_task_id:
        raise ValueError("backtrack_to cannot point to a future task")

    if seq >= current_executor_seq:
        raise ValueError("backtrack_to must point to an older executor seq")

    history_record = _read_executor_history_record(
        now_state=now_state,
        task_id=task_id,
        seq=seq
    )
    checkpoint = create_checkpoint(
        state=now_state,
        history_record=history_record,
        checkpoint_id=f"task-{task_id}-seq-{seq}"
    )

    descriptions = [
        (
            f"逻辑回溯至 task_id={task_id}, seq={seq}，"
            f"重新处理目标：{checkpoint.target}。"
        ),
        (
            "回溯只恢复任务调度位置和描述，不会撤销文件、进程、数据库或外部服务状态；"
            "执行前必须重新读取当前真实状态。"
        ),
    ]

    if now_state.error_review.backtrack_reason:
        descriptions.append(
            "backtrack_reason: " + now_state.error_review.backtrack_reason
        )

    if checkpoint.previous_description:
        descriptions.append(
            "restored_previous_description: "
            + checkpoint.previous_description
        )

    if checkpoint.error_advice:
        descriptions.append(
            "error_advice: " + checkpoint.error_advice
        )

    if checkpoint.verification_requirement:
        descriptions.append(
            "verification_requirement: "
            + checkpoint.verification_requirement
        )

    if next_description:
        descriptions.append(next_description)

    return task_id,checkpoint,"\n".join(descriptions)


def _read_executor_history_record(now_state,task_id,seq):
    history_path = Path(now_state.session_address) / "executor_history.jsonl"
    if not history_path.exists():
        raise ValueError("executor history does not exist for backtrack")

    matched_record = None
    with history_path.open("r",encoding="utf-8-sig") as history_file:
        for line in history_file:
            if not line.strip():
                continue
            record = json.loads(line)
            if (
                    record.get("chat_id") == now_state.chat_id
                    and record.get("task_id") == task_id
                    and record.get("seq") == seq
            ):
                matched_record = record

    if matched_record is None:
        raise ValueError(
            f"backtrack history does not exist: task_id={task_id}, seq={seq}"
        )

    return matched_record


def _history_previous_description(history_record):
    input_content = history_record.get("input_content")
    if isinstance(input_content,dict):
        description = input_content.get("previous_description")
        if isinstance(description,str):
            return description

    supervisor_state = history_record.get("supervisor") or {}
    description = supervisor_state.get("description_content","")
    return description if isinstance(description,str) else ""


def _build_task_plan(query,executor_client,executor_tools):
    with PLAN_SKILL_PATH.open("r",encoding="utf-8") as f:
        skill_content = f.read()

    runtime_policy = """

本轮只生成计划，不执行工具。tools 仅表示 executor 实际拥有的能力边界。
输入中的 previous_description 是上一模型轮次描述；首次规划时为空。
不要输出 tool call，只输出技能规定的 JSON。
"""
    messages = build_model_messages(
        provider=executor,
        prompt=skill_content + runtime_policy,
        information={
            "user_query":bounded_text(
                query,
                USER_QUERY_MAX_CHARS,
                keep_tail=True
            ),
            "previous_description":"",
        }
    )
    response = call_chat_function(
        chat_function=_chat_map()[executor],
        provider=executor,
        client=executor_client,
        model=executor_model,
        messages=messages,
        tools=executor_tools,
        temperature=temperature
    )

    if response.get("status") == "error":
        raise RuntimeError(response.get("message","planner model error"))

    tool_calls = normalize_tool_calls(
        provider=executor,
        tool_calls=response.get("tool",[])
    )
    if tool_calls:
        raise ValueError("planner must return task_list without calling tools")

    result = parse_json_object(response.get("message"))
    task_list = result.get("task_list")

    if (
            not isinstance(task_list,list)
            or not task_list
            or any(
                not isinstance(task,str) or not task.strip()
                for task in task_list
            )
    ):
        raise ValueError(
            "plan task_list must be a non-empty list of non-empty strings"
        )

    return [task.strip() for task in task_list]


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


def _finalize_executor_history(now_state):
    history_path = Path(now_state.session_address) / "executor_history.jsonl"
    if not history_path.exists():
        raise ValueError("executor history was not saved before supervisor review")

    lines = history_path.read_text(encoding="utf-8-sig").splitlines()
    target_index = None
    target_record = None

    for index in range(len(lines) - 1,-1,-1):
        if not lines[index].strip():
            continue
        record = json.loads(lines[index])
        if (
                record.get("chat_id") == now_state.chat_id
                and record.get("task_id") == now_state.now_task_id
                and record.get("seq") == now_state.seq
        ):
            target_index = index
            target_record = record
            break

    if target_index is None or target_record is None:
        raise ValueError("cannot find current executor history record")

    target_record["supervisor"] = {
        "description_content":(
            now_state.correctness_review.supervisor_description
        ),
        "is_task_finished":(
            now_state.correctness_review.is_task_finished
        ),
        "supervisor_judge_error":not (
            now_state.correctness_review.supervisor_check
        ),
        "supervisor_judge_message":(
            now_state.error_review.executor_instruction
        ),
    }
    target_record["is_solved"] = bool(
        now_state.correctness_review.supervisor_check
    )
    target_record["repairs"] = now_state.repaired
    target_record["invalidates"] = now_state.invalid
    target_record["review_status"] = (
        now_state.correctness_review.review_status
    )
    target_record["record_status"] = "reviewed"
    lines[target_index] = json.dumps(target_record,ensure_ascii=False)

    temp_path = history_path.with_name(history_path.name + ".updating")
    temp_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8"
    )
    temp_path.replace(history_path)


def _build_next_description(now_state):
    descriptions = []
    correctness = now_state.correctness_review
    error_review = now_state.error_review

    if correctness.supervisor_description:
        descriptions.append(correctness.supervisor_description)

    if error_review.supervisor_description:
        descriptions.append(error_review.supervisor_description)

    if error_review.executor_instruction:
        descriptions.append(
            "executor_instruction: " + error_review.executor_instruction
        )

    if error_review.verification_requirement:
        descriptions.append(
            "verification_requirement: "
            + error_review.verification_requirement
        )

    if error_review.block_reason:
        descriptions.append("block_reason: " + error_review.block_reason)

    return "\n".join(descriptions)


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
