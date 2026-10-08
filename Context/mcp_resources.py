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
from State.save_tool_result import read_tool_result_record


# 从项目根目录运行：python -m Context.mcp_resources
mcp_server = FastMCP("history tools")

@mcp_server.tool(
    name="read_now_task",
    description=(
        "读取指定 task_id 内已经保存的有效执行摘要与 locator。仅当当前 handoff、"
        "recent_descriptions 或 latest_tool_context 不足以回答当前缺口时使用，勿在每轮开头"
        "例行调用。根据 description 判断相关性；需要原始细节时，把 locator 中的 task_id 和 "
        "seq 原样交给 read_task_error。普通失败不会出现在这里，未解决问题请用 "
        "read_task_history_error。空数组只表示没有可返回记录，不代表任务已经完成。"
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
        "读取同一 chat 中早于当前 task_id 的有效任务记录。优先使用 handoff 中已经提供的 "
        "dependency_context；只有缺少某个前序结论、locator 或修复状态时才调用本工具，避免"
        "重新查询已经验收的内容。传入当前 task_id，返回范围严格小于它。指定当前任务用 "
        "read_now_task；已知 task_id 与 seq 并需要原始记录时用 read_task_error。"
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
        "仅在当前输入与 handoff 缺少必要的用户约束、范围或历史决策时读取聊天记录。"
        "它不读取 Executor 执行历史；当前上下文足够时不要调用。chat_id 必须使用 Host "
        "提供的原值，不要自行构造或转换。"
    )
)
async def read_history_chat(
        session_address:str,
        chat_id:str | None = None,
):
    records = _read_history_chat(session_address)
    if chat_id is None or not isinstance(records,list):
        return records
    return [record for record in records if str(record.get("chat_id", "main")) == chat_id]

@mcp_server.tool(
    name = "read_task_history_error",
    description=(
        "读取指定任务中当前仍未解决的失败或被失效记录。仅在 handoff 指出历史错误、修复链"
        "不清楚，或当前证据与历史结论冲突时使用；不要把它当成常规上下文加载工具。"
        "已被有效修复的问题会被排除。空数组表示历史中未重建出待解决问题，不等于当前代码"
        "已经通过验证。"
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
        "按已知 task_id 与 seq 精确读取一条 Executor 历史记录，可用于补取其输入、输出或"
        "保存的 Supervisor 结论。不要用它做模糊搜索，也不要在已有摘要足够时回读原文。"
        "它可返回成功或失败记录，但不计算记录当前是否仍有效；未解决问题请用 "
        "read_task_history_error。"
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


@mcp_server.tool(
    name="read_tool_result",
    description=(
        "按 tool_result_seq 精确读取一条原始工具结果。仅当 latest_tool_context 或摘要缺少"
        "完成当前判断所必需的原始字段时调用；已有 description 和结构化元数据足够时不要"
        "回读。session_address 与 chat_id 由 Host 绑定，模型只选择 tool_result_seq。"
    ),
)
async def read_tool_result(
    session_address: str,
    chat_id: str,
    tool_result_seq: int,
):
    return read_tool_result_record(
        session_address=session_address,
        chat_id=chat_id,
        tool_result_seq=tool_result_seq,
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
