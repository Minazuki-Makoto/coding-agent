from __future__ import annotations
from terminal_render import process

import inspect
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from mcp import StdioServerParameters
from openai import OpenAI
from zai import ZhipuAiClient

from AgentChat.Chatgpt_chat import chatGpt_chat
from AgentChat.Claude_chat import claude_chat
from AgentChat.DeepSeek_chat import deepseek_chat
from AgentChat.GLM_chat import zhipu_chat
from AgentChat.Qwen_chat import qwen_chat
from AgentLoop.executor_loop import run_executor_loop
from AgentLoop.supervisor_loop import (
    run_supervisor_loop,
    summarize_supervisor_descriptions,
    supervisor_making_plan,
)
from MCP_functions.MCP_hosts import mcp_host
from MCP_functions.Search.mcp_search_server import get_remote_mcp_link
from MCP_functions.tool_registry import AgentRole, ToolRegistry
from State.save_chat_history import save_chat_history
from State.save_supervision_state_history import load_last_supervisor_seq
from State.save_tool_result import (
    ToolResultLocator,
    ToolSummary,
    load_last_tool_result_seq,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INFORMATION_PATH = PROJECT_ROOT / "INFORMATION.json"
SKILLS_PATH = PROJECT_ROOT / "Skills"


@dataclass
class Skill:
    id: int
    name: str
    description: str
    address: str
    belong: AgentRole


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
class EvidenceReference:
    tool_result_seq: int
    tool_name: str
    root: str | None = None
    address: str | None = None
    status: str = ""
    paths: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "tool_result_seq": self.tool_result_seq,
            "tool_name": self.tool_name,
            "root": self.root,
            "address": self.address,
            "status": self.status,
            "paths": list(self.paths),
        }


@dataclass
class TaskOutcome:
    task_id: int
    target: str
    accepted_summary: str
    verification_summary: str
    evidence_references: list[EvidenceReference] = field(default_factory=list)
    reusable_read_calls: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "target": self.target,
            "accepted_summary": self.accepted_summary,
            "verification_summary": self.verification_summary,
            "evidence_references": [item.to_dict() for item in self.evidence_references],
            "reusable_read_calls": list(self.reusable_read_calls),
        }


@dataclass
class SupervisorHandoff:
    supervisor_seq: int
    task_id: int
    target: str
    instruction: str
    previous_work: list[str] = field(default_factory=list)
    accepted_work: list[str] = field(default_factory=list)
    remaining_work: list[str] = field(default_factory=list)
    completion_criteria: str = ""
    evidence_locators: list[dict[str, int]] = field(default_factory=list)
    completed_tool_calls: list[str] = field(default_factory=list)
    dependency_context: list[TaskOutcome] = field(default_factory=list)
    execution_mode: str = "execute"

    def to_dict(self):
        return {
            "supervisor_seq": self.supervisor_seq,
            "task_id": self.task_id,
            "target": self.target,
            "instruction": self.instruction,
            "previous_work": list(self.previous_work),
            "accepted_work": list(self.accepted_work),
            "remaining_work": list(self.remaining_work),
            "completion_criteria": self.completion_criteria,
            "evidence_locators": list(self.evidence_locators),
            "completed_tool_calls": list(self.completed_tool_calls),
            "dependency_context": [item.to_dict() for item in self.dependency_context],
            "execution_mode": self.execution_mode,
        }


@dataclass(frozen=True)
class SupervisorDescriptionEntry:
    supervisor_seq: int
    task_id: int
    target: str
    phase: str
    description: str

    def to_dict(self):
        return {
            "supervisor_seq": self.supervisor_seq,
            "task_id": self.task_id,
            "target": self.target,
            "phase": self.phase,
            "description": self.description,
        }


