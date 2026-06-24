import os
import sys
import requests
import pandas as pd
from datetime import datetime
import time
import json

def fetch_top_traders(limit=20):
    """Fetch top traders from Polymarket"""
    api_key = os.getenv("SCRAPER_API_KEY")
    target_url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit={limit}"
    
    if not api_key:
        print("❌ Error: SCRAPER_API_KEY is missing.")
        sys.exit(1)
        
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {limit} traders...")
    
    try:
        response = requests.get(proxy_url, params=params, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

def fetch_trader_data(wallet):
    """Fetch trader name and win rate in ONE API call"""
    api_key = os.getenv("SCRAPER_API_KEY")
    
    # Polymarket API endpoint for user stats
    # This gives us username, win rate, and trade count all at once!
    target_url = f"https://data-api.polymarket.com/v1/users/{wallet}/stats"
    
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url
    }
    
    try:
        response = requests.get(proxy_url, params=params, timeout=30)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict):
                name = data.get('userName') or data.get('username') or wallet[:8] + "..."
                
                # Win rate calculation
                total_trades = data.get('totalTrades', 0)
                winning_trades = data.get('winningTrades', 0)
                win_rate = round((winning_trades / total_trades) * 100, 1) if total_trades > 0 else 0
                
                return name, win_rate, total_trades
    except Exception as e:
        print(f"  ⚠️ Could not fetch stats: {e}")
    
    # Fallback: return shortened address
    return wallet[:8] + "...", 0, 0

def analyze_traders():
    # Fetch top 20 traders only
    raw_data = fetch_top_traders(limit=20)
    
    if not isinstance(raw_data, list):
        print("❌ Error: Invalid data format.")
        sys.exit(1)
        
    print(f"✅ Retrieved {len(raw_data)} traders. Fetching details...")
    print("💡 Using 1 API call per trader (≈20 total credits)")
    
    traders = []
    total = len(raw_data)
    
    for idx, entry in enumerate(raw_data):
        wallet = entry.get('proxyWallet') or entry.get('address')
        if not wallet:
            continue
            
        profit = float(entry.get('pnl', 0))
        volume = float(entry.get('vol', 0))
        
        if volume <= 0:
            continue
            
        profit_rate = round(profit / volume, 4)
        
        # ONE API call for name + win rate
        print(f"  [{idx+1}/{total}] Fetching data for {wallet[:8]}...")
        name, win_rate, trade_count = fetch_trader_data(wallet)
        
        traders.append({
            'Wallet': wallet,
            'Name': name,
            'Win_Rate_%': win_rate,
            'Total_Trades': trade_count,
            'Profit': round(profit, 2),
            'Volume': round(volume, 2),
            'Profit_Rate': profit_rate
        })
        
        # Small delay to avoid rate limiting
        time.sleep(0.5)
    
    if not traders:
        print("❌ No valid traders found.")
        sys.exit(1)
        
    df = pd.DataFrame(traders)
    
    # Sort by Profit Rate (most efficient first)
    df_sorted = df.sort_values(by='Profit_Rate', ascending=False)
    
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df_sorted.to_csv(filename, index=False)
    
    print(f"\n✅ Success! File saved: {filename}")
    print(f"📊 Found {len(df_sorted)} traders")
    print(f"💡 Used approximately {len(df_sorted) + 1} API credits")
    
    print("\n=== TOP 5 TRADERS BY PROFIT RATE ===")
    print(df_sorted.head(5)[['Name', 'Win_Rate_%', 'Profit_Rate', 'Volume']].to_string(index=False))

if __name__ == "__main__":
    analyze_traders()
