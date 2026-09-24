import json
from pathlib import Path


def read_supervisor_history(
        session_address: str,
        chat_id: str,
        task_id: int | None = None,
        executor_seq: int | None = None,
        seq: int | None = None,
):
    """读取指定 chat 的 supervisor 工具执行历史，可按 seq 查询单条记录。"""
    history_path = Path(session_address) / "supervisor_history.jsonl"
    if not history_path.exists():
        return {
            "status": "error",
            "message": "the supervisor history address is not valid",
        }

    records = []
    with history_path.open("r", encoding="utf-8-sig") as history_file:
        for line in history_file:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            if record.get("chat_id") != chat_id:
                continue
            if task_id is not None and record.get("task_id") != task_id:
                continue
            if executor_seq is not None and record.get("executor_seq") != executor_seq:
                continue
            if seq is not None and record.get("seq") != seq:
                continue
            record = record.copy()
            record.pop("chat_id", None)
            records.append(record)

    return records