@dataclass
class SupervisorDescriptionHistory:
    entries: list[SupervisorDescriptionEntry] = field(default_factory=list)

    def append(self, entry: SupervisorDescriptionEntry):
        if entry.description.strip():
            self.entries.append(entry)

    def append_turn(
        self,
        supervisor_seq: int,
        task_id: int,
        target: str,
        phase: str,
        description: str,
    ):
        self.append(
            SupervisorDescriptionEntry(
                supervisor_seq=supervisor_seq,
                task_id=task_id,
                target=target,
                phase=phase,
                description=description,
            )
        )

    def to_dicts(self):
        return [entry.to_dict() for entry in self.entries]

    def model_context(self, max_chars: int | None = None):
        selected = []
        used = 0
        for entry in reversed(self.entries):
            item = entry.to_dict()
            size = len(json.dumps(item, ensure_ascii=False))
            if max_chars is not None and selected and used + size > max_chars:
                break
            selected.append(item)
            used += size
        selected.reverse()
        return {
            "entries": selected,
            "omitted_earlier_entries": len(self.entries) - len(selected),
        }

    def fallback_summary(self):
        if not self.entries:
            return ""
        return "；".join(
            f"{entry.phase}: {entry.description}"
            for entry in self.entries[-2:]
        )


@dataclass
class SupervisorState:
    supervisor_seq: int = 0
    session_address: str = ""
    chat_id: str = ""
    task_id: int = 0
    target: str = ""
    input_content: Any = ""
    output_content: Any = ""
    description: str = ""
    memory_window: list[str] = field(default_factory=list)
    executor_plan: str = ""
    is_executor_passed: bool | None = None
    is_executor_error: bool = False
    executor_error_message: str = ""
    reviewed_executor_seqs: list[int] = field(default_factory=list)
    tool_name: str = ""
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    last_tool_result_ref: ToolResultLocator | None = None
    tool_result_refs: list[ToolResultLocator] = field(default_factory=list)
    tool_summaries: list[ToolSummary] = field(default_factory=list)
    referenced_tool_result_seqs: list[int] = field(default_factory=list)
    tool_ok: bool | None = None
    need_date_back: bool = False
    date_back_locations: list[dict[str, Any]] = field(default_factory=list)
    date_back_input: str = ""
    modify_content: str = ""
    verification_requirement: str = ""
    is_next_target: bool = False
    proposed_task_list: list[str] | None = None
    decision_finished: bool = False
    final_answer: str = ""
    exit_reason: str = ""
    task_attempt: int = 0
    model_stage: str = ""
    raw_model_response_excerpt: str = ""
    validation_error: str = ""
    handoff: SupervisorHandoff | None = None
    original_completion_criteria: str = ""
    accepted_work: list[str] = field(default_factory=list)
    remaining_work: list[str] = field(default_factory=list)
    tool_event_seq: int | None = None
    evaluation_seq: int | None = None
    task_summary: dict[str, Any] | None = None
    task_outcome: dict[str, Any] | None = None


@dataclass
class SupervisorEvaluationState:
    supervisor_seq: int = 0
    chat_id: str = ""
    task_id: int = 0
    target: str = ""
    input_content: Any = ""
    memory_window: list[str] = field(default_factory=list)
    output_content: str = ""
    description: str = ""
    is_executor_passed: bool = False
    not_pass_reason: str = ""
    is_executor_error: bool = False
    executor_error_message: str = ""
    tool_name: str = ""
    last_tool_result_ref: ToolResultLocator | None = None
    tool_result_refs: list[ToolResultLocator] = field(default_factory=list)
    tool_summaries: list[ToolSummary] = field(default_factory=list)
    is_finished: bool = False
    blocked: bool = False


