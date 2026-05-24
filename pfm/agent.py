import asyncio
import json
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def run_finance_agent():
    # 1. Load the configuration
    print("Loading config.json...")
    with open('config.json', 'r') as f:
        config = json.load(f)
        
    stocks_to_track = config['tracking']['stocks']
    print(f"Target Stocks for tonight's run: {', '.join(stocks_to_track)}")
    
    # 2. Configure the Zerodha Kite MCP Server connection
    # We use npx to run the hosted Kite MCP remote bridge
    server_params = StdioServerParameters(
        command="npx",
        args=["mcp-remote", "https://mcp.kite.trade/mcp"],
        env=dict(os.environ) # Pass your environment variables (like Kite API keys if needed)
    )
    
    print("Connecting to Zerodha Kite MCP Server...")
    
    # 3. Establish the connection and list available tools
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the protocol session
            await session.initialize()
            print("Connection established successfully!\n")
            
            # Fetch all the tools the MCP server exposes
            tools = await session.list_tools()
            
            print("Available Zerodha Tools for the LLM:")
            print("-" * 40)
            for tool in tools:
                print(f"- {tool.name}: {tool.description}")
                
            print("-" * 40)
            
            # Example of how the agent will call a tool in the future:
            # print("Fetching current holdings...")
            # holdings = await session.call_tool("get_holdings", {})
            # print(holdings)

if __name__ == "__main__":
    asyncio.run(run_finance_agent())