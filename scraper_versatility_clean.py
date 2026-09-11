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
CLOSED_POSITIONS_LIMIT = 2000    # broader sample for validation; not a curve filter
CLOSED_PAGE_SIZE = 50
CLOSED_MAX_PAGES = CLOSED_POSITIONS_LIMIT // CLOSED_PAGE_SIZE  # 40 pages
ACTIVITY_MAX_PAGES = 5          # see NOTE 6 above re: hold-time bias
MIN_HOLD_MATCHES = 5
MAX_RETRIES = 3
BACKOFF_SECONDS = 2.0
POLITE_DELAY = 0.12

MIN_TRADES_PER_WEEK = 21
MAX_TRADES_PER_WEEK = 700
MIN_SAMPLE_SIZE = 30            # keep a minimum sample, but inspect up to 2000 closed positions
# Experimental net-profit floor.
# PolyGun publishes 1% on each buy and sell = 2% round trip.
# Polymarket taker costs vary by market/price; reserve 5% conservatively.
POLYGUN_FEE_RESERVE = 0.02
POLYMARKET_FEE_RESERVE = 0.04
SAFETY_MARGIN_RESERVE = 0.00
COST_RESERVE_RATE = POLYGUN_FEE_RESERVE + POLYMARKET_FEE_RESERVE
MIN_PROFIT_RATE = 0.06
MAX_PROFIT_RATE = 1.00          # sanity ceiling: 200%+ weekly return on volume is
                                 # almost never real skill — reject as a probable data glitch