@dataclass
class ExecutorState:
    executor_seq: int = 0
    chat_id: str = ""
    task_id: int = 0
    session_address: str = ""
    tool_event_seqs: list[int] = field(default_factory=list)
    executor_turn_seq: int | None = None
    tool_summaries: list[ToolSummary] = field(default_factory=list)
    completed_tool_calls: list[str] = field(default_factory=list)
    now_target: str = ""
    supervisor_guidance: str = ""
    input_content: Any = ""
    tool_name: str = ""
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    output_content: str = ""
    description: str = ""
    last_tool_result_ref: ToolResultLocator | None = None
    tool_result_refs: list[ToolResultLocator] = field(default_factory=list)
    tool_ok: bool | None = None
    is_finished: bool = False
    is_error: bool = False
    error_message: str = ""
    memory_window: list[str] = field(default_factory=list)
    selected_skill: Skill | None = None
    exit_reason: str = ""
    task_attempt: int = 0
    model_stage: str = ""
    raw_model_response_excerpt: str = ""
    validation_error: str = ""
    latest_tool_context: LatestToolContext | None = None


@dataclass
class LatestToolContext:
    tool_name: str = ""
    tool_ok: bool | None = None
    tool_result_seq: int | None = None
    root: str | None = None
    address: str | None = None
    paths: list[str] = field(default_factory=list)
    status: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    pagination: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentState:
    session_address: str
    chat_id: str
    user_query: str = ""
    task_list: list[str] = field(default_factory=list)
    now_task_id: int = 0
    now_target: str = ""
    executor_seq: int = 0
    supervisor_seq: int = 0
    task_attempt: int = 0
    executor_model: str = ""
    supervisor_model: str = ""
    temperature: float = 0.3
    max_executor_steps: int = 10
    project_root: str | None = None
    staging_root: str | None = None
    workspace_revision: int = 0
    workspace_hash: str | None = None
    max_supervision_times: int = 10
    max_task_attempts: int = 3
    max_model_requests: int = 80
    model_request_count: int = 0
    java: dict[str, Any] | None = None
    python: dict[str, Any] | None = None
    supervisor_input: Any = ""
    supervisor_output: Any = ""
    supervisor_executing_times: int = 0
    executor_input: Any = ""
    executor_output: Any = ""
    executor_executing_times: int = 0
    supervisor_descriptions: SupervisorDescriptionHistory = field(
        default_factory=SupervisorDescriptionHistory
    )
    total_token: int = 0
    tool_result_seq: int = 0
    task_completion_criteria: dict[int, str] = field(default_factory=dict)
    task_handoffs: dict[int, SupervisorHandoff] = field(default_factory=dict)
    task_previous_work: dict[int, list[str]] = field(default_factory=dict)
    task_accepted_work: dict[int, list[str]] = field(default_factory=dict)
    task_remaining_work: dict[int, list[str]] = field(default_factory=dict)
    task_evidence_locators: dict[int, list[dict[str, int]]] = field(default_factory=dict)
    task_completed_tool_calls: dict[int, list[str]] = field(default_factory=dict)
    task_evidence_references: dict[int, list[EvidenceReference]] = field(
        default_factory=dict
    )
    task_reusable_read_calls: dict[int, list[str]] = field(default_factory=dict)
    task_outcomes: dict[int, TaskOutcome] = field(default_factory=dict)
    task_cumulative_summaries: dict[int, str] = field(default_factory=dict)
    task_summary_saved_seqs: set[int] = field(default_factory=set)
    branch_id: str = "main"
    request_id: str = ""
    context_trigger_chars: int = 60000
    context_summary_max_chars: int = 6000
    context_max_chars: int = 80000
    compression_cache: dict = field(default_factory=dict)
    conversation_context: list[dict] = field(default_factory=list)
    request_start_task_id: int = 0

state = AgentState


@dataclass(frozen=True)
class RuntimeConfig:
    supervisor: str
    executor: str
    supervisor_api: str | None
    executor_api: str | None
    supervisor_model: str
    executor_model: str
    temperature: float
    java: dict[str, Any] | None
    python: dict[str, Any] | None
    writable_roots: tuple[str, ...] = ()
    tool_capabilities: dict[str, list[str]] = field(default_factory=dict)


