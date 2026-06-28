import os
import sys
import requests
import pandas as pd
from datetime import datetime
from urllib.parse import urlencode, quote
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
    Fetch JSON from a Polymarket endpoint via ScraperAPI.

    ScraperAPI requires the full target URL (including its own query string)
    to be passed as a single pre-encoded 'url' parameter.  We build that URL
    ourselves with urlencode() and then percent-encode the whole thing into
    the ScraperAPI query string — so the requests library has nothing left
    to re-encode and cannot corrupt the '?' or '&' separators.
    """
    if query_params:
        target_url = f"{target_url}?{urlencode(query_params)}"

    proxy_url = (
        f"https://api.scraperapi.com"
        f"?api_key={SCRAPER_API_KEY}"
        f"&url={quote(target_url, safe='')}"
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
    GET /traded?user=<wallet>
    Official docs: /api-spec/data-openapi.yaml  path: /traded
    Response shape: { "user": "0x...", "traded": <int> }
    Note: no /v1/ prefix — this endpoint lives at the root.
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
    Derive win rate from the user's current positions.

    Polymarket has no native winRate field anywhere in its public API.
    We call GET /positions and count only resolved positions:
      - redeemable=True  →  market has settled, we can redeem
      - cashPnl is set   →  realised P&L is available

    wins   = resolved positions where cashPnl > 0
    losses = resolved positions where cashPnl <= 0
    win_rate = wins / (wins + losses) * 100

    Official docs: /api-spec/data-openapi.yaml  path: /positions
    Note: no /v1/ prefix — this endpoint lives at the root.
    """
    data = fetch_from_polymarket(
        f"{DATA_API}/positions",
        query_params={
            "user": proxy_wallet,
            "sizeThreshold": "0.01",
            "limit": "500"
        }
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

    total = wins + losses
    return round((wins / total) * 100, 1) if total > 0 else 0.0


def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print("=" * 60)

    # ── Step 1: Leaderboard ────────────────────────────────────────
    # Official path: GET /v1/leaderboard  (note the /v1/ prefix!)
    # Valid timePeriod values: DAY | WEEK | MONTH | ALL
    # Valid orderBy values:    PNL | VOL
    # Max limit: 50
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top 10 traders...")
    leaderboard_data = fetch_from_polymarket(
        f"{DATA_API}/v1/leaderboard",
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
        # Leaderboard returns proxyWallet (not 'address' or 'user')
        wallet = entry.get("proxyWallet")
        if not wallet:
            print(f"  [{idx+1}] ⚠️  No proxyWallet found, skipping.")
            continue

        # userName is returned directly on the leaderboard entry
        username = entry.get("userName") or entry.get("xUsername") or wallet[:8] + "..."
        pnl      = float(entry.get("pnl", 0))
        volume   = float(entry.get("vol", 0))

        if volume <= 0:
            print(f"  [{idx+1}] ⚠️  Zero volume for {wallet[:10]}, skipping.")
            continue

        profit_rate = round(pnl / volume, 4)

        print(f"  [{idx+1}/10] {username} | fetching markets traded...")
        markets_traded = fetch_markets_traded(wallet)
        time.sleep(0.5)

        print(f"  [{idx+1}/10] {username} | calculating win rate...")
        win_rate = fetch_win_rate(wallet)
        time.sleep(0.5)

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
