import sys

from mcp import StdioServerParameters
from mcp.server import FastMCP

from Context.History_Resorce.mcp_supervisor_history import read_supervisor_history


mcp_server = FastMCP("supervisor history tools")


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
