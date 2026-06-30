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

# ── Trade-count filter band ─────────────────────────────────────
# Below MIN: too few trades this week to trust a win rate (could be 2/2 = 100%)
# Above MAX: bot-speed trading — a small starting balance can't survive the
#            variance of thousands of micro-trades before any edge plays out
MIN_WEEKLY_TRADES = 20
MAX_WEEKLY_TRADES = 500

LEADERBOARD_POOL_SIZE = 50   # screen the top 50 traders, not just top 10

# Polymarket's /closed-positions endpoint hard-caps at 50 results per page —
# anything higher gets silently clamped, so we paginate with offset instead.
CLOSED_POS_PAGE_SIZE  = 50
MAX_CLOSED_POS_PAGES  = 6    # 6 x 50 = up to 300 resolved positions checked


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
    PHASE 1 — cheap screening call.

    Counts BUY trades placed in the past 7 days via /activity, paginating
    with `offset` until either we run out of trades, hit the window edge,
    or exceed count_cap (a little above MAX_WEEKLY_TRADES, so we can
    correctly classify a trader as "over the limit" without needing to
    paginate through their full bot-speed history).

    This function alone is what makes screening 50 traders cheap: a bot
    doing 4,000 trades/week gets cut off and rejected after ~2 pages
    instead of fetching all 4,000.

    Returns: weekly_trade_count <int> (accurate up to count_cap, then
             just "count_cap or more")
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
        time.sleep(0.2)

    return total


def fetch_weekly_win_rate(proxy_wallet):
    """
    Win rate from positions in the past 7 days, with TWO layers of signal:

    1. RESOLVED outcomes — the market actually settled via Polymarket's
       oracle. curPrice lands at exactly 1.0 (won) or 0.0 (lost). This is
       the cleanest, most trustworthy signal of real trading skill.

    2. EARLY EXITS — the trader sold before resolution. We no longer just
       exclude these. Instead we classify them as a win or loss based on
       realizedPnl (did they sell for a profit or a loss?), but we track
       them SEPARATELY from resolved wins/losses so you can always see how
       much of a trader's win rate comes from real market calls playing
       out vs. from actively managing risk by exiting early.

    3. PUSHES — the rare case where curPrice lands right around 0.50,
       suggesting an ambiguous/tied resolution. Excluded from win rate
       entirely since neither a win nor a loss.

    Overall win_rate combines both resolved + early-exit wins/losses,
    since a deliberate profitable exit is still a real trading decision
    that paid off — but Resolved_Wins/Resolved_Losses and
    Early_Exit_Wins/Early_Exit_Losses are reported individually so you
    can judge how "real" the headline win rate actually is.

    Returns a dict with all the breakdown fields plus avg win/loss $.
    """
    resolved_wins = resolved_losses = 0
    early_exit_wins = early_exit_losses = 0
    pushes = 0
    win_amounts = []
    loss_amounts = []

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

            cur_price    = pos.get("curPrice")
            realized_pnl = pos.get("realizedPnl")
            if cur_price is None:
                continue

            try:
                price_val = float(cur_price)
                pnl_val   = float(realized_pnl) if realized_pnl is not None else 0.0
            except (TypeError, ValueError):
                continue

            if price_val >= 0.999:
                resolved_wins += 1
                win_amounts.append(pnl_val)
            elif price_val <= 0.001:
                resolved_losses += 1
                loss_amounts.append(pnl_val)
            elif 0.499 <= price_val <= 0.501:
                pushes += 1
            else:
                # Early exit — classify by actual realized PnL rather than
                # excluding it, but keep it in its OWN bucket so it never
                # gets confused with a true market resolution.
                if pnl_val > 0:
                    early_exit_wins += 1
                    win_amounts.append(pnl_val)
                else:
                    early_exit_losses += 1
                    loss_amounts.append(pnl_val)

        if window_closed:
            break
        if len(data) < CLOSED_POS_PAGE_SIZE:
            break
        time.sleep(0.25)

    total_wins   = resolved_wins + early_exit_wins
    total_losses = resolved_losses + early_exit_losses
    total        = total_wins + total_losses

    win_rate = round((total_wins / total) * 100, 1) if total > 0 else None
    avg_win  = round(sum(win_amounts) / len(win_amounts), 2) if win_amounts else 0.0
    avg_loss = round(sum(loss_amounts) / len(loss_amounts), 2) if loss_amounts else 0.0

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
    }


