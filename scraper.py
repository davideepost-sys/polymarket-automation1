import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
# ============================================================
# NO API KEY NEEDED
# Polymarket's Data API is fully public and free.
# No ScraperAPI, no proxy, no secrets required.
# ============================================================
DATA_API = "https://data-api.polymarket.com"
NOW_TS      = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS = NOW_TS - (7 * 24 * 60 * 60)
# ── Filters ────────────────────────────────────────────────────
MIN_WEEKLY_TRADES  = 20      # too few = can't trust the win rate
MAX_WEEKLY_TRADES  = 500     # too many = bot, not safe on a small balance
MIN_WIN_RATE       = 60.0    # % — must win majority of trades
MIN_PROFIT_RATE    = 0.10    # must make at least 10c profit per $1 volume
MIN_SPAN_DAYS      = 3       # need at least 3 days of trading history
MIN_RISK_REWARD    = 0.5     # avg_win must be at least half of avg_loss (floor)
MIN_MARKETS        = 3       # must trade across at least 3 distinct markets
MAX_AVG_HOLD_DAYS  = 2.0     # day-traders only: avg hold <= 2 days
LEADERBOARD_POOL   = 100     # screen the top 100 by weekly PNL
TOP_N_OUTPUT       = 10      # show only the best 10 in the final list
# Polymarket's /closed-positions hard-caps at 50 per page
CLOSED_PAGE_SIZE   = 50
CLOSED_PAGES       = 6       # 6x50 = up to 300 recent resolved positions
MIN_SAMPLE         = 40      # need at least this many to trust a win rate
MIN_LOSSES         = 3       # must have at least 3 losses — catches 100% WR survivorship bias
# How many BUY activity records to fetch for hold-time matching
ACTIVITY_PAGES     = 5       # 5x500 = up to 2500 recent BUY trades
MAX_WORKERS        = 5       # parallel threads — keeps runtime fast
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
                print(f"    warn: {url} failed: {e}")
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
# ── Phase 2: win rate + market diversity + hold time ───────────
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
            ts    = trade.get("timestamp")
            if asset and ts and (asset not in asset_map or ts < asset_map[asset]):
                asset_map[asset] = ts
        if len(data) < 500:
            break
    return asset_map
