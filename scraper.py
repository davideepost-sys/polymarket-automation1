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
ACTIVITY_PAGE_SIZE        = 500   # /activity: max trades counted per week (Polymarket allows up to 500/page)

# IMPORTANT: Polymarket's /closed-positions endpoint has a HARD MAXIMUM of 50
# results per page (the API rejects/clamps anything higher). To get a real
# sample for high-frequency traders we must paginate using `offset` instead
# of asking for one giant page.
CLOSED_POS_PAGE_SIZE      = 50    # API hard max — do not increase
MAX_CLOSED_POS_PAGES      = 6     # 6 x 50 = up to 300 resolved positions checked per trader


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
    Real win rate from positions that ACTUALLY RESOLVED in the past 7 days —
    not positions the trader simply sold early.

    Why this matters: Polymarket lets traders exit anytime by selling before
    a market resolves. /closed-positions returns BOTH early-sell exits and
    true market resolutions mixed together, with no boolean flag to tell
    them apart. A trader who panic-sold a bad bet at a small loss looks
    identical in the raw data to a trader who lost because the market
    resolved against them — but those are very different risk signals for
    copy-trading.

    The fix: a market that has genuinely resolved settles outcome tokens at
    EXACTLY $1.00 (won) or $0.00 (lost) — that's how Polymarket's oracle
    redemption works. An early sell almost never lands on exactly 1.0 or 0.0
    because it's exiting at whatever the live market price happens to be.
    So we use curPrice to separate real resolutions from early exits:

        curPrice == 1.0  →  market resolved YES, position WON  (real result)
        curPrice == 0.0  →  market resolved NO,  position LOST (real result)
        anything else    →  trader sold early — EXCLUDED from win rate,
                             since it's not a real win/loss outcome yet

    win_rate = real_wins / (real_wins + real_losses) * 100

    Returns: (win_rate <float or None>, real_resolved_count <int>,
              early_exit_count <int>)
    """
    real_wins = real_losses = 0
    early_exits = 0

    for page in range(MAX_CLOSED_POS_PAGES):
        offset = page * CLOSED_POS_PAGE_SIZE

        data = fetch_from_polymarket(
            f"{DATA_API}/closed-positions",
            query_params={
                "user":          proxy_wallet,
                "limit":         CLOSED_POS_PAGE_SIZE,
                "offset":        offset,
                "sortBy":        "TIMESTAMP",
                "sortDirection": "DESC",
            }
        )

        if not data or not isinstance(data, list) or len(data) == 0:
            break

        window_closed = False

        for pos in data:
            ts = pos.get("timestamp")
            if ts is None:
                continue

            if ts < WEEK_AGO_TS:
                window_closed = True
                break

            cur_price = pos.get("curPrice")
            if cur_price is None:
                continue

            try:
                price_val = float(cur_price)
            except (TypeError, ValueError):
                continue

            # Only count GENUINE resolutions — exact 1.0 or 0.0 settlement.
            # A small float tolerance handles any rounding from the API.
            if price_val >= 0.999:
                real_wins += 1
            elif price_val <= 0.001:
                real_losses += 1
            else:
                # Sold early at some in-between price — not a real
                # win/loss outcome, so we exclude it from the win rate.
                early_exits += 1

        if window_closed:
            break

        if len(data) < CLOSED_POS_PAGE_SIZE:
            break

        time.sleep(0.25)

    total_resolved = real_wins + real_losses
    if total_resolved == 0:
        return None, 0, early_exits

    return round((real_wins / total_resolved) * 100, 1), total_resolved, early_exits


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

        print(f"  [{idx+1}/10] {username} | calculating real win rate...")
        win_rate, resolved_count, early_exits = fetch_weekly_win_rate(wallet)
        time.sleep(0.3)

        trades_display = f"{weekly_trades}+" if hit_cap else str(weekly_trades)
        win_rate_display = win_rate if win_rate is not None else "N/A"

        # Flag thin samples so you never mistake a lucky streak for a track
        # record. 20+ genuinely RESOLVED trades is a reasonable minimum
        # before a win rate % is trustworthy enough for a copy-trade call.
        if resolved_count == 0:
            confidence = "NO DATA"
        elif resolved_count < 20:
            confidence = "LOW (thin sample)"
        else:
            confidence = "OK"

        results.append({
            "Wallet":            wallet,
            "Name":              username,
            "Weekly_Trades":     trades_display,
            "Win_Rate_%":        win_rate_display,   # real wins / real losses only
            "Resolved_Trades":   resolved_count,     # markets that ACTUALLY settled this week
            "Early_Exits":       early_exits,         # sold before resolution — excluded from win rate
            "Confidence":        confidence,
            "Profit_$":          round(pnl, 2),
            "Volume_$":          round(volume, 2),
            "Profit_Rate":       profit_rate,
        })

        print(
            f"         ✅ Weekly trades: {trades_display} | "
            f"Win Rate: {win_rate_display}% (resolved n={resolved_count}, early exits={early_exits}) | "
            f"Profit: ${pnl:,.0f}"
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
        df_sorted[["Name", "Weekly_Trades", "Win_Rate_%", "Resolved_Trades", "Early_Exits", "Confidence", "Profit_$", "Profit_Rate"]]
        .to_string(index=False)
    )
    print("=" * 60)
    print("✅ Analysis complete!")


if __name__ == "__main__":
    analyze_traders()
