"""
Polymarket Smart Money — concurrent build for speed.

Pipeline:
  1. Pull the weekly PnL leaderboard (top POOL traders, last 7 days).
  2. Keep only wallets doing MIN_TRADES_PER_WEEK-MAX_TRADES_PER_WEEK trades/week.
  3. For survivors, pull their last CLOSED_POSITIONS_LIMIT closed positions
     and compute WinRate, RR, AvgWin, AvgLoss, AvgHoldingDays, MarketCount.
  4. Apply hard filters (WR / ProfitRate / hold-time / loss-ratio /
     sample size), then rank survivors by a weighted composite Score.
  5. Write survivors to a CSV.

Design principles:
  1. No hidden math. Each number comes from a clearly named source.
  2. No silent guessing. Rate limits and errors are reported honestly.
  3. Concurrent processing — fetches multiple traders at once for speed.

No API key needed — Polymarket's Data API is public.

--- CHANGES FROM PREVIOUS VERSION ---
1. FIXED: trade count now counts BOTH buys and sells (was BUY-only).
2. FIXED: traders with too little holding-time data are REJECTED, not
   waved through with "N/A".
3. FIXED: closed-positions fetch caps at exactly 300.
4. FIXED (accuracy): hold time is now matched per-trade via a FIFO
   queue of buy timestamps per asset. The old version matched every
   closed position on an asset to the SAME single "earliest buy ever
   seen" for that asset — so a trader who round-tripped the same
   market five times had all five holds measured against one stale
   buy instead of each trade's own real entry. FIFO (oldest buy is
   consumed by the oldest close) gives each trade its own real hold.
5. ADDED: AvgWin, AvgLoss, RR (reward:risk = AvgWin / |AvgLoss|),
   MarketCount — computed from data already being fetched.
6. ADDED (filter): MIN_HOLD_DAYS floor. A trader whose average hold
   rounds to ~0 is very likely running latency-sensitive arbitrage —
   a copy-bot can't realistically react fast enough to replicate that
   edge, so it's rejected even if every number about it is accurate.
7. REPLACED: Score is no longer a naive multiplication (which let
   near-zero hold time explode and dominate the entire ranking — the
   #1 trader in a prior run scored 4x higher than #2 purely because
   of that). Score is now a proper weighted composite:
     ProfitRate 25% + WinRate 25% + RR 20% + AvgHoldingDays 15%
     + SampleSize 7.5% + MarketCount 7.5%
   Each metric is min-max normalized across the surviving pool first
   (0-1 scale) so the percentages are real percentages of the final
   score, not just raw numbers with wildly different scales fighting
   each other. This can only be computed AFTER every trader has been
   analyzed (needs the min/max across the whole survivor pool), so
   Score is now assigned in a second pass in main(), not inside
   analyze_trader().
8. NOTED (not auto-fixed, needs your input): ProfitRate still comes
   from the leaderboard's weekly pnl/vol, while WinRate/RR come from
   up to 300 closed positions with no week limit — two different data
   windows. If you want them fully consistent, ProfitRate should be
   recomputed from the same closed-positions batch.
9. NO drawdown / consecutive-loss filter added — this is a new
   feature, not a bug fix, and needs you to decide thresholds first.
"""
import sys
import time
import csv
import json
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_API = "https://data-api.polymarket.com"
USER_AGENT = "polymarket-minimal/2.1"

# ---- knobs ---------------------------------------------------------------
POOL = 1000
CLOSED_POSITIONS_LIMIT = 300     # "last 300 closed positions"
CLOSED_PAGE_SIZE = 50
CLOSED_MAX_PAGES = CLOSED_POSITIONS_LIMIT // CLOSED_PAGE_SIZE  # 6 pages
ACTIVITY_MAX_PAGES = 5          # see NOTE 6 above re: hold-time bias
MIN_HOLD_MATCHES = 5
MAX_RETRIES = 3
BACKOFF_SECONDS = 2.0
POLITE_DELAY = 0.12

