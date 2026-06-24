import os
import sys
import requests
import pandas as pd
from datetime import datetime

def fetch_top_traders():
    # Matches your exact GitHub Secret name
    api_key = os.getenv("SCRAPER_API_KEY")
    target_url = "https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit=50"    
    if not api_key:
        print("❌ Error: SCRAPER_API_KEY is missing from GitHub Secrets.")
        sys.exit(1)
        
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url,
        'premium': 'true'  # 🔥 Forces residential proxies to bypass Cloudflare masking
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Routing request through ScraperAPI Premium Tunnel...")
    
    try:
        # Bumping timeout to 60s because residential handshakes take a few moments
        response = requests.get(proxy_url, params=params, timeout=60)
        print(f"Proxy Response Code: {response.status_code}")
        response.raise_for_status()
        return response.json()
        
    except Exception as e:
        print(f"❌ ScraperAPI proxy route failed: {e}")
        sys.exit(1)

def analyze_traders():
    data = fetch_top_traders()
    
    if isinstance(data, dict):
        print(f"ℹ️ API returned a dictionary container. Keys found: {list(data.keys())}")
        list_found = False
        for key, value in data.items():
            if isinstance(value, list):
                print(f"👉 Automatically extracting data list from key: '{key}'")
                data = value
                list_found = True
                break
        if not list_found:
            print(f"❌ Could not find a data list inside the dictionary response.")
            sys.exit(1)
            
    if not isinstance(data, list):
        print(f"❌ Unexpected data format after processing: {type(data)}")
        sys.exit(1)
        
    print(f"Successfully retrieved {len(data)} records. Calculating custom metrics...")
    traders = []
    
    for idx, entry in enumerate(data):
        try:
            if not isinstance(entry, dict):
                continue
                
            wallet = entry.get('address') or entry.get('user') or entry.get('username') or f"Unknown_{idx}"
            profit = float(entry.get('amount') or entry.get('pnl') or 0)
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
