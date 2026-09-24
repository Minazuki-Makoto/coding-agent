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
    executor_seq = _value(current_state, "seq")
    task_id = _value(current_state, "now_task_id")
    if not isinstance(session_address, (str, Path)) or not session_address:
        raise ValueError("state.session_address is required")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("state.chat_id is required")
    if type(task_id) is not int or type(executor_seq) is not int:
        raise ValueError("state.now_task_id and state.seq must be integers")

    session_address = Path(session_address)
    session_address.mkdir(parents=True, exist_ok=True)
    history_path = session_address / "supervisor_history.jsonl"
    record = {
        "seq": _value(current_state, "supervisor_seq") or _next_supervisor_seq(history_path, chat_id),
        "chat_id": chat_id,
        "task_id": task_id,
        "target": _value(current_state, "now_target", ""),
        "executor_seq": executor_seq,
        "tool_name": _value(current_state, "tool", ""),
        "exists_error": bool(_value(current_state, "is_error", False)),
        "error_message": _value(current_state, "error_message", ""),
        "input_content": _value(
            current_state, "tool_input", _value(current_state, "user_query", "")
        ),
        "output_content": _value(current_state, "tool_results", ""),
    }
    with history_path.open("a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(record, ensure_ascii=False) + "\n")
