import asyncio
import os
import time
import requests
import schedule
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Global variables to hold the active session
active_mcp_session = None

# Add your credentials here (or better yet, add them to config.json later)
TELEGRAM_TOKEN = "8702448779:AAHC0WXrI8dsqbRkRNaYyjya3MLVifqmTSw"
TELEGRAM_CHAT_ID = "1858329386"

def send_telegram_message(message_text):
    """Sends a text message to your Telegram account"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

# Inside your morning routine function...
async def generate_daily_login():
    global active_mcp_session
    if active_mcp_session is None:
        return
        
    print("\n[MORNING ROUTINE] Generating new login URL...")
    login_result = await active_mcp_session.call_tool("login", arguments={})
    
    url = login_result.content[0].text
    
    # Construct the message and push it to Telegram
    msg = f"🌅 Good morning! Here is your daily Zerodha login link for the AI Analyst:\n\n{url}"
    send_telegram_message(msg)
    print("Login link sent to Telegram!")

async def run_nightly_analysis():
    """Runs at night to fetch data and process it"""
    global active_mcp_session
    print("\n[NIGHT ROUTINE] Starting analysis...")
    
    try:
        holdings_result = await active_mcp_session.call_tool("get_holdings", arguments={})
        print("Successfully fetched holdings! Sending to Qwen2...")
        # await analyze_with_ai(holdings_result.content[0].text)
    except Exception as e:
        print(f"Failed to fetch holdings. Did you click the morning login link? Error: {e}")

# Synchronous wrappers for the schedule library
def job_morning():
    asyncio.create_task(generate_daily_login())

def job_night():
    asyncio.create_task(run_nightly_analysis())

async def main_loop():
    global active_mcp_session
    
    print("Starting persistent MCP connection...")
    server_params = StdioServerParameters(command="npx", args=["-y", "mcp-remote", "https://mcp.kite.trade/mcp"], env=dict(os.environ))
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            active_mcp_session = session
            print("✅ MCP Connection held open successfully.")
            
            # Schedule the jobs (Times in 24hr format)
            schedule.every().day.at("09:00").do(job_morning)
            schedule.every().day.at("23:00").do(job_night)
            
            # For testing purposes right now, let's trigger the morning job immediately
            job_morning()
            
            # Keep the script running forever
            while True:
                schedule.run_pending()
                await asyncio.sleep(1)

if __name__ == "__main__":
    # Using get_event_loop to allow create_task to work properly in the schedule wrappers
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())