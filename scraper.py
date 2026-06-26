import os
import sys
import requests
import pandas as pd
from datetime import datetime
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

def fetch_from_polymarket(target_url, params=None):
    """Fetch data from Polymarket using ScraperAPI proxy."""
    proxy_url = "http://api.scraperapi.com"
    scraper_params = {
        "api_key": SCRAPER_API_KEY,
        "url": target_url,
        "premium": "true",  # Required to bypass Cloudflare
    }
    # Append any extra query params into the target URL manually
    if params:
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        separator = "&" if "?" in target_url else "?"
        scraper_params["url"] = f"{target_url}{separator}{query_string}"

    try:
        response = requests.get(proxy_url, params=scraper_params, timeout=60)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"    ⚠️  Request failed: {e}")
        return None


def fetch_markets_traded(proxy_wallet):
    """
    Fetch the number of markets a user has traded via /traded endpoint.
    Returns an integer count, or 0 on failure.
    Docs: https://docs.polymarket.com/api-reference/misc/get-total-markets-a-user-has-traded
    Response shape: { "user": "0x...", "traded": <int> }
    """
    target_url = f"{DATA_API}/traded"
    data = fetch_from_polymarket(target_url, params={"user": proxy_wallet})
    if data and isinstance(data, dict):
        return int(data.get("traded", 0))
    return 0


def fetch_win_rate(proxy_wallet):
    """
    Calculate win rate from the user's resolved positions.

    Polymarket has no native winRate field — we derive it from /positions.
    A position is considered 'resolved' when its market is closed (redeemable=true
    or the position has a cashPnl value attached to a resolved market).

    We count:
      - wins:   resolved positions where cashPnl > 0
      - losses: resolved positions where cashPnl <= 0
      - win_rate = wins / (wins + losses) * 100

    Docs: https://docs.polymarket.com/api-reference/core (positions endpoint)
    Response items include: cashPnl, redeemable, title, outcome, ...
    """
    target_url = f"{DATA_API}/positions"
    data = fetch_from_polymarket(target_url, params={"user": proxy_wallet, "sizeThreshold": "0.01"})

    if not data or not isinstance(data, list):
        return 0.0

    wins = 0
    losses = 0

    for pos in data:
        # Only count resolved/redeemable positions for win rate
        # (open positions haven't settled yet so we skip them)
        redeemable = pos.get("redeemable", False)
        # Also check if the position has a realized cashPnl from a closed market
        cash_pnl = pos.get("cashPnl")

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

    # ── Step 1: Fetch top leaderboard traders ──────────────────────
    # The leaderboard already returns userName directly — no second
    # profile call is needed just to get the name.
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top 10 traders from leaderboard...")
    leaderboard_url = (
        f"{DATA_API}/leaderboard"
        "?timePeriod=WEEK&orderBy=PNL&limit=10"
    )
    leaderboard_data = fetch_from_polymarket(leaderboard_url)

    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("❌ Failed to fetch leaderboard data.")
        sys.exit(1)

    print(f"✅ Retrieved {len(leaderboard_data)} traders from leaderboard.")

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
            print(f"  [{idx+1}] ⚠️  No wallet found, skipping entry.")
            continue

        # Name is already on the leaderboard response as 'userName'
        username = entry.get("userName") or entry.get("xUsername") or wallet[:8] + "..."

        pnl = float(entry.get("pnl", 0))
        volume = float(entry.get("vol", 0))

        if volume <= 0:
            print(f"  [{idx+1}] ⚠️  Zero volume for {wallet[:10]}, skipping.")
            continue

        profit_rate = round(pnl / volume, 4)

        print(f"  [{idx+1}/10] {username} | Fetching markets traded...")
        markets_traded = fetch_markets_traded(wallet)
        time.sleep(0.4)  # Avoid hammering the API / ScraperAPI quota

        print(f"         {username} | Calculating win rate...")
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
            f"         ✅ Done | Traded: {markets_traded} | "
            f"Win Rate: {win_rate}% | Profit: ${pnl:,.0f}"
        )

    if not results:
        print("❌ No valid traders found after enrichment.")
        sys.exit(1)

    # ── Step 3: Build DataFrame and sort ──────────────────────────
    df = pd.DataFrame(results)
    df_sorted = df.sort_values(by="Win_Rate_%", ascending=False)

    # ── Step 4: Save CSV ───────────────────────────────────────────
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    df_sorted.to_csv(filename, index=False)

    # ── Step 5: Print summary ──────────────────────────────────────
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

                