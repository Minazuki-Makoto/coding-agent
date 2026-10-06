from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from AgentLoop.loop_utils import to_jsonable
from MCP_functions.tool_registry import ToolExecutionResult


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ToolResultLocator:
    session_address: str
    chat_id: str
    tool_result_seq: int


@dataclass
class ToolResultRecord:
    schema_version: int
    tool_result_seq: int
    event_id: str
    chat_id: str
    task_id: int
    actor: Literal["executor", "supervisor"]
    actor_turn_seq: int | None
    phase: str
    tool_name: str
    arguments: dict[str, Any]
    ok: bool
    error_type: str | None
    message: str
    content: Any
    created_at: str


@dataclass
class ToolSummary:
    tool_result_seq: int
    tool_name: str
    tool_ok: bool
    description: str


def _history_path(session_address):
    return Path(session_address) / "tool_results.jsonl"


def _path_lock(path: Path):
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _cross_process_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, 2)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if __import__("os").name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_last_tool_result_seq(session_address: str, chat_id: str):
    path = _history_path(session_address)
    if not path.exists():
        return 0
    maximum = 0
    with path.open("r", encoding="utf-8") as history_file:
        for line in history_file:
            if not line.strip():
                continue
            record = json.loads(line)
            seq = record.get("tool_result_seq")
            if type(seq) is int:
                maximum = max(maximum, seq)
    return maximum


def save_tool_result(
    session_address: str,
    chat_id: str,
    task_id: int,
    actor: str,
    actor_turn_seq: int | None,
    phase: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_result: ToolExecutionResult,
    tool_result_seq: int,
) -> ToolResultLocator:
    if actor not in {"executor", "supervisor"}:
        raise ValueError("actor must be executor or supervisor")
    if type(task_id) is not int or type(tool_result_seq) is not int:
        raise ValueError("task_id and tool_result_seq must be integers")
    path = _history_path(session_address)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = ToolResultRecord(
        schema_version=1,
        tool_result_seq=tool_result_seq,
        event_id=str(uuid.uuid4()),
        chat_id=str(chat_id),
        task_id=task_id,
        actor=actor,
        actor_turn_seq=actor_turn_seq,
        phase=phase,
        tool_name=tool_name,
        arguments=dict(arguments),
        ok=tool_result.ok,
        error_type=tool_result.error_type,
        message=tool_result.message,
        content=tool_result.to_dict(),
        created_at=datetime.now().isoformat(timespec="milliseconds"),
    )
    from State.session_checkpoint import annotate_record
    serialized_record = annotate_record(to_jsonable(asdict(record)), "tool_results.jsonl")
    from State.session_checkpoint import active_run
    run = active_run()
    if run and run.current_operation:
        serialized_record["operation_id"] = run.current_operation
    with _path_lock(path):
        with _cross_process_lock(path):
            if tool_result_seq <= load_last_tool_result_seq(session_address, str(chat_id)):
                raise ValueError("tool_result_seq conflicts with an existing record")
            with path.open("a", encoding="utf-8") as history_file:
                history_file.write(
                    json.dumps(serialized_record, ensure_ascii=False) + "\n"
                )
                history_file.flush()
    return ToolResultLocator(
        session_address=str(session_address),
        chat_id=str(chat_id),
        tool_result_seq=tool_result_seq,
    )


def read_tool_result_record(session_address: str, chat_id: str, tool_result_seq: int):
    path = _history_path(session_address)
    if not path.exists():
        return {"status": "not_found", "message": "tool result history not found"}
    with path.open("r", encoding="utf-8") as history_file:
        for line in history_file:
            if not line.strip():
                continue
            record = json.loads(line)
            from State.session_checkpoint import record_visible
            if not record_visible(record):
                continue
            if (
                record.get("chat_id") == chat_id
                and record.get("tool_result_seq") == tool_result_seq
            ):
                return {"status": "success", "record": record}
    return {"status": "not_found", "message": "tool result record not found"}
