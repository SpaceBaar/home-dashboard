import asyncio
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def run_finance_agent():
    print("Starting Zerodha Kite MCP bridge...")
    
    # The -y flag ensures npx doesn't prompt for confirmation
    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "mcp-remote", "https://mcp.kite.trade/mcp"],
        env=dict(os.environ)
    )
    
    print("Connecting to the server...")
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("\n✅ MCP Connection established successfully!\n")
            
            tools_response = await session.list_tools()
            
            print("🛠️ Available Zerodha Tools:")
            print("-" * 50)
            for tool in tools_response.tools:
                # Splitting at the first period to keep the description concise
                desc = tool.description.split('.')[0] if tool.description else "No description"
                print(f"- {tool.name}: {desc}")
            print("-" * 50)

if __name__ == "__main__":
    asyncio.run(run_finance_agent())