import os
import sys
import requests
import pandas as pd
from datetime import datetime
import time

def fetch_top_traders(limit=20):
    """Fetch top traders from Polymarket (limited to save API credits)"""
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

def fetch_trader_details(wallet):
    """Fetch trader name and win rate from Polymarket"""
    api_key = os.getenv("SCRAPER_API_KEY")
    
    # Fetch user info (for name)
    name_url = f"https://data-api.polymarket.com/v1/user/{wallet}"
    name_params = {'api_key': api_key, 'url': name_url}
    
    # Fetch trade history (for win rate)
    trades_url = f"https://data-api.polymarket.com/trades?user={wallet}&limit=100"
    trades_params = {'api_key': api_key, 'url': trades_url}
    
    name = wallet[:8] + "..."  # Default: shortened address
    win_rate = 0
    trades_count = 0
    
    try:
        # Get trader name
        name_response = requests.get("http://api.scraperapi.com", params=name_params, timeout=30)
        if name_response.status_code == 200:
            name_data = name_response.json()
            if isinstance(name_data, dict):
                name = name_data.get('userName') or name_data.get('username') or wallet[:8] + "..."
    except:
        pass
    
    try:
        # Get trade history for win rate
        trades_response = requests.get("http://api.scraperapi.com", params=trades_params, timeout=30)
        if trades_response.status_code == 200:
            trades_data = trades_response.json()
            if isinstance(trades_data, list) and len(trades_data) > 0:
                trades_count = len(trades_data)
                winning_trades = sum(1 for t in trades_data if float(t.get('pnl', 0)) > 0)
                win_rate = round((winning_trades / trades_count) * 100, 1) if trades_count > 0 else 0
    except:
        pass
    
    return name, win_rate, trades_count

def analyze_traders():
    # Fetch top 20 traders only (saves API credits)
    raw_data = fetch_top_traders(limit=20)
    
    if not isinstance(raw_data, list):
        print("❌ Error: Invalid data format.")
        sys.exit(1)
        
    print(f"✅ Retrieved {len(raw_data)} traders. Fetching details...")
    
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
        
        # Fetch name and win rate
        print(f"  [{idx+1}/{total}] Fetching details for {wallet[:8]}...")
        name, win_rate, trade_count = fetch_trader_details(wallet)
        
        traders.append({
            'Wallet': wallet,  # Full address
            'Name': name,
            'Win_Rate_%': win_rate,
            'Trades_Sampled': trade_count,
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
    print(f"💡 Used ~{len(df_sorted) * 2 + 1} API credits")
    
    print("\n=== TOP 5 TRADERS BY PROFIT RATE ===")
    print(df_sorted.head(5)[['Name', 'Win_Rate_%', 'Profit_Rate', 'Volume']].to_string(index=False))

if __name__ == "__main__":
    analyze_traders()
