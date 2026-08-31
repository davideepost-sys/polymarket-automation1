import os
import csv
import json
from urllib.request import Request, urlopen

def get_ai_response_for_summary(prompt, system_prompt="Du är en expert på trading och Polymarket och sammanfattar dagens topp-traders."):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "Fel: GROQ_API_KEY saknas."

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 1024
    }

    req = Request(url, data=json.dumps(data).encode(), headers=headers)
    try:
        with urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"AI-fel: {str(e)}"

def analyze_latest_traders_for_digest():
    if not os.path.exists("latest_traders.csv"):
        return "Ingen data hittades att analysera för daglig sammanfattning."

    with open("latest_traders.csv", "r") as f:
        reader = csv.DictReader(f)
        traders = list(reader)[:5] # Analysera topp 5 för att spara tokens

    if not traders:
        return "Inga traders i latest_traders.csv att sammanfatta."

    trader_data = json.dumps(traders, indent=2)
    prompt = f"Här är dagens topp-traders från Polymarket:\n{trader_data}\n\nGe en kort, proffsig sammanfattning på svenska om vem som ser mest lovande ut och varför. Fokusera på WinRate och ProfitRate."
    
    return get_ai_response_for_summary(prompt)

if __name__ == "__main__":
    print(analyze_latest_traders_for_digest())
