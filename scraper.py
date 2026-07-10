import sys
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# ============================================================
# NO API KEY NEEDED — Polymarket's Data API is public and free.
# ============================================================
DATA_API = "https://data-api.polymarket.com"
NOW_TS = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS = NOW_TS - (7 * 24 * 60 * 60)

# ── Filters ────────────────────────────────────────────────────
MIN_WEEKLY_TRADES = 20          # too few = can't trust the win rate
MAX_WEEKLY_TRADES = 500         # too many = can't realistically copy-trade it
MIN_WIN_RATE = 60.0             # % — must win majority of trades
MIN_PROFIT_RATE = 0.10          # must make at least 10c profit per $1 risked
MIN_SPAN_DAYS = 3               # need at least 3 days of trading history
MIN_RISK_REWARD = 0.5           # avg_win must be at least half of avg_loss
MIN_MARKETS = 3                 # must trade across at least 3 distinct markets
MAX_AVG_HOLD_DAYS = 2.0         # day-traders only: avg hold <= 2 days
LEADERBOARD_POOL = 500          # screen the top 500 by weekly PNL

# --- consistency filters (the "smooth line" requirement) -------
MAX_CONSECUTIVE_LOSSES = 4      # reject anyone with a losing streak worse than this
MAX_DRAWDOWN_PCT = 35.0         # reject anyone whose peak-to-valley dip exceeds this %

# Polymarket's /closed-positions hard-caps at 50 per page
CLOSED_PAGE_SIZE = 50
CLOSED_PAGES = 5                # 5x50 = up to 250 recent resolved positions
MIN_SAMPLE = 75                 # need at least this many for "Good data"

# How many BUY activity records to fetch for hold-time matching
ACTIVITY_PAGES = 8              # 8x500 = up to 4,000 recent BUY trades
MAX_WORKERS = 5                 # parallel threads — keeps runtime fast

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-analyzer/1.0"})


# ── API helper ─────────────────────────────────────────────────
def get(path, params=None, retries=3):
    url = f"{DATA_API}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                print(f" warn: {url} failed: {e}")
                return None


# ── Phase 1: weekly trade count (cheap) ────────────────────────
def weekly_trade_count(wallet, cap=MAX_WEEKLY_TRADES + 100):
    total, offset = 0, 0
    while total < cap:
        data = get("/activity", {
            "user": wallet, "type": "TRADE", "side": "BUY",
            "start": WEEK_AGO_TS, "end": NOW_TS,
            "limit": 500, "offset": offset,
            "sortBy": "TIMESTAMP", "sortDirection": "DESC",
        })
        if not data or not isinstance(data, list):
            break
        total += len(data)
        if len(data) < 500:
            break
        offset += 500
    return total


# ── Phase 2: win rate + profit rate + consistency + hold time ──
def fetch_entry_timestamps(wallet):
    asset_map = {}
    for page in range(ACTIVITY_PAGES):
        data = get("/activity", {
            "user": wallet, "type": "TRADE", "side": "BUY",
            "limit": 500, "offset": page * 500,
            "sortBy": "TIMESTAMP", "sortDirection": "ASC",
        })
        if not data or not isinstance(data, list):
            break
        for trade in data:
            asset = trade.get("asset")
            ts = trade.get("timestamp")
            if asset and ts and (asset not in asset_map or ts < asset_map[asset]):
                asset_map[asset] = ts
        if len(data) < 500:
            break
    return asset_map