def load_runtime_config(path: Path = INFORMATION_PATH) -> RuntimeConfig:
    with path.open("r", encoding="utf-8") as config_file:
        infos = json.load(config_file)
    supervisor = infos.get("supervisor")
    executor = infos.get("executor")
    if supervisor and (infos.get(supervisor) or {}).get("mode") != "on":
        supervisor = None
    if executor and (infos.get(executor) or {}).get("mode") != "on":
        executor = None
    executor = executor or supervisor
    supervisor = supervisor or executor
    if not executor or not supervisor:
        raise ValueError("please enable at least one model provider")
    executor_model = (infos.get(executor) or {}).get("model")
    supervisor_model = (infos.get(supervisor) or {}).get("model")
    if not executor_model or not supervisor_model:
        raise ValueError("please select both executor and supervisor model names")
    return RuntimeConfig(
        supervisor=supervisor,
        executor=executor,
        supervisor_api=(infos.get(supervisor) or {}).get("api") or None,
        executor_api=(infos.get(executor) or {}).get("api") or None,
        supervisor_model=supervisor_model,
        executor_model=executor_model,
        temperature=float(infos.get("temperature", 0.3)),
        java=infos.get("java"),
        python=infos.get("python"),
        writable_roots=tuple(
            str(item) for item in (infos.get("writable_roots") or []) if item
        ),
        tool_capabilities=infos.get("tool_capabilities") or {},
    )


def state_init(session_address, chat_id, query="", config=None):
    config = config or load_runtime_config()
    result = AgentState(
        session_address=str(session_address),
        chat_id=str(chat_id),
        user_query=query,
        supervisor_seq=load_last_supervisor_seq(session_address, chat_id),
        executor_model=config.executor_model,
        supervisor_model=config.supervisor_model,
        temperature=config.temperature,
        java=config.java,
        python=config.python,
    )
    from State.session_checkpoint import rows
    result.executor_seq = max((row.get("seq", 0) for row in rows(Path(session_address) / "executor_history.jsonl")
                               if row.get("chat_id") == str(chat_id)), default=0)
    return result


