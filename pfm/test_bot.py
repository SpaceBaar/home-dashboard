import os
import requests
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TOKEN, CHAT_ID = TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
res = requests.post(url, json={"chat_id": CHAT_ID, "text": "🚀 Pi Agent Connection Successful!"})
print("Sent! Status:", res.status_code, res.json().get("ok"))