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
    task_id = _value(current_state, "task_id")
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


    session_address = Path(session_address)
    session_address.mkdir(parents=True, exist_ok=True)
    record = {

        "seq": seq,
        "chat_id": chat_id,
        "task_id": task_id,
        "target": target,
        "supervisor_guidance":_value(current_state, "supervisor_guidance",""),
        "tool_name": _value(current_state, "tool", ""),

        "input_content": _value(
            current_state, "input_content",""
        ),

        "output_content": _value(current_state, "output_content", ""),

        "is_finished": bool(_value(current_state, "is_finished", False)),
        "is_error":bool(_value(current_state, "is_error", False)),
        "error_message": _value(current_state, "error_message", ""),
        "memory_window": _value(current_state, "memory_window"),
    }

    with (session_address / "executor_history.jsonl").open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")