async def _run_main(
    query: str,
    session_address: str,
    chat_id: str,
    started_at: datetime,
    *, session_run=None, runtime_config=None, sandbox_policy=None, approval=None, project_workflow=None,
):
    config = runtime_config or load_runtime_config()
    now_state = state_init(session_address, chat_id, query, config)
    now_state.tool_result_seq = load_last_tool_result_seq(
        now_state.session_address, now_state.chat_id
    )
    supervisor_state = SupervisorState(
        session_address=now_state.session_address, chat_id=now_state.chat_id
    )
    evaluation_state = SupervisorEvaluationState(chat_id=now_state.chat_id)
    executor_state = ExecutorState(
        session_address=now_state.session_address, chat_id=now_state.chat_id
    )
    from State.session_checkpoint import ACTIVE_RUN, boundary
    if session_run:
        if session_run.bundle:
            now_state = session_run.bundle["agent"]
            supervisor_state = session_run.bundle["supervisor"]
            executor_state = session_run.bundle["executor"]
            evaluation_state = session_run.bundle["evaluation"]
        else:
            session_run.bundle = {"agent": now_state, "supervisor": supervisor_state,
                                  "executor": executor_state, "evaluation": evaluation_state}
        now_state.branch_id = session_run.branch
        ACTIVE_RUN.set(session_run)
    now_state.tool_result_seq = max(now_state.tool_result_seq,
        load_last_tool_result_seq(now_state.session_address, now_state.chat_id))
    from MCP_functions.sandbox import SandboxPolicy
    from MCP_functions.execution_gateway import ExecutionGateway
    # No implicit cwd/project grant. Legacy callers can supply a trusted policy;
    # the terminal always supplies the runtime-authorization workflow.
    gateway = ExecutionGateway(sandbox_policy, approve=approval, capabilities=config.tool_capabilities,
                               project_workflow=project_workflow)
    supervisor_client = None
    executor_client = None
    status = "blocked"
    answer = ""
    block_reason = ""
    chat_description = ""

    async with AsyncExitStack() as stack:
        host = mcp_host(stack)
        await _register_mcp_clients(host, () if project_workflow else config.writable_roots, sandbox_policy=sandbox_policy,
                                    allow_external=bool(config.tool_capabilities) and not project_workflow)
        registry = ToolRegistry(
            host,
            execution_gateway=gateway,
            context_provider=lambda: {
                "session_address": now_state.session_address,
                "chat_id": now_state.chat_id,
                "task_id": now_state.now_task_id,
            },
        )
        supervisor_tools = registry.schemas_for(AgentRole.SUPERVISOR, config.supervisor)
        executor_tools = registry.schemas_for(AgentRole.EXECUTOR, config.executor)
        supervisor_skills = _load_skills(SKILLS_PATH, AgentRole.SUPERVISOR)
        executor_skills = _load_skills(SKILLS_PATH, AgentRole.EXECUTOR)
        supervisor_client = _build_model_client(config.supervisor, config.supervisor_api)
        executor_client = _build_model_client(config.executor, config.executor_api)
        chat_functions = _chat_map()

        try:
            if session_run and session_run.resume and session_run.phase == "planning_tool_summary":
                from AgentLoop.supervisor_loop import resume_supervisor_tool_summary
                await resume_supervisor_tool_summary(now_state, supervisor_state, evaluation_state, executor_state,
                    config.supervisor, supervisor_client, chat_functions[config.supervisor])
            elif session_run and session_run.resume and session_run.phase == "initial_guidance":
                from AgentLoop.supervisor_loop import _prepare_initial_guidance
                await _prepare_initial_guidance(now_state, supervisor_state, chat_functions[config.supervisor],
                                                 supervisor_client, config.supervisor)
                session_run.resume = False
            elif not (session_run and session_run.resume and now_state.task_list):
                boundary("planning", "supervisor")
                await supervisor_making_plan(
                query,
                now_state,
                chat_functions[config.supervisor],
                supervisor_client,
                registry,
                config.supervisor,
                supervisor_tools,
                supervisor_state,
                executor_tools=executor_tools,
                skill_lists=supervisor_skills,
            )

            process(f"""
            \n任务列表清单：{now_state.task_list}
            """)

            while now_state.now_task_id < len(now_state.task_list):
                phase = session_run.phase if session_run and session_run.resume else "executor_start"
                if phase in {"executor_start", "task_start"} and now_state.task_attempt >= now_state.max_task_attempts:
                    block_reason = (
                        f"task {now_state.now_task_id} exceeded "
                        f"{now_state.max_task_attempts} attempts"
                    )
                    break
                if phase not in {"evaluation", "decision", "decision_tool_summary", "task_advance"}:
                    if phase not in {"executor_action", "executor_tool_summary"}:
                        boundary("task_start", "host")
                        now_state.task_attempt += 1
                    await run_executor_loop(
                    now_state,
                    config.executor,
                    executor_client,
                    chat_functions[config.executor],
                    executor_tools,
                    registry,
                    executor_skills,
                    supervisor_state,
                    executor_state,
                )
                    if project_workflow and project_workflow.denied:
                        block_reason = "authorization_denied: 用户拒绝授权，当前请求已停止，请重新说明需求"
                        break
                    boundary("evaluation", "supervisor")
                    if executor_state.exit_reason == "executor_operation_unknown":
                        block_reason = "unknown operation: inspect real effects before selecting a new boundary"
                        break
                if phase == "task_advance":
                    from AgentLoop.supervisor_loop import SupervisorDecisionResult
                    decision = SupervisorDecisionResult.model_validate(session_run.payload["decision"])
                else:
                    decision = await run_supervisor_loop(
                    now_state,
                    config.supervisor,
                    supervisor_client,
                    chat_functions[config.supervisor],
                    supervisor_tools,
                    registry,
                    supervisor_skills,
                    supervisor_state,
                    evaluation_state,
                    executor_state,
                )
                if session_run:
                    # A restored decision that rejects must return to Executor,
                    # not keep re-entering the restored decision phase forever.
                    session_run.resume = False
                if supervisor_state.proposed_task_list is not None:
                    now_state.task_list = list(supervisor_state.proposed_task_list)
                if decision.is_next_target:
                    boundary("task_advance", "host", decision=decision.model_dump())
                    now_state.now_task_id += 1
                    now_state.task_attempt = 0
                    if now_state.now_task_id >= len(now_state.task_list):
                        status = "completed"
                        answer = (
                            decision.final_answer
                            or executor_state.output_content
                            or decision.description
                        )
                        break
                    now_state.now_target = now_state.task_list[now_state.now_task_id]
                    supervisor_state.task_id = now_state.now_task_id
                    supervisor_state.target = now_state.now_target
                    if not supervisor_state.executor_plan:
                        supervisor_state.executor_plan = (
                            f"执行当前任务并提供可核验证据：{now_state.now_target}"
                        )
                    criteria = now_state.task_completion_criteria.setdefault(
                        now_state.now_task_id, now_state.now_target
                    )
                    handoff = SupervisorHandoff(
                        supervisor_seq=now_state.supervisor_seq,
                        task_id=now_state.now_task_id,
                        target=now_state.now_target,
                        instruction=supervisor_state.executor_plan,
                        completion_criteria=criteria,
                        dependency_context=[
                            now_state.task_outcomes[task_id]
                            for task_id in sorted(now_state.task_outcomes)
                            if task_id < now_state.now_task_id
                        ],
                        execution_mode=decision.next_execution_mode,
                    )
                    now_state.task_handoffs[now_state.now_task_id] = handoff
                    supervisor_state.handoff = handoff
                    if session_run:
                        session_run.resume = False
                    boundary("task_start", "host")
                elif supervisor_state.exit_reason:
                    block_reason = supervisor_state.exit_reason
                    break
            else:
                status = "completed"
                answer = supervisor_state.final_answer or executor_state.output_content
        except asyncio.CancelledError:
            from MCP_functions.sandbox import cancel_all
            await asyncio.to_thread(cancel_all)
            if session_run:
                session_run.emit("interrupted", "host", status="interrupted")
                session_run.checkpoint("interrupted")
            raise
        except Exception as exc:
            block_reason = str(exc)
        finally:
            if (now_state.supervisor_descriptions.entries and not asyncio.current_task().cancelling()
                    and not (project_workflow and project_workflow.denied)):
                try:
                    chat_description = await summarize_supervisor_descriptions(
                        now_state=now_state,
                        supervisor_state=supervisor_state,
                        supervisor=config.supervisor,
                        supervisor_client=supervisor_client,
                        chat_function=chat_functions[config.supervisor],
                        status=status,
                        answer=answer,
                        block_reason=block_reason,
                    )
                except Exception:
                    chat_description = (
                        now_state.supervisor_descriptions.fallback_summary()
                    )
            await _close_model_client(supervisor_client)
            if executor_client is not supervisor_client:
                await _close_model_client(executor_client)

    if status != "completed":
        answer = (
            "任务未完成。"
            + (f"阻塞原因：{block_reason}。" if block_reason else "")
            + (
                f"未完成任务：{now_state.now_target}"
                if now_state.now_target
                else "未能生成有效计划。"
            )
        )

    if project_workflow and project_workflow.staging_root:
        answer += "\n\nHost 提示：以上文件修改、构建或检查发生在隔离副本；是否写回真实项目，以随后终端的固定变更包批准和应用结果为准。"

    if session_run:
        session_run.phase = "completed" if status == "completed" else session_run.phase
        session_run.emit("answer", "host", status=status, answer=answer,
                         total_tokens=now_state.total_token, model_requests=now_state.model_request_count)
        session_run.checkpoint(status)

    finished_at = datetime.now()
    elapsed_time_seconds = round((finished_at - started_at).total_seconds(), 3)
    save_chat_history(
        chat_id=now_state.chat_id,
        session_address=now_state.session_address,
        query_content=query,
        answer_content=answer,
        description=(
            chat_description
            or supervisor_state.description
            or executor_state.description
            or block_reason
        ),
        task_number=len(now_state.task_list),
        seq_number=max(now_state.executor_seq, now_state.supervisor_seq),
        supervisor_descriptions=now_state.supervisor_descriptions.to_dicts(),
        total_tokens=now_state.total_token,
        elapsed_time_seconds=elapsed_time_seconds,
        started_at=started_at.isoformat(timespec="seconds"),
        finished_at=finished_at.isoformat(timespec="seconds"),
    )

    process(f"""
    executor任务执行总轮次:{now_state.executor_seq},
    \nsupervisor任务执行总轮次:{now_state.supervisor_seq},
    \n任务列表清单：{now_state.task_list}
    """)

    return {
        "status": status,
        "answer": answer,
        "completed_tasks": now_state.now_task_id,
        "task_list": now_state.task_list,
        "block_reason": block_reason,
        "total_tokens": now_state.total_token,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "elapsed_time_seconds": elapsed_time_seconds,
    }


