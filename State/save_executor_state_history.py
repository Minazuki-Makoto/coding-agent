import json
from pathlib import Path


def _value(current_state, name, default=None):
    return getattr(current_state, name, default)


def _references(current_state, name):
    references = _value(current_state, name, None)
    if references is None:
        return []
    if not isinstance(references, list):
        raise ValueError(f"{name} must be a list of task_id/seq references")
    normalized = []
    for reference in references:
        if not isinstance(reference, dict):
            raise ValueError(f"{name} must contain dictionaries")
        task_id = reference.get("task_id")
        seq = reference.get("seq")
        if type(task_id) is not int or type(seq) is not int:
            raise ValueError(f"{name} must identify records by integer task_id and seq")
        normalized.append({"task_id": task_id, "seq": seq})
    return normalized


def save_state_history(current_state):
    """从 AgentLoop state 保存一条 executor 执行记录。"""
    task_id = _value(current_state, "now_task_id")
    seq = _value(current_state, "executor_seq")
    session_address = _value(current_state, "session_address")
    chat_id = _value(current_state, "chat_id")
    target = _value(current_state, "now_target", "")

    if type(task_id) is not int or type(seq) is not int:
        raise ValueError("state.now_task_id and state.seq must be integers")
    if not isinstance(session_address, (str, Path)) or not session_address:
        raise ValueError("state.session_address is required")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("state.chat_id is required")

    repairs = _references(current_state, "repaired")
    invalidates = _references(current_state, "invalid")
    own = {"task_id": task_id, "seq": seq}
    if own in repairs or own in invalidates:
        raise ValueError("state cannot reference its own executor record")

    session_address = Path(session_address)
    session_address.mkdir(parents=True, exist_ok=True)
    record = {

        "seq": seq,
        "chat_id": chat_id,
        "task_id": task_id,
        "target": target,
        "tool_name": _value(current_state, "tool", ""),
        "exists_error": bool(_value(current_state, "is_error", False)),
        "error_message": _value(current_state, "error_message", ""),
        "input_content": _value(
            current_state, "tool_input", _value(current_state, "tool_input", "")
        ),

        "output_content": _value(current_state, "tool_results", ""),

        "supervisor": {
            "description_content": _value(current_state, "supervisor_description", ""),
            "supervisor_judge_error": not bool(_value(current_state, "supervisor_check", False)),
            "supervisor_judge_message": _value(current_state, "supervisor_error_advice", ""),
        },

        "is_solved": bool(_value(current_state, "supervisor_check", False)),
        "repairs": repairs,
        "invalidates": invalidates,
    }

    with (session_address / "executor_history.jsonl").open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")
