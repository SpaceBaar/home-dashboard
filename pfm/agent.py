import asyncio
import os
import json
import re
import time
import requests
import schedule
import ollama
import xml.etree.ElementTree as ET
from datetime import datetime
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
active_mcp_session = None
last_update_id = 0
keep_alive=-1
temperature=0.1
# qwen2.5-instruct is instruction-tuned (follows structured formats reliably)
# and at 1.5B loads in ~half the time of llama3.2:3b on the Hailo NPU.
LLM_MODEL = 'qwen2.5-instruct:1.5b'

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

async def probe_session():
    """Checks whether the current Kite token is still valid by attempting get_holdings.
    Returns the raw holdings text on success, or None if auth is required.

    IMPORTANT: Kite MCP returns plain-text error messages (e.g. 'Please log in
    first using the login tool') rather than raising exceptions for unauth requests.
    We must validate the content is actual JSON data, not an error string."""
    global active_mcp_session
    if active_mcp_session is None:
        return None
    try:
        result = await active_mcp_session.call_tool("get_holdings", arguments={})
        text = result.content[0].text
        # extract_holdings_json returns None for non-JSON text like auth error messages
        if extract_holdings_json(text) is not None:
            return text  # Real holdings data — session is live
        print(f"  ℹ️  Kite responded: '{text[:80]}' — treating as auth required.")
        return None
    except Exception:
        return None  # Any failure → need a fresh login

def extract_holdings_json(text):
    """Robustly extract a holdings list from MCP tool output.
    Kite MCP may return JSON directly, wrapped in a code fence, or embedded in prose."""
    if not text:
        return None

    # 1. Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and 'data' in result:
            return result['data']  # Some MCP wrappers use {"data": [...]}
    except json.JSONDecodeError:
        pass

    # 2. Strip Markdown code fences (```json ... ``` or ``` ... ```)
    fenced = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if fenced:
        try:
            result = json.loads(fenced.group(1))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # 3. Find the first bare JSON array embedded anywhere in the text
    array_match = re.search(r'(\[\s*\{[\s\S]*?\}\s*\])', text)
    if array_match:
        try:
            result = json.loads(array_match.group(1))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None

# ==========================================
# MASTER LOGIC: SNAPSHOT & SYNTHESIS
# ==========================================
async def analyze_with_ai_and_save(holdings_text, news_intelligence):
    """Blends portfolio numbers and news analysis, generates markdown summary, and updates Telegram"""
    print("\n🧠 Synthesizing metrics and market news...")
    
    holdings_list = extract_holdings_json(holdings_text)
    if holdings_list is None:
        print("❌ Could not parse holdings from MCP response. Raw output:")
        print(repr(holdings_text[:500]))  # Print first 500 chars to reveal the actual format
        return

    summary = []
    total_investment = 0
    total_current = 0
    
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
    
    # Core prompt blending context together
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
    
    print("Streaming master report generation from Hailo-Ollama...\n")
    client = ollama.AsyncClient(host='http://127.0.0.1:8000')
    
    full_response = ""
    print("📈 AI Analyst Integrated Report:\n" + "="*50)
    # keep_alive=-1 holds the model in Hailo VRAM so it stays warm for this long generation
    async for chunk in await client.generate(
        model=LLM_MODEL,
        prompt=prompt,
        keep_alive=keep_alive,
        options={'temperature': temperature},
        stream=True
    ):
        print(chunk['response'], end='', flush=True)
        full_response += chunk['response']
    print("\n" + "="*50)

    # Save Markdown file locally
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"portfolio_analysis_{date_str}.md"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# Portfolio Integrated Analysis - {date_str}\n\n")
        f.write(f"**Total Invested:** ₹{total_investment:.2f}\n")
        f.write(f"**Current Value:** ₹{total_current:.2f}\n")
        f.write(f"**Overall P&L:** ₹{overall_pnl:.2f}\n\n")
        f.write("## Holdings Breakdown\n")
        for line in summary:
            f.write(f"{line}\n")
        f.write("\n## Contextual News Scored\n")
        f.write(news_intelligence)
        f.write("\n\n## AI Analysis & Insights\n\n")
        f.write(full_response)
        
    print(f"\n💾 Integrated report saved locally to: {filename}")
    
    status_msg = (
        f"📉 Daily Integrated Analysis Complete!\n"
        f"Total Portfolio Value: ₹{total_current:.2f}\n"
        f"Overall P&L: ₹{overall_pnl:.2f}\n\n"
        f"Check your local directory for the comprehensive markdown dossier."
    )
    send_telegram_message(status_msg)

