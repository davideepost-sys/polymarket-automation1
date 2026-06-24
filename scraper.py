import os
import sys
import requests
import pandas as pd
from datetime import datetime

def fetch_top_traders():
    api_key = os.getenv("SCRAPER_API_KEY")
    target_url = "https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit=50"
    
    if not api_key:
        print("❌ Error: SCRAPER_API_KEY is missing.")
        sys.exit(1)
        
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching leaderboard data...")
    
    try:
        response = requests.get(proxy_url, params=params, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

def analyze_traders():
    raw_data = fetch_top_traders()
    
    if not isinstance(raw_data, list):
        print("❌ Error: Invalid data format.")
        sys.exit(1)
        
    print(f"✅ Retrieved {len(raw_data)} records. Processing...")
    
    # Aggregate by wallet
    traders_dict = {}
    
    for entry in raw_data:
        wallet = entry.get('proxyWallet') or entry.get('address')
        if not wallet:
            continue
            
        profit = float(entry.get('pnl', 0))
        volume = float(entry.get('vol', 0))
        
        if wallet not in traders_dict:
            traders_dict[wallet] = {
                'profit': 0,
                'volume': 0,
                'trades': 0
            }
        
        traders_dict[wallet]['profit'] += profit
        traders_dict[wallet]['volume'] += volume
        traders_dict[wallet]['trades'] += 1
    
    # Build final list
    traders = []
    for wallet, data in traders_dict.items():
        profit = data['profit']
        volume = data['volume']
        trades = data['trades']
        
        if volume <= 0:
            continue
            
        profit_rate = profit / volume
        
        traders.append({
            'Wallet': wallet,
            'Trades': trades,
            'Profit': round(profit, 2),
            'Volume': round(volume, 2),
            'Profit_Rate': round(profit_rate, 4)
        })
    
    if not traders:
        print("❌ No valid traders found.")
        sys.exit(1)
        
    df = pd.DataFrame(traders)
    df_sorted = df.sort_values(by='Profit_Rate', ascending=False)
    
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df_sorted.to_csv(filename, index=False)
    
    print(f"✅ Success! File saved: {filename}")
    print(f"📊 Found {len(df_sorted)} unique traders")
    
    print("\n=== TOP 5 TRADERS BY PROFIT RATE ===")
    print(df_sorted.head(5).to_string(index=False))

if __name__ == "__main__":
    analyze_traders()
