import requests

TOKEN, CHAT_ID = "8702448779:AAHC0WXrI8dsqbRkRNaYyjya3MLVifqmTSw", "1858329386"
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
res = requests.post(url, json={"chat_id": CHAT_ID, "text": "🚀 Pi Agent Connection Successful!"})
print("Sent! Status:", res.status_code, res.json().get("ok"))