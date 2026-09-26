import json
from pathlib import Path

from AgentLoop.loop_utils import to_jsonable


def _value(current_state, name, default=None):
    return getattr(current_state, name, default)


def _validate_location(current_state):
    task_id = _value(current_state, "task_id")
    seq = _value(current_state, "executor_seq")
    session_address = _value(current_state, "session_address")
    chat_id = _value(current_state, "chat_id")
    if type(task_id) is not int or type(seq) is not int:
        raise ValueError("task_id and executor_seq must be integers")
    if not isinstance(session_address, (str, Path)) or not session_address:
        raise ValueError("session_address is required")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("chat_id is required")
    return Path(session_address), chat_id, task_id, seq


def _append_record(session_address: Path, record: dict):
    session_address.mkdir(parents=True, exist_ok=True)
    with (session_address / "executor_history.jsonl").open(
        "a", encoding="utf-8"
    ) as history_file:
        history_file.write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")


def save_state_history(current_state, record_type: str = "tool_event"):
    """Save a normalized executor tool event or final turn record."""
    session_address, chat_id, task_id, seq = _validate_location(current_state)
    record = {
        "record_type": record_type,
        "seq": seq,
        "chat_id": chat_id,
        "task_id": task_id,
        "target": _value(current_state, "now_target", ""),
        "supervisor_guidance": _value(current_state, "supervisor_guidance", ""),
        "tool_name": _value(current_state, "tool_name", ""),
        "arguments": _value(current_state, "tool_arguments", {}),
        "tool_result": _value(current_state, "tool_result"),
        "tool_ok": _value(current_state, "tool_ok"),
        "description": _value(current_state, "description", ""),
        "input_content": _value(current_state, "input_content", ""),
        "output_content": _value(current_state, "output_content", ""),
        "is_finished": bool(_value(current_state, "is_finished", False)),
        "is_error": bool(_value(current_state, "is_error", False)),
        "error_message": _value(current_state, "error_message", ""),
        "exit_reason": _value(current_state, "exit_reason", ""),
        "review_status": "pending",
    }
    _append_record(session_address, record)
    return record


def save_executor_review(
    current_state,
    reviewed_executor_seqs: list[int],
    is_passed: bool,
    reason: str,
):
    """Append a supervisor judgment without rewriting executor events."""
    session_address = Path(_value(current_state, "session_address"))
    record = {
        "record_type": "supervisor_review",
        "chat_id": _value(current_state, "chat_id"),
        "task_id": _value(current_state, "task_id"),
        "supervisor_seq": _value(current_state, "supervisor_seq"),
        "reviewed_executor_seqs": list(reviewed_executor_seqs),
        "is_passed": bool(is_passed),
        "reason": reason or "",
    }
    _append_record(session_address, record)
    return record
