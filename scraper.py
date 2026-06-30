import os
import sys
import time
import requests
import pandas as pd
from datetime import datetime, timezone
from urllib.parse import urlencode, quote

# ============================================================
# CONFIGURATION
# ============================================================

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")

if not SCRAPER_API_KEY:
    print("Error: SCRAPER_API_KEY is missing from GitHub Secrets.")
    sys.exit(1)

DATA_API = "https://data-api.polymarket.com"

NOW_TS      = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS = NOW_TS - (7 * 24 * 60 * 60)
TODAY_STR   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Hard API limits confirmed against Polymarket's official docs - do not raise.
ACTIVITY_PAGE_SIZE    = 500   # /activity max per page
CLOSED_POS_PAGE_SIZE  = 50    # /closed-positions max per page

# Safety caps so one hyperactive wallet can't blow up runtime/cost.
MAX_ACTIVITY_PAGES    = 6     # 6 x 500 = up to 3,000 trades checked per trader
MAX_CLOSED_POS_PAGES  = 8     # 8 x 50  = up to 400 resolved positions checked per trader

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SmartMoneyBot/1.0)"}
REQUEST_TIMEOUT = 20
MAX_RETRIES = 2

HISTORY_FILE = "smart_money_history.csv"


# ============================================================
# REQUEST HANDLING
# Tries a direct call first (Polymarket's data API is public and needs
# no auth key). Falls back to the ScraperAPI proxy only if direct fails,
# so proxy credits are spent only when actually needed. Retries twice
# at each stage before giving up.
# ============================================================

def _get_json(url, timeout):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_from_polymarket(target_url, query_params=None):
    if query_params:
        target_url = f"{target_url}?{urlencode(query_params)}"

    for attempt in range(MAX_RETRIES):
        try:
            return _get_json(target_url, REQUEST_TIMEOUT)
        except Exception:
            time.sleep(1.0 * (attempt + 1))

    proxy_url = (
        f"https://api.scraperapi.com"
        f"?api_key={SCRAPER_API_KEY}"
        f"&url={quote(target_url, safe='')}"
        f"&premium=true"
    )
    for attempt in range(MAX_RETRIES):
        try:
            return _get_json(proxy_url, 60)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                print(f"    Request failed (direct + proxy): {e}")
            time.sleep(1.5 * (attempt + 1))

    return None


# ============================================================
# REAL WEEKLY TRADE COUNT (no more "500+" placeholders)
# ============================================================

def fetch_weekly_trade_count(proxy_wallet):
    """
    True count of BUY trades placed in the past 7 days. start/end are
    filtered server-side by Polymarket, so pagination here is only about
    getting past the 500-per-page cap, not about the date window.
    Returns: (true_count, capped) - capped=True only means the safety
    limit (3,000) was hit and the real number could be higher.
    """
    total = 0
    capped = False

    for page in range(MAX_ACTIVITY_PAGES):
        offset = page * ACTIVITY_PAGE_SIZE
        data = fetch_from_polymarket(
            f"{DATA_API}/activity",
            query_params={
                "user":          proxy_wallet,
                "type":          "TRADE",
                "start":         WEEK_AGO_TS,
                "end":           NOW_TS,
                "limit":         ACTIVITY_PAGE_SIZE,
                "offset":        offset,
                "side":          "BUY",
                "sortBy":        "TIMESTAMP",
                "sortDirection": "DESC",
            }
        )

        if not data or not isinstance(data, list) or len(data) == 0:
            break

        total += len(data)

        if len(data) < ACTIVITY_PAGE_SIZE:
            break
        if page == MAX_ACTIVITY_PAGES - 1:
            capped = True

        time.sleep(0.2)

    return total, capped


# ============================================================
# REAL WIN / LOSS COUNT + PAYOFF SIZE
# ============================================================

def fetch_weekly_win_rate(proxy_wallet):
    """
    Counts genuine market resolutions (not early sells) in the past 7 days.

        curPrice == 1.0   -> WON  (oracle redeemed at $1.00)
        curPrice == 0.0   -> LOST (oracle redeemed at $0.00)
        curPrice == 0.5   -> PUSH (rare UMA "50/50" outcome - not a win or loss)
        anything else     -> sold early, excluded from win rate

    Also sums each position's own realizedPnl (already in the API response)
    so we get average $ won per win and average $ lost per loss. Win rate
    alone can't tell you if wins are big enough to cover losses and fees -
    this gives the numbers needed for that math later.
    """
    wins = losses = pushes = early_exits = 0
    win_pnl_total = 0.0
    loss_pnl_total = 0.0

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
                pnl_val = float(pos.get("realizedPnl") or 0.0)
            except (TypeError, ValueError):
                continue

            if price_val >= 0.999:
                wins += 1
                win_pnl_total += pnl_val
            elif price_val <= 0.001:
                losses += 1
                loss_pnl_total += pnl_val
            elif 0.499 <= price_val <= 0.501:
                pushes += 1
            else:
                early_exits += 1

        if window_closed:
            break
        if len(data) < CLOSED_POS_PAGE_SIZE:
            break
        time.sleep(0.2)

    resolved = wins + losses
    return {
        "win_rate":         round((wins / resolved) * 100, 1) if resolved > 0 else None,
        "wins":              wins,
        "losses":            losses,
        "pushes":            pushes,
        "resolved":          resolved,
        "early_exits":       early_exits,
        "avg_win":           round(win_pnl_total / wins, 2) if wins > 0 else None,
        "avg_loss":          round(loss_pnl_total / losses, 2) if losses > 0 else None,
        "net_resolved_pnl":  round(win_pnl_total + loss_pnl_total, 2),
    }


