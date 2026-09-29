import json
from pathlib import Path

from AgentLoop.loop_utils import to_jsonable


def _value(current_state, name, default=None):
    return getattr(current_state, name, default)


def save_supervisor_state_history(current_state, record_type: str = "supervisor_turn"):
    """Append a JSON-serializable supervisor event or turn result."""
    session_address = _value(current_state, "session_address")
    chat_id = _value(current_state, "chat_id")
    task_id = _value(current_state, "task_id")
    seq = _value(current_state, "supervisor_seq")
    if not isinstance(session_address, (str, Path)) or not session_address:
        raise ValueError("session_address is required")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("chat_id is required")
    if type(seq) is not int:
        raise ValueError("supervisor_seq must be an integer")

    session_address = Path(session_address)
    session_address.mkdir(parents=True, exist_ok=True)
    record = {
        "record_type": record_type,
        "seq": seq,
        "chat_id": chat_id,
        "task_id": task_id,
        "target": _value(current_state, "target", ""),
        "description": _value(current_state, "description", ""),
        "tool_name": _value(current_state, "tool_name", ""),
        "arguments": _value(current_state, "tool_arguments", {}),
        "tool_result": _value(current_state, "tool_result"),
        "tool_ok": _value(current_state, "tool_ok"),
        "input_content": _value(current_state, "input_content", ""),
        "output_content": _value(current_state, "output_content", ""),
        "executor_plan": _value(current_state, "executor_plan", ""),
        "is_executor_passed": _value(current_state, "is_executor_passed"),
        "is_executor_error": bool(_value(current_state, "is_executor_error", False)),
        "executor_error_message": _value(
            current_state, "executor_error_message", ""
        ),
        "reviewed_executor_seqs": list(
            _value(current_state, "reviewed_executor_seqs", []) or []
        ),
        "need_date_back": bool(_value(current_state, "need_date_back", False)),
        "date_back_locations": _value(current_state, "date_back_locations", []),
        "date_back_input": _value(current_state, "date_back_input", ""),
        "modify_content": _value(current_state, "modify_content", ""),
        "verification_requirement": _value(
            current_state, "verification_requirement", ""
        ),
        "is_next_target": bool(_value(current_state, "is_next_target", False)),
        "decision_finished": bool(
            _value(current_state, "decision_finished", False)
        ),
        "proposed_task_list": _value(current_state, "proposed_task_list"),
        "final_answer": _value(current_state, "final_answer", ""),
        "task_attempt": _value(current_state, "task_attempt", 0),
        "model_stage": _value(current_state, "model_stage", ""),
        "raw_model_response_excerpt": _value(
            current_state, "raw_model_response_excerpt", ""
        ),
        "validation_error": _value(current_state, "validation_error", ""),
        "exit_reason": _value(current_state, "exit_reason", ""),
    }
    with (session_address / "supervisor_history.jsonl").open(
        "a", encoding="utf-8"
    ) as history_file:
        history_file.write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")
    return record
