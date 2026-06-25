import os
import sys
import requests
import pandas as pd
from datetime import datetime
import time
import json

# ============================================================
# CONFIGURATION
# ============================================================

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

if not SCRAPER_API_KEY:
    print("❌ Error: SCRAPER_API_KEY is missing from GitHub Secrets.")
    sys.exit(1)

# ============================================================
# DO NOT CHANGE BELOW THIS LINE
# ============================================================

def fetch_from_polymarket(target_url):
    """Fetch data from Polymarket using ScraperAPI proxy"""
    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': SCRAPER_API_KEY,
        'url': target_url,
        'premium': 'true'  # Required to bypass Cloudflare
    }
    
    try:
        response = requests.get(proxy_url, params=params, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def fetch_trader_details(wallet):
    """
    Fetch trader details from Polymarket's user API.
    Returns: username, win_rate, total_trades
    """
    target_url = f"https://data-api.polymarket.com/v1/user/{wallet}"
    
    try:
        data = fetch_from_polymarket(target_url)
        if data and isinstance(data, dict):
            username = data.get('userName') or data.get('username') or wallet[:8] + '...'
            win_rate = data.get('winRate', 0)
            total_trades = data.get('totalTrades', 0)
            
            # Convert win rate to percentage if it's a decimal
            if isinstance(win_rate, float) and win_rate < 1:
                win_rate = round(win_rate * 100, 1)
            
            return username, win_rate, total_trades
    except:
        pass
    
    return wallet[:8] + '...', 0, 0

def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print("=" * 60)
    
    # Step 1: Fetch top 50 traders from leaderboard
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top 50 traders...")
    leaderboard_url = "https://data-api.polymarket.com/v1/leaderboard?timePeriod=WEEK&orderBy=PNL&limit=50"
    leaderboard_data = fetch_from_polymarket(leaderboard_url)
    
    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("❌ Failed to fetch leaderboard data.")
        sys.exit(1)
    
    print(f"✅ Retrieved {len(leaderboard_data)} traders from leaderboard.")
    
    # Step 2: Process each trader
    print("📊 Processing trader data...")
    results = []
    
    for idx, entry in enumerate(leaderboard_data):
        # Get wallet address
        wallet = entry.get('address') or entry.get('proxyWallet') or entry.get('user')
        if not wallet:
            continue
        
        # Get basic metrics
        profit = float(entry.get('pnl', 0))
        volume = float(entry.get('vol', 0))
        
        if volume <= 0:
            continue
        
        # Calculate profit rate (efficiency)
        profit_rate = round(profit / volume, 4)
        
        # Fetch user details (username, win_rate, total_trades)
        print(f"  [{idx+1}/50] Fetching details for {wallet[:10]}...")
        username, win_rate, total_trades = fetch_trader_details(wallet)
        
        results.append({
            'Wallet': wallet,
            'Name': username,
            'Win_Rate_%': win_rate,
            'Total_Trades': total_trades,
            'Profit': round(profit, 2),
            'Volume': round(volume, 2),
            'Profit_Rate': profit_rate
        })
        
        # Small delay to avoid rate limiting
        time.sleep(0.3)
    
    if not results:
        print("❌ No valid traders found.")
        sys.exit(1)
    
    # Step 3: Create DataFrame
    df = pd.DataFrame(results)
    
    # Sort by Win Rate (highest first)
    df_sorted = df.sort_values(by='Win_Rate_%', ascending=False)
    
    # Step 4: Save to CSV
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    df_sorted.to_csv(filename, index=False)
    
    print("\n" + "=" * 60)
    print(f"✅ Success! File saved: {filename}")
    print(f"📊 Found {len(df_sorted)} traders")
    
    print("\n=== TOP 5 TRADERS BY WIN RATE ===")
    print(df_sorted.head(5)[['Name', 'Win_Rate_%', 'Total_Trades', 'Profit']].to_string(index=False))
    
    print("\n" + "=" * 60)
    print("✅ Analysis complete!")

if __name__ == "__main__":
    analyze_traders()
