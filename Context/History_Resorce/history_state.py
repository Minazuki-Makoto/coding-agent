import json


def read_memory_state(path, chat_id):
    """恢复同chat的原记录、失效声明、待处理问题；唯一键为(task_id, seq)。

    旧记录不改。按追加顺序关联；失效声明持续有效，不自动撤回。
    后来失效的修复记录不再消除旧问题。
    """
    records = {}
    invalid = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("chat_id") != chat_id:
                continue
            # 只接受指向已出现记录的关联。
            for ref in record.get("invalidates") or []:
                key = (ref["task_id"], ref["seq"])
                if key in records:
                    invalid[key] = record
            records[(record["task_id"], record["seq"])] = record

    # 先得到最终失效集合，再计算修复效果，避免失效修复继续消除问题。
    pending = {}
    seen = set()
    for key, record in records.items():
        for ref in record.get("invalidates") or []:
            target = (ref["task_id"], ref["seq"])
            if target in seen:
                pending[target] = records[target]
        if record.get("is_solved") is True and key not in invalid:
            for ref in record.get("repairs") or []:
                pending.pop((ref["task_id"], ref["seq"]), None)
        if record.get("is_solved") is False:
            pending[key] = record
        seen.add(key)
    return records, invalid, pending