MIN_WIN_RATE = 65.0
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
_print_lock = threading.Lock()
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
    """Single leaderboard page — used by lookup_trader.py to paginate
    manually when searching for one specific trader."""
    return _get("/v1/leaderboard", {
        "timePeriod": time_period, "orderBy": order_by,
        "limit": limit, "offset": offset,
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
    hold_data_reliable = len(holds) >= MIN_HOLD_MATCHES

    # If hold timestamps are missing, keep the trader for inspection instead
    # of silently losing a potentially good trader. Hold is reported as N/A
    # and its copy-speed filters are not applied without reliable data.

    # If only a small slice has a matched buy-time, keep the trader but mark
    # hold data unavailable; never invent a hold value or discard the trader.
    hold_coverage = len(holds) / decided
    if hold_coverage < MIN_HOLD_COVERAGE:
        hold_data_reliable = False

    avg_hold = round(sum(holds) / len(holds), 2) if hold_data_reliable else None
    if avg_hold is not None and avg_hold < MIN_HOLD_DAYS:
        # Closes too fast to realistically copy-trade — likely latency-
        # sensitive arbitrage, not a repeatable strategy you can follow.
        return {"skip": "too_fast_to_copy", "trade_count": trade_count,
                 "win_rate": win_rate, "avg_hold": avg_hold}
    if avg_hold is not None and avg_hold > MAX_HOLD_DAYS:
        return {"skip": "high_hold", "trade_count": trade_count, "win_rate": win_rate, "avg_hold": avg_hold}

    avg_win = round(sum(win_amounts) / len(win_amounts), 2) if win_amounts else 0.0
    avg_loss = round(sum(loss_amounts) / len(loss_amounts), 2) if loss_amounts else 0.0

    # Reject lopsided risk profiles: a high win rate doesn't help you if
    # the rare loss is big enough to erase several wins' worth of profit.
    if avg_win > 0 and abs(avg_loss) > MAX_LOSS_TO_WIN_RATIO * avg_win:
        return {"skip": "risky_loss_ratio", "trade_count": trade_count,
                 "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss}

    # RR (reward:risk) = avg win / avg loss magnitude. If a trader has zero
    # losses (100% win rate on all decided trades), RR is undefined — mark
    # it None here and main() will assign it the best finite RR seen across
    # the survivor pool once every trader has been analyzed.
    rr = round(avg_win / abs(avg_loss), 4) if avg_loss != 0 else None

    complete = activity_complete and closed_complete
    return {
        "Name": name,
        "TraderID": wallet,
        "WeeklyTrades": trade_count,
        "ProfitRate": pr,
        "WinRate": win_rate,
        "RR": rr,
        "AvgWin": avg_win,
        "AvgLoss": avg_loss,
        "AvgHoldingDays": avg_hold,
        "MarketCount": len(markets),
        "_complete": complete,
        "sample": decided,
        "matched": len(holds),
        "HoldDataStatus": "OK" if hold_data_reliable else "N/A - insufficient matched hold data",
    }

# ==========================================================================
#  Run — concurrent
# ==========================================================================
def main():
    pool = POOL
    if len(sys.argv) > 1:
        try:
            pool = int(sys.argv[1])
        except ValueError:
            pass

    _safe_print("Polymarket Smart Money — concurrent build")
    _safe_print(f"Reading top {pool} weekly traders (by PNL)")
    _safe_print(f"Using {WORKERS} parallel workers\n")

    lb = get_leaderboard(pool)
    if not lb:
        _safe_print("Could not read the leaderboard. Stopping.")
        sys.exit(1)

    _safe_print(f"Got {len(lb)} traders from leaderboard. Analyzing with {WORKERS} workers...\n")

    filtered_traders = []
    skipped_details = []
    skipped = {"no_wallet": 0, "trade_count": 0, "low_profit": 0,
               "implausible_profit_rate": 0, "small_sample": 0,
               "low_winrate": 0, "too_fast_to_copy": 0, "high_hold": 0,
               "insufficient_hold_data": 0, "unreliable_hold_data": 0,
               "risky_loss_ratio": 0}
    completed = 0
    total = len(lb)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_to_entry = {executor.submit(analyze_trader, entry): entry for entry in lb}
        for future in as_completed(future_to_entry):
            completed += 1
            entry = future_to_entry[future]
            result = future.result()
            if result is None:
                skipped["no_wallet"] += 1
                skipped_details.append({
                    "Name": entry.get("userName") or entry.get("xUsername") or "N/A",
                    "TraderID": entry.get("proxyWallet") or "N/A",
                    "ProfitRate": profit_rate(entry),
                    "SkipReason": "no_wallet",
                })
                continue
            if "skip" in result:
                reason = result["skip"]
                skipped[reason] += 1
                skipped_details.append({
                    "Name": entry.get("userName") or entry.get("xUsername") or (entry.get("proxyWallet") or "N/A")[:12],
                    "TraderID": entry.get("proxyWallet") or "N/A",
                    "ProfitRate": profit_rate(entry),
                    "SkipReason": reason,
                })
                if completed % 100 == 0:
                    _safe_print(f"  Progress: {completed}/{total} done, {len(filtered_traders)} passed so far")
                continue
            # passed all filters
            flag = "OK  " if result["_complete"] else "PART"
            note = "" if result["_complete"] else "  <-- INCOMPLETE"
            _safe_print(f"[{completed:>4}/{total}] {flag} {result['Name'][:22]:<22} "
                        f"Trades/wk={result['WeeklyTrades']} PR={result['ProfitRate']} "
                        f"WR={result['WinRate']}% Hold={result['AvgHoldingDays']}d "
                        f"(n={result['sample']}, matched={result['matched']}){note}")
            filtered_traders.append(result)

    _safe_print(f"\nFiltering complete:")
    _safe_print(f"  - Traders checked: {total}")
    _safe_print(f"  - Skipped (no wallet): {skipped['no_wallet']}")
    _safe_print(f"  - Skipped (trade count outside {MIN_TRADES_PER_WEEK}-{MAX_TRADES_PER_WEEK}): {skipped['trade_count']}")
    _safe_print(f"  - Skipped (profit rate < {MIN_PROFIT_RATE*100:.1f}%): {skipped['low_profit']}")
    _safe_print(f"  - Skipped (profit rate > {MAX_PROFIT_RATE*100:.0f}%, likely bad data): {skipped['implausible_profit_rate']}")
    _safe_print(f"  - Skipped (sample size < {MIN_SAMPLE_SIZE}): {skipped['small_sample']}")
    _safe_print(f"  - Skipped (win rate < {MIN_WIN_RATE:.0f}%): {skipped['low_winrate']}")
    _safe_print(f"  - Skipped (not enough hold-time data): {skipped['insufficient_hold_data']}")
    _safe_print(f"  - Skipped (hold-time data covers < {MIN_HOLD_COVERAGE*100:.0f}% of trades): {skipped['unreliable_hold_data']}")
    _safe_print(f"  - Skipped (holding time < {MIN_HOLD_DAYS} days, too fast to copy): {skipped['too_fast_to_copy']}")
    _safe_print(f"  - Skipped (holding time > {MAX_HOLD_DAYS} days): {skipped['high_hold']}")
    _safe_print(f"  - Skipped (avg loss > {MAX_LOSS_TO_WIN_RATIO}x avg win): {skipped['risky_loss_ratio']}")
    _safe_print(f"  - Passed ALL filters: {len(filtered_traders)}")

    # Write an audit file so we can see exactly where lower-PR candidates go.
    audit_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    audit_path = f"trader_skip_diagnostics_{audit_stamp}.csv"
    with open(audit_path, "w", newline="", encoding="utf-8") as audit_file:
        audit_writer = csv.DictWriter(
            audit_file,
            fieldnames=["Name", "TraderID", "ProfitRate", "SkipReason"],
        )
        audit_writer.writeheader()
        audit_writer.writerows(skipped_details)
    _safe_print(f"Wrote skip diagnostics -> {audit_path}")

    # ----------------------------------------------------------------
    # Score: computed HERE, not per-trader, because normalizing each
    # metric needs the min/max across the whole surviving pool.
    # ----------------------------------------------------------------
    def _normalize(value, lo, hi):
        if hi == lo:
            return 1.0  # everyone tied on this metric — don't penalize anyone
        return (value - lo) / (hi - lo)

    if filtered_traders:
        finite_rrs = [r["RR"] for r in filtered_traders if r["RR"] is not None]
        best_rr = max(finite_rrs) if finite_rrs else 1.0
        for r in filtered_traders:
            if r["RR"] is None:
                r["RR"] = best_rr

        def norm(value, values):
            lo, hi = min(values), max(values)
            return 1.0 if hi == lo else (value - lo) / (hi - lo)

        metric_values = {
            "ProfitRate": [r["ProfitRate"] for r in filtered_traders],
            "WinRate": [r["WinRate"] for r in filtered_traders],
            "RR": [r["RR"] for r in filtered_traders],
            "sample": [r["sample"] for r in filtered_traders],
            "MarketCount": [r["MarketCount"] for r in filtered_traders],
        }
        hold_values = [r["AvgHoldingDays"] for r in filtered_traders if r["AvgHoldingDays"] is not None]
        if hold_values:
            metric_values["AvgHoldingDays"] = hold_values

        weights = {
            "ProfitRate": WEIGHT_PROFIT_RATE,
            "WinRate": WEIGHT_WIN_RATE,
            "RR": WEIGHT_RR,
            "AvgHoldingDays": WEIGHT_HOLD,
            "sample": WEIGHT_SAMPLE_SIZE,
            "MarketCount": WEIGHT_MARKET_COUNT,
        }
        for r in filtered_traders:
            terms = []
            for key, weight in weights.items():
                value = r.get(key)
                if key == "AvgHoldingDays":
                    score_value = 1.0 - norm(value, metric_values[key]) if value is not None else None
                else:
                    score_value = norm(value, metric_values[key])
                if score_value is not None:
                    terms.append((weight, score_value))
            total_weight = sum(weight for weight, _ in terms)
            r["Score"] = round(sum(weight * value for weight, value in terms) / total_weight, 4) if total_weight else 0.0

    # rank by composite Score descending
    filtered_traders.sort(key=lambda r: r.get("Score", 0), reverse=True)

    # write CSV
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out = f"traders_{stamp}.csv"
    cols = ["Name", "TraderID", "WeeklyTrades", "ProfitRate", "WinRate", "RR",
            "AvgWin", "AvgLoss", "AvgHoldingDays", "HoldDataStatus", "MarketCount", "Score"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in filtered_traders:
            w.writerow({c: r[c] for c in cols})

    incomplete = sum(1 for r in filtered_traders if not r["_complete"])
    _safe_print(f"\nDone. Wrote {len(filtered_traders)} traders -> {out}")
    if incomplete:
        _safe_print(f"WARNING: {incomplete} trader(s) had INCOMPLETE data.")
    else:
        _safe_print("All traders fetched cleanly.")

if __name__ == "__main__":
    main()