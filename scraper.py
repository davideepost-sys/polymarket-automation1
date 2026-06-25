import os
import sys
import requests
import pandas as pd
from datetime import datetime
import time

def fetch_top_traders(limit=50):
    """Fetch top traders from Polymarket"""
    api_key = os.getenv("SCRAPER_API_KEY")
    
    # TimePeriod=WEEK ensures the Volume and Profit we get are STRICTLY from the past week
    target_url = f"https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit={limit}"
    
    if not api_key:
        print("❌ Error: SCRAPER_API_KEY is missing.")
        sys.exit(1)
        
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {limit} traders for the week...")
    
    try:
        response = requests.get(proxy_url, params=params, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

def fetch_trader_data(wallet):
    """Fetch trader name and win rate"""
    api_key = os.getenv("SCRAPER_API_KEY")
    
    # Adding timePeriod=WEEK to attempt pulling weekly stats specifically
    target_url = f"https://data-api.polymarket.com/v1/users/{wallet}/stats?timePeriod=WEEK"
    
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
                # Flexible key checks in case Polymarket updates their response structure
                name = data.get('userName') or data.get('username') or data.get('name')
                
                total_trades = float(data.get('totalTrades') or data.get('tradesCount') or data.get('trades') or 0)
                winning_trades = float(data.get('winningTrades') or data.get('successfulTrades') or data.get('wins') or 0)
                
                win_rate = data.get('winRate')
                if win_rate is None:
                    win_rate = round((winning_trades / total_trades) * 100, 1) if total_trades > 0 else 0
                    
                return name, float(win_rate), int(total_trades)
    except Exception as e:
        print(f"  ⚠️ Could not fetch stats: {e}")
    
    return None, 0, 0

def analyze_traders():
    # 1. Fetch Top 50 traders (Only 1 API call)
    raw_data = fetch_top_traders(limit=50)
    
    if not isinstance(raw_data, list):
        print("❌ Error: Invalid data format.")
        sys.exit(1)
        
    print(f"✅ Retrieved {len(raw_data)} traders. Calculating base metrics locally to save credits...")
    
    basic_traders = []
    
    for idx, entry in enumerate(raw_data):
        # 🔥 CRITICAL FIX: The stats API requires the MAIN 'address', NOT the 'proxyWallet'.
        wallet = entry.get('address') or entry.get('user') or entry.get('proxyWallet')
        lb_name = entry.get('username') or entry.get('name')
        
        if not wallet:
            continue
            
        profit = float(entry.get('pnl', 0) or entry.get('amount', 0))
        volume = float(entry.get('vol', 0) or entry.get('volume', 0))
        
        if volume <= 0:
            continue
            
        profit_rate = round(profit / volume, 4)
        
        basic_traders.append({
            'Wallet': wallet,
            'Leaderboard_Name': lb_name,
            'Profit': round(profit, 2),
            'Volume': round(volume, 2),
            'Profit_Rate': profit_rate
        })
        
    if not basic_traders:
        print("❌ No valid traders found.")
        sys.exit(1)
        
    df = pd.DataFrame(basic_traders)
    
    # 2. Sort by Profit Rate FIRST
    df_sorted = df.sort_values(by='Profit_Rate', ascending=False)
    
    # 3. ONLY fetch detailed stats for the Top 10 to drastically cut API costs
    TOP_N = 10
    top_traders = df_sorted.head(TOP_N).copy()
    
    print(f"\n💡 Smart Scraping: Only fetching deep stats for the Top {TOP_N} most efficient traders.")
    print(f"This reduces your API calls from {len(raw_data)+1} down to just {TOP_N+1}!\n")
    
    final_traders = []
    total_requests_made = 1 # 1 for the initial leaderboard fetch
    
    for idx, row in enumerate(top_traders.itertuples()):
        print(f"  [{idx+1}/{TOP_N}] Fetching stats for {row.Wallet[:8]}...")
        
        name, win_rate, trade_count = fetch_trader_data(row.Wallet)
        total_requests_made += 1
        
        # Fallbacks for the name if the stats API comes up empty
        final_name = name if name else (row.Leaderboard_Name if row.Leaderboard_Name else row.Wallet[:8] + "...")
        
        final_traders.append({
            'Wallet': row.Wallet,
            'Name': final_name,
            'Win_Rate_%': win_rate,
            'Total_Trades': trade_count,
            'Profit': row.Profit,
            'Volume': row.Volume,
            'Profit_Rate': row.Profit_Rate
        })
        
        time.sleep(0.5)
        
    df_final = pd.DataFrame(final_traders)
    
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df_final.to_csv(filename, index=False)
    
    print(f"\n✅ Success! File saved: {filename}")
    print(f"📊 Processed the Top {TOP_N} traders.")
    print(f"💰 APIs Called: {total_requests_made} (Costing ~{total_requests_made * 10} ScraperAPI credits instead of 210+)")
    
    print("\n=== TOP 5 TRADERS BY PROFIT RATE ===")
    print(df_final.head(5)[['Name', 'Win_Rate_%', 'Profit_Rate', 'Volume']].to_string(index=False))

if __name__ == "__main__":
    analyze_traders()
