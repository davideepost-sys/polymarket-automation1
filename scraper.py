bash

cat > /mnt/user-data/outputs/scraper.py << 'PYEOF'
import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Rolling window: always the past 7 days from right now.
# Used ONLY for weekly trade count and weekly profit/volume (which come
# straight from Polymarket's own rolling-week leaderboard) — NOT for win
# rate anymore (see fetch_recent_win_rate for why).
NOW_TS      = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS = NOW_TS - (7 * 24 * 60 * 60)

# ── Trade-count filter band ─────────────────────────────────────
# Below MIN: too few trades this week to trust a win rate (could be 2/2 = 100%)
# Above MAX: bot-speed trading — a small starting balance can't survive the
#            variance of thousands of micro-trades before any edge plays out
MIN_WEEKLY_TRADES = 20
MAX_WEEKLY_TRADES = 500

LEADERBOARD_POOL_SIZE = 50   # screen the top 50 traders, not just top 10

# Polymarket's /closed-positions endpoint hard-caps at 50 results per page.
CLOSED_POS_PAGE_SIZE     = 50
WIN_RATE_LOOKBACK_PAGES  = 3    # 3 x 50 = up to 150 most recent resolved
                                 # positions checked — NOT date-limited.
                                 # See fetch_recent_win_rate() for why.
MIN_SAMPLE_FOR_CONFIDENCE = 20  # need at least this many resolved trades
                                 # before a win rate is considered trustworthy

# How many traders to process in parallel. Each one hits ScraperAPI's
# premium (Cloudflare-bypass) proxy, which is slow per-call — running them
# one at a time is what made earlier runs take 10+ minutes. Start at 5;
# raise it if your ScraperAPI plan supports more concurrent connections,
# lower it if you start seeing "Request failed" errors in the log (a sign
# you're hitting your plan's concurrency limit).
MAX_WORKERS = 5


