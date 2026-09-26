import json
from pathlib import Path


def _value(current_state, name, default=None):
    return getattr(current_state, name, default)


def _next_supervisor_seq(history_path, chat_id):
    largest = 0
    if not history_path.exists():
        return 1
    with history_path.open("r", encoding="utf-8-sig") as history_file:
        for line in history_file:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("chat_id") == chat_id and type(record.get("seq")) is int:
                largest = max(largest, record["seq"])
    return largest + 1


def save_supervisor_state_history(current_state):
    """从 AgentLoop state 保存一条 supervisor 工具执行记录。"""
    session_address = _value(current_state, "session_address")
    chat_id = _value(current_state, "chat_id")

    task_id = _value(current_state, "task_id")
    if not isinstance(session_address, (str, Path)) or not session_address:
        raise ValueError("state.session_address is required")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("state.chat_id is required")


    session_address = Path(session_address)
    session_address.mkdir(parents=True, exist_ok=True)
    history_path = session_address / "supervisor_history.jsonl"
    record = {
        "seq": _value(current_state, "supervisor_seq"),
        "chat_id": chat_id,
        "task_id": task_id,
        "target": _value(current_state, "target", ""),
        "tool_name": _value(current_state, "tool_name", ""),
        "tool_result": _value(current_state, "tool_result", ""),
        "input_content": _value(
            current_state, "input_content", _value(current_state, "user_query", "")
        ),
        "memory_window": _value(current_state, "memory_window",[]),
        "executor_plan": _value(current_state, "executor_plan",""),
        "is_executor_error":_value(current_state, "is_executor_error",""),
        "executor_error_message": _value(current_state, "executor_error_message", ""),
        "need_date_back":_value(current_state, "need_date_back",""),
        "date_back_input":_value(current_state, "date_back_input", ""),
        "modify_content":_value(current_state, "modify_content", ""),
    }
    with history_path.open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")
