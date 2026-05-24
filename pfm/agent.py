import asyncio
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def run_finance_agent():
    print("Starting Zerodha Kite MCP bridge...")
    
    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "mcp-remote", "https://mcp.kite.trade/mcp"],
        env=dict(os.environ)
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("\n✅ MCP Connection established successfully!\n")
            
            # 1. Trigger the Login flow
            print("Requesting Login Authorization...")
            login_result = await session.call_tool("login", arguments={})
            
            # Print the authorization URL returned by the MCP server
            print("\n" + "="*60)
            print("ACTION REQUIRED: " + login_result.content[0].text)
            print("="*60 + "\n")
            
            # 2. Pause the script to allow you to log in via the browser
            input("Press Enter here in the terminal AFTER you have successfully logged in...")
            
            # 3. Fetch the Holdings
            print("\nFetching portfolio holdings...")
            try:
                holdings_result = await session.call_tool("get_holdings", arguments={})
                
                print("\n📊 Your Holdings Data (Raw JSON):")
                print("-" * 60)
                print(holdings_result.content[0].text)
                print("-" * 60)
            except Exception as e:
                print(f"\nFailed to fetch holdings: {e}")
                print("Did you complete the browser login before pressing Enter?")

if __name__ == "__main__":
    asyncio.run(run_finance_agent())