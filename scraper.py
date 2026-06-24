import os
import sys
import requests
import pandas as pd
from datetime import datetime
import json

def fetch_top_traders():
    api_key = os.getenv("SCRAPER_API_KEY")
    target_url = "https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit=50"
    
    if not api_key:
        print("❌ Error: SCRAPER_API_KEY is missing from GitHub Secrets.")
        sys.exit(1)
        
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Routing request through ScraperAPI...")
    
    try:
        response = requests.get(proxy_url, params=params, timeout=60)
        print(f"Proxy Response Code: {response.status_code}")
        response.raise_for_status()
        return response.json()
        
    except Exception as e:
        print(f"❌ ScraperAPI proxy route failed: {e}")
        sys.exit(1)

def analyze_traders():
    data = fetch_top_traders()
    
    # DEBUG: Print the raw data structure
    print("\n=== DEBUG: RAW DATA STRUCTURE ===")
    print(f"Data type: {type(data)}")
    
    if isinstance(data, list) and len(data) > 0:
        print(f"First record keys: {list(data[0].keys())}")
        print(f"First record preview: {json.dumps(data[0], indent=2)[:500]}")
    elif isinstance(data, dict):
        print(f"Data keys: {list(data.keys())}")
        # Try to find a list inside
        for key, value in data.items():
            if isinstance(value, list) and len(value) > 0:
                print(f"Found list under key: '{key}'")
                print(f"First record keys: {list(value[0].keys())}")
                print(f"First record preview: {json.dumps(value[0], indent=2)[:500]}")
                data = value
                break
    
    print("\n=== END DEBUG ===\n")
    
    # Check if data is a list
    if not isinstance(data, list):
        print(f"❌ Unexpected data format: {type(data)}")
        sys.exit(1)
        
    if len(data) == 0:
        print("❌ No records found in the response.")
        sys.exit(1)
        
    print(f"Successfully retrieved {len(data)} records. Calculating custom metrics...")
    traders = []
    
    for idx, entry in enumerate(data):
        try:
            if not isinstance(entry, dict):
                continue
            
            # Try different possible field names
            wallet = (entry.get('proxyWallet') or 
                     entry.get('address') or 
                     entry.get('user') or 
                     entry.get('username') or 
                     entry.get('wallet') or
                     f"Unknown_{idx}")
            
            profit = float(entry.get('pnl') or entry.get('amount') or entry.get('profit') or 0)
            volume = float(entry.get('vol') or entry.get('volume') or entry.get('totalVolume') or 0)
            
            print(f"Record {idx}: wallet={wallet[:10]}..., profit={profit}, volume={volume}")
            
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
            print(f"Error processing record {idx}: {e}")
            continue
            
    if not traders:
        print("❌ Data processing yielded zero valid entries.")
        print("💡 Check the debug output above to see the actual field names.")
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
