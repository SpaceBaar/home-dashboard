import asyncio
import os
import json
import time
import requests
import schedule
import ollama
from datetime import datetime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_TOKEN = "8702448779:AAHC0WXrI8dsqbRkRNaYyjya3MLVifqmTSw"
TELEGRAM_CHAT_ID = "1858329386"
active_mcp_session = None

# ==========================================
# TELEGRAM HELPER
# ==========================================
def send_telegram_message(message_text):
    """Sends a text message to your Telegram account"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message_text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

# ==========================================
# CORE LOGIC: LOGIN & AI ANALYSIS
# ==========================================
async def generate_daily_login():
    """Fetches the Zerodha login URL and pushes it to Telegram"""
    global active_mcp_session
    if active_mcp_session is None:
        print("Error: MCP Session is not active.")
        return
        
    print("\n[ROUTINE] Generating new login URL...")
    login_result = await active_mcp_session.call_tool("login", arguments={})
    url = login_result.content[0].text
    
    msg = f"🌅 Good morning! Here is your daily Zerodha login link for the AI Analyst:\n\n{url}"
    send_telegram_message(msg)
    print("✅ Login link sent to Telegram!")

async def analyze_with_ai_and_save(holdings_text, news_intelligence):
    """Parses holdings flat list array, prompts Qwen2, saves to MD, and notifies Telegram"""
    print("\n🧠 Preparing data for local AI analysis...")
    
    # 1. Parse the flat JSON array directly
    try:
        holdings_list = json.loads(holdings_text)
        if not isinstance(holdings_list, list):
            # Fallback wrapper if schema changes unexpectedly
            if isinstance(holdings_list, dict):
                holdings_list = holdings_list.get('data', [])
    except json.JSONDecodeError:
        print("Could not parse JSON. Returning early.")
        return

    summary = []
    total_investment = 0
    total_current = 0
    
    # 2. Iterate through the array using exact keys from payload
    for item in holdings_list:
        symbol = item.get('tradingsymbol', 'Unknown')
        qty = item.get('quantity', 0)
        avg_price = item.get('average_price', 0)
        ltp = item.get('last_price', 0)
        pnl = item.get('pnl', 0)
        day_change_pct = item.get('day_change_percentage', 0)
        
        invested = qty * avg_price
        current_val = qty * ltp
        total_investment += invested
        total_current += current_val
        
        summary.append(
            f"- {symbol}: Qty {qty}, Invested: ₹{invested:.2f}, "
            f"Current: ₹{current_val:.2f}, P&L: ₹{pnl:.2f} ({day_change_pct:.2f}% Today)"
        )

    overall_pnl = total_current - total_investment
    
    # 3. Build context-optimized prompt for Qwen2
    prompt = f"""
    You are an expert financial analyst. Review the following daily portfolio summary alongside today's AI-scored market news intelligence.
    Provide a brief, professional 3-paragraph analysis. 
    
    In your analysis:
    - Detail the overall financial health of the portfolio.
    - Directly correlate any major stock price movements or portfolio P&L variance with the provided news summaries.
    - Maintain an objective, institutional tone.

    Total Invested: ₹{total_investment:.2f}
    Total Current Value: ₹{total_current:.2f}
    Overall P&L: ₹{overall_pnl:.2f}

    Holdings Breakdown:
    {chr(10).join(summary)}

    Recent Scored News Intelligence:
    {news_intelligence}
    """
    
    print("Sending prompt to Qwen2 (Port 8000)...\n")
    client = ollama.AsyncClient(host='http://127.0.0.1:8000')
    
    full_response = ""
    print("📈 AI Analyst Report:\n" + "="*50)
    async for chunk in await client.generate(model='qwen2:1.5b', prompt=prompt, stream=True):
        print(chunk['response'], end='', flush=True)
        full_response += chunk['response']
    print("\n" + "="*50)

    # 4. Save locally to a Markdown file
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"portfolio_analysis_{date_str}.md"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# Portfolio Analysis - {date_str}\n\n")
        f.write(f"**Total Invested:** ₹{total_investment:.2f}\n")
        f.write(f"**Current Value:** ₹{total_current:.2f}\n")
        f.write(f"**Overall P&L:** ₹{overall_pnl:.2f}\n\n")
        f.write("## Holdings Breakdown\n")
        for line in summary:
            f.write(f"{line}\n")
        f.write("\n## AI Insights\n\n")
        f.write(full_response)
        
    print(f"\n💾 Report successfully saved locally to: {filename}")
    
    # 5. Notify completion via Telegram
    status_msg = (
        f"📉 Daily Analysis Complete!\n"
        f"Total Value: ₹{total_current:.2f}\n"
        f"P&L: ₹{overall_pnl:.2f}\n\n"
        f"Check your Raspberry Pi for the full markdown report."
    )
    send_telegram_message(status_msg)

async def fetch_and_score_news(config_path='config.json'):
    print("\n📰 Fetching market news from RSS feeds...")
    
    # 1. Load config settings
    with open(config_path, 'r') as f:
        config = json.load(f)
        
    tracked_stocks = config['tracking']['stocks']
    news_sources = config['news_sources']
    
    relevant_articles = []
    client = ollama.AsyncClient(host='http://127.0.0.1:8000')

    # 2. Scrape each RSS feed
    for source in news_sources:
        print(f"Scanning {source['name']}...")
        try:
            response = requests.get(source['rss_url'], timeout=10)
            if response.status_code != 200:
                continue
                
            # Parse XML tree
            root = ET.fromstring(response.content)
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ""
                description = item.find('description').text if item.find('description') is not None else ""
                link = item.find('link').text if item.find('link') is not None else ""
                
                combined_text = f"{title} {description}".upper()
                
                # Filter: Check if any of your tracked stock symbols appear in the article text
                for stock in tracked_stocks:
                    if stock in combined_text:
                        relevant_articles.append({
                            "stock": stock,
                            "source": source['name'],
                            "title": title,
                            "link": link
                        })
                        break # Avoid duplicating the same article if multiple keywords match
        except Exception as e:
            print(f"Error parsing {source['name']}: {e}")

    print(f"Found {len(relevant_articles)} articles relevant to your portfolio.")
    
    # 3. Score relevant articles using Qwen2
    scored_news_summary = []
    
    for article in relevant_articles[:10]: # Limit to top 10 to save context/time during testing
        print(f"Evaluating sentiment for: {article['title'][:50]}...")
        
        prompt = f"""
        Analyze the market sentiment of this headline for the stock {article['stock']}.
        Headline: "{article['title']}"
        
        Respond strictly in this exact format:
        SCORE: [integer from 1 to 10, where 1 is deeply bearish and 10 is deeply bullish]
        REASON: [one short sentence explaining why]
        """
        
        try:
            response = await client.generate(model='qwen2:1.5b', prompt=prompt, stream=False)
            ai_output = response['response'].strip()
            
            scored_news_summary.append(
                f"Stock: {article['stock']} | Source: {article['source']}\n"
                f"Headline: {article['title']}\n"
                f"AI Evaluation:\n{ai_output}\n"
                f"Link: {article['link']}\n"
                f"{'-'*40}"
            )
        except Exception as e:
            print(f"Failed to score article: {e}")
            
    return "\n".join(scored_news_summary)

async def run_nightly_analysis():
    global active_mcp_session
    print("\n[ROUTINE] Starting portfolio fetch and market news analysis...")
    
    try:
        # Step A: Fetch Live Holdings
        holdings_result = await active_mcp_session.call_tool("get_holdings", arguments={})
        holdings_text = holdings_result.content[0].text
        
        # Step B: Run the RSS feed scraper and AI evaluation pipeline
        news_intelligence = await fetch_and_score_news()
        
        # Step C: Pass both to the final renderer
        await analyze_with_ai_and_save(holdings_text, news_intelligence)
        
    except Exception as e:
        print(f"Failed execution loop: {e}")
        send_telegram_message("⚠️ Agent failed to execute nightly analysis routine.")

# ==========================================
# SCHEDULER WRAPPERS
# ==========================================
def job_morning():
    asyncio.create_task(generate_daily_login())

def job_night():
    asyncio.create_task(run_nightly_analysis())

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
async def main_loop():
    global active_mcp_session
    
    print("Starting Zerodha Kite MCP bridge...")
    server_params = StdioServerParameters(command="npx", args=["-y", "mcp-remote", "https://mcp.kite.trade/mcp"], env=dict(os.environ))
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            active_mcp_session = session
            print("✅ MCP Connection held open successfully.")
            
            # --- ON-DEMAND TEST PROMPT ---
            print("\n" + "="*50)
            choice = input("Do you want to run an ON-DEMAND TEST right now? (y/n): ").strip().lower()
            if choice == 'y':
                await generate_daily_login()
                input("\nPress Enter HERE in the terminal AFTER you have clicked the Telegram link and logged in...")
                await run_nightly_analysis()
                print("\n✅ Test complete! The agent will now enter background mode.")
            print("="*50 + "\n")
            
            # --- BACKGROUND DAEMON MODE ---
            print("🕒 Scheduling background jobs: Login @ 09:00 | Analysis @ 23:00")
            schedule.every().day.at("09:00").do(job_morning)
            schedule.every().day.at("23:00").do(job_night)
            
            print("Agent is now running quietly in the background. Press Ctrl+C to exit.")
            while True:
                schedule.run_pending()
                await asyncio.sleep(1)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())