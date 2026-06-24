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
        print(f"❌ Error fetching leaderboard: {e}")
        sys.exit(1)

def fetch_trade_count(wallet):
    """Fetch total number of trades for a specific wallet from the past week."""
    api_key = os.getenv("SCRAPER_API_KEY")
    target_url = f"https://data-api.polymarket.com/trades?user={wallet}&limit=10000"

    proxy_url = "http://api.scraperapi.com"
    params = {
        'api_key': api_key,
        'url': target_url
    }

    try:
        response = requests.get(proxy_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return len(data)
        return 0
    except Exception as e:
        print(f"⚠️ Could not fetch trades for {wallet[:8]}...: {e}")
        return 0

def analyze_traders():
    raw_data = fetch_top_traders()

    if not isinstance(raw_data, list):
        print("❌ Error: Invalid data format from leaderboard.")
        sys.exit(1)

    print(f"✅ Retrieved {len(raw_data)} leaderboard records.")
    print("📊 Fetching trade counts for each trader (this takes ~30-45 seconds)...")

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

        profit_rate = profit / volume

        # NEW: Fetch trade count for this specific wallet
        trade_count = fetch_trade_count(wallet)
        trades_per_day = round(trade_count / 7, 1)  # Leaderboard is 7 days

        traders.append({
            'Wallet': wallet[:10] + '...',
            'Trades_7d': trade_count,
            'Trades_Per_Day': trades_per_day,
            'Profit': round(profit, 2),
            'Volume': round(volume, 2),
            'Profit_Rate': round(profit_rate, 4)
        })

        # Progress indicator
        print(f"  [{idx+1}/{total}] {wallet[:8]}... → {trade_count} trades")

    if not traders:
        print("❌ No valid traders found.")
        sys.exit(1)

    df = pd.DataFrame(traders)

    # OPTIONAL: Filter by trades per day (remove the # to enable)
    # df = df[(df['Trades_Per_Day'] >= 10) & (df['Trades_Per_Day'] <= 30)]

    df_sorted = df.sort_values(by='Profit_Rate', ascending=False)

    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"

    df_sorted.to_csv(filename, index=False)

    print(f"\n✅ Success! File saved: {filename}")
    print(f"📊 Found {len(df_sorted)} traders with trade data")

    print("\n=== TOP 5 TRADERS BY PROFIT RATE ===")
    print(df_sorted.head(5).to_string(index=False))

if __name__ == "__main__":
    analyze_traders()
