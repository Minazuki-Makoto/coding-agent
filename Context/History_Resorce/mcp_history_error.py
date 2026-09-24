import json
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
        supervisor = history.get("supervisor") or {}
        history["description"] = supervisor.get("description_content", "")
        if key in invalid:
            notice = invalid[key]
            supervisor = notice.get("supervisor") or {}
            history["invalidated_by"] = {"task_id": notice["task_id"], "seq": notice["seq"]}
        history["update_advice"] = supervisor.get("supervisor_judge_message", "")
        pointed_history.append(history)
    return pointed_history

# 按task_id、seq回查原始记录，不受原判断、失效或修复状态限制
def read_history_by_seq(
        session_address,
        chat_id,
        task_id,
        seq
):
    session_address = Path(session_address)/"executor_history.jsonl"
    if not session_address.exists():

        return {
            "status": "error",
            "message": "the address of history savings is not valid"
        }

    pointed_history = []
    with open(session_address, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue

            history = json.loads(line)
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

    return pointed_history
