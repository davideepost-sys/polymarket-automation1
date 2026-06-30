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
NOW_TS       = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS  = NOW_TS - (7 * 24 * 60 * 60)
 
 
def fetch_from_polymarket(target_url, query_params=None):
    """
    Fetch JSON from a Polymarket endpoint via ScraperAPI.
 
    We build the full target URL ourselves (including its query string),
    then percent-encode the whole thing into the ScraperAPI call so the
    requests library cannot re-encode the '?' or '&' separators inside it.
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
 
 
def fetch_weekly_activity(proxy_wallet):
    """
    Fetch all TRADE-type activity for a wallet in the past 7 days.
 
    Uses GET /activity with:
      type=TRADE          — only actual trades, not splits/merges/redeems
      start=<7 days ago>  — server-side timestamp filter (efficient)
      end=<now>
      limit=500           — max per page
      sortBy=TIMESTAMP
      sortDirection=ASC
 
    Paginates automatically if the trader placed more than 500 trades
    in the week (rare but possible for high-frequency traders).
 
    Returns a list of activity dicts.
    """
    all_activity = []
    offset = 0
    page_size = 500
 
    while True:
        data = fetch_from_polymarket(
            f"{DATA_API}/activity",
            query_params={
                "user":          proxy_wallet,
                "type":          "TRADE",
                "start":         WEEK_AGO_TS,
                "end":           NOW_TS,
                "limit":         page_size,
                "offset":        offset,
                "sortBy":        "TIMESTAMP",
                "sortDirection": "ASC",
            }
        )
 
        if not data or not isinstance(data, list):
            break
 
        all_activity.extend(data)
 
        if len(data) < page_size:
            break  # last page
 
        offset += page_size
        time.sleep(0.3)  # be polite between pagination calls
 
    return all_activity
 
 
def derive_weekly_metrics(activity):
    """
    From a list of weekly TRADE activity entries, calculate:
 
      weekly_trades  — total number of BUY trades placed this week.
                       We count only BUYs (opening a position) because
                       SELL trades are exits — counting both would double-count
                       every round-trip and inflate the number.
 
      win_rate       — among trades that have a resolved outcome we can infer:
                       a REDEEM event exists in the broader history, but that
                       requires a second call.  Instead we use price as a proxy:
                       a BUY at price > 0.5 that later resolved YES wins;
                       a BUY at price < 0.5 that resolved NO wins.
                       Since we only have trade-open data here (not resolution),
                       we derive win rate from the /positions endpoint
                       (redeemable=True means the market resolved in the
                       trader's favour) — see fetch_win_rate_weekly() below.
 
    Returns: (weekly_trades <int>)
    """
    buys = [a for a in activity if a.get("side") == "BUY"]
    return len(buys)
 
 
def fetch_win_rate_weekly(proxy_wallet):
    """
    Win rate over the past 7 days, derived from resolved positions.
 
    Strategy:
      1. Fetch the trader's current positions (includes recently resolved ones
         that haven't been cleaned up yet) filtered to sizeThreshold=0.01
         so tiny dust positions don't skew the count.
      2. A position is 'resolved this week' if:
           - redeemable=True  (market settled, they can claim)
           - AND the underlying conditionId was traded in the window
             (we cross-reference with the weekly activity list)
      3. wins   = resolved-this-week positions where cashPnl > 0
         losses = resolved-this-week positions where cashPnl <= 0
         win_rate = wins / (wins + losses) * 100
 
    If no resolved positions exist in the window we fall back to
    all-time resolved positions so the column is never blank.
    """
    # Get weekly trade conditionIds for cross-reference
    weekly_activity = fetch_from_polymarket(
        f"{DATA_API}/activity",
        query_params={
            "user":          proxy_wallet,
            "type":          "TRADE",
            "start":         WEEK_AGO_TS,
            "end":           NOW_TS,
            "limit":         500,
            "sortBy":        "TIMESTAMP",
            "sortDirection": "ASC",
        }
    )
    weekly_condition_ids = set()
    if weekly_activity and isinstance(weekly_activity, list):
        for a in weekly_activity:
            cid = a.get("conditionId")
            if cid:
                weekly_condition_ids.add(cid)
 
    # Get current positions
    positions = fetch_from_polymarket(
        f"{DATA_API}/positions",
        query_params={
            "user":           proxy_wallet,
            "sizeThreshold":  "0.01",
            "limit":          "500",
        }
    )
 
    if not positions or not isinstance(positions, list):
        return 0.0
 
    wins_week = losses_week = 0
    wins_all  = losses_all  = 0
 
    for pos in positions:
        redeemable = pos.get("redeemable", False)
        cash_pnl   = pos.get("cashPnl")
 
        if not (redeemable or cash_pnl is not None):
            continue  # still open / unresolved
 
        try:
            pnl_val = float(cash_pnl) if cash_pnl is not None else 0.0
        except (TypeError, ValueError):
            continue
 
        # All-time bucket
        if pnl_val > 0:
            wins_all += 1
        else:
            losses_all += 1
 
        # Weekly bucket (only if this market was traded in our window)
        cid = pos.get("conditionId")
        if cid and cid in weekly_condition_ids:
            if pnl_val > 0:
                wins_week += 1
            else:
                losses_week += 1
 
    # Prefer weekly win rate; fall back to all-time if no weekly resolutions yet
    total_week = wins_week + losses_week
    if total_week > 0:
        return round((wins_week / total_week) * 100, 1)
 
    total_all = wins_all + losses_all
    if total_all > 0:
        return round((wins_all / total_all) * 100, 1)
 
    return 0.0
 
 
def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print(f"   Rolling window: {datetime.utcfromtimestamp(WEEK_AGO_TS).strftime('%Y-%m-%d')} "
          f"→ {datetime.utcfromtimestamp(NOW_TS).strftime('%Y-%m-%d')} (7 days)")
    print("=" * 60)
 
    # ── Step 1: Leaderboard (rolling 7-day PNL) ───────────────────
    # Official path: GET /v1/leaderboard  (note the /v1/ prefix)
    # timePeriod=WEEK is itself a rolling 7-day window on Polymarket's side
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
 
        # Weekly trade count via /activity (start/end filtered server-side)
        print(f"  [{idx+1}/10] {username} | fetching weekly trades...")
        activity = fetch_weekly_activity(wallet)
        weekly_trades = derive_weekly_metrics(activity)
        time.sleep(0.4)
 
        # Weekly win rate via /positions cross-referenced with activity
        print(f"  [{idx+1}/10] {username} | calculating weekly win rate...")
        win_rate = fetch_win_rate_weekly(wallet)
        time.sleep(0.4)
 
        results.append({
            "Wallet":              wallet,
            "Name":                username,
            "Weekly_Trades":       weekly_trades,   # BUY trades placed in past 7 days
            "Win_Rate_%":          win_rate,         # % of resolved positions that were profitable
            "Profit_$":            round(pnl, 2),    # 7-day PNL from leaderboard
            "Volume_$":            round(volume, 2), # 7-day volume from leaderboard
            "Profit_Rate":         profit_rate,      # Profit_$ / Volume_$ (capital efficiency)
        })
 
        print(
            f"         ✅ Weekly trades: {weekly_trades} | "
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
    print(f"   Window: past 7 days  |  Run date: {date_str}")
    print("\n=== TOP 10 TRADERS (sorted by Win Rate) ===")
    print(
        df_sorted[["Name", "Weekly_Trades", "Win_Rate_%", "Profit_$", "Profit_Rate"]]
        .to_string(index=False)
    )
    print("=" * 60)
    print("✅ Analysis complete!")
 
 
if __name__ == "__main__":
    analyze_traders()
 
