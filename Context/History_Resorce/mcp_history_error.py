from State.session_checkpoint import record_visible
import json
import sys
from pathlib import Path
from Context.History_Resorce.history_state import read_memory_state

# 读取当前待处理问题：失败尝试或后来失效的旧成功记录，排除有效修复目标
def read_question_task(
        session_address,
        chat_id,
        task_id
):
    session_address = Path(session_address)/"executor_history.jsonl"
    if not session_address.exists():

        return {
            "status": "error",
            "message": "the address of history savings is not valid"
        }

    records, invalid, pending = read_memory_state(session_address, chat_id)
    pointed_history = []
    for key, original in pending.items():
        if original["task_id"] != task_id:
            continue
        history = original.copy()
        history.pop("chat_id", None)
        history["description"] = (
            history.get("description") or history.get("output_content") or ""
        )
        if key in invalid:
            notice = invalid[key]
            history["invalidated_by"] = {"task_id": notice["task_id"], "seq": notice["seq"]}
            history["update_advice"] = (
                notice.get("reason") or notice.get("description") or ""
            )
        else:
            history["update_advice"] = history.get("review_reason", "")
        pointed_history.append(history)
    return pointed_history

# 按task_id、seq回查原始记录，不受原判断、失效或修复状态限制
def read_history_by_seq(
        session_address,
        chat_id,
        task_id,
        seq
):
    history_path = Path(session_address)/"executor_history.jsonl"
    print(
        f"[memory.exact] start chat_id={chat_id} task_id={task_id} seq={seq}",
        file=sys.stderr,
        flush=True,
    )
    if not history_path.exists():
        print(
            f"[memory.exact] history file not found: {history_path}",
            file=sys.stderr,
            flush=True,
        )

        return {
            "status": "error",
            "message": "the address of history savings is not valid"
        }

    pointed_history = []
    with open(history_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue

            history = json.loads(line)

            if not record_visible(history):

                continue
            if not isinstance(history,dict):
                continue

            if (
                    history.get("chat_id") == chat_id and
                    history.get("task_id") == task_id and
                    history.get("seq") == seq
            ):
                #剪枝叶，删去次要信息
                history.pop("chat_id"),

                supervisor = history.get("supervisor") or {}
                pointed_history.append(history)

    print(
        f"[memory.exact] done matches={len(pointed_history)}",
        file=sys.stderr,
        flush=True,
    )
    return pointed_history