def recent_win_rate(wallet):
    entry_map = fetch_entry_timestamps(wallet)
    r_wins = r_losses = ee_wins = ee_losses = pushes = 0
    win_pnl = []
    loss_pnl = []
    oldest_ts = None
    markets_seen = set()
    hold_times = []
    three_days_ago = NOW_TS - (3 * 24 * 60 * 60)
    weighted_wins = 0.0
    weighted_total = 0.0
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
            ts    = pos.get("timestamp")
            cp    = pos.get("curPrice")
            pnl   = pos.get("realizedPnl")
            mkt   = pos.get("market") or pos.get("conditionId") or pos.get("marketSlug")
            asset = pos.get("asset")
            if cp is None:
                continue
            try:
                cp_f  = float(cp)
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
            weight = 2.0 if (ts and ts >= three_days_ago) else 1.0
            if cp_f >= 0.999:
                r_wins += 1; win_pnl.append(pnl_f)
                weighted_wins += weight
                weighted_total += weight
            elif cp_f <= 0.001:
                r_losses += 1; loss_pnl.append(pnl_f)
                weighted_total += weight
            elif 0.499 <= cp_f <= 0.501:
                pushes += 1
            else:
                if pnl_f > 0:
                    ee_wins += 1; win_pnl.append(pnl_f)
                    weighted_wins += weight
                else:
                    ee_losses += 1; loss_pnl.append(pnl_f)
                weighted_total += weight
        if len(data) < CLOSED_PAGE_SIZE:
            break
    total_w = r_wins + ee_wins
    total_l = r_losses + ee_losses
    total   = total_w + total_l
    recency_wr = round(weighted_wins / weighted_total * 100, 1) if weighted_total else None
    avg_hold = round(sum(hold_times) / len(hold_times), 2) if hold_times else None
    median_hold = round(sorted(hold_times)[len(hold_times) // 2], 2) if hold_times else None
    return {
        "win_rate":         round(total_w / total * 100, 1) if total else None,
        "recency_wr":       recency_wr,
        "sample":           total,
        "resolved_wins":    r_wins,
        "resolved_losses":  r_losses,
        "early_exit_wins":  ee_wins,
        "early_exit_losses":ee_losses,
        "pushes":           pushes,
        "avg_win":          round(sum(win_pnl) / len(win_pnl), 2) if win_pnl else 0.0,
        "avg_loss":         round(sum(loss_pnl) / len(loss_pnl), 2) if loss_pnl else 0.0,
        "span_days":        round((NOW_TS - oldest_ts) / 86400, 1) if oldest_ts else None,
        "markets_traded":   len(markets_seen),
        "avg_hold_days":    avg_hold,
        "median_hold_days": median_hold,
        "matched_positions": len(hold_times),
    }
# ── Composite ranking score ────────────────────────────────────
def compute_score(row):
    wr  = (row.get("Recency_WR") or 0) / 100
    pr  = min(row.get("Profit_Rate", 0), 1.0)
    n   = min(row.get("Sample", 0), 200) / 200
    avg_w = row.get("Avg_Win_$", 0)
    avg_l = abs(row.get("Avg_Loss_$", 0)) or 0.01
    rr  = min(avg_w / avg_l, 5.0) / 5.0
    mkts = min(row.get("Markets_Traded", 0), 15) / 15
    hold = row.get("Avg_Hold_Days")
    if hold is not None:
        speed = max(0.0, 1.0 - (hold / 4.0))
    else:
        speed = 0.3
    score = (0.25 * wr) + (0.20 * pr) + (0.15 * n) + (0.15 * rr) + (0.10 * mkts) + (0.15 * speed)
    return round(score, 4)
# ── Worker functions (run in threads) ─────────────────────────
def screen(entry):
    wallet = entry.get("proxyWallet")
    if not wallet:
        return None
    pnl    = float(entry.get("pnl", 0))
    volume = float(entry.get("vol", 0))
    if volume <= 0:
        return None
    trades = weekly_trade_count(wallet)
    in_band = MIN_WEEKLY_TRADES <= trades <= MAX_WEEKLY_TRADES
    print(f"  {'ok' if in_band else 'no'} "
          f"{entry.get('userName', wallet[:8]):<20} {trades:>4} trades/wk")
    if not in_band:
        return None
    return {
        "Wallet":        wallet,
        "Name":          entry.get("userName") or entry.get("xUsername") or wallet[:8] + "...",
        "Weekly_Trades": trades,
        "Profit_$":      round(pnl, 2),
        "Volume_$":      round(volume, 2),
        "Profit_Rate":   round(pnl / volume, 4),
    }
def analyze(trader):
    wr = recent_win_rate(trader["Wallet"])
    wr_val = wr["win_rate"]
    confidence = ("NO DATA" if wr["sample"] == 0
                  else "LOW" if wr["sample"] < MIN_SAMPLE
                  else "OK")
    rr_ratio = round(wr["avg_win"] / max(abs(wr["avg_loss"]), 0.01), 2) if wr["avg_loss"] != 0 else "N/A"
    hold_str = f"{wr['avg_hold_days']}d" if wr["avg_hold_days"] is not None else "?"
    print(f"  {'ok' if wr_val and wr_val >= MIN_WIN_RATE else '  '} "
          f"{trader['Name']:<20} WR={wr_val}% "
          f"(n={wr['sample']}, hold={hold_str}, mkts={wr['markets_traded']}) "
          f"RR={rr_ratio} [{confidence}]")
    resolved_str = f"{wr['resolved_wins']}W/{wr['resolved_losses']}L"
    early_str    = f"{wr['early_exit_wins']}W/{wr['early_exit_losses']}L"
    total_losses = wr["resolved_losses"] + wr["early_exit_losses"]
    return {
        **trader,
        "Win_Rate_%":         wr_val if wr_val is not None else "N/A",
        "Recency_WR":         wr["recency_wr"],
        "Sample":             wr["sample"],
        "Sample_Span_Days":   wr["span_days"],
        "Resolved":           resolved_str,
        "Early_Exit":         early_str,
        "Total_Losses":       total_losses,
        "Pushes":             wr["pushes"],
        "Avg_Win_$":          wr["avg_win"],
        "Avg_Loss_$":         wr["avg_loss"],
        "Risk_Reward":        rr_ratio,
        "Markets_Traded":     wr["markets_traded"],
        "Avg_Hold_Days":      wr["avg_hold_days"],
        "Median_Hold_Days":   wr["median_hold_days"],
        "Matched_Positions":  wr["matched_positions"],
        "Confidence":         confidence,
    }
# ── Main ───────────────────────────────────────────────────────
def main():
    t0 = time.time()
    print("Polymarket Smart Money Analyzer v2")
    print(f"   Filters: {MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/wk | "
          f"WR >= {MIN_WIN_RATE}% | PR >= {MIN_PROFIT_RATE}")
    print(f"   Day-traders only: avg hold <= {MAX_AVG_HOLD_DAYS}d | "
          f"Recency weighting | Market diversity | Risk-reward")
    print(f"   No API key needed - direct Polymarket API, completely free")
    print("=" * 70)
    # Step 1: leaderboard (API caps at 50 per page, paginate to reach LEADERBOARD_POOL)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {LEADERBOARD_POOL} traders...")
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
    if not lb:
        print("Failed to fetch leaderboard.")
        sys.exit(1)
    print(f"{len(lb)} traders retrieved.\n")
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
    # Step 3: Phase 2 - parallel win-rate + diversity analysis
    print(f"PHASE 2: win-rate + market diversity analysis ({MAX_WORKERS} parallel)...")
    enriched = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for result in as_completed([ex.submit(analyze, t) for t in survivors]):
            enriched.append(result.result())
    # Step 4: apply quality filters
    df = pd.DataFrame(enriched)
    df["_wr_num"]   = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df["_rec_wr"]   = pd.to_numeric(df["Recency_WR"], errors="coerce")
    df["_rr_num"]   = pd.to_numeric(df["Risk_Reward"], errors="coerce")
    df["_mkt_num"]  = pd.to_numeric(df["Markets_Traded"], errors="coerce")
    df["_span_num"] = pd.to_numeric(df["Sample_Span_Days"], errors="coerce")
    df["_hold_num"] = pd.to_numeric(df["Avg_Hold_Days"], errors="coerce")
    qualified = df[
        (df["_wr_num"] >= MIN_WIN_RATE) &
        (df["Profit_Rate"] >= MIN_PROFIT_RATE) &
        (df["Confidence"] != "NO DATA") &
        (df["_rr_num"] >= MIN_RISK_REWARD) &
        (df["_mkt_num"] >= MIN_MARKETS) &
        (df["_span_num"] >= MIN_SPAN_DAYS) &
        (df["_hold_num"].notna() & (df["_hold_num"] <= MAX_AVG_HOLD_DAYS)) &
        (df["Total_Losses"] >= MIN_LOSSES)
    ].copy()
    qualified["Score"] = qualified.apply(compute_score, axis=1)
    qualified = (qualified
                 .sort_values("Score", ascending=False)
                 .head(TOP_N_OUTPUT)
                 .drop(columns=["_wr_num", "_rec_wr", "_rr_num", "_mkt_num", "_span_num", "_hold_num"]))
    df = df.drop(columns=["_wr_num", "_rec_wr", "_rr_num", "_mkt_num", "_span_num", "_hold_num"])
    # Step 5: save
    date_str  = datetime.now().strftime("%Y%m%d")
    full_file = f"smart_money_{date_str}.csv"
    best_file = f"best_traders_{date_str}.csv"
    col_order = [
        "Wallet", "Name", "Score", "Weekly_Trades", "Win_Rate_%", "Recency_WR",
        "Sample", "Sample_Span_Days", "Resolved", "Early_Exit", "Pushes",
        "Confidence", "Avg_Win_$", "Avg_Loss_$", "Risk_Reward", "Markets_Traded",
        "Avg_Hold_Days", "Median_Hold_Days", "Matched_Positions",
        "Profit_$", "Volume_$", "Profit_Rate"
    ]
    df["_score"] = df.apply(
        lambda r: compute_score(r) if r.get("Confidence") == "OK" else 0, axis=1
    )
    df_sorted = df.sort_values("_score", ascending=False).drop(columns="_score")
    existing_cols = [c for c in col_order if c in df_sorted.columns]
    df_sorted[existing_cols].to_csv(full_file, index=False)
    if not qualified.empty:
        best_cols = ["Name", "Win_Rate_%", "Recency_WR", "Profit_Rate",
                     "Risk_Reward", "Avg_Hold_Days", "Markets_Traded",
                     "Score", "Wallet"]
        qualified[best_cols].to_csv(best_file, index=False)
    elapsed = round(time.time() - t0, 1)
    print("\n" + "=" * 70)
    print(f"Done in {elapsed}s")
    print(f"   Full data  -> {full_file}  ({len(df_sorted)} traders)")
    print(f"   Shortlist  -> {best_file}  ({len(qualified)} traders)")
    print(f"\n{'=' * 70}")
    if qualified.empty:
        print("No traders met all filters today.")
        print("   Tip: check full CSV - you may want to loosen MIN_WIN_RATE or MIN_PROFIT_RATE.")
    else:
        print(f"TOP {len(qualified)} TRADERS TO COPY-TRADE TODAY")
        print(f"   Filters: WR >= {MIN_WIN_RATE}% | PR >= {MIN_PROFIT_RATE} | "
              f"RR >= {MIN_RISK_REWARD} | Mkts >= {MIN_MARKETS} | "
              f"Hold <= {MAX_AVG_HOLD_DAYS}d | Span >= {MIN_SPAN_DAYS}d\n")
        display = qualified[["Name", "Win_Rate_%", "Recency_WR", "Profit_Rate",
                             "Risk_Reward", "Avg_Hold_Days", "Markets_Traded",
                             "Score"]].copy()
        display.columns = ["Name", "Win%", "Recent_WR", "ProfRate", "RR", "Hold_D", "Mkts", "Score"]
        print(display.to_string(index=False))
    print("=" * 70)
if __name__ == "__main__":
    main()