async def main(query: str, session_address: str, chat_id: str, *, session_run=None,
               runtime_config=None, sandbox_policy=None, approval=None, project_workflow=None):
    """Convert startup failures into a saved blocked result."""
    started_at = datetime.now()
    from State.session_checkpoint import ACTIVE_RUN
    token = ACTIVE_RUN.set(session_run)
    from State.project_workspace import WORKSPACE
    workspace_token = WORKSPACE.set(project_workflow)
    try:
        if session_run or runtime_config or sandbox_policy or project_workflow:
            result = await _run_main(query, session_address, chat_id, started_at,
                session_run=session_run, runtime_config=runtime_config,
                sandbox_policy=sandbox_policy, approval=approval, project_workflow=project_workflow)
        else:
            result = await _run_main(query, session_address, chat_id, started_at)

    except Exception as exc:
        finished_at = datetime.now()
        elapsed_time_seconds = round((finished_at - started_at).total_seconds(), 3)
        answer = f"任务未完成。启动或连接阶段失败：{exc}"
        preserved = session_run.bundle["agent"] if session_run and session_run.bundle else None
        preserved_tokens = preserved.total_token if preserved else 0
        save_chat_history(
            chat_id=str(chat_id),
            session_address=str(session_address),
            query_content=query,
            answer_content=answer,
            description=str(exc),
            task_number=len(preserved.task_list) if preserved else 0,
            seq_number=max(preserved.executor_seq, preserved.supervisor_seq) if preserved else 0,
            total_tokens=preserved_tokens,
            elapsed_time_seconds=elapsed_time_seconds,
            started_at=started_at.isoformat(timespec="seconds"),
            finished_at=finished_at.isoformat(timespec="seconds"),
        )
        result = {
            "status": "blocked",
            "answer": answer,
            "completed_tasks": preserved.now_task_id if preserved else 0,
            "task_list": list(preserved.task_list) if preserved else [],
            "block_reason": str(exc),
            "total_tokens": preserved_tokens,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "elapsed_time_seconds": elapsed_time_seconds,
        }
        if session_run:
            session_run.emit("answer", "supervisor", status="blocked", answer=answer,
                             total_tokens=preserved_tokens, block_reason=str(exc))
            session_run.checkpoint("blocked")
    finally:
        WORKSPACE.reset(workspace_token)
        ACTIVE_RUN.reset(token)
    process(
        f"Total tokens: {result['total_tokens']}, "
        f"elapsed time: {result['elapsed_time_seconds']} seconds"
    )
    return result