def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print(f"   Rolling window: {datetime.utcfromtimestamp(WEEK_AGO_TS).strftime('%Y-%m-%d')} "
          f"→ {datetime.utcfromtimestamp(NOW_TS).strftime('%Y-%m-%d')} (7 days)")
    print(f"   Trade-count filter band: {MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/week")
    print("=" * 60)

    # ── Step 1: Pull top 50 leaderboard (not just top 10) ──────────
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {LEADERBOARD_POOL_SIZE} traders...")
    leaderboard_data = fetch_from_polymarket(
        f"{DATA_API}/v1/leaderboard",
        query_params={"timePeriod": "WEEK", "orderBy": "PNL", "limit": str(LEADERBOARD_POOL_SIZE)}
    )

    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("❌ Failed to fetch leaderboard data.")
        sys.exit(1)

    print(f"✅ Retrieved {len(leaderboard_data)} traders.")

    # ── Step 2: PHASE 1 — cheap trade-count screening ───────────────
    print(f"\n🔍 PHASE 1: Screening all {len(leaderboard_data)} traders by weekly trade count...")
    survivors = []

    for idx, entry in enumerate(leaderboard_data):
        wallet = entry.get("proxyWallet")
        if not wallet:
            continue

        username = entry.get("userName") or entry.get("xUsername") or wallet[:8] + "..."
        pnl      = float(entry.get("pnl", 0))
        volume   = float(entry.get("vol", 0))

        if volume <= 0:
            continue

        weekly_trades = count_weekly_trades(wallet, count_cap=MAX_WEEKLY_TRADES + 100)
        time.sleep(0.2)

        in_band = MIN_WEEKLY_TRADES <= weekly_trades <= MAX_WEEKLY_TRADES
        status = "✅ PASS" if in_band else "❌ filtered out"
        print(f"  [{idx+1}/{len(leaderboard_data)}] {username:<20} {weekly_trades:>4} trades/wk  {status}")

        if in_band:
            survivors.append({
                "Wallet":        wallet,
                "Name":          username,
                "Weekly_Trades": weekly_trades,
                "Profit_$":      round(pnl, 2),
                "Volume_$":      round(volume, 2),
                "Profit_Rate":   round(pnl / volume, 4),
            })

    print(f"\n✅ Phase 1 complete: {len(survivors)} of {len(leaderboard_data)} traders fall within "
          f"{MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/week.")

    if not survivors:
        print("❌ No traders survived the trade-count filter. Try widening MIN/MAX_WEEKLY_TRADES.")
        sys.exit(1)

    # ── Step 3: PHASE 2 — full win-rate analysis on survivors only ──
    print(f"\n📊 PHASE 2: Calculating real win rates for {len(survivors)} surviving traders...")
    results = []

    for idx, trader in enumerate(survivors):
        wallet   = trader["Wallet"]
        username = trader["Name"]

        print(f"  [{idx+1}/{len(survivors)}] {username} | calculating win rate...")
        wr = fetch_weekly_win_rate(wallet)
        time.sleep(0.3)

        win_rate_display = wr["win_rate"] if wr["win_rate"] is not None else "N/A"

        if wr["total_sample"] == 0:
            confidence = "NO DATA"
        elif wr["total_sample"] < 20:
            confidence = "LOW (thin sample)"
        else:
            confidence = "OK"

        results.append({
            **trader,
            "Win_Rate_%":         win_rate_display,
            "Total_Sample":       wr["total_sample"],
            "Resolved_Wins":      wr["resolved_wins"],
            "Resolved_Losses":    wr["resolved_losses"],
            "Early_Exit_Wins":    wr["early_exit_wins"],
            "Early_Exit_Losses":  wr["early_exit_losses"],
            "Pushes":             wr["pushes"],
            "Confidence":         confidence,
            "Avg_Win_$":          wr["avg_win"],
            "Avg_Loss_$":         wr["avg_loss"],
        })

        print(
            f"         ✅ Win Rate: {win_rate_display}% "
            f"(resolved {wr['resolved_wins']}W/{wr['resolved_losses']}L, "
            f"early-exit {wr['early_exit_wins']}W/{wr['early_exit_losses']}L, "
            f"pushes={wr['pushes']}) | Confidence: {confidence}"
        )

    # ── Step 4: Sort & save ──────────────────────────────────────────
    df = pd.DataFrame(results)
    df["_sort_key"] = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df_sorted = df.sort_values(by="_sort_key", ascending=False, na_position="last").drop(columns="_sort_key")

    # Reorder columns for readability
    column_order = [
        "Wallet", "Name", "Weekly_Trades", "Win_Rate_%", "Total_Sample",
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
    print(f"   Window: past 7 days  |  Pool: top {LEADERBOARD_POOL_SIZE}  |  "
          f"Filter: {MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/week")
    print("\n=== QUALIFYING TRADERS (sorted by Win Rate) ===")
    print(
        df_sorted[["Name", "Weekly_Trades", "Win_Rate_%", "Resolved_Wins", "Resolved_Losses",
                   "Early_Exit_Wins", "Early_Exit_Losses", "Confidence", "Profit_$", "Profit_Rate"]]
        .to_string(index=False)
    )
    print("=" * 60)
    print("✅ Analysis complete!")


if __name__ == "__main__":
    analyze_traders()
