import os
import sys
import requests
import pandas as pd
from datetime import datetime
import time

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
        return response.json()
    except Exception as e:
        print(f"❌ Error fetching leaderboard: {e}")
        sys.exit(1)

def fetch_user_stats(wallet):
    """Fetch username and win rate for a specific wallet"""
    api_key = os.getenv("SCRAPER_API_KEY")
    
    # CORRECT endpoint!
    target_url = f"https://data-api.polymarket.com/v1/user/{wallet}"
    
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url,
        'premium': 'true'
    }
    
    try:
        response = requests.get(proxy_url, params=params, timeout=30)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                name = data.get('userName') or data.get('username') or data.get('name') or wallet[:8] + "..."
                
                win_rate = data.get('winRate', 0)
                total_trades = data.get('totalTrades', 0)
                
                # Convert decimal to percentage (0.45 → 45.0)
                if isinstance(win_rate, float) and win_rate < 1:
                    win_rate = round(win_rate * 100, 1)
                
                return name, win_rate, total_trades
        else:
            print(f"  ⚠️ API returned {response.status_code} for {wallet[:8]}...")
    except Exception as e:
        print(f"  ⚠️ Error fetching user data: {e}")
    
    return wallet[:8] + "...", 0, 0

def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print("=" * 50)
    
    # Step 1: Fetch top 50 traders
    raw_data = fetch_top_traders(limit=50)
    
    if not isinstance(raw_data, list):
        print("❌ Error: Invalid data format.")
        sys.exit(1)
    
    print(f"✅ Retrieved {len(raw_data)} traders from leaderboard.")
    
    # Step 2: Calculate Profit Rate for ALL traders
    print("📊 Calculating efficiency scores...")
    all_traders = []
    
    for entry in raw_data:
        wallet = entry.get('address') or entry.get('proxyWallet')
        if not wallet:
            continue
        
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
    print(f"💡 Will use {len(top_10) + 1} API credits total")
    
    # Step 4: Fetch user details for TOP 10
    print("🔍 Fetching usernames and win rates for top 10...")
    results = []
    
    for idx, trader in enumerate(top_10):
        wallet = trader['wallet']
        print(f"  [{idx+1}/10] Fetching details for {wallet[:10]}...")
        
        name, win_rate, total_trades = fetch_user_stats(wallet)
        
        results.append({
            'Wallet': wallet,
            'Name': name,
            'Win_Rate_%': win_rate,      # ← CORRECT column name!
            'Total_Trades': total_trades,
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
    print(f"💡 Used approximately {len(results) + 1} API credits")
    
    print("\n=== TOP 5 TRADERS BY PROFIT RATE ===")
    print(df.head(5)[['Name', 'Win_Rate_%', 'Profit_Rate', 'Volume']].to_string(index=False))
    
    print("\n" + "=" * 50)
    print("✅ Analysis complete!")

if __name__ == "__main__":
    analyze_traders()