MIN_TRADES_PER_WEEK = 21
MAX_TRADES_PER_WEEK = 700
MIN_SAMPLE_SIZE = 30            # was 10 — a 75% win rate on 11 trades is noise, not skill
MIN_PROFIT_RATE = 0.10
MAX_PROFIT_RATE = 2.00          # sanity ceiling: 200%+ weekly return on volume is
                                 # almost never real skill — reject as a probable data glitch
MIN_WIN_RATE = 75.0
MIN_HOLD_DAYS = 0.02            # ~29 minutes. Below this, a trade closes
                                 # faster than a copy-bot can realistically
                                 # react — reject even if the number is real,
                                 # since it's not something you can copy.
MAX_HOLD_DAYS = 1.5
MIN_HOLD_COVERAGE = 0.30        # matched hold-times must cover at least 30% of a
                                 # trader's decided trades, or the average isn't trustworthy
MAX_LOSS_TO_WIN_RATIO = 2.0     # reject if the average loss is more than 2x the
                                 # average win — a single bad trade shouldn't be able
                                 # to erase several good ones

# Score weights — must sum to 1.0. See docstring point 7.
WEIGHT_PROFIT_RATE = 0.25
WEIGHT_WIN_RATE = 0.25
WEIGHT_RR = 0.20
WEIGHT_HOLD = 0.15
WEIGHT_SAMPLE_SIZE = 0.075
WEIGHT_MARKET_COUNT = 0.075

WORKERS = 15                # how many traders to fetch in parallel

# thread-safe print and lock for API politeness
_print_lock = threading.Lock( )
_api_lock = threading.Lock()
_last_call_time = [0.0]     # mutable container so threads share it

def _safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

def _polite_wait():
    """Ensure minimum delay between API calls across all threads."""
    with _api_lock:
        now = time.monotonic()
        elapsed = now - _last_call_time[0]
        if elapsed < POLITE_DELAY:
            time.sleep(POLITE_DELAY - elapsed)
        _last_call_time[0] = time.monotonic()

# ==========================================================================
#  Fetch layer
# ==========================================================================
class RateLimited(Exception):
    pass
class FetchError(Exception):
    pass
class EndOfData(Exception):
    pass

def _get(path, params):
    url = f"{DATA_API}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(MAX_RETRIES):
        _polite_wait()
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = BACKOFF_SECONDS * (attempt + 1) * 2
                time.sleep(wait)
                last_err = RateLimited(url)
                continue
            if e.code >= 500:
                last_err = FetchError(f"server {e.code}")
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
                continue
            if e.code == 400:
                try:
                    body = e.read().decode("utf-8", "replace").lower()
                except Exception:
                    body = ""
                if "offset" in body:
                    raise EndOfData(url)
                raise FetchError(f"{url}: HTTP 400 {body[:120]}")
            raise FetchError(f"{url}: HTTP {e.code}")
        except (URLError, TimeoutError, ValueError) as e:
            last_err = e
            time.sleep(BACKOFF_SECONDS * (attempt + 1))
            continue
    if isinstance(last_err, RateLimited):
        raise RateLimited(url)
    raise FetchError(f"{url}: {last_err}")

def _fetch_pages(path, base_params, page_size, max_pages):
    rows = []
    for page in range(max_pages):
        params = dict(base_params)
        params["limit"] = page_size
        params["offset"] = page * page_size
        try:
            data = _get(path, params)
        except EndOfData:
            return rows, True
        except (RateLimited, FetchError):
            return rows, False
        if not isinstance(data, list):
            return rows, False
        rows.extend(data)
        if len(data) < page_size:
            return rows, True
    return rows, True

