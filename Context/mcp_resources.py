from mcp.server import FastMCP
import sys
from mcp import StdioServerParameters

from Context.History_Resorce.mcp_history_resource_service import (
read_task_all_history,
read_chat_history,
read_history_chat as _read_history_chat
)

from Context.History_Resorce.mcp_history_error import (
read_question_task,
read_history_by_seq
)


# 从项目根目录运行：python -m Context.mcp_resources
mcp_server = FastMCP("history tools")

@mcp_server.tool(
    name="read_now_task",
    description=(
        'Use when starting or resuming a task, or when you need description-only semantic matching over that task\'s saved context. session_address is the existing session directory containing the history files, not a file path or URI; pass it unchanged without URL encoding. chat_id selects the chat within that directory. Use the session_address and chat_id supplied by the host or task context; do not invent them. task_id selects exactly that task. Returns a JSON array of candidates. Each candidate contains only a description for relevance judgment and a locator object with session_address, chat_id, task_id and seq. Judge relevance only from description; locator is opaque routing metadata and must not influence relevance. To inspect a selected candidate, pass its locator fields unchanged to read_task_error. Summaries omit full tool inputs and outputs. Ordinary failed attempts are omitted; use read_task_history_error for unresolved problems. An empty list means no matching summaries or notices, not proof that the task is complete.'
    )
)
async def read_now_task(
        session_address:str,
        chat_id:str,
        task_id:int
):
    return read_task_all_history(
        session_address=session_address,
        chat_id=chat_id,
        task_id=task_id
    )


@mcp_server.tool(
    name="read_history_task",
    description=(
        'Use when the current task depends on earlier tasks or you need previous decisions and results from the same chat. session_address is the existing session directory containing the history files, not a file path or URI; pass it unchanged without URL encoding. chat_id selects the chat within that directory. Use the session_address and chat_id supplied by the host or task context; do not invent them. Pass the CURRENT task_id: this returns only records whose task_id is strictly smaller, excluding the current task and later tasks. Returns valid successful summaries and unresolved invalidation notices, without full tool inputs and outputs or ordinary failed attempts. Validity accounts for repair and invalidation links throughout the chat, including later tasks. For one specific task use read_now_task; for a referenced execution use read_task_error with its task_id and seq.'
    )
)
async def read_history_task(
        session_address:str,
        chat_id:str,
        task_id:int
):
    return read_chat_history(
        session_address=session_address,
        chat_id=chat_id,
        task_id=task_id
    )

@mcp_server.tool(
    name = "read_history_chat",
    description=(
        "Use only when the current input lacks necessary user constraints or prior decisions. session_address is the known session directory containing chat_history.jsonl; pass it unchanged. Optionally pass the host-provided chat_id to filter records and preserve leading zeroes. It does not read executor history. When the current input is sufficient, skip this tool."
    )
)
async def read_history_chat(
        session_address:str,
        chat_id:str | None = None,
):
    records = _read_history_chat(session_address)
    if chat_id is None or not isinstance(records,list):
        return records
    return [record for record in records if record.get("chat_id") == chat_id]

@mcp_server.tool(
    name = "read_task_history_error",
    description=(
        'Use to investigate what still needs fixing for a specific task. session_address is the existing session directory containing the history files, not a file path or URI; pass it unchanged without URL encoding. chat_id selects the chat within that directory. Use the session_address and chat_id supplied by the host or task context; do not invent them. task_id selects exactly that task. Returns currently unresolved failed attempts and invalidated execution records, excluding problems with a valid repair. Includes stored execution details, a summary, and available supervisor advice; invalidated records also identify the invalidating execution. A record may retain is_solved=true from its original execution and still be unresolved because it was later invalidated. An empty list means no pending problems were reconstructed from the saved history; this tool does not test the current code or prove it is correct.'
    )
)
async def read_task_history_error(
        session_address:str,
        chat_id:str,
        task_id:int
):
    return read_question_task(
        session_address=session_address,
        chat_id=chat_id,
        task_id=task_id
    )

@mcp_server.tool(
    name = "read_task_error",
    description=(
        "Use when task_id and seq are already known and you need the stored details of that exact execution, for example to inspect inputs, outputs or a historical decision. session_address is the existing session directory containing the history files, not a file path or URI; pass it unchanged without URL encoding. chat_id selects the chat within that directory. Use the session_address and chat_id supplied by the host or task context; do not invent them. task_id and seq together identify the execution within the chat. Returns matching original execution data and its saved supervisor summary, regardless of whether the execution originally succeeded, failed, was later invalidated or repaired. It does not reconstruct the record's current validity or attach later correction advice; use read_task_history_error for currently unresolved problems. An empty list means no matching record. Despite its name, it can retrieve successful executions as well as errors."
    )
)
async def read_task_error(
        session_address:str,
        chat_id:str,
        task_id:int,
        seq:int
):
    return read_history_by_seq(
        session_address=session_address,
        chat_id=chat_id,
        task_id=task_id,
        seq=seq
    )

def register_memory_tools():
    return {
        "memory": StdioServerParameters(
        command=sys.executable,
        args=["-m", "Context.mcp_resources"]
    )
    }

if __name__ == '__main__':
    mcp_server.run("stdio")
