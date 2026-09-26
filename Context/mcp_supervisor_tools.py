import json
import sys
from pathlib import Path

from mcp import StdioServerParameters
from mcp.server import FastMCP

from Context.History_Resorce.mcp_supervisor_history import read_supervisor_history


mcp_server = FastMCP("supervisor history tools")


def read_supervisor_task_history(
        session_address: str,
        chat_id: str,
        task_id: int,
):
    """读取当前 task 的 supervisor 记录，并返回描述、工具信息与精确定位信息。"""
    session_directory = str(session_address)
    history_path = Path(session_directory) / "supervisor_history.jsonl"
    if not history_path.exists():
        return {
            "status": "error",
            "message": "the supervisor history address is not valid",
        }

    pointed_history = []
    with history_path.open("r", encoding="utf-8-sig") as history_file:
        for line in history_file:
            if not line.strip():
                continue

            history = json.loads(line)
            if not isinstance(history, dict):
                continue
            if history.get("chat_id") != chat_id:
                continue
            if history.get("task_id") != task_id:
                continue

            supervisor = history.get("supervisor") or {}
            pointed_history.append({
                "description": (
                    history.get("description")
                    or history.get("description_content")
                    or supervisor.get("description_content")
                    or ""
                ),
                "target": history.get("target"),
                "tool_name": history.get("tool_name"),
                "input_content": history.get("input_content"),
                "output_content": history.get("output_content"),
                "exists_error": bool(history.get("exists_error", False)),
                "error_message": history.get("error_message", ""),
                "locator": {
                    "session_address": session_directory,
                    "chat_id": chat_id,
                    "task_id": history.get("task_id"),
                    "executor_seq": history.get("executor_seq"),
                    "seq": history.get("seq"),
                },
            })

    return pointed_history


@mcp_server.tool(
    name="read_supervisor_task",
    description=(
        "Use when you need the supervisor's saved inspection context for one "
        "specific task. session_address is the existing session directory "
        "containing supervisor_history.jsonl, not a file path or URI; pass it "
        "unchanged. chat_id and task_id select exactly one task. Returns records "
        "in append order with the saved description, target, tool name, tool "
        "input and output, error information, and a locator containing "
        "session_address, chat_id, task_id, executor_seq and supervisor seq. "
        "The description is returned only when it was actually saved; an empty "
        "description must not be treated as proof that no inspection occurred. "
        "Use read_supervisor_history when you need optional executor_seq or seq "
        "filters across the supervisor history."
    ),
)
async def read_supervisor_task(
        session_address: str,
        chat_id: str,
        task_id: int,
):
    return read_supervisor_task_history(
        session_address=session_address,
        chat_id=chat_id,
        task_id=task_id,
    )


@mcp_server.tool(
    name="read_supervisor_history",
    description=(
        "Read the supervisor's saved tool-execution history for one chat. "
        "Use this to recover what the supervisor previously inspected, including "
        "tool names, inputs, outputs, errors, task identifiers and sequence numbers. "
        "Pass the existing session directory and chat_id supplied by the host; "
        "do not invent them. You may filter with task_id, executor_seq or the "
        "supervisor's own seq. If no filter is provided, return all matching "
        "records in append order. "
        "This reads supervisor_history.jsonl, not executor_history.jsonl, and "
        "does not decide whether the executor task is correct."
    ),
)
async def read_supervisor_history_tool(
        session_address: str,
        chat_id: str,
        task_id: int | None = None,
        executor_seq: int | None = None,
        seq: int | None = None,
):
    return read_supervisor_history(
        session_address=session_address,
        chat_id=chat_id,
        task_id=task_id,
        executor_seq=executor_seq,
        seq=seq,
    )


def register_supervisor_memory_tools():
    return {
        "supervisor_memory": StdioServerParameters(
            command=sys.executable,
            args=["-m", "Context.mcp_supervisor_tools"],
        )
    }


if __name__ == "__main__":
    mcp_server.run(transport="stdio")