# ==========================================
# NEWS PROCESSING PIPELINE
# ==========================================
async def fetch_and_score_news():
    """Scrapes RSS feeds from config.json, filters by keywords, and scores sentiment via Qwen2"""
    print("\n📰 Scraping market news from RSS feeds...")
    
    with open('config.json', 'r') as f:
        config = json.load(f)
        
    tracked_entities = config['tracking']['stocks']
    news_sources = config['news_sources']
    
    relevant_articles = []
    client = ollama.AsyncClient(host='http://127.0.0.1:8000')

    # 1. Fetch and Filter RSS Feeds
    for source in news_sources:
        print(f"Scanning {source['name']}...")
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(source['rss_url'], headers=headers, timeout=10)
            if response.status_code != 200:
                continue
                
            root = ET.fromstring(response.content)
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ""
                desc = item.find('description').text if item.find('description') is not None else ""
                link = item.find('link').text if item.find('link') is not None else ""
                
                combined_text = f"{title} {desc}".upper()
                
                # Verify match against keyword mappings
                for entity in tracked_entities:
                    for keyword in entity['keywords']:
                        if keyword.upper() in combined_text:
                            relevant_articles.append({
                                "symbol": entity['symbol'],
                                "title": title,
                                "source": source['name'],
                                "link": link
                            })
                            break
                    else:
                        continue
                    break # Break outer loop if keyword matched
        except Exception as e:
            print(f"  -> Error parsing {source['name']}: {e}")

    print(f"Found {len(relevant_articles)} articles matching your portfolio keywords today.")
    
    if not relevant_articles:
        return "No significant news found for your holdings today."

    # 2. Score all relevant articles in a SINGLE batched LLM call.
    # Batching is the key fix for Hailo: the model cold-loads exactly once,
    # then the NPU generates all scores in one continuous inference pass
    # instead of reloading for each article. Total latency drops from
    # (N × cold-load time) to (1 cold-load + 1 inference).
    articles_to_score = relevant_articles[:5]
    print(f"Evaluating {len(articles_to_score)} article(s) via Hailo-Ollama (single batched call)...")

    # Build a numbered list of headlines for the batch prompt
    numbered_headlines = "\n".join(
        f"{i+1}. [{a['symbol']}] \"{a['title']}\""
        for i, a in enumerate(articles_to_score)
    )

    batch_prompt = f"""You are a financial analyst. Score the market sentiment of each headline below.

For EACH headline respond on its own line in this exact format (no extra text):
[N] SCORE: <1-10> REASON: <one short sentence>

Where N is the headline number, 1 = deeply bearish, 10 = deeply bullish.

Headlines:
{numbered_headlines}
"""

    scored_news_summary = []
    try:
        print("  ⏳ Waiting for Hailo model (cold-load may take a few minutes)...")
        response = await asyncio.wait_for(
            client.generate(
                model=LLM_MODEL,
                prompt=batch_prompt,
                keep_alive=keep_alive,
                options={'temperature': temperature},
                stream=False
            ),
            timeout=900  # 15-minute ceiling: hailo cold-load (~7 min) + inference
        )
        print("  ✅ Batch scoring complete.")
        raw_output = response['response'].strip()

        # Parse "[N] SCORE: X REASON: ..." lines back into per-article results
        line_pattern = re.compile(
            r'\[(\d+)\]\s*SCORE:\s*(\d+)\s*REASON:\s*(.+)', re.IGNORECASE
        )
        parsed = {}
        for line in raw_output.splitlines():
            m = line_pattern.search(line)
            if m:
                idx, score, reason = int(m.group(1)) - 1, m.group(2), m.group(3).strip()
                parsed[idx] = f"SCORE: {score}\nREASON: {reason}"

        for i, article in enumerate(articles_to_score):
            ai_output = parsed.get(i, "Could not parse score from model output.")
            scored_news_summary.append(
                f"Stock: {article['symbol']} | Source: {article['source']}\n"
                f"Headline: {article['title']}\n"
                f"AI Evaluation: {ai_output}\n"
                f"Link: {article['link']}\n"
                f"{'-'*40}"
            )

    except asyncio.TimeoutError:
        print("  ⚠️  Batch scoring timed out after 15 minutes. Proceeding without news scores.")
    except Exception as e:
        print(f"Batch scoring failed: {e}")

    return "\n".join(scored_news_summary)

