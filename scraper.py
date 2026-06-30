import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIGURATION – adjust these to your liking
# ============================================================

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

if not SCRAPER_API_KEY:
    print("❌ Error: SCRAPER_API_KEY is missing from GitHub Secrets.")
    sys.exit(1)

# ── Trade count filter (weekly) ──────────────────────────────
MIN_WEEKLY_TRADES = 10    # catch slower traders
MAX_WEEKLY_TRADES = 1000  # include more active traders

# ── Win rate sample size ──────────────────────────────────────
WIN_RATE_LOOKBACK_PAGES = 15   # up to 750 resolved positions
MIN_SAMPLE_SIZE = 10           # only show traders with at least this many resolved trades

LEADERBOARD_POOL_SIZE = 100    # scan top 100 traders
MAX_WORKERS = 5                # raise to 10 if your ScraperAPI plan allows

# ============================================================
# DO NOT CHANGE BELOW THIS LINE
# ============================================================

DATA_API = "https://data-api.polymarket.com"

NOW_TS      = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS = NOW_TS - (7 * 24 * 60 * 60)


def fetch_from_polymarket(target_url, query_params=None):
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


def count_weekly_trades(proxy_wallet, count_cap=1100):  # increased cap
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
            break
        offset += page_size
    return total


def fetch_recent_win_rate(proxy_wallet):
    resolved_wins = resolved_losses = 0
    early_exit_wins = early_exit_losses = 0
    pushes = 0
    win_amounts = []
    loss_amounts = []
    oldest_ts = None

    for page in range(WIN_RATE_LOOKBACK_PAGES):
        offset = page * 50
        data = fetch_from_polymarket(
            f"{DATA_API}/closed-positions",
            query_params={
                "user":          proxy_wallet,
                "limit":         50,
                "offset":        offset,
                "sortBy":        "TIMESTAMP",
                "sortDirection": "DESC",
            }
        )
        if not data or not isinstance(data, list) or len(data) == 0:
            break

        for pos in data:
            ts = pos.get("timestamp")
            cur_price = pos.get("curPrice")
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
                if pnl_val > 0:
                    early_exit_wins += 1
                    win_amounts.append(pnl_val)
                else:
                    early_exit_losses += 1
                    loss_amounts.append(pnl_val)

        if len(data) < 50:
            break

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
    wallet   = trader["Wallet"]
    username = trader["Name"]
    wr = fetch_recent_win_rate(wallet)
    win_rate_display = wr["win_rate"] if wr["win_rate"] is not None else "N/A"
    print(
        f"  {username:<20} Win Rate: {win_rate_display}% "
        f"(sample {wr['total_sample']}, span {wr['sample_span_days']}d)"
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
        "Avg_Win_$":          wr["avg_win"],
        "Avg_Loss_$":         wr["avg_loss"],
    }


def analyze_traders():
    print("🚀 Starting Polymarket Smart Money Analyzer")
    print(f"   Weekly window: {datetime.utcfromtimestamp(WEEK_AGO_TS).strftime('%Y-%m-%d')} → {datetime.utcfromtimestamp(NOW_TS).strftime('%Y-%m-%d')}")
    print(f"   Trade filter: {MIN_WEEKLY_TRADES}–{MAX_WEEKLY_TRADES} trades/week")
    print(f"   Win rate basis: up to {WIN_RATE_LOOKBACK_PAGES*50} resolved positions (minimum sample {MIN_SAMPLE_SIZE})")
    print(f"   Parallel workers: {MAX_WORKERS}")
    print("=" * 60)

    # Step 1 – fetch top leaderboard
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {LEADERBOARD_POOL_SIZE} traders...")
    leaderboard_data = fetch_from_polymarket(
        f"{DATA_API}/v1/leaderboard",
        query_params={"timePeriod": "WEEK", "orderBy": "PNL", "limit": str(LEADERBOARD_POOL_SIZE)}
    )
    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("❌ Failed to fetch leaderboard.")
        sys.exit(1)
    print(f"✅ Retrieved {len(leaderboard_data)} traders.")

    # Step 2 – screen by trade count
    print(f"\n🔍 Phase 1: screening {len(leaderboard_data)} traders ...")
    survivors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(screen_trader, entry) for entry in leaderboard_data]
        for future in as_completed(futures):
            result = future.result()
            if result:
                survivors.append(result)
    print(f"\n✅ Phase 1: {len(survivors)} traders passed the trade filter.")

    if not survivors:
        print("❌ No traders survived. Widen MIN/MAX_WEEKLY_TRADES.")
        sys.exit(1)

    # Step 3 – compute win rates for survivors
    print(f"\n📊 Phase 2: calculating win rates for {len(survivors)} traders ...")
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(analyze_one_survivor, trader) for trader in survivors]
        for future in as_completed(futures):
            results.append(future.result())

    # Step 4 – build DataFrame
    df = pd.DataFrame(results)

    # Combine early exits
    df["Early_Exits"] = df["Early_Exit_Wins"] + df["Early_Exit_Losses"]

    # Combine Profit and Volume into one column
    df["Profit / Volume"] = df["Profit_$"].astype(str) + " / " + df["Volume_$"].astype(str)

    # Filter out traders with too small sample
    df = df[df["Total_Sample"] >= MIN_SAMPLE_SIZE]

    # Final columns (Confidence removed)
    final_columns = [
        "Wallet",
        "Name",
        "Weekly_Trades",
        "Win_Rate_%",
        "Total_Sample",
        "Sample_Span_Days",
        "Early_Exits",
        "Profit / Volume",
        "Profit_Rate"
    ]
    df = df[final_columns]

    # Sort by Win Rate (highest first)
    df["_sort"] = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df_sorted = df.sort_values("_sort", ascending=False, na_position="last").drop(columns="_sort")

    # Save CSV
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"smart_money_{date_str}.csv"
    df_sorted.to_csv(filename, index=False)

    # Print clean summary – only reliable traders
    print("\n" + "=" * 60)
    print(f"✅ Saved: {filename}  ({len(df_sorted)} traders with ≥{MIN_SAMPLE_SIZE} resolved trades)")
    print("\n=== RELIABLE TRADERS (sorted by Win Rate) ===")
    print(df_sorted[["Name", "Weekly_Trades", "Win_Rate_%", "Total_Sample", "Sample_Span_Days", "Profit / Volume", "Profit_Rate"]].to_string(index=False))
    print("=" * 60)
    print("✅ Done.")


if __name__ == "__main__":
    analyze_traders()
