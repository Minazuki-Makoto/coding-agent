from State.session_checkpoint import record_visible
import json
import sys
from pathlib import Path

from mcp import StdioServerParameters
from mcp.server import FastMCP

from Context.History_Resorce.mcp_supervisor_history import read_supervisor_history
from State.task_summary import read_task_summary_context


mcp_server = FastMCP("supervisor history tools")


@mcp_server.tool(
    name="read_task_summary",
    description=(
        "读取当前 chat 的 task_summary.md 中指定 task_id 的最新累计任务摘要，包含仍有效成果、"
        "验收、修正结论、遗留工作和证据定位。对累计成果、历史依赖或任务推进条件没有特别"
        "准确的把握时先调用；最新执行结果和验收已经充分时可直接决策。默认只读取指定 task；"
        "include_other_tasks=true 时同时读取早于它的任务最新摘要。session_address/chat_id "
        "由 Host 强制绑定，模型只选择 task_id 和 include_other_tasks；找不到返回 not_found。"
    ),
)
async def read_task_summary(
    session_address: str,
    chat_id: str,
    task_id: int,
    include_other_tasks: bool = False,
):
    return read_task_summary_context(
        session_address=session_address,
        chat_id=chat_id,
        current_task_id=task_id,
        include_other_tasks=include_other_tasks,
    )


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

            if not record_visible(history):

                continue
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
                "arguments": history.get("arguments", {}),
                "input_content": history.get("input_content"),
                "output_content": history.get("output_content"),
                "exists_error": bool(history.get("exists_error", False)),
                "error_message": history.get("error_message", ""),
                "is_executor_passed": history.get("is_executor_passed"),
                "reviewed_executor_seqs": history.get("reviewed_executor_seqs", []),
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
        "读取指定 task_id 已保存的 Supervisor 核查记录。仅当本轮评估输入与 handoff 缺少"
        "某次核查结论时使用，不要例行回放历史。返回 description、工具输入输出、错误信息"
        "以及精确 locator；空 description 不代表没有执行过核查。需要按 executor_seq 或 "
        "Supervisor seq 精确过滤时使用 read_supervisor_history。"
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
        "按 task_id、executor_seq 或 Supervisor seq 查询已保存的 Supervisor 工具历史。"
        "仅为补齐明确缺失或核对矛盾而调用；不要为了增加信心而读取全部记录。它读取的是 "
        "supervisor_history.jsonl，不是 Executor 历史，也不能单独证明任务正确。"
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
