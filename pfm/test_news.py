import json
import requests
import xml.etree.ElementTree as ET
import asyncio
import ollama

async def test_news_pipeline():
    print("📰 Fetching market news from RSS feeds...\n")
    
    with open('config.json', 'r') as f:
        config = json.load(f)
        
    tracked_entities = config['tracking']['stocks']
    news_sources = config['news_sources']
    
    relevant_articles = []
    
    # 1. Scrape and Filter
    for source in news_sources:
        print(f"Scanning {source['name']}...")
        try:
            # We use a user-agent header because some news sites block raw python requests
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(source['rss_url'], headers=headers, timeout=10)
            
            if response.status_code != 200:
                print(f"  -> Failed: HTTP {response.status_code}")
                continue
                
            root = ET.fromstring(response.content)
            for item in root.findall('.//item'):
                title = item.find('title').text if item.find('title') is not None else ""
                desc = item.find('description').text if item.find('description') is not None else ""
                
                # Clean up description (sometimes it contains HTML tags)
                combined_text = f"{title} {desc}".upper()
                
                # Check against keywords
                for entity in tracked_entities:
                    for keyword in entity['keywords']:
                        if keyword.upper() in combined_text:
                            relevant_articles.append({
                                "symbol": entity['symbol'],
                                "title": title,
                                "source": source['name']
                            })
                            break # Move to next article to avoid duplicates
                            
        except Exception as e:
            print(f"  -> Error parsing feed: {e}")

    print(f"\n✅ Found {len(relevant_articles)} articles relevant to your portfolio.")
    
    if not relevant_articles:
        print("No news found for your specific holdings today.")
        return

    # 2. Score with Qwen2
    client = ollama.AsyncClient(host='http://127.0.0.1:8000')
    print("\n🧠 Passing articles to Hailo-Ollama for Sentiment Scoring:\n")
    
    for idx, article in enumerate(relevant_articles[:5]): # Test first 5 only
        print(f"--- Article {idx+1} ---")
        print(f"Stock: {article['symbol']} | Source: {article['source']}")
        print(f"Headline: {article['title']}")
        
        prompt = f"""
        Analyze the market sentiment of this headline for the stock {article['symbol']}.
        Headline: "{article['title']}"
        
        Respond strictly in this exact format:
        SCORE: [integer from 1 to 10, where 1 is deeply bearish/negative and 10 is deeply bullish/positive. 5 is neutral.]
        REASON: [one short sentence explaining why]
        """
        
        try:
            keep_alive=-1
            temperature=0.1
            response = await client.generate(model='llama3.2:3b', prompt=prompt, options={keep_alive: keep_alive,temperature: temperature}, stream=False)
            print(f"\n{response['response'].strip()}\n")
        except Exception as e:
            print(f"\nFailed to score: {e}\n")

if __name__ == "__main__":
    asyncio.run(test_news_pipeline())