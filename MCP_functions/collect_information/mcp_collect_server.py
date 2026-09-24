from mcp.server import FastMCP

mcp_server = FastMCP("collect")

@mcp_server.tool(
    name = "get_needed_info_tool",
    description="""
    you can get information from the INFORMATION.json,provided by the user
    """
)
async def get_needed_info_tool(needed:str):
    return {
        "name":"get_needed_info",
        "needed":needed
    }


if __name__ == "__main__":
    mcp_server.run(transport="stdio")