# ==========================================================================
#  Leaderboard (sequential — single call per page, fast)
# ==========================================================================
def get_leaderboard(pool):
    rows = []
    page_size = 50
    offset = 0
    while len(rows) < pool:
        limit = min(page_size, pool - offset)
        try:
            page = _get("/v1/leaderboard", {
                "timePeriod": "WEEK", "orderBy": "PNL",
                "limit": limit, "offset": offset,
            })
        except (RateLimited, FetchError) as e:
            _safe_print(f"  leaderboard fetch failed: {e}")
            break
        if not page:
            break
        rows.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return rows[:pool]

def get_leaderboard_page(time_period, order_by, limit, offset):
    # This function is specifically for lookup_trader.py to get a single page
    return _get("/v1/leaderboard", {
        "timePeriod": time_period, 
        "orderBy": order_by,
        "limit": limit, 
        "offset": offset,
    })

# ==========================================================================
#  Per-trader analysis — everything needed for ONE trader
# ==========================================================================
def profit_rate(entry):
    vol = entry.get("vol")
    pnl = entry.get("pnl")
    if not vol or vol <= 0 or pnl is None:
        return None
    return round(pnl / vol, 4)

def analyze_trader(entry):
    """
    Full analysis of one trader. Returns a dict with all stats, or None if
    the trader doesn't pass filters. Thread-safe — no shared mutable state.
    """
    wallet = entry.get("proxyWallet")
    name = entry.get("userName") or entry.get("xUsername") or (wallet[:8] + "…")
    if not wallet:
        return None

    # --- profit rate (instant, from leaderboard) ---
    pr = profit_rate(entry)
    if pr is None or pr < MIN_PROFIT_RATE:
        return {"skip": "low_profit"}
    if pr > MAX_PROFIT_RATE:
        # A 200%+ weekly return on volume is almost always a data artifact
        # (e.g. tiny volume divided into an outsized pnl), not real skill.
        # Reject rather than let it dominate the ranking.
        return {"skip": "implausible_profit_rate"}

    # --- activity data (shared by trade count + entry times) ---
    # FIX #1: no "side" filter here anymore — we need BOTH buys and sells
    # to get a true count of weekly trades. (Original only pulled BUY,
    # which cut the real trade count roughly in half.)
    activity_rows, activity_complete = _fetch_pages(
        "/activity",
        {"user": wallet, "type": "TRADE",
         "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        page_size=500, max_pages=ACTIVITY_MAX_PAGES,
    )

    # count ALL trades (buys + sells) in past 7 days
    week_ago = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    trade_count = sum(1 for t in activity_rows if t.get("timestamp", 0) >= week_ago)
    if trade_count < MIN_TRADES_PER_WEEK or trade_count > MAX_TRADES_PER_WEEK:
        return {"skip": "trade_count", "trade_count": trade_count}

    # FIFO queue of BUY timestamps per asset, oldest first. Each closed
    # position later gets matched to its own earliest *unused* buy —
    # not just the single earliest buy ever seen for that asset — so
    # repeat round-trips on the same market get their own real hold
    # time instead of all being measured against one stale buy.
    buys_by_asset = defaultdict(list)
    for t in activity_rows:
        if t.get("side") != "BUY":
            continue
        a, ts = t.get("asset"), t.get("timestamp")
        if a and ts:
            buys_by_asset[a].append(ts)
    buys_by_asset = {a: deque(sorted(ts_list)) for a, ts_list in buys_by_asset.items()}

    # --- closed positions (last CLOSED_POSITIONS_LIMIT, most recent first) ---
    closed_rows, closed_complete = _fetch_pages(
        "/closed-positions",
        {"user": wallet, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        page_size=CLOSED_PAGE_SIZE, max_pages=CLOSED_MAX_PAGES,
    )
    closed_rows = closed_rows[:CLOSED_POSITIONS_LIMIT]

    wins = losses = ties = 0
    win_amounts = []
    loss_amounts = []
    markets = set()
    for p in closed_rows:
        pnl_val = p.get("realizedPnl")
        if pnl_val is None:
            continue
        try:
            pnl_val = float(pnl_val)
        except (TypeError, ValueError):
            continue
        asset = p.get("asset")
        if asset:
            markets.add(asset)
        if pnl_val > 0:
            wins += 1
            win_amounts.append(pnl_val)
        elif pnl_val < 0:
            losses += 1
            loss_amounts.append(pnl_val)
        else:
            ties += 1

    # Match each closed position to its own buy via FIFO: process closes
    # oldest-first, and for each one consume the oldest still-unused buy
    # on that asset. This gives repeat-traded markets accurate individual
    # hold times instead of all sharing one stale buy timestamp.
    holds = []
    for p in sorted(closed_rows, key=lambda x: x.get("timestamp") or 0):
        asset = p.get("asset")
        ts = p.get("timestamp")
        if not asset or not ts:
            continue
        queue = buys_by_asset.get(asset)
        if queue:
            buy_ts = queue.popleft()
            d = (ts - buy_ts) / 86400.0
            if d >= 0:
                holds.append(d)

    decided = wins + losses
    if decided < MIN_SAMPLE_SIZE:
        return {"skip": "small_sample", "trade_count": trade_count, "sample": decided}

    win_rate = round(wins / decided * 100, 1)
    if win_rate < MIN_WIN_RATE:
        return {"skip": "low_winrate", "trade_count": trade_count, "win_rate": win_rate}

    # FIX #2: not enough matched holding-time data means we DON'T KNOW
    # the hold time — that is a REJECT, not a free pass. The original
    # code let these traders through as "N/A" without ever checking
    # MAX_HOLD_DAYS.
    if len(holds) < MIN_HOLD_MATCHES:
        return {"skip": "insufficient_hold_data", "trade_count": trade_count,
                 "win_rate": win_rate, "matched": len(holds)}

    # If only a small slice of a trader's decided trades have a matched
    # buy-time, the average hold time is built off a cherry-picked handful,
    # not a representative sample. This is what causes the fake "0.0 day"
    # holds seen on very active traders — reject rather than trust it.
    hold_coverage = len(holds) / decided
    if hold_coverage < MIN_HOLD_COVERAGE:
        return {"skip": "unreliable_hold_data", "trade_count": trade_count,
                 "win_rate": win_rate, "matched": len(holds), "sample": decided}

    avg_hold = round(sum(holds) / len(holds), 2)
    if avg_hold < MIN_HOLD_DAYS:
        # Closes too fast to realistically copy. See docstring point 6.
        return {"skip": "too_fast_to_copy", "trade_count": trade_count,
                 "win_rate": win_rate, "avg_hold": avg_hold}
    if avg_hold > MAX_HOLD_DAYS:
        return {"skip": "high_hold", "trade_count": trade_count,
                 "win_rate": win_rate, "avg_hold": avg_hold}

    avg_win = sum(win_amounts) / wins if wins > 0 else 0
    avg_loss = sum(loss_amounts) / losses if losses > 0 else 0
    rr = round(abs(avg_win / avg_loss), 1) if avg_loss < 0 else None
    if rr is not None and rr < MAX_LOSS_TO_WIN_RATIO:
        return {"skip": "risky_loss_ratio", "trade_count": trade_count,
                 "win_rate": win_rate, "avg_hold": avg_hold, "rr": rr}

    return {
        "ProfitRate": pr,
        "WinRate": win_rate,
        "RR": rr,
        "AvgHoldingDays": avg_hold,
        "WeeklyTrades": trade_count,
        "MarketCount": len(markets),
        "SampleSize": decided,
    }

# ==========================================================================
#  Main entry point
# ==========================================================================

def main():
    _safe_print("Starting Polymarket Smart Money Analyzer...")
    start_time = time.monotonic()

    # 1. Pull the weekly PnL leaderboard
    _safe_print(f"Fetching top {POOL} traders from weekly leaderboard...")
    leaderboard = get_leaderboard(POOL)
    _safe_print(f"  Found {len(leaderboard)} traders.")

    # 2. Filter by trade count (MIN_TRADES_PER_WEEK-MAX_TRADES_PER_WEEK)
    # This is done inside analyze_trader now.

    # 3. Analyze each trader concurrently
    _safe_print("Analyzing traders...")
    analyzed_traders = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(analyze_trader, entry): entry for entry in leaderboard}
        for future in as_completed(futures):
            entry = futures[future]
            try:
                result = future.result()
                if result and "skip" not in result:
                    analyzed_traders.append(result)
                else:
                    pass # _safe_print(f"  Skipping {entry.get('userName') or entry.get('proxyWallet')[:8]}...: {result.get('skip')}")
            except (RateLimited, FetchError) as e:
                _safe_print(f"  Error analyzing {entry.get('userName') or entry.get('proxyWallet')[:8]}...: {e}")

    _safe_print(f"  {len(analyzed_traders)} traders passed initial filters.")

    if not analyzed_traders:
        _safe_print("No traders passed all filters. Exiting.")
        return

    # 4. Normalize metrics and compute composite Score
    _safe_print("Normalizing metrics and computing scores...")
    # Extract all values for normalization
    profit_rates = [t["ProfitRate"] for t in analyzed_traders]
    win_rates = [t["WinRate"] for t in analyzed_traders]
    rrs = [t["RR"] for t in analyzed_traders if t["RR"] is not None]
    avg_holds = [t["AvgHoldingDays"] for t in analyzed_traders]
    sample_sizes = [t["SampleSize"] for t in analyzed_traders]
    market_counts = [t["MarketCount"] for t in analyzed_traders]

    # Min-Max Normalization helper
    def normalize(value, min_val, max_val):
        if max_val == min_val: return 0.0
        return (value - min_val) / (max_val - min_val)

    min_pr, max_pr = min(profit_rates), max(profit_rates)
    min_wr, max_wr = min(win_rates), max(win_rates)
    min_rr, max_rr = min(rrs), max(rrs) if rrs else 0
    min_hold, max_hold = min(avg_holds), max(avg_holds)
    min_sample, max_sample = min(sample_sizes), max(sample_sizes)
    min_market, max_market = min(market_counts), max(market_counts)

    for trader in analyzed_traders:
        norm_pr = normalize(trader["ProfitRate"], min_pr, max_pr)
        norm_wr = normalize(trader["WinRate"], min_wr, max_wr)
        norm_rr = normalize(trader["RR"], min_rr, max_rr) if trader["RR"] is not None else 0.0
        # For hold time, lower is generally better (within limits), so invert normalization
        norm_hold = 1.0 - normalize(trader["AvgHoldingDays"], min_hold, max_hold)
        norm_sample = normalize(trader["SampleSize"], min_sample, max_sample)
        norm_market = normalize(trader["MarketCount"], min_market, max_market)

        trader["Score"] = (
            norm_pr * WEIGHT_PROFIT_RATE +
            norm_wr * WEIGHT_WIN_RATE +
            norm_rr * WEIGHT_RR +
            norm_hold * WEIGHT_HOLD +
            norm_sample * WEIGHT_SAMPLE_SIZE +
            norm_market * WEIGHT_MARKET_COUNT
        )

    # Sort by Score (descending)
    analyzed_traders.sort(key=lambda x: x["Score"], reverse=True)

    # 5. Write survivors to a CSV
    output_filename = f"traders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    _safe_print(f"Writing {len(analyzed_traders)} traders to {output_filename}...")
    if analyzed_traders:
        fieldnames = analyzed_traders[0].keys()
        with open(output_filename, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(analyzed_traders)

    end_time = time.monotonic()
    _safe_print(f"Analysis complete in {end_time - start_time:.2f} seconds.")

if __name__ == "__main__":
    # If run directly, allow overriding POOL size for testing
    if len(sys.argv) > 1:
        try:
            POOL = int(sys.argv[1])
        except ValueError:
            _safe_print("Invalid pool size. Using default.")
    main()
