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

APIFY_TOKEN = os.getenv("APIFY_API_TOKEN")

if not APIFY_TOKEN:
    print("❌ Error: APIFY_API_TOKEN is missing from GitHub Secrets.")
    print("   Get one at: https://apify.com/signup")
    sys.exit(1)

# ============================================================
# DO NOT CHANGE BELOW THIS LINE
# ============================================================

def fetch_top_traders(limit=50):
    """
    Fetch top traders from Apify Polymarket Whale Tracker.
    Free tier: 50 calls per day (plenty for daily runs).
    """
    
    # Apify actor endpoint for Polymarket Whale Tracker
    url = "https://api.apify.com/v2/acts/trudax~polymarket-whale-alerts/runs"
    
    headers = {
        "Content-Type": "application/json"
    }
    
    # Payload for the actor
    payload = {
        "apiKey": APIFY_TOKEN,
        "action": "get_top_traders",
        "limit": limit,
        "timeframe": "7d"  # 7 days = weekly leaderboard
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {limit} traders from Apify...")
    
    try:
        # Start the actor run
        print("  Starting Apify actor...")
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"  Response code: {response.status_code}")
        
        if response.status_code != 200:
            print(f"  ❌ Error: {response.text}")
            return None
        
        data = response.json()
        
        # Get the run ID
        if 'data' in data and 'id' in data['data']:
            run_id = data['data']['id']
            print(f"  ✅ Actor started, run ID: {run_id}")
            
            # Wait for the actor to finish
            print("  Waiting for results...")
            time.sleep(10)  # Give the actor time to process
            
            # Get the results
            result_url = f"https://api.apify.com/v2/acts/trudax~polymarket-whale-alerts/runs/{run_id}/results"
            result_response = requests.get(result_url, headers=headers, timeout=30)
            
            if result_response.status_code == 200:
                result_data = result_response.json()
                if 'data' in result_data and 'items' in result_data['data']:
                    traders = result_data['data']['items']
                    print(f"  ✅ Retrieved {len(traders)} traders")
                    return traders
                else:
                    print("  ❌ No traders found in results")
                    return None
            else:
                print(f"  ❌ Error fetching results: {result_response.status_code}")
                return None
        else:
            print("  ❌ Failed to start actor")
            return None
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def fetch_top_traders_simple(limit=50):
    """
    Alternative simpler method using Apify's dataset API
    This bypasses the actor run and uses pre-computed data
    """
    
    # Use the public dataset (already computed, instant results)
    url = f"https://api.apify.com/v2/datasets/trudax~polymarket-whale-alerts/items?limit={limit}&format=json"
    params = {
        "apiKey": APIFY_TOKEN
    }
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {limit} traders from Apify dataset...")
    
    try:
        response = requests.get(url, params=params, timeout=30)
        print(f"  Response code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                print(f"  ✅ Retrieved {len(data)} traders")
                return data
            else:
                print(f"  ❌ Unexpected data format")
                return None
        else:
            print(f"  ❌ Error: {response.text}")
            return None
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer (Apify)")
    print("=" * 60)
    
    # Try the simple method first (instant results)
    raw_data = fetch_top_traders_simple(limit=50)
    
    # If that fails, try the actor method
    if not raw_data:
        print("  Falling back to actor method...")
        raw_data = fetch_top_traders(limit=50)
    
    if not raw_data:
        print("❌ Failed to fetch trader data from Apify.")
        print("💡 The free tier gives 50 calls per day. Try again tomorrow.")
        sys.exit(1)
    
    print(f"✅ Retrieved {len(raw_data)} traders.")
    
    # Process the data
    results = []
    for trader in raw_data:
        # Extract data (field names may vary)
        wallet = trader.get('address') or trader.get('wallet') or trader.get('id', '')
        if not wallet:
            continue
        
        # Map fields (Apify may use different field names)
        name = trader.get('username') or trader.get('name') or trader.get('userName') or wallet[:8] + '...'
        win_rate = float(trader.get('winRate', 0) or trader.get('win_rate', 0) or 0)
        total_trades = int(trader.get('totalTrades', 0) or trader.get('trades', 0) or 0)
        pnl = float(trader.get('pnl', 0) or trader.get('profit', 0) or 0)
        volume = float(trader.get('volume', 0) or trader.get('vol', 0) or 0)
        profit_factor = float(trader.get('profitFactor', 0) or trader.get('profit_factor', 0) or 0)
        winning_trades = int(trader.get('winningTrades', 0) or trader.get('wins', 0) or 0)
        losing_trades = int(trader.get('losingTrades', 0) or trader.get('losses', 0) or 0)
        
        # Convert win rate from decimal to percentage if needed
        if win_rate < 1:
            win_rate = round(win_rate * 100, 1)
        
        results.append({
            'Wallet': wallet,
            'Name': name,
            'Win_Rate_%': win_rate,
            'Total_Trades': total_trades,
            'Winning_Trades': winning_trades,
            'Losing_Trades': losing_trades,
            'Total_PnL': round(pnl, 2),
            'Volume': round(volume, 2),
            'Profit_Factor': round(profit_factor, 2)
        })
    
    if not results:
        print("❌ No valid traders found.")
        sys.exit(1)
    
    # Create DataFrame and save
    df = pd.DataFrame(results)
    
    # Sort by Win Rate (highest first)
    df_sorted = df.sort_values(by='Win_Rate_%', ascending=False)
    
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    
    df_sorted.to_csv(filename, index=False)
    
    print("\n" + "=" * 60)
    print(f"✅ Success! File saved: {filename}")
    print(f"📊 Found {len(df_sorted)} traders")
    print(f"💡 Apify free tier: {50 - len(df_sorted)} calls remaining today")
    
    print("\n=== TOP 5 TRADERS BY WIN RATE ===")
    print(df_sorted.head(5)[['Name', 'Win_Rate_%', 'Total_Trades', 'Profit_Factor']].to_string(index=False))
    
    print("\n" + "=" * 60)
    print("✅ Analysis complete!")

if __name__ == "__main__":
    analyze_traders()
