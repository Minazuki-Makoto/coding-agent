from MCP_functions.MCP_client import mcp_client
from contextlib import AsyncExitStack
from mcp.client.stdio import stdio_client
from mcp import StdioServerParameters,ClientSession

class mcp_host:
    def __init__(self,stack:AsyncExitStack):

        self.stack = stack
        self.session_dictionary = {}
        self.tool_dictionary = {}
        self.tools = []

    async def run(self,stdio_parameters:StdioServerParameters,client_name):
        if client_name in self.session_dictionary:
            return

        read,write = await self.stack.enter_async_context(
            stdio_client(stdio_parameters)
        )

        session = await self.stack.enter_async_context(
            ClientSession(read, write)
        )

        client = mcp_client(session)
        await client.initialize()

        self.session_dictionary[client_name] = client

        tools = await client.list_all_tools()

        for tool in tools.tools:
            if(tool.name not in self.tool_dictionary):
                self.tool_dictionary[tool.name] = client_name


        self.tools.extend(
            self.build_tool_schema(
                tools
            )
        )
        return

    def build_tool_schema(self,tools):
        record = []
        for tool in tools.tools:
            record.append(
                {
                    "type":"function",
                    "function":{
                        "name":tool.name,
                        "description":tool.description,
                        "parameters":tool.inputSchema
                    }
                }
            )

        return record