def recent_win_rate(wallet):
    """
    Pulls this trader's recent closed positions ONCE and computes everything
    from that same batch: win rate, profit rate, avg win/loss, hold time,
    market diversity, and consistency (drawdown + losing streaks).
    Using one shared window means every number describes the same stretch
    of trading history — nothing is glued together from mismatched periods.
    """
    entry_map = fetch_entry_timestamps(wallet)

    positions = []          # (timestamp, pnl) in fetch order — re-sorted below
    win_pnl, loss_pnl = [], []
    oldest_ts = None
    markets_seen = set()
    hold_times = []
    weighted_wins = weighted_total = 0.0
    pushes = 0
    total_realized_pnl = 0.0
    total_capital_risked = 0.0

    for page in range(CLOSED_PAGES):
        data = get("/closed-positions", {
            "user": wallet,
            "limit": CLOSED_PAGE_SIZE,
            "offset": page * CLOSED_PAGE_SIZE,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        })
        if not data or not isinstance(data, list):
            break
        for pos in data:
            ts = pos.get("timestamp")
            cp = pos.get("curPrice")
            pnl = pos.get("realizedPnl")
            mkt = pos.get("market") or pos.get("conditionId") or pos.get("marketSlug")
            asset = pos.get("asset")
            if cp is None:
                continue
            try:
                pnl_f = float(pnl) if pnl is not None else 0.0
            except (TypeError, ValueError):
                continue

            if ts and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts
            if mkt:
                markets_seen.add(mkt)
            if asset and asset in entry_map and ts:
                hold_days = (ts - entry_map[asset]) / 86400
                if hold_days >= 0:
                    hold_times.append(hold_days)

            # same-window profit rate: total $ made / total $ risked
            total_realized_pnl += pnl_f
            tb = pos.get("totalBought")
            try:
                tb_f = float(tb) if tb is not None else None
            except (TypeError, ValueError):
                tb_f = None
            if tb_f and tb_f > 0:
                total_capital_risked += tb_f

            if ts:
                positions.append((ts, pnl_f))

            # recency weight: trades from the last 7 days count double
            weight = 2.0 if (ts and ts >= WEEK_AGO_TS) else 1.0
            if pnl_f > 0:
                win_pnl.append(pnl_f)
                weighted_wins += weight
                weighted_total += weight
            elif pnl_f < 0:
                loss_pnl.append(pnl_f)
                weighted_total += weight
            else:
                pushes += 1

        if len(data) < CLOSED_PAGE_SIZE:
            break

    wins, losses = len(win_pnl), len(loss_pnl)
    total = wins + losses
    recency_wr = round(weighted_wins / weighted_total * 100, 1) if weighted_total else None
    avg_hold = round(sum(hold_times) / len(hold_times), 2) if hold_times else None
    median_hold = round(sorted(hold_times)[len(hold_times) // 2], 2) if hold_times else None
    profit_rate_recent = (round(total_realized_pnl / total_capital_risked, 4)
                           if total_capital_risked > 0 else None)

    # --- consistency: walk the trades in TIME order and track the curve ---
    positions.sort(key=lambda p: p[0])
    cum = peak = max_dd = 0.0
    streak = max_streak = 0
    for _, pnl_f in positions:
        cum += pnl_f
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak
            if dd > max_dd:
                max_dd = dd
        if pnl_f < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "win_rate": round(wins / total * 100, 1) if total else None,
        "recency_wr": recency_wr,
        "profit_rate_recent": profit_rate_recent,
        "sample": total,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "avg_win": round(sum(win_pnl) / len(win_pnl), 2) if win_pnl else 0.0,
        "avg_loss": round(sum(loss_pnl) / len(loss_pnl), 2) if loss_pnl else 0.0,
        "span_days": round((NOW_TS - oldest_ts) / 86400, 1) if oldest_ts else None,
        "markets_traded": len(markets_seen),
        "avg_hold_days": avg_hold,
        "median_hold_days": median_hold,
        "matched_positions": len(hold_times),
        "max_drawdown_pct": round(max_dd * 100, 1),
        "max_consecutive_losses": max_streak,
    }


# ── Composite ranking score ────────────────────────────────────
def compute_score(row):
    wr = (row.get("Recency_WR") or 0) / 100
    pr = min(max(row.get("Profit_Rate", 0) or 0, 0), 1.0)
    n = min(row.get("Sample", 0), 200) / 200
    mkts = min(row.get("Markets_Traded", 0), 15) / 15

    hold = row.get("Avg_Hold_Days")
    speed = max(0.0, 1.0 - (hold / 4.0)) if hold is not None and hold > 0 else 0.3

    dd = row.get("Max_Drawdown_%", 0) or 0
    streak = row.get("Max_Consecutive_Losses", 0) or 0
    drawdown_score = max(0.0, 1.0 - (dd / 100.0) / 0.5)   # 0% dd -> 1.0, 50%+ dd -> 0
    streak_score = max(0.0, 1.0 - (streak / 8.0))          # 0 losses in a row -> 1.0, 8+ -> 0
    consistency = (drawdown_score + streak_score) / 2

    score = (0.30 * wr) + (0.20 * pr) + (0.20 * consistency) + (0.10 * n) + (0.10 * speed) + (0.10 * mkts)
    return round(score, 4)


# ── Worker functions (run in threads) ─────────────────────────
def screen(entry):
    wallet = entry.get("proxyWallet")
    if not wallet:
        return None
    trades = weekly_trade_count(wallet)
    in_band = MIN_WEEKLY_TRADES <= trades <= MAX_WEEKLY_TRADES
    print(f" {'ok' if in_band else 'no'} "
          f"{entry.get('userName', wallet[:8]):<20} {trades:>4} trades/wk")
    if not in_band:
        return None
    return {
        "Wallet": wallet,
        "Name": entry.get("userName") or entry.get("xUsername") or wallet[:8] + "...",
        "Weekly_Trades": trades,
    }


def analyze(trader):
    wr = recent_win_rate(trader["Wallet"])
    wr_val = wr["win_rate"]
    sample = wr["sample"]

    if sample < 40:
        confidence = "LOW"
    elif sample < MIN_SAMPLE:
        confidence = "OK"
    else:
        confidence = "Good data"

    if wr["avg_loss"] != 0:
        raw_rr = wr["avg_win"] / max(abs(wr["avg_loss"]), 1.0)   # $1 floor stops divide-by-dust blowups
        rr_ratio = round(min(raw_rr, 25.0), 2)                    # capped so it can't display something absurd
    else:
        rr_ratio = "N/A"

    print(f" {'ok' if wr_val and wr_val >= MIN_WIN_RATE else ' '} "
          f"{trader['Name']:<20} WR={wr_val}% PR={wr['profit_rate_recent']} "
          f"(n={sample}, dd={wr['max_drawdown_pct']}%, streak={wr['max_consecutive_losses']}) "
          f"[{confidence}]")

    return {
        "Wallet": trader["Wallet"],
        "Name": trader["Name"],
        "Weekly_Trades": trader["Weekly_Trades"],
        "Win_Rate_%": wr_val if wr_val is not None else "N/A",
        "Recency_WR": wr["recency_wr"],
        "Profit_Rate": wr["profit_rate_recent"] if wr["profit_rate_recent"] is not None else "N/A",
        "Sample": sample,
        "Sample_Span_Days": wr["span_days"],
        "Wins": wr["wins"],
        "Losses": wr["losses"],
        "Pushes": wr["pushes"],
        "Confidence": confidence,
        "Avg_Win_$": wr["avg_win"],
        "Avg_Loss_$": wr["avg_loss"],
        "Risk_Reward": rr_ratio,
        "Max_Consecutive_Losses": wr["max_consecutive_losses"],
        "Max_Drawdown_%": wr["max_drawdown_pct"],
        "Markets_Traded": wr["markets_traded"],
        "Avg_Hold_Days": wr["avg_hold_days"],
        "Median_Hold_Days": wr["median_hold_days"],
        "Matched_Positions": wr["matched_positions"],
    }


# ── Main ───────────────────────────────────────────────────────
def main():
    t0 = time.time()
    print("Polymarket Smart Money Analyzer v4 (weekly + consistency)")
    print(f" Filters: {MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/wk | "
          f"WR >= {MIN_WIN_RATE}% | PR >= {MIN_PROFIT_RATE}")
    print(f" Consistency: max {MAX_CONSECUTIVE_LOSSES} losses in a row | "
          f"max {MAX_DRAWDOWN_PCT}% drawdown | Hold <= {MAX_AVG_HOLD_DAYS}d")
    print("=" * 70)

    # Step 1: weekly leaderboard
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {LEADERBOARD_POOL} weekly traders...")
    lb = []
    page_size = 50
    for offset in range(0, LEADERBOARD_POOL, page_size):
        limit = min(page_size, LEADERBOARD_POOL - offset)
        page = get("/v1/leaderboard", {
            "timePeriod": "WEEK", "orderBy": "PNL", "limit": limit, "offset": offset
        })
        if not page or not isinstance(page, list):
            break
        lb.extend(page)
        if len(page) < page_size:
            break

    total_fetched = len(lb)
    seen = set()
    lb = [x for x in lb if x.get("proxyWallet") not in seen and not seen.add(x.get("proxyWallet"))]
    if not lb:
        print("Failed to fetch leaderboard.")
        sys.exit(1)
    print(f"{total_fetched} traders retrieved, {total_fetched - len(lb)} duplicates removed, "
          f"{len(lb)} unique traders.\n")

    # Step 2: Phase 1 - parallel trade-count screening
    print(f"PHASE 1: trade-count screen ({MAX_WORKERS} parallel)...")
    survivors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for result in as_completed([ex.submit(screen, e) for e in lb]):
            r = result.result()
            if r:
                survivors.append(r)
    print(f"\n{len(survivors)}/{len(lb)} passed trade-count filter "
          f"({MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES}/wk).\n")
    if not survivors:
        print("No survivors. Widen MIN/MAX_WEEKLY_TRADES and retry.")
        sys.exit(1)

    # Step 3: Phase 2 - win rate, profit rate, consistency, diversity
    print(f"PHASE 2: full analysis ({MAX_WORKERS} parallel)...")
    enriched = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for result in as_completed([ex.submit(analyze, t) for t in survivors]):
            enriched.append(result.result())

    # Step 4: quality + consistency filters
    df = pd.DataFrame(enriched)
    df["_wr_num"] = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df["_pr_num"] = pd.to_numeric(df["Profit_Rate"], errors="coerce")
    df["_rr_num"] = pd.to_numeric(df["Risk_Reward"], errors="coerce")
    df["_mkt_num"] = pd.to_numeric(df["Markets_Traded"], errors="coerce")
    df["_span_num"] = pd.to_numeric(df["Sample_Span_Days"], errors="coerce")
    df["_hold_num"] = pd.to_numeric(df["Avg_Hold_Days"], errors="coerce")
    df["_dd_num"] = pd.to_numeric(df["Max_Drawdown_%"], errors="coerce")
    df["_streak_num"] = pd.to_numeric(df["Max_Consecutive_Losses"], errors="coerce")

    qualified = df[
        (df["_wr_num"] >= MIN_WIN_RATE) &
        (df["_pr_num"] >= MIN_PROFIT_RATE) &
        (df["Confidence"] == "Good data") &
        (df["_rr_num"] >= MIN_RISK_REWARD) &
        (df["_mkt_num"] >= MIN_MARKETS) &
        (df["_span_num"] >= MIN_SPAN_DAYS) &
        (df["_hold_num"].notna() & (df["_hold_num"] <= MAX_AVG_HOLD_DAYS)) &
        (df["_streak_num"] <= MAX_CONSECUTIVE_LOSSES) &
        (df["_dd_num"] <= MAX_DRAWDOWN_PCT)
    ].copy()

    timestamp_str = datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%Y%m%d-%H%M")
    out_file = f"smart_money_{timestamp_str}.csv"

    col_order = [
        "Wallet", "Name", "Score", "Win_Rate_%", "Profit_Rate",
        "Max_Consecutive_Losses", "Max_Drawdown_%", "Sample", "Confidence",
        "Avg_Hold_Days", "Risk_Reward", "Markets_Traded", "Weekly_Trades",
        "Recency_WR", "Avg_Win_$", "Avg_Loss_$", "Sample_Span_Days",
        "Median_Hold_Days", "Matched_Positions", "Wins", "Losses", "Pushes",
    ]

    if qualified.empty:
        print("\nNo traders met all filters today.")
        print(" Tip: check the raw numbers above — you may want to loosen a threshold.")
        fallback_cols = [c for c in col_order if c in df.columns and c != "Score"]
        df[fallback_cols].to_csv(out_file, index=False)
    else:
        qualified["Score"] = qualified.apply(compute_score, axis=1)
        qualified = qualified.sort_values("Score", ascending=False)
        existing_cols = [c for c in col_order if c in qualified.columns]
        qualified[existing_cols].to_csv(out_file, index=False)

    elapsed = round(time.time() - t0, 1)
    print("\n" + "=" * 70)
    print(f"Done in {elapsed}s")
    print(f" Results -> {out_file} ({len(qualified)} traders)")
    print("=" * 70)

    if not qualified.empty:
        print(f"\nTOP {len(qualified)} TRADERS TO COPY-TRADE TODAY\n")
        display = qualified[["Name", "Win_Rate_%", "Profit_Rate", "Max_Drawdown_%",
                              "Max_Consecutive_Losses", "Score"]].copy()
        display.columns = ["Name", "Win%", "ProfRate", "MaxDD%", "MaxStreak", "Score"]
        print(display.to_string(index=False))
    print("=" * 70)


if __name__ == "__main__":
    main()
