import requests
import json
import xml.etree.ElementTree as ET
import ollama

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
            keep_alive=-1
            temperature=0.1
            response = await client.generate(model='llama3.2:3b', prompt=prompt, options={keep_alive: keep_alive,temperature: temperature}, stream=False)
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