# ============================================================
# MAIN
# ============================================================

def analyze_traders():
    print("Starting Polymarket Smart Money Analyzer")
    print(f"Window: {datetime.fromtimestamp(WEEK_AGO_TS, tz=timezone.utc):%Y-%m-%d} to "
          f"{datetime.fromtimestamp(NOW_TS, tz=timezone.utc):%Y-%m-%d}")

    leaderboard_data = fetch_from_polymarket(
        f"{DATA_API}/v1/leaderboard",
        query_params={"timePeriod": "WEEK", "orderBy": "PNL", "limit": "10"}
    )

    if not leaderboard_data or not isinstance(leaderboard_data, list):
        print("Failed to fetch leaderboard data.")
        sys.exit(1)

    print(f"Retrieved {len(leaderboard_data)} traders.")

    CONF_RANK = {"OK": 2, "LOW (thin sample)": 1, "NO DATA": 0}
    results = []

    for idx, entry in enumerate(leaderboard_data):
        wallet = entry.get("proxyWallet")
        if not wallet:
            continue

        username = entry.get("userName") or entry.get("xUsername") or wallet[:8] + "..."
        pnl      = float(entry.get("pnl", 0))
        volume   = float(entry.get("vol", 0))
        if volume <= 0:
            continue
        profit_rate = round(pnl / volume, 4)

        print(f"[{idx+1}/10] {username}: trade count...")
        trades, trades_capped = fetch_weekly_trade_count(wallet)
        time.sleep(0.2)

        print(f"[{idx+1}/10] {username}: win rate...")
        wr = fetch_weekly_win_rate(wallet)
        time.sleep(0.2)

        if wr["resolved"] == 0:
            confidence = "NO DATA"
        elif wr["resolved"] < 20:
            confidence = "LOW (thin sample)"
        else:
            confidence = "OK"

        results.append({
            "Date":                         TODAY_STR,
            "Wallet":                       wallet,
            "Name":                         username,
            "Trades_This_Week":             trades,
            "Trades_Capped":                "Yes" if trades_capped else "No",
            "Wins":                         wr["wins"],
            "Losses":                       wr["losses"],
            "Pushes":                       wr["pushes"],
            "Win_Rate_%":                   wr["win_rate"] if wr["win_rate"] is not None else "N/A",
            "Resolved_Sample":              wr["resolved"],
            "Early_Exits":                  wr["early_exits"],
            "Confidence":                   confidence,
            "Avg_Win_$":                    wr["avg_win"] if wr["avg_win"] is not None else "N/A",
            "Avg_Loss_$":                   wr["avg_loss"] if wr["avg_loss"] is not None else "N/A",
            "Net_Realized_PnL_Resolved_$":  wr["net_resolved_pnl"],
            "Profit_$":                     round(pnl, 2),
            "Volume_$":                     round(volume, 2),
            "Profit_Rate":                  profit_rate,
        })

        print(f"    trades={trades}{'+' if trades_capped else ''} | "
              f"win rate={wr['win_rate']}% (wins={wr['wins']}, losses={wr['losses']}, n={wr['resolved']}) | "
              f"avg win=${wr['avg_win']} avg loss=${wr['avg_loss']} | confidence={confidence}")

    if not results:
        print("No valid traders found.")
        sys.exit(1)

    df = pd.DataFrame(results)

    # Sort by trustworthiness first (Confidence), then win rate within each
    # tier - a 100% win rate on 10 trades can no longer outrank a 95% win
    # rate on 100 trades.
    df["_conf_rank"] = df["Confidence"].map(CONF_RANK)
    df["_wr_sort"] = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df_sorted = df.sort_values(
        by=["_conf_rank", "_wr_sort"], ascending=[False, False], na_position="last"
    ).drop(columns=["_conf_rank", "_wr_sort"])

    # Daily snapshot (one file per day)
    daily_filename = f"smart_money_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    df_sorted.to_csv(daily_filename, index=False)

    # Master history - appended to every run, so the same wallet's numbers
    # can be compared day over day. IMPORTANT: this only accumulates if
    # your GitHub Action commits smart_money_history.csv back to the repo
    # after each run - otherwise it resets every time the runner spins up.
    write_header = not os.path.exists(HISTORY_FILE)
    df_sorted.to_csv(HISTORY_FILE, mode="a", header=write_header, index=False)

    print(f"\nSaved: {daily_filename}  ({len(df_sorted)} traders)")
    print(f"Appended to: {HISTORY_FILE}")
    print("\n=== TODAY'S RANKING (most trustworthy samples first) ===")
    print(df_sorted[[
        "Name", "Trades_This_Week", "Win_Rate_%", "Resolved_Sample",
        "Confidence", "Avg_Win_$", "Avg_Loss_$", "Profit_Rate"
    ]].to_string(index=False))
    print("Done.")


if __name__ == "__main__":
    analyze_traders()
