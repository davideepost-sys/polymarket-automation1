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
MAX_AVG_HOLD_DAYS  = 2.0     # day-traders only: avg hold ≤ 2 days

LEADERBOARD_POOL   = 50      # screen the top 50 by weekly PNL
TOP_N_OUTPUT       = 10      # show only the best 10 in the final list

# Polymarket's /closed-positions hard-caps at 50 per page
CLOSED_PAGE_SIZE   = 50
CLOSED_PAGES       = 3       # 3×50 = up to 150 recent resolved positions

MIN_SAMPLE         = 40      # need at least this many to trust a win rate

# How many BUY activity records to fetch for hold-time matching
ACTIVITY_PAGES     = 3       # 3×500 = up to 1500 recent BUY trades

MAX_WORKERS        = 5       # parallel threads — keeps runtime fast

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-analyzer/1.0"})


# ── API helper ─────────────────────────────────────────────────

def get(path, params=None, retries=3):
    """
    Direct GET to Polymarket's Data API — no proxy, no key, completely free.
    Retries up to 3 times on failure with a short backoff.
    """
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
                print(f"    ⚠️  {url} failed: {e}")
    return None


# ── Phase 1: weekly trade count (cheap) ────────────────────────

def weekly_trade_count(wallet, cap=MAX_WEEKLY_TRADES + 100):
    """
    Count BUY trades placed in the past 7 days.
    Stops early once count exceeds cap — bots get cut off fast.
    """
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
    """
    Fetch recent BUY activity to build an asset→first_buy_timestamp map.
    Used to calculate hold time for each closed position.

    Returns dict: {asset_id: earliest_buy_timestamp}
    """
    asset_map = {}  # asset → earliest buy timestamp

    for page in range(ACTIVITY_PAGES):
        data = get("/activity", {
            "user": wallet, "type": "TRADE", "side": "BUY",
            "limit": 500, "offset": page * 500,
            "sortBy": "TIMESTAMP", "sortDirection": "ASC",  # oldest first
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
    """
    Win rate from the most recent ~150 closed positions.

    Hold time: matches each closed position to the earliest BUY in /activity
    by asset ID. hold_days = (close_timestamp - first_buy_timestamp) / 86400.
    Positions without a matching BUY are excluded from hold-time stats but
    still counted for win/loss.

    Resolution detection via curPrice:
      1.0  → market resolved YES  → WIN  (real outcome)
      0.0  → market resolved NO   → LOSS (real outcome)
      ~0.5 → ambiguous push       → excluded
      else → sold early           → counted by realizedPnl (profitable
                                    exit = win, losing exit = loss) —
                                    EARLY EXIT trades are kept and labeled.
    """
    # First, get entry timestamps from BUY activity
    entry_map = fetch_entry_timestamps(wallet)

    r_wins = r_losses = ee_wins = ee_losses = pushes = 0
    win_pnl = []
    loss_pnl = []
    oldest_ts = None
    markets_seen = set()
    hold_times = []  # list of hold_days for each matched position

    # For recency weighting: trades in last 3 days count 2x
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

            # Calculate hold time if we have entry timestamp
            if asset and asset in entry_map and ts:
                hold_days = (ts - entry_map[asset]) / 86400
                if hold_days >= 0:  # sanity check
                    hold_times.append(hold_days)

            # Recency weight: 2x for trades in last 3 days, 1x otherwise
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
                # EARLY EXIT: sold before resolution — real trade, kept & labeled
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

    # Recency-weighted win rate: recent trades count more
    recency_wr = round(weighted_wins / weighted_total * 100, 1) if weighted_total else None

    # Hold time stats
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
    """
    Composite score combining multiple signals.
    Higher = better candidate for copy-trading.

    Components (weights sum to 1.0):
      25% — recency-weighted win rate (recent performance matters more)
