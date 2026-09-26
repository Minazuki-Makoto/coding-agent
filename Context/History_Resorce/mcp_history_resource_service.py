import json
import sys
from pathlib import Path
from Context.History_Resorce.history_state import read_memory_state

"""读取有效总结；已失效但未修复的记录仅提供提示，详情由查错模块读取。"""

# 当前task的有效总结及失效待解决提示，不返回全部原始历史。
# 模型只根据description判断相关性；locator只用于下一步精确读取。
def read_task_all_history(
        session_address:str,
        chat_id:str,
        task_id:int
):
    session_directory = str(session_address)
    history_path = Path(session_directory)/"executor_history.jsonl"
    print(
        f"[memory.summary] start chat_id={chat_id} task_id={task_id}",
        file=sys.stderr,
        flush=True,
    )
    if not history_path.exists():
        print(
            f"[memory.summary] history file not found: {history_path}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "status":"error",
            "message":"the address of history savings is not valid"
        }

    records, invalid, pending = read_memory_state(history_path, chat_id)
    pointed_history = []
    for key, history in records.items():
        if history["task_id"] == task_id:
            supervisor = history.get("supervisor") or {}
            if key not in invalid and history.get("is_solved") is True:
                pointed_history.append({
                    "description": supervisor.get("description_content", ""),
                    "locator": {
                        "session_address": session_directory,
                        "chat_id": chat_id,
                        "task_id": history["task_id"],
                        "seq": history["seq"],
                    },
                })
            elif key in invalid and key in pending:
                notice = invalid[key]
                reason = notice.get("supervisor") or {}
                pointed_history.append({
                    "description": "旧结论已失效，问题待解决：" + (
                        reason.get("supervisor_judge_message") or
                        reason.get("description_content") or "请按编号回查"
                    ),
                    "locator": {
                        "session_address": session_directory,
                        "chat_id": chat_id,
                        "task_id": history["task_id"],
                        "seq": history["seq"],
                    },
                })
    print(
        f"[memory.summary] done candidates={len(pointed_history)}",
        file=sys.stderr,
        flush=True,
    )
    return pointed_history


# 回查同chat较早task的总结；状态恢复仍考虑整个chat的后续关联
def read_chat_history(
        session_address,
        chat_id,
        #注意，因为已经执行完了read_task_all_history，所以对task_id以前进行检查
        task_id:int
):
    session_address = Path(session_address)/"executor_history.jsonl"
    if not session_address.exists():
        return {
            "status":"error",
            "message":"the address of history savings is not valid"
        }
    records, invalid, pending = read_memory_state(session_address, chat_id)
    pointed_history = []
    for key, history in records.items():
        if history["task_id"] < task_id:
            supervisor = history.get("supervisor") or {}
            if key not in invalid and history.get("is_solved") is True:
                pointed_history.append({
                    "task_id": history["task_id"],
                    "seq": history["seq"],
                    "target": history.get("target"),
                    "tool_name": history.get("tool_name"),
                    "description": supervisor.get("description_content", ""),
                })
            elif key in invalid and key in pending:
                notice = invalid[key]
                reason = notice.get("supervisor") or {}
                pointed_history.append({
                    "task_id": history["task_id"],
                    "seq": history["seq"],
                    "target": history.get("target"),
                    "status": "unresolved_invalidated",
                    "invalidated_by": {"task_id": notice["task_id"], "seq": notice["seq"]},
                    "description": "旧结论已失效，问题待解决：" + (
                        reason.get("supervisor_judge_message") or
                        reason.get("description_content") or "请按编号回查"
                    ),
                })
    return pointed_history


# 读取当前目录所有chat记录供选择，不自动查找其他会话目录
def read_history_chat(
        session_address:str,
):
    session_address = Path(session_address)/"chat_history.jsonl"
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


            pointed_history.append(
                history
            )

    return pointed_history

