import json


def read_memory_state(path, chat_id):
    """Rebuild executor records, invalidations and unresolved records.

    New records stay pending until an explicit supervisor_review references them.
    Legacy is_solved/invalidates/repairs records remain readable.
    """
    records = {}
    invalid = {}
    pending = {}
    accepted = set()
    ordered = []

    with open(path, "r", encoding="utf-8") as history_file:
        for line_number, line in enumerate(history_file, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("chat_id") != chat_id:
                continue
            record_type = record.get("record_type")
            if record_type == "supervisor_review":
                task_id = record.get("task_id")
                for seq in record.get("reviewed_executor_seqs") or []:
                    key = (task_id, seq)
                    if record.get("is_passed") is True:
                        accepted.add(key)
                        pending.pop(key, None)
                    else:
                        accepted.discard(key)
                        if key in records:
                            pending[key] = {
                                **records[key],
                                "review_reason": record.get("reason", ""),
                                "review_supervisor_seq": record.get("supervisor_seq"),
                            }
                continue

            task_id, seq = record.get("task_id"), record.get("seq")
            if type(task_id) is not int or type(seq) is not int:
                continue
            key = (task_id, seq)
            records[key] = record
            ordered.append((key, record))
            if record.get("record_type") in {"tool_event", "executor_turn"}:
                pending[key] = record
            if record.get("tool_ok") is False or record.get("is_error") is True:
                pending[key] = record

            for reference in record.get("invalidates") or []:
                target = (reference.get("task_id"), reference.get("seq"))
                if target in records:
                    invalid[target] = record
                    pending[target] = records[target]
            if record.get("is_solved") is True:
                accepted.add(key)
                pending.pop(key, None)
            elif record.get("is_solved") is False:
                pending[key] = record

    for key, record in ordered:
        if key in invalid:
            accepted.discard(key)
            pending[key] = record
            continue
        if key not in accepted:
            continue
        for reference in record.get("repairs") or []:
            pending.pop((reference.get("task_id"), reference.get("seq")), None)
    return records, invalid, pending
