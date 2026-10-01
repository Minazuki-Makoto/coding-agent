from mcp.server import FastMCP

mcp_server = FastMCP("collect")

@mcp_server.tool(
    name = "get_needed_info_tool",
    description=(
        "占位式信息请求工具：只回显 needed 字段，不读取 INFORMATION.json、环境变量、"
        "凭据或其他外部数据。仅用于记录当前缺少的具体信息，不能把返回值当成事实证据。"
    )
)
async def get_needed_info_tool(needed:str):
    return {
        "name":"get_needed_info",
        "needed":needed
    }


if __name__ == "__main__":
    mcp_server.run(transport="stdio")
