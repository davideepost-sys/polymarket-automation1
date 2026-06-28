import os
import sys
import requests
import pandas as pd
from datetime import datetime
from urllib.parse import urlencode
import time

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

DATA_API = "https://data-api.polymarket.com"


def fetch_from_polymarket(target_url, query_params=None):
    """
    Fetch data from Polymarket via ScraperAPI proxy.

    Key fix: the full Polymarket URL (including its own query string) must be
    built first and passed as a *single* 'url' value to ScraperAPI.  If we let
    the `requests` library append ScraperAPI's params it will percent-encode
    '?' and '&' inside the target URL, producing a 404 on Polymarket's side.
    """
    # 1. Build the complete Polymarket target URL first
    if query_params:
        target_url = f"{target_url}?{urlencode(query_params)}"

    # 2. Build the ScraperAPI proxy URL with the full target URL as a plain string
    proxy_url = (
        f"https://api.scraperapi.com"
        f"?api_key={SCRAPER_API_KEY}"
        f"&url={requests.utils.quote(target_url, safe='')}"
        f"&premium=true"
    )

    try:
        response = requests.get(proxy_url, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"    ⚠️  Request failed: {e}")
        return None


def fetch_markets_traded(proxy_wallet):
    """
    Number of markets the trader has ever traded.
    Endpoint: GET /traded?user=<wallet>
    Response: { "user": "0x...", "traded": <int> }
    """
    data = fetch_from_polymarket(
        f"{DATA_API}/traded",
        query_params={"user": proxy_wallet}
    )
    if data and isinstance(data, dict):
        return int(data.get("traded", 0))
    return 0


def fetch_win_rate(proxy_wallet):
    """
    Derive win rate from resolved positions.

    Polymarket has no native winRate field.  We call /positions and look at
    every position whose market has resolved (redeemable=True or cashPnl set).
      wins   = resolved positions where cashPnl > 0
      losses = resolved positions where cashPnl <= 0
      win_rate = wins / (wins + losses) * 100
    """
    data = fetch_from_polymarket(
        f"{DATA_API}/positions",
        query_params={"user": proxy_wallet, "sizeThreshold": "0.01"}
    )

    if not data or not isinstance(data, list):
        return 0.0

    wins = 0
    losses = 0

    for pos in data:
        redeemable = pos.get("redeemable", False)
        cash_pnl   = pos.get("cashPnl")

        if redeemable or cash_pnl is not None:
            try:
                pnl_val = float(cash_pnl) if cash_pnl is not None else 0.0
                if pnl_val > 0:
                    wins += 1
                else:
                    losses += 1
            except (TypeError, ValueError):
                continue

    total_resolved = wins + losses
    if total_resolved == 0:
        return 0.0

    return round((wins / total_resolved) * 100, 1)


def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print("=" * 60)

    # ── Step 1: Leaderboard ────────────────────────────────────────
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top 10 traders...")
    leaderboard_data = fetch_from_polymarket(
        f"{DATA_API}/leaderboard",
        query_params={"timePeriod": "WEEK", "orderBy": "PNL", "limit": "10"}
    )

    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("❌ Failed to fetch leaderboard data.")
        sys.exit(1)

    print(f"✅ Retrieved {len(leaderboard_data)} traders.")

    # ── Step 2: Enrich each trader ─────────────────────────────────
    print("📊 Enriching trader data (markets traded + win rate)...")
    results = []

    for idx, entry in enumerate(leaderboard_data):
        wallet = (
            entry.get("proxyWallet")
            or entry.get("address")
            or entry.get("user")
        )
        if not wallet:
            print(f"  [{idx+1}] ⚠️  No wallet found, skipping.")
            continue

        username     = entry.get("userName") or entry.get("xUsername") or wallet[:8] + "..."
        pnl          = float(entry.get("pnl", 0))
        volume       = float(entry.get("vol", 0))

        if volume <= 0:
            print(f"  [{idx+1}] ⚠️  Zero volume for {wallet[:10]}, skipping.")
            continue

        profit_rate = round(pnl / volume, 4)

        print(f"  [{idx+1}/10] {username} | fetching trades...")
        markets_traded = fetch_markets_traded(wallet)
        time.sleep(0.4)

        print(f"  [{idx+1}/10] {username} | calculating win rate...")
        win_rate = fetch_win_rate(wallet)
        time.sleep(0.4)

        results.append({
            "Wallet":         wallet,
            "Name":           username,
            "Markets_Traded": markets_traded,
            "Win_Rate_%":     win_rate,
            "Profit_$":       round(pnl, 2),
            "Volume_$":       round(volume, 2),
            "Profit_Rate":    profit_rate,
        })

        print(
            f"         ✅ Traded: {markets_traded} | "
            f"Win Rate: {win_rate}% | Profit: ${pnl:,.0f}"
        )

    if not results:
        print("❌ No valid traders found.")
        sys.exit(1)

    # ── Step 3: Sort & save ────────────────────────────────────────
    df        = pd.DataFrame(results)
    df_sorted = df.sort_values(by="Win_Rate_%", ascending=False)

    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    df_sorted.to_csv(filename, index=False)

    print("\n" + "=" * 60)
    print(f"✅ Saved: {filename}  ({len(df_sorted)} traders)")
    print("\n=== TOP 10 TRADERS (sorted by Win Rate) ===")
    print(
        df_sorted[["Name", "Win_Rate_%", "Markets_Traded", "Profit_$", "Profit_Rate"]]
        .to_string(index=False)
    )
    print("=" * 60)
    print("✅ Analysis complete!")


if __name__ == "__main__":
    analyze_traders()
                
