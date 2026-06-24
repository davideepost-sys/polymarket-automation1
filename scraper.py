import os
import sys
import requests
import pandas as pd
from datetime import datetime

def fetch_top_traders():
    # Retrieve the secret key securely from GitHub Environment Variables
    api_key = os.getenv("SCRAPER_API_KEY")
    
    target_url = "https://lb-api.polymarket.com/leaderboard?window=1w&limit=100&sortBy=volume"
    
    if not api_key:
        print("❌ Error: SCRAPER_API_KEY is missing from GitHub Secrets.")
        sys.exit(1)
        
    # Format the request to route through ScraperAPI
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Routing request through ScraperAPI tunnel...")
    
    try:
        response = requests.get(proxy_url, params=params, timeout=30)
        print(f"Proxy Response Code: {response.status_code}")
        response.raise_for_status()
        return response.json()
        
    except Exception as e:
        print(f"❌ ScraperAPI proxy route failed: {e}")
        sys.exit(1)

def analyze_traders():
    data = fetch_top_traders()
    
    if not isinstance(data, list):
        print(f"❌ Unexpected API format: {type(data)}")
        sys.exit(1)
        
    print(f"Successfully retrieved {len(data)} records. Calculating custom metrics...")
    traders = []
    
    for idx, entry in enumerate(data):
        try:
            wallet = entry.get('address') or entry.get('user') or f"Unknown_{idx}"
            profit = float(entry.get('amount') or 0)
            volume = float(entry.get('volume') or 0)
            
            if volume <= 0:
                continue
                
            profit_rate = profit / volume
            
            traders.append({
                'Wallet': wallet,
                'Profit': profit,
                'Volume': volume,
                'Profit_Rate': profit_rate
            })
        except Exception as e:
            continue
            
    if not traders:
        print("❌ Data processing yielded zero valid entries.")
        sys.exit(1)
        
    df = pd.DataFrame(traders)
    df_sorted = df.sort_values(by='Profit_Rate', ascending=False)
    
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df_sorted.to_csv(filename, index=False)
    print(f"✅ Sheet compiled successfully: {filename}")
    
    print("\n=== TOP 5 MOST EFFICIENT TRADERS ===")
    print(df_sorted.head(5).to_string(index=False))

if __name__ == "__main__":
    analyze_traders()
