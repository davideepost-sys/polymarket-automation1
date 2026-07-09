import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# ------------------------------------------------------------
# API settings – no key needed
# ------------------------------------------------------------
DATA_API = "https://data-api.polymarket.com"
NOW_TS = int(datetime.now(timezone.utc).timestamp())
WEEK_AGO_TS = NOW_TS - (7 * 24 * 60 * 60)

# ------------------------------------------------------------
# Filters – you can adjust these
# ------------------------------------------------------------
MIN_WEEKLY_TRADES = 20
MAX_WEEKLY_TRADES = 500
MIN_WIN_RATE = 60.0
MIN_PROFIT_RATE = 0.10
MIN_SPAN_DAYS = 3
MIN_RISK_REWARD = 0.5
MIN_MARKETS = 3
MAX_AVG_HOLD_DAYS = 2.0
LEADERBOARD_POOL = 500

CLOSED_PAGE_SIZE = 50
CLOSED_PAGES = 5                # 250 most recent closed positions
MIN_SAMPLE = 40                 # minimum trades to trust (filter)
ACTIVITY_PAGES = 8              # up to 4,000 BUY records for hold‑time matching
MAX_WORKERS = 5

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-analyzer/1.0"})

# ------------------------------------------------------------
# API helper
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# Phase 1: weekly trade count
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# Phase 2: win rate + market diversity + hold time
# ------------------------------------------------------------
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
    entry_map = fetch_entry_timestamps(wallet)

    # Stats based on realised PnL (reliable)
    wins = 0
    losses = 0
    pushes = 0
    win_pnl = []
    loss_pnl = []
    oldest_ts = None
    markets_seen = set()
    hold_times = []
    three_days_ago = NOW_TS - (3 * 24 * 60 * 60)
    weighted_wins = 0.0
    weighted_total = 0.0

    # For display only (resolved/early‑exit categories)
    r_wins = r_losses = ee_wins = ee_losses = 0

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
                cp_f = float(cp)
                pnl_f = float(pnl) if pnl is not None else 0.0
            except (TypeError, ValueError):
                continue

            if ts and (oldest_ts is None or ts < oldest_ts):
                oldest_ts = ts
            if mkt:
                markets_seen.add(mkt)

            # Hold time
            if asset and asset in entry_map and ts:
                hold_days = (ts - entry_map[asset]) / 86400
                if hold_days >= 0:
                    hold_times.append(hold_days)

            # Win/loss based on realised PnL – the reliable way
            if pnl_f > 0:
                wins += 1
                win_pnl.append(pnl_f)
                weight = 2.0 if (ts and ts >= three_days_ago) else 1.0
                weighted_wins += weight
                weighted_total += weight
            elif pnl_f < 0:
                losses += 1
                loss_pnl.append(pnl_f)
                weight = 2.0 if (ts and ts >= three_days_ago) else 1.0
                weighted_total += weight
            else:
                pushes += 1

            # Display categories (not used for win rate)
            if cp_f >= 0.999:
                r_wins += 1
            elif cp_f <= 0.001:
                r_losses += 1
            else:
                if pnl_f > 0:
                    ee_wins += 1
                else:
                    ee_losses += 1

        if len(data) < CLOSED_PAGE_SIZE:
            break

    total = wins + losses
    recency_wr = round(weighted_wins / weighted_total * 100, 1) if weighted_total else None
    avg_hold = round(sum(hold_times) / len(hold_times), 2) if hold_times else None
    median_hold = round(sorted(hold_times)[len(hold_times) // 2], 2) if hold_times else None

    return {
        "win_rate": round(wins / total * 100, 1) if total else None,
        "recency_wr": recency_wr,
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
        "resolved_wins": r_wins,
        "resolved_losses": r_losses,
        "early_exit_wins": ee_wins,
        "early_exit_losses": ee_losses,
    }

# ------------------------------------------------------------
# Composite ranking score (Profit Rate has 20% weight; RR not included)
# ------------------------------------------------------------
def compute_score(row):
    wr = (row.get("Recency_WR") or 0) / 100
    pr = min(row.get("Profit_Rate", 0), 1.0)          # 20% weight
    n = min(row.get("Sample", 0), 200) / 200           # 15%
    mkts = min(row.get("Markets_Traded", 0), 15) / 15  # 10%
    hold = row.get("Avg_Hold_Days")
    if hold is not None:
        speed = max(0.0, 1.0 - (hold / 4.0))           # 15%
    else:
        speed = 0.3
    # Weights: WR 25%, PR 20%, sample 15%, markets 10%, speed 15% = 85% total
    # (RR has 0% weight)
    score = (0.25 * wr) + (0.20 * pr) + (0.15 * n) + (0.10 * mkts) + (0.15 * speed)
    return round(score, 4)

# ------------------------------------------------------------
# Worker functions
# ------------------------------------------------------------
def screen(entry):
    wallet = entry.get("proxyWallet")
    if not wallet:
        return None
    pnl = float(entry.get("pnl", 0))
    volume = float(entry.get("vol", 0))
    if volume <= 0:
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
        "Profit_$": round(pnl, 2),
        "Volume_$": round(volume, 2),
        "Profit_Rate": round(pnl / volume, 4),
    }

def analyze(trader):
    wr = recent_win_rate(trader["Wallet"])
    wr_val = wr["win_rate"]
    sample = wr["sample"]
    # Confidence labels: LOW < 40, OK between 40 and 74, Good data >= 75
    if sample < MIN_SAMPLE:
        confidence = "LOW"
    elif sample < 75:
        confidence = "OK"
    else:
        confidence = "Good data"

    rr_ratio = round(wr["avg_win"] / max(abs(wr["avg_loss"]), 0.01), 2) if wr["avg_loss"] != 0 else "N/A"
    hold_str = f"{wr['avg_hold_days']}d" if wr['avg_hold_days'] is not None else "?"
    print(f" {'ok' if wr_val and wr_val >= MIN_WIN_RATE else ' '} "
          f"{trader['Name']:<20} WR={wr_val}% "
          f"(n={sample}, hold={hold_str}, mkts={wr['markets_traded']}) "
          f"RR={rr_ratio} [{confidence}]")

    resolved_str = f"{wr['resolved_wins']}W/{wr['resolved_losses']}L"
    early_str = f"{wr['early_exit_wins']}W/{wr['early_exit_losses']}L"

    return {
        **trader,
        "Win_Rate_%": wr_val if wr_val is not None else "N/A",
        "Recency_WR": wr["recency_wr"],
        "Sample": sample,
        "Sample_Span_Days": wr["span_days"],
        "Resolved": resolved_str,
        "Early_Exit": early_str,
        "Wins": wr["wins"],
        "Losses": wr["losses"],
        "Pushes": wr["pushes"],
        "Avg_Win_$": wr["avg_win"],
        "Avg_Loss_$": wr["avg_loss"],
        "Risk_Reward": rr_ratio,
        "Markets_Traded": wr["markets_traded"],
        "Avg_Hold_Days": wr["avg_hold_days"],
        "Median_Hold_Days": wr["median_hold_days"],
        "Matched_Positions": wr["matched_positions"],
        "Confidence": confidence,
    }

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    t0 = time.time()
    print("Polymarket Smart Money Analyzer v2 (weekly)")
    print(f" Filters: {MIN_WEEKLY_TRADES}-{MAX_WEEKLY_TRADES} trades/wk | "
          f"WR >= {MIN_WIN_RATE}% | PR >= {MIN_PROFIT_RATE}")
    print(f" Day-traders only: avg hold <= {MAX_AVG_HOLD_DAYS}d | "
          f"Recency weighting | Market diversity")
    print("=" * 70)

    # Step 1: fetch weekly leaderboard
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching top {LEADERBOARD_POOL} weekly traders...")
    lb = []
    page_size = 50
    for offset in range(0, LEADERBOARD_POOL, page_size):
        limit = min(page_size, LEADERBOARD_POOL - offset)
        page = get("/v1/leaderboard", {
            "timePeriod": "WEEK",          # <-- WEEKLY, not monthly
            "orderBy": "PNL",
            "limit": limit,
            "offset": offset
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

    # Step 2: Phase 1 - trade-count screening
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

    # Step 3: Phase 2 - win-rate + diversity analysis
    print(f"PHASE 2: win-rate + market diversity analysis ({MAX_WORKERS} parallel)...")
    enriched = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for result in as_completed([ex.submit(analyze, t) for t in survivors]):
            enriched.append(result.result())

    # Step 4: apply quality filters
    df = pd.DataFrame(enriched)
    df["wr_num"] = pd.to_numeric(df["Win_Rate_%"], errors="coerce")
    df["_rec_wr"] = pd.to_numeric(df["Recency_WR"], errors="coerce")
    df["_rr_num"] = pd.to_numeric(df["Risk_Reward"], errors="coerce")
    df["_mkt_num"] = pd.to_numeric(df["Markets_Traded"], errors="coerce")
    df["_span_num"] = pd.to_numeric(df["Sample_Span_Days"], errors="coerce")
    df["_hold_num"] = pd.to_numeric(df["Avg_Hold_Days"], errors="coerce")

    # Filter: require confidence not LOW (i.e., sample >= 40)
    qualified = df[
        (df["wr_num"] >= MIN_WIN_RATE) &
        (df["Profit_Rate"] >= MIN_PROFIT_RATE) &
        (df["Confidence"] != "LOW") &
        (df["_rr_num"] >= MIN_RISK_REWARD) &
        (df["_mkt_num"] >= MIN_MARKETS) &
        (df["_span_num"] >= MIN_SPAN_DAYS) &
        (df["_hold_num"].notna() & (df["_hold_num"] <= MAX_AVG_HOLD_DAYS))
    ].copy()

    if qualified.empty:
        print("\nNo traders met all filters today.")
        print(" Tip: check full CSV - you may want to loosen MIN_WIN_RATE or MIN_PROFIT_RATE.")
        # Save empty CSV anyway (with all analysed traders)
        timestamp_str = datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%Y%m%d-%H%M")
        full_file = f"smart_money_{timestamp_str}.csv"
        df[["Wallet", "Name", "Weekly_Trades", "Win_Rate_%", "Recency_WR",
            "Sample", "Sample_Span_Days", "Resolved", "Early_Exit", "Wins", "Losses", "Pushes",
            "Confidence", "Avg_Win_$", "Avg_Loss_$", "Risk_Reward",
            "Markets_Traded", "Avg_Hold_Days", "Median_Hold_Days",
            "Matched_Positions", "Profit_$", "Volume_$", "Profit_Rate"]].to_csv(full_file, index=False)
        print(f" Full data saved to {full_file}")
        return

    # Compute scores and sort
    qualified["Score"] = qualified.apply(compute_score, axis=1)
    qualified = qualified.sort_values("Score", ascending=False)

    # Step 5: save only the full CSV (one file, no watchlist)
    timestamp_str = datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%Y%m%d-%H%M")
    full_file = f"smart_money_{timestamp_str}.csv"

    col_order = [
        "Wallet", "Name", "Score", "Weekly_Trades", "Win_Rate_%", "Recency_WR",
        "Sample", "Sample_Span_Days", "Resolved", "Early_Exit", "Wins", "Losses", "Pushes",
        "Confidence", "Avg_Win_$", "Avg_Loss_$", "Risk_Reward",
        "Markets_Traded", "Avg_Hold_Days", "Median_Hold_Days",
        "Matched_Positions", "Profit_$", "Volume_$", "Profit_Rate"
    ]
    existing_cols = [c for c in col_order if c in qualified.columns]
    qualified[existing_cols].to_csv(full_file, index=False)

    elapsed = round(time.time() - t0, 1)
    print("\n" + "=" * 70)
    print(f"Done in {elapsed}s")
    print(f" Full data -> {full_file} ({len(qualified)} traders)")

    # Display top traders in console (bold for top 5)
    print(f"\nTOP {len(qualified)} TRADERS TO COPY-TRADE TODAY")
    print(f" Filters: WR >= {MIN_WIN_RATE}% | PR >= {MIN_PROFIT_RATE} | "
          f"RR >= {MIN_RISK_REWARD} | Mkts >= {MIN_MARKETS} | "
          f"Hold <= {MAX_AVG_HOLD_DAYS}d | Span >= {MIN_SPAN_DAYS}d\n")
    display = qualified[["Name", "Win_Rate_%", "Recency_WR", "Profit_Rate",
                         "Risk_Reward", "Avg_Hold_Days", "Markets_Traded",
                         "Score"]].copy()
    display.columns = ["Name", "Win%", "Recent_WR", "ProfRate", "RR", "Hold_D", "Mkts", "Score"]
    lines = display.to_string(index=False).split("\n")
    for i, line in enumerate(lines):
        if i == 0:
            print(line)
        elif 1 <= i <= min(5, len(lines) - 1):
            print(f"\033[1m{line}\033[0m")   # bold for top 5
        else:
            print(line)
    print("=" * 70)

if __name__ == "__main__":
    main()
