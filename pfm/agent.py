import asyncio
import os
import json
import time
import requests
import schedule
import ollama
import xml.etree.ElementTree as ET
from datetime import datetime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"
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

    # 2. Score Relevant Articles using local LLM
    scored_news_summary = []
    print("Evaluating article sentiment via Hailo-Ollama...")
    
    # Analyze a maximum of 5 articles to keep processing times crisp
    for article in relevant_articles[:5]:
        prompt = f"""
        Analyze the market sentiment of this headline for the stock {article['symbol']}.
        Headline: "{article['title']}"
        
        Respond strictly in this exact format:
        SCORE: [integer from 1 to 10, where 1 is deeply bearish and 10 is deeply bullish]
        REASON: [one short sentence explaining why]
        """
        try:
            response = await client.generate(model='qwen2:1.5b', prompt=prompt, stream=False)
            ai_output = response['response'].strip()
            
            scored_news_summary.append(
                f"Stock: {article['symbol']} | Source: {article['source']}\n"
                f"Headline: {article['title']}\n"
                f"AI Evaluation: {ai_output}\n"
                f"Link: {article['link']}\n"
                f"{'-'*40}"
            )
        except Exception as e:
            print(f"Failed to evaluate headline: {e}")
            
    return "\n".join(scored_news_summary)

# ==========================================
# MASTER LOGIC: SNAPSHOT & SYNTHESIS
# ==========================================
async def analyze_with_ai_and_save(holdings_text, news_intelligence):
    """Blends portfolio numbers and news analysis, generates markdown summary, and updates Telegram"""
    print("\n🧠 Synthesizing metrics and market news...")
    
    try:
        holdings_list = json.loads(holdings_text)
    except json.JSONDecodeError:
        print("Could not parse JSON payload.")
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
    
    print("Streaming master report generation from Qwen2...\n")
    client = ollama.AsyncClient(host='http://127.0.0.1:8000')
    
    full_response = ""
    print("📈 AI Analyst Integrated Report:\n" + "="*50)
    async for chunk in await client.generate(model='qwen2:1.5b', prompt=prompt, stream=True):
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

async def run_nightly_analysis():
    """Main daemon pipeline loop wrapper"""
    global active_mcp_session
    print("\n[ROUTINE] Initiating full nightly ingestion loop...")
    
    try:
        holdings_result = await active_mcp_session.call_tool("get_holdings", arguments={})
        holdings_text = holdings_result.content[0].text
        
        # Scrape and score live news
        news_intelligence = await fetch_and_score_news()
        
        # Build comprehensive dossier
        await analyze_with_ai_and_save(holdings_text, news_intelligence)
    except Exception as e:
        print(f"Execution failure: {e}")
        send_telegram_message("⚠️ Agent experienced an exception running the nightly analysis loop.")

# ==========================================
# SCHEDULER WRAPPERS & MAIN
# ==========================================
def job_morning():
    asyncio.create_task(generate_daily_login())

def job_night():
    asyncio.create_task(run_nightly_analysis())

async def main_loop():
    global active_mcp_session
    
    print("Starting Zerodha Kite MCP bridge...")
    server_params = StdioServerParameters(command="npx", args=["-y", "mcp-remote", "https://mcp.kite.trade/mcp"], env=dict(os.environ))
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            active_mcp_session = session
            print("✅ MCP Connection held open successfully.")
            
            print("\n" + "="*50)
            choice = input("Do you want to run an INTEGRATED ON-DEMAND TEST right now? (y/n): ").strip().lower()
            if choice == 'y':
                await generate_daily_login()
                input("\nPress Enter HERE in the terminal AFTER you have clicked the Telegram link and logged in...")
                await run_nightly_analysis()
                print("\n✅ Integrated test execution successfully complete.")
            print("="*50 + "\n")
            
            print("🕒 Scheduling background jobs: Login @ 09:00 | Ingestion & Analysis @ 23:00")
            schedule.every().day.at("09:00").do(job_morning)
            schedule.every().day.at("23:00").do(job_night)
            
            while True:
                schedule.run_pending()
                await asyncio.sleep(1)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_loop())