async def _register_mcp_clients(host, writable_roots: tuple[str, ...] = (), *, sandbox_policy=None, allow_external=False):
    from dataclasses import asdict
    base_environment = {key: value for key, value in os.environ.items()
                        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    base_environment["PYTHONIOENCODING"] = "utf-8"
    if sandbox_policy:
        base_environment["CODING_AGENT_SANDBOX_POLICY"] = json.dumps(asdict(sandbox_policy))
    required_parameters = {
        "memory": StdioServerParameters(
            command=sys.executable,
            args=["-m", "Context.mcp_resources"],
            cwd=str(PROJECT_ROOT),
        ),
        "supervisor_memory": StdioServerParameters(
            command=sys.executable,
            args=["-m", "Context.mcp_supervisor_tools"],
            cwd=str(PROJECT_ROOT),
        ),
        "system": StdioServerParameters(
            command=sys.executable,
            args=["-m", "MCP_functions.System_Files.Files_server.mcp_system_server"],
            cwd=str(PROJECT_ROOT),
            env={
                **base_environment,
                "CODING_AGENT_WRITABLE_ROOTS": os.pathsep.join(writable_roots),
            },
        ),
        "collect": StdioServerParameters(
            command=sys.executable,
            args=["-m", "MCP_functions.collect_information.mcp_collect_server"],
            cwd=str(PROJECT_ROOT),
        ),
    }
    for client_name, parameters in required_parameters.items():
        if parameters.env is None:
            parameters.env = dict(base_environment)
        await host.run(parameters, client_name)
    from State.session_checkpoint import active_run
    # Restricted CLI does not spawn optional external executables as a side door.
    if not allow_external:
        return
    try:
        optional_parameters = get_remote_mcp_link()
    except Exception:
        optional_parameters = {}
    for client_name, parameters in optional_parameters.items():
        try:
            await host.run(parameters, client_name)
        except Exception:
            continue


SKILL_GUIDE = {
    "supervisor_plan.md": "生成可执行、可验收的顺序任务计划。",
    "supervisor_decison.md": "根据验收结论形成后续指导与任务推进决定。",
    "memory_retrieval.md": "按需恢复聊天、执行和监督历史。",
}
SKILL_BELONG = {
    "supervisor_plan.md": {AgentRole.SUPERVISOR},
    "supervisor_decison.md": {AgentRole.SUPERVISOR},
    "memory_retrieval.md": {AgentRole.SUPERVISOR},
}


def _load_skills(skills_route, agent_role):
    skills_route = Path(skills_route)
    if not skills_route.exists():
        raise FileNotFoundError(f"{skills_route} does not exist")
    if not skills_route.is_dir():
        raise NotADirectoryError(f"{skills_route} is not a directory")
    skill_gather = []
    for item in sorted(skills_route.iterdir(), key=lambda path: path.name.lower()):
        if not item.is_file() or agent_role not in SKILL_BELONG.get(item.name, set()):
            continue
        skill_gather.append(
            Skill(
                id=len(skill_gather),
                name=item.name,
                description=SKILL_GUIDE.get(item.name, ""),
                address=str(item.resolve()),
                belong=agent_role,
            )
        )
    return skill_gather


def _build_model_client(provider, api_key):
    client_map = {
        "glm": ZhipuAiClient,
        "chatgpt": OpenAI,
        "deepseek": OpenAI,
        "qwen": OpenAI,
        "claude": Anthropic,
    }
    base_urls = {
        "deepseek": "https://api.deepseek.com",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    client_type = client_map.get(provider)
    if client_type is None:
        raise ValueError(f"unsupported model provider: {provider}")
    arguments = {"api_key": api_key}
    if provider in base_urls:
        arguments["base_url"] = base_urls[provider]
    return client_type(**arguments)


async def _close_model_client(client):
    if client is None:
        return
    close = getattr(client, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _chat_map():
    return {
        "glm": zhipu_chat,
        "chatgpt": chatGpt_chat,
        "qwen": qwen_chat,
        "claude": claude_chat,
        "deepseek": deepseek_chat,
    }