def fetch_from_polymarket(target_url, query_params=None):
    """
    Fetch JSON from a Polymarket endpoint via ScraperAPI.

    We build the full target URL (including its own query string) ourselves,
    then percent-encode the whole thing into the ScraperAPI call. This keeps
    the requests library's own param-encoding out of the picture, which is
    what caused 404s earlier when '?' and '&' got double-encoded.
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


def count_weekly_trades(proxy_wallet, count_cap=600):
    """
    Counts BUY trades placed in the past 7 days via /activity, paginating
    until we run out of trades, or exceed count_cap (a little above
    MAX_WEEKLY_TRADES, so a 4,000-trade bot gets correctly classified as
    "over the limit" after just 1-2 pages instead of fetching everything).

    Returns: weekly_trade_count <int>
    """
    total = 0
    offset = 0
    page_size = 500

    while total < count_cap:
        data = fetch_from_polymarket(
            f"{DATA_API}/activity",
            query_params={
                "user":          proxy_wallet,
                "type":          "TRADE",
                "side":          "BUY",
                "start":         WEEK_AGO_TS,
                "end":           NOW_TS,
                "limit":         page_size,
                "offset":        offset,
                "sortBy":        "TIMESTAMP",
                "sortDirection": "DESC",
            }
        )

        if not data or not isinstance(data, list) or len(data) == 0:
            break

        total += len(data)

        if len(data) < page_size:
            break  # last page

        offset += page_size

    return total


def fetch_recent_win_rate(proxy_wallet):
    """
    Win rate based on the trader's most recent RESOLVED positions —
    deliberately NOT limited to the last 7 days.

    Why the change: most trades placed this week haven't resolved yet,
    since prediction markets often take days or weeks to settle. Strictly
    requiring the resolution to also fall within the last 7 days produced
    samples as small as 1-7 trades for highly active traders — nowhere
    near enough to trust for a copy-trade decision.

    Instead, we look at the trader's most recent ~150 resolved positions,
    however far back that naturally spans, which gives a far more reliable
    read on their real track record. We report how many days back the
    sample reaches (Sample_Span_Days) so you can still judge recency.

    Resolution rules (unchanged from before):
      curPrice == 1.0   → market resolved YES, position WON
      curPrice == 0.0   → market resolved NO,  position LOST
      curPrice ≈ 0.50   → PUSH (ambiguous resolution) — excluded entirely
      anything else     → EARLY EXIT (sold before resolution) — classified
                           as a win or loss by realizedPnl, but tracked in
                           its OWN bucket, separate from true resolutions

    Overall Win_Rate_% combines resolved + early-exit wins/losses (a
    profitable early exit is still a real trading decision that paid off),
    but every bucket is reported individually so you can see how much of
    the headline number comes from real market outcomes vs. active exits.

    Returns a dict with the full breakdown, including:
      sample_span_days — how many days back the resolved sample reaches
                          (None if no resolved positions were found at all)
    """
    resolved_wins = resolved_losses = 0
    early_exit_wins = early_exit_losses = 0
    pushes = 0
    win_amounts = []
    loss_amounts = []
    oldest_ts = None

    for page in range(WIN_RATE_LOOKBACK_PAGES):
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

        for pos in data:
            ts           = pos.get("timestamp")
            cur_price    = pos.get("curPrice")
            realized_pnl = pos.get("realizedPnl")

            if cur_price is None:
                continue

            try:
                price_val = float(cur_price)
                pnl_val   = float(realized_pnl) if realized_pnl is not None else 0.0
            except (TypeError, ValueError):
                continue

            if ts is not None:
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts

            if price_val >= 0.999:
                resolved_wins += 1
                win_amounts.append(pnl_val)
            elif price_val <= 0.001:
                resolved_losses += 1
                loss_amounts.append(pnl_val)
            elif 0.499 <= price_val <= 0.501:
                pushes += 1
            else:
                # Early exit — classified by actual realized PnL, but kept
                # in its own bucket so it's never confused with a true
                # market resolution.
                if pnl_val > 0:
                    early_exit_wins += 1
                    win_amounts.append(pnl_val)
                else:
                    early_exit_losses += 1
                    loss_amounts.append(pnl_val)

        if len(data) < CLOSED_POS_PAGE_SIZE:
            break  # last page, no more data to fetch

    total_wins   = resolved_wins + early_exit_wins
    total_losses = resolved_losses + early_exit_losses
    total        = total_wins + total_losses

    win_rate = round((total_wins / total) * 100, 1) if total > 0 else None
    avg_win  = round(sum(win_amounts) / len(win_amounts), 2) if win_amounts else 0.0
    avg_loss = round(sum(loss_amounts) / len(loss_amounts), 2) if loss_amounts else 0.0
    span_days = round((NOW_TS - oldest_ts) / 86400, 1) if oldest_ts else None

    return {
        "win_rate":           win_rate,
        "total_sample":       total,
        "resolved_wins":      resolved_wins,
        "resolved_losses":    resolved_losses,
        "early_exit_wins":    early_exit_wins,
        "early_exit_losses":  early_exit_losses,
        "pushes":             pushes,
        "avg_win":            avg_win,
        "avg_loss":           avg_loss,
        "sample_span_days":   span_days,
    }


def screen_trader(entry):
    """
    PHASE 1 worker — runs in a thread. Checks one leaderboard trader's
    weekly trade count and returns a trader dict if they fall within the
    MIN/MAX_WEEKLY_TRADES band, or None if they're filtered out.
    """
    wallet = entry.get("proxyWallet")
    if not wallet:
        return None

    username = entry.get("userName") or entry.get("xUsername") or wallet[:8] + "..."
    pnl      = float(entry.get("pnl", 0))
    volume   = float(entry.get("vol", 0))

    if volume <= 0:
        return None

    weekly_trades = count_weekly_trades(wallet, count_cap=MAX_WEEKLY_TRADES + 100)
    in_band = MIN_WEEKLY_TRADES <= weekly_trades <= MAX_WEEKLY_TRADES

    status = "✅ PASS" if in_band else "❌ filtered out"
    print(f"  {username:<20} {weekly_trades:>4} trades/wk  {status}")

    if not in_band:
        return None

    return {
        "Wallet":        wallet,
        "Name":          username,
        "Weekly_Trades": weekly_trades,
        "Profit_$":      round(pnl, 2),
        "Volume_$":      round(volume, 2),
        "Profit_Rate":   round(pnl / volume, 4),
    }


def analyze_one_survivor(trader):
    """
    PHASE 2 worker — runs in a thread. Calculates the full win-rate
    breakdown for one trader who survived Phase 1 screening.
    """
    wallet   = trader["Wallet"]
    username = trader["Name"]

    wr = fetch_recent_win_rate(wallet)

    win_rate_display = wr["win_rate"] if wr["win_rate"] is not None else "N/A"

    if wr["total_sample"] == 0:
        confidence = "NO DATA"
    elif wr["total_sample"] < MIN_SAMPLE_FOR_CONFIDENCE:
        confidence = "LOW (thin sample)"
    else:
        confidence = "OK"

    print(
        f"  {username:<20} Win Rate: {win_rate_display}% "
        f"(resolved {wr['resolved_wins']}W/{wr['resolved_losses']}L, "
        f"early-exit {wr['early_exit_wins']}W/{wr['early_exit_losses']}L, "
        f"span={wr['sample_span_days']}d) | {confidence}"
    )

    return {
        **trader,
        "Win_Rate_%":         win_rate_display,
        "Total_Sample":       wr["total_sample"],
        "Sample_Span_Days":   wr["sample_span_days"],
        "Resolved_Wins":      wr["resolved_wins"],
        "Resolved_Losses":    wr["resolved_losses"],
        "Early_Exit_Wins":    wr["early_exit_wins"],
        "Early_Exit_Losses":  wr["early_exit_losses"],
        "Pushes":             wr["pushes"],
        "Confidence":         confidence,
        "Avg_Win_$":          wr["avg_win"],
        "Avg_Loss_$":         wr["avg_loss"],
    }


def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print(f"   Weekly window (trades/profit): {datetime.utcfromtimestamp(WEEK_AGO_TS).strftime('%Y-%m-%d')} "
          f"→ {datetime.utcfromtimestamp(NOW_TS).strftime('%Y-%m-%d')}")
    print(f"   Trade-count filter band: {MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/week")
    print(f"   Win rate basis: most recent {WIN_RATE_LOOKBACK_PAGES * CLOSED_POS_PAGE_SIZE} resolved positions (not date-limited)")
    print(f"   Running {MAX_WORKERS} traders in parallel")
    print("=" * 60)

    # ── Step 1: Pull top 50 leaderboard ─────────────────────────────
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {LEADERBOARD_POOL_SIZE} traders...")
    leaderboard_data = fetch_from_polymarket(
        f"{DATA_API}/v1/leaderboard",
        query_params={"timePeriod": "WEEK", "orderBy": "PNL", "limit": str(LEADERBOARD_POOL_SIZE)}
    )

    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("❌ Failed to fetch leaderboard data.")
        sys.exit(1)

    print(f"✅ Retrieved {len(leaderboard_data)} traders.")

    # ── Step 2: PHASE 1 — parallel trade-count screening ────────────
    print(f"\n🔍 PHASE 1: Screening all {len(leaderboard_data)} traders by weekly trade count "
          f"({MAX_WORKERS} at a time)...")
    survivors = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(screen_trader, entry) for entry in leaderboard_data]
        for future in as_completed(futures):
            result = future.result()
            if result:
                survivors.append(result)

    print(f"\n✅ Phase 1 complete: {len(survivors)} of {len(leaderboard_data)} traders fall within "
          f"{MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/week.")

    if not survivors:
        print("❌ No traders survived the trade-count filter. Try widening MIN/MAX_WEEKLY_TRADES.")
        sys.exit(1)

    # ── Step 3: PHASE 2 — parallel win-rate analysis on survivors ──
    print(f"\n📊 PHASE 2: Calculating win rates for {len(survivors)} surviving traders "
          f"({MAX_WORKERS} at a time)...")
    results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(analyze_one_survivor, trader) for trader in survivors]
        for future in as_completed(futures):
            results.append(future.result())

    # ── Step 4: Sort & save ──────────────────────────────────────────
    df = pd.DataFrame(results)
    df["_sort_key"] = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df_sorted = df.sort_values(by="_sort_key", ascending=False, na_position="last").drop(columns="_sort_key")

    column_order = [
        "Wallet", "Name", "Weekly_Trades", "Win_Rate_%", "Total_Sample", "Sample_Span_Days",
        "Resolved_Wins", "Resolved_Losses", "Early_Exit_Wins", "Early_Exit_Losses",
        "Pushes", "Confidence", "Avg_Win_$", "Avg_Loss_$",
        "Profit_$", "Volume_$", "Profit_Rate"
    ]
    df_sorted = df_sorted[column_order]

    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    df_sorted.to_csv(filename, index=False)

    print("\n" + "=" * 60)
    print(f"✅ Saved: {filename}  ({len(df_sorted)} qualifying traders)")
    print(f"   Weekly window: past 7 days  |  Pool: top {LEADERBOARD_POOL_SIZE}  |  "
          f"Filter: {MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/week")
    print("\n=== QUALIFYING TRADERS (sorted by Win Rate) ===")
    print(
        df_sorted[["Name", "Weekly_Trades", "Win_Rate_%", "Total_Sample", "Sample_Span_Days",
                   "Confidence", "Profit_$", "Profit_Rate"]]
        .to_string(index=False)
    )
    print("=" * 60)
    print("✅ Analysis complete!")


if __name__ == "__main__":
    analyze_traders()
PYEOF
echo "done"