async def run_nightly_analysis(holdings_text=None):
    """Main daemon pipeline loop wrapper.
    Accepts optional pre-fetched holdings_text to avoid a redundant get_holdings
    call when the session was already probed (e.g. interactive on-demand mode)."""
    global active_mcp_session
    print("\n[ROUTINE] Initiating full nightly ingestion loop...")
    
    try:
        # Fetch holdings if not already provided by the caller
        if holdings_text is None:
            holdings_result = await active_mcp_session.call_tool("get_holdings", arguments={})
            holdings_text = holdings_result.content[0].text
        
        # Scrape and score live news
        news_intelligence = await fetch_and_score_news()
        
        # Build comprehensive dossier
        await analyze_with_ai_and_save(holdings_text, news_intelligence)
    except Exception as e:
        print(f"Execution failure: {e}")
        send_telegram_message("⚠️ Agent experienced an exception running the nightly analysis loop.")
        
async def listen_for_expenses():
    """Continuously polls Telegram for new text messages (expenses)"""
    global last_update_id
    print("📡 Expense listener active...")
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    
    while True:
        try:
            # timeout=20 creates a 'long poll' so we don't spam the API unnecessarily
            payload = {"offset": last_update_id + 1, "timeout": 20}
            
            # We use asyncio.to_thread so the synchronous requests library doesn't block the MCP bridge
            response = await asyncio.to_thread(requests.post, url, json=payload, timeout=25)
            data = response.json()
            
            if data.get("ok"):
                for result in data.get("result", []):
                    last_update_id = result["update_id"]
                    msg_text = result.get("message", {}).get("text", "")
                    
                    if msg_text:
                        # 1. Acknowledge receipt
                        send_telegram_message(f"💸 Logged: {msg_text}")
                        
                        # 2. Append to a local CSV file
                        with open("daily_expenses.csv", "a", encoding="utf-8") as f:
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            f.write(f"{timestamp},{msg_text}\n")
                            
        except Exception:
            # Silently handle network timeouts and loop again
            pass
            
        # Brief pause before checking again
        await asyncio.sleep(1)

# ==========================================
# MCP KEEPALIVE
# ==========================================
async def mcp_keepalive():
    """Pings the MCP server every 60s with a list_tools call to prevent
    Cloudflare from killing the idle SSE stream (CF drops streams after ~100s)."""
    while True:
        await asyncio.sleep(60)
        try:
            if active_mcp_session:
                await active_mcp_session.list_tools()
        except Exception:
            # Silently ignore; the bridge will log its own reconnect attempts
            pass

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
    # NOTE: Ensure your absolute path to npx is still here!
    server_params = StdioServerParameters(command="/home/spacebaar/.nvm/versions/node/v20.19.5/bin/npx", args=["-y", "mcp-remote", "https://mcp.kite.trade/mcp"], env=dict(os.environ))
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            active_mcp_session = session
            print("✅ MCP Connection held open successfully.")
            
            # --- CHECK FOR DAEMON FLAG ---
            is_daemon = '--daemon' in sys.argv
            
            if not is_daemon:
                print("\n" + "="*50)
                choice = input("Do you want to run an INTEGRATED ON-DEMAND TEST right now? (y/n): ").strip().lower()
                if choice == 'y':
                    # Probe the session before asking for a login; Kite tokens survive
                    # until midnight / 6 AM IST, so a same-day re-run won't need a new one.
                    print("\nChecking if existing Kite session is still valid...")
                    holdings_text = await probe_session()
                    if holdings_text is not None:
                        print("✅ Session still valid — skipping login.")
                    else:
                        print("🔐 Session expired or not found — requesting fresh login.")
                        await generate_daily_login()
                        input("\nPress Enter HERE in the terminal AFTER you have clicked the Telegram link and logged in...")
                        # Fetch holdings now that we have a fresh token
                        holdings_result = await active_mcp_session.call_tool("get_holdings", arguments={})
                        holdings_text = holdings_result.content[0].text
                    await run_nightly_analysis(holdings_text=holdings_text)
                    print("\n✅ Integrated test execution successfully complete.")
                print("="*50 + "\n")
            else:
                print("\n[DAEMON MODE] Bypassing interactive prompts.\n")
            
            print("🕒 Scheduling background jobs: Login @ 09:00 | Ingestion & Analysis @ 23:00")
            schedule.every().day.at("09:00").do(job_morning)
            schedule.every().day.at("23:00").do(job_night)

            # ACTIVATE BACKGROUND TASKS
            asyncio.create_task(listen_for_expenses())
            # Keep the MCP SSE stream alive so Cloudflare doesn't drop it
            asyncio.create_task(mcp_keepalive())
            
            print("Agent is now running quietly in the background. Press Ctrl+C to exit.")
            while True:
                schedule.run_pending()
                await asyncio.sleep(1)

if __name__ == "__main__":
    # asyncio.run() is the modern, correct entry point (Python 3.7+)
    asyncio.run(main_loop())