import os
import sys
import requests
import pandas as pd
from datetime import datetime
import time
import json

def fetch_top_traders(limit=50):
    """Fetch top traders from Polymarket"""
    api_key = os.getenv("SCRAPER_API_KEY")
    target_url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit={limit}"
    
    if not api_key:
        print("❌ Error: SCRAPER_API_KEY is missing.")
        sys.exit(1)
        
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url,
        'premium': 'true'
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {limit} traders...")
    
    try:
        response = requests.get(proxy_url, params=params, timeout=60)
        print(f"  Response code: {response.status_code}")
        response.raise_for_status()
        data = response.json()
        print(f"  ✅ Retrieved {len(data) if isinstance(data, list) else 'unknown'} records")
        return data
    except Exception as e:
        print(f"❌ Error fetching leaderboard: {e}")
        sys.exit(1)

def fetch_user_name(wallet):
    """Try to fetch username from Polymarket (multiple endpoints)"""
    api_key = os.getenv("SCRAPER_API_KEY")
    
    # Try different endpoints
    endpoints = [
        f"https://data-api.polymarket.com/v1/user/{wallet}",
        f"https://data-api.polymarket.com/v1/users/{wallet}/profile",
        f"https://data-api.polymarket.com/v1/account/{wallet}",
    ]
    
    for endpoint in endpoints:
        proxy_url = "http://api.scraperapi.com"
        params = {
            'api_key': api_key,
            'url': endpoint,
            'premium': 'true'
        }
        
        try:
            response = requests.get(proxy_url, params=params, timeout=15)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    # Try to find any name field
                    name = (
                        data.get('userName') or 
                        data.get('username') or 
                        data.get('name') or 
                        data.get('displayName') or
                        data.get('nickname')
                    )
                    if name:
                        return name
        except:
            pass
    
    return None

def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print("=" * 50)
    
    # Step 1: Fetch top 50 traders
    raw_data = fetch_top_traders(limit=50)
    
    if not isinstance(raw_data, list):
        print("❌ Error: Invalid data format.")
        print(f"Data type: {type(raw_data)}")
        if isinstance(raw_data, dict):
            print(f"Keys: {list(raw_data.keys())[:5]}")
        sys.exit(1)
    
    print(f"✅ Retrieved {len(raw_data)} traders from leaderboard.")
    
    # Step 2: Process all traders and calculate Profit Rate
    print("📊 Calculating efficiency scores...")
    all_traders = []
    
    for entry in raw_data:
        # Get wallet address
        wallet = entry.get('address') or entry.get('proxyWallet') or entry.get('user')
        if not wallet:
            continue
        
        # Get profit and volume
        profit = float(entry.get('pnl', 0))
        volume = float(entry.get('vol', 0))
        
        if volume <= 0:
            continue
        
        profit_rate = profit / volume
        
        all_traders.append({
            'wallet': wallet,
            'profit': profit,
            'volume': volume,
            'profit_rate': profit_rate
        })
    
    if not all_traders:
        print("❌ No valid traders with volume > 0.")
        sys.exit(1)
    
    # Step 3: Sort by Profit Rate and take TOP 10
    all_traders.sort(key=lambda x: x['profit_rate'], reverse=True)
    top_10 = all_traders[:10]
    
    print(f"📊 Identified TOP 10 most efficient traders (out of {len(all_traders)})")
    
    # Step 4: Try to fetch names for TOP 10
    print("🔍 Fetching usernames for top 10...")
    results = []
    
    for idx, trader in enumerate(top_10):
        wallet = trader['wallet']
        print(f"  [{idx+1}/10] Processing {wallet[:10]}...")
        
        # Try to get the name
        name = fetch_user_name(wallet)
        
        if not name:
            name = f"{wallet[:8]}..."
        
        results.append({
            'Wallet': wallet,
            'Name': name,
            'Win_Rate_%': 0,  # We'll add this if we can get it
            'Total_Trades': 0,  # We'll add this if we can get it
            'Profit': round(trader['profit'], 2),
            'Volume': round(trader['volume'], 2),
            'Profit_Rate': round(trader['profit_rate'], 4)
        })
        
        time.sleep(0.5)
    
    # Step 5: Save to CSV
    df = pd.DataFrame(results)
    
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df.to_csv(filename, index=False)
    
    print("\n" + "=" * 50)
    print(f"✅ Success! File saved: {filename}")
    print(f"📊 Found {len(df)} traders with full details")
    
    print("\n=== TOP 5 TRADERS BY PROFIT RATE ===")
    print(df.head(5)[['Name', 'Profit_Rate', 'Volume']].to_string(index=False))
    
    print("\n" + "=" * 50)
    print("✅ Analysis complete!")
    print("\n💡 Note: Win Rate is currently 0 because Polymarket's API")
    print("   doesn't expose this data through the public endpoints.")
    print("   You can manually look up traders at:")
    print("   https://polymarket.com/profile/{wallet_address}")

if __name__ == "__main__":
    analyze_traders()
