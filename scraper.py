import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone
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

# Rolling window: always the past 7 days from right now
NOW_TS      = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS = NOW_TS - (7 * 24 * 60 * 60)

# Hard caps so a single hyperactive trader can never blow up runtime/credits.
# 300 weekly trades and 100 closed positions is already a LOT of signal —
# anything beyond that doesn't meaningfully change a win-rate %.
MAX_ACTIVITY_PAGES   = 1     # /activity:  1 page x 500 = up to 500 weekly trades counted
ACTIVITY_PAGE_SIZE   = 500
MAX_CLOSED_POSITIONS = 100   # /closed-positions: capped, sorted newest-first


def fetch_from_polymarket(target_url, query_params=None):
    """
    Fetch JSON from a Polymarket endpoint via ScraperAPI.

    Builds the full target URL (incl. its own query string) ourselves, then
    percent-encodes the whole thing into the ScraperAPI call. This keeps
    requests' own param-encoding out of the picture, which is what caused
    the earlier 404s.
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


def fetch_weekly_trade_count(proxy_wallet):
    """
    Count BUY trades placed in the past 7 days.

    Uses GET /activity with type=TRADE and start/end as server-side filters
    (Polymarket filters these for us, so we don't fetch all-time data).

    Capped at MAX_ACTIVITY_PAGES x ACTIVITY_PAGE_SIZE = 500 trades.
    If a trader hits that cap we report "500+" worth of activity rather
    than paginate indefinitely and burn credits — at that volume they've
    already proven they're hyperactive, the exact count past 500 doesn't
    change your copy-trade decision.

    Returns: (count <int>, hit_cap <bool>)
    """
    data = fetch_from_polymarket(
        f"{DATA_API}/activity",
        query_params={
            "user":          proxy_wallet,
            "type":          "TRADE",
            "start":         WEEK_AGO_TS,
            "end":           NOW_TS,
            "limit":         ACTIVITY_PAGE_SIZE,
            "side":          "BUY",
            "sortBy":        "TIMESTAMP",
            "sortDirection": "DESC",
        }
    )

    if not data or not isinstance(data, list):
        return 0, False

    hit_cap = len(data) >= ACTIVITY_PAGE_SIZE
    return len(data), hit_cap


def fetch_weekly_win_rate(proxy_wallet):
    """
    Win rate from CLOSED (resolved) positions in the past 7 days.

    Uses GET /closed-positions sorted by TIMESTAMP DESC (newest first),
    capped at MAX_CLOSED_POSITIONS. Because results come back newest-first,
    we can stop as soon as we see a position older than our 7-day window —
    no need to fetch the trader's whole history.

    win  = realizedPnl > 0
    loss = realizedPnl <= 0
    win_rate = wins / (wins + losses) * 100

    If the trader has zero closed positions in the window (e.g. everything
    they hold is still open / unresolved), we return None so the caller can
    report "N/A" instead of a misleading 0%.
    """
    data = fetch_from_polymarket(
        f"{DATA_API}/closed-positions",
        query_params={
            "user":          proxy_wallet,
            "limit":         MAX_CLOSED_POSITIONS,
            "sortBy":        "TIMESTAMP",
            "sortDirection": "DESC",
        }
    )

    if not data or not isinstance(data, list):
        return None, 0

    wins = losses = 0

    for pos in data:
        ts = pos.get("timestamp")
        if ts is None:
            continue

        # Results are newest-first: once we hit something older than our
        # window, every position after it is also older — stop counting.
        if ts < WEEK_AGO_TS:
            break

        realized_pnl = pos.get("realizedPnl")
        if realized_pnl is None:
            continue

        try:
            pnl_val = float(realized_pnl)
        except (TypeError, ValueError):
            continue

        if pnl_val > 0:
            wins += 1
        else:
            losses += 1

    total = wins + losses
    if total == 0:
        return None, 0

    return round((wins / total) * 100, 1), total


def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print(f"   Rolling window: {datetime.utcfromtimestamp(WEEK_AGO_TS).strftime('%Y-%m-%d')} "
          f"→ {datetime.utcfromtimestamp(NOW_TS).strftime('%Y-%m-%d')} (7 days)")
    print("=" * 60)

    # ── Step 1: Leaderboard (rolling 7-day PNL) ───────────────────
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top 10 traders...")
    leaderboard_data = fetch_from_polymarket(
        f"{DATA_API}/v1/leaderboard",
        query_params={"timePeriod": "WEEK", "orderBy": "PNL", "limit": "10"}
    )

    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("❌ Failed to fetch leaderboard data.")
        sys.exit(1)

    print(f"✅ Retrieved {len(leaderboard_data)} traders.")

    # ── Step 2: Enrich each trader (exactly 2 calls each, capped) ──
    print("📊 Enriching trader data (weekly trades + weekly win rate)...")
    results = []

    for idx, entry in enumerate(leaderboard_data):
        wallet = entry.get("proxyWallet")
        if not wallet:
            print(f"  [{idx+1}] ⚠️  No proxyWallet found, skipping.")
            continue

        username = entry.get("userName") or entry.get("xUsername") or wallet[:8] + "..."
        pnl      = float(entry.get("pnl", 0))
        volume   = float(entry.get("vol", 0))

        if volume <= 0:
            print(f"  [{idx+1}] ⚠️  Zero volume for {wallet[:10]}, skipping.")
            continue

        profit_rate = round(pnl / volume, 4)

        print(f"  [{idx+1}/10] {username} | fetching weekly trades...")
        weekly_trades, hit_cap = fetch_weekly_trade_count(wallet)
        time.sleep(0.3)

        print(f"  [{idx+1}/10] {username} | calculating weekly win rate...")
        win_rate, sample_size = fetch_weekly_win_rate(wallet)
        time.sleep(0.3)

        trades_display = f"{weekly_trades}+" if hit_cap else str(weekly_trades)
        win_rate_display = win_rate if win_rate is not None else "N/A"

        results.append({
            "Wallet":          wallet,
            "Name":            username,
            "Weekly_Trades":   trades_display,
            "Win_Rate_%":      win_rate_display,
            "Win_Sample_Size": sample_size,   # how many resolved trades the % is based on
            "Profit_$":        round(pnl, 2),
            "Volume_$":        round(volume, 2),
            "Profit_Rate":     profit_rate,
        })

        print(
            f"         ✅ Weekly trades: {trades_display} | "
            f"Win Rate: {win_rate_display}% (n={sample_size}) | Profit: ${pnl:,.0f}"
        )

    if not results:
        print("❌ No valid traders found.")
        sys.exit(1)

    # ── Step 3: Sort & save ────────────────────────────────────────
    df = pd.DataFrame(results)
    # Sort by win rate, pushing N/A (string) to the bottom
    df["_sort_key"] = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df_sorted = df.sort_values(by="_sort_key", ascending=False, na_position="last").drop(columns="_sort_key")

    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    df_sorted.to_csv(filename, index=False)

    print("\n" + "=" * 60)
    print(f"✅ Saved: {filename}  ({len(df_sorted)} traders)")
    print(f"   Window: past 7 days  |  Run date: {date_str}")
    print("\n=== TOP 10 TRADERS (sorted by Win Rate) ===")
    print(
        df_sorted[["Name", "Weekly_Trades", "Win_Rate_%", "Win_Sample_Size", "Profit_$", "Profit_Rate"]]
        .to_string(index=False)
    )
    print("=" * 60)
    print("✅ Analysis complete!")


if __name__ == "__main__":
    analyze_traders()
