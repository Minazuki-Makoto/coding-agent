from mcp import ClientSession,StdioServerParameters

class mcp_client():
    def __init__(self,session:ClientSession):
        self.session = session

    async def initialize(self):
        await self.session.initialize()

    async def list_all_tools(self):
        tools = await self.session.list_tools()

        return tools

    async def call_tool(self,tool_name:str,argument:dict):

        results = await self.session.call_tool(
            name=tool_name,
            arguments=argument
        )

        return results


