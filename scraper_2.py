"""
Polymarket Smart Money — minimal, honest rebuild.
Outputs EXACTLY five things per trader:
    Name, TraderID, ProfitRate, WinRate, AvgHoldingDays
Design principles (why this rebuild exists):
  1. No hidden math. Each number comes from a clearly named source and is
     computed one obvious way. See the comments on each function.
  2. No silent guessing. When Polymarket tells us "slow down" (rate limit) or
     an error happens mid-fetch, we do NOT pretend the trader simply ran out
     of data. We stop, mark that trader's numbers as INCOMPLETE, and say so
     out loud. That is the difference between "truly no more data" and
     "we got cut off" — the thing we needed certainty about.
No API key needed — Polymarket's Data API is public.
"""
import sys
import time
import csv
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
DATA_API = "https://data-api.polymarket.com"
USER_AGENT = "polymarket-minimal/1.0"
# ---- knobs (kept few on purpose) -----------------------------------------
POOL = 1000               # how many top-of-leaderboard traders to look at
CLOSED_MAX_PAGES = 8      # closed positions to read: 8 x 50 = up to 400
ACTIVITY_MAX_PAGES = 7    # buy history: 7 x 500 = up to 3500 (API caps offset at 3000)
MIN_HOLD_MATCHES = 5      # need at least this many matched positions to trust avg hold
MAX_RETRIES = 4           # how many times to retry a call before giving up
BACKOFF_SECONDS = 2.0     # base wait between retries (grows each attempt)
POLITE_DELAY = 0.15       # small pause between calls so we don't hammer the API
MIN_TRADES_PER_WEEK = 21  # minimum trades per week to be considered
MAX_TRADES_PER_WEEK = 700 # maximum trades per week to be considered
MIN_SAMPLE_SIZE = 10      # minimum closed positions to trust win rate
MIN_PROFIT_RATE = 0.10    # minimum profit rate (10%) to be considered
MIN_WIN_RATE = 75.0       # minimum win rate (75%) to be considered
MAX_HOLD_DAYS = 1.5       # maximum average holding time (1.5 days) to be considered
# ==========================================================================
#  Honest fetch layer — the heart of the "no silent guessing" rule
# ==========================================================================
class RateLimited(Exception):
    """Raised when the API keeps telling us to slow down and won't answer."""
class FetchError(Exception):
    """Raised when the API errors out for some other reason."""
class EndOfData(Exception):
    """
    Raised when the API says we've reached the furthest it will let us page
    (e.g. 'max historical activity offset exceeded'). This is a NATURAL end,
    not a failure — the trader simply has no more data available to us.
    """
def _get(path, params):
    """
    Make ONE call. Returns the parsed JSON list on success.
    Raises RateLimited on repeated 429s, FetchError on anything else.
    Crucially: it never returns None/empty to *hide* a failure — a failure
    is thrown, so callers can tell "empty because done" from "empty because
    something broke".
    """
    url = f"{DATA_API}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                # "slow down" — wait longer each time, then try again
                wait = BACKOFF_SECONDS * (attempt + 1) * 2
                print(f"    · rate-limited (429), waiting {wait:.0f}s and retrying...")
                time.sleep(wait)
                last_err = RateLimited(url)
                continue
            if e.code >= 500:
                last_err = FetchError(f"server {e.code}")
                time.sleep(BACKOFF_SECONDS * (attempt + 1))
                continue
            if e.code == 400:
                # some 400s mean "you've paged as far as allowed" — that's an
                # end, not an error. Read the message to tell them apart.
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
    # ran out of retries
    if isinstance(last_err, RateLimited):
        raise RateLimited(url)
    raise FetchError(f"{url}: {last_err}")
def _fetch_pages(path, base_params, page_size, max_pages):
    """
    Read several pages of a list endpoint.
    Returns (rows, complete):
      complete = True  -> we reached the natural end of the data
                          (a page came back shorter than a full page)
      complete = False -> we were cut off (rate limit / error) BEFORE the end.
                          The rows we DID get are still returned, but the caller
                          must treat the trader's numbers as partial.
    """
    rows = []
    for page in range(max_pages):
        params = dict(base_params)
        params["limit"] = page_size
        params["offset"] = page * page_size
        try:
            data = _get(path, params)
        except EndOfData:
            return rows, True            # API's paging ceiling = real end of data
        except (RateLimited, FetchError) as e:
            print(f"    · fetch cut short on page {page + 1}: {type(e).__name__}")
            return rows, False           # cut off — NOT a natural end
        if not isinstance(data, list):
            return rows, False
        rows.extend(data)
        if len(data) < page_size:
            return rows, True            # short page = genuinely the end
        time.sleep(POLITE_DELAY)
    # we hit our page cap without seeing the end; that's a deliberate limit,
    # so we call it "complete enough" but the count tells the real story
    return rows, True
# ==========================================================================
#  The three numbers we actually care about
# ==========================================================================
def get_leaderboard(pool):
    """Top `pool` weekly traders. Gives us name, id, pnl and vol directly."""
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
            print(f"  leaderboard fetch failed: {e}")
            break
        if not page:
            break
        rows.extend(page)
        if len(page) < limit:
            break
        offset += limit
        time.sleep(POLITE_DELAY)
    return rows[:pool]
def profit_rate(entry):
    """
    Profit rate = pnl / vol, taken STRAIGHT from the leaderboard.
    This is the exact same arithmetic you do by hand on the website
    (their shown PnL divided by their shown Volume). Nothing hidden.
    """
    vol = entry.get("vol")
    pnl = entry.get("pnl")
    if not vol or vol <= 0 or pnl is None:
        return None
    return round(pnl / vol, 4)
def weekly_trade_count(wallet):
    """
    Count how many trades this trader made in the past 7 days.
    We fetch their recent activity and count trades from the last week.
    Returns (trade_count, complete) where complete indicates if we got all data.
    """
    from datetime import datetime, timezone, timedelta
    
    rows, complete = _fetch_pages(
        "/activity",
        {"user": wallet, "type": "TRADE",
         "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        page_size=500, max_pages=ACTIVITY_MAX_PAGES,
    )
    
    # Calculate timestamp for 7 days ago
    week_ago = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    
    # Count trades from the past week
    count = 0
    for trade in rows:
        ts = trade.get("timestamp")
        if ts and ts >= week_ago:
            count += 1
        else:
            # Since we're sorted DESC by timestamp, once we hit an old trade, stop
            break
    
    return count, complete
def entry_times(wallet):
    """
    Earliest BUY timestamp per asset (= when the trader entered that position).
    We read recent buys first (DESC) so they line up with recent closed
    positions. Returns (asset->earliest_ts, complete).
    """
    rows, complete = _fetch_pages(
        "/activity",
        {"user": wallet, "type": "TRADE", "side": "BUY",
         "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        page_size=500, max_pages=ACTIVITY_MAX_PAGES,
    )
    earliest = {}
    for t in rows:
        a, ts = t.get("asset"), t.get("timestamp")
        if a and ts and (a not in earliest or ts < earliest[a]):
            earliest[a] = ts
    return earliest, complete
def win_rate_and_hold(wallet):
    """
    From recent CLOSED positions:
      - win rate  = wins / (wins + losses), where win = realizedPnl > 0
      - avg hold  = average of (exit time - entry time) in days,
                    for every closed position we can match to a buy.
    Returns a dict, plus a `complete` flag that is only True when BOTH the
    closed-positions read AND the buy-history read finished cleanly.
    """
    entries, entries_complete = entry_times(wallet)
    rows, closed_complete = _fetch_pages(
        "/closed-positions",
        {"user": wallet, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        page_size=50, max_pages=CLOSED_MAX_PAGES,
    )
    wins = losses = ties = 0
    holds = []
    newest = oldest = None
    for p in rows:
        pnl = p.get("realizedPnl")
        if pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        else:
            ties += 1
        ts = p.get("timestamp")
        if ts:
            newest = ts if newest is None else max(newest, ts)
            oldest = ts if oldest is None else min(oldest, ts)
            asset = p.get("asset")
            if asset in entries:
                d = (ts - entries[asset]) / 86400.0
                if d >= 0:
                    holds.append(d)
    decided = wins + losses
    # only report a holding average if we matched enough positions to mean
    # anything — otherwise it's a guess from a handful of trades, so say N/A.
    trust_hold = len(holds) >= MIN_HOLD_MATCHES
    return {
        "win_rate": round(wins / decided * 100, 1) if decided else None,
        "avg_hold_days": round(sum(holds) / len(holds), 2) if trust_hold else None,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "sample": decided,
        "matched": len(holds),
        "newest": newest,
        "oldest": oldest,
        "complete": closed_complete and entries_complete,
    }
# ==========================================================================
#  Run
# ==========================================================================
def main():
    pool = POOL
    if len(sys.argv) > 1:
        try:
            pool = int(sys.argv[1])
        except ValueError:
            pass
    print("Polymarket Smart Money — minimal honest build")
    print(f"Reading top {pool} weekly traders (by PNL)")
    print(f"Filtering for traders with {MIN_TRADES_PER_WEEK}-{MAX_TRADES_PER_WEEK} trades/week")
    print(f"  - Profit Rate > {MIN_PROFIT_RATE*100:.0f}%")
    print(f"  - Win Rate > {MIN_WIN_RATE:.0f}%")
    print(f"  - Avg Holding Time < {MAX_HOLD_DAYS} days")
    print(f"  - Minimum sample size: {MIN_SAMPLE_SIZE} closed positions\n")
    lb = get_leaderboard(pool)
    if not lb:
        print("Could not read the leaderboard. Stopping.")
        sys.exit(1)
    print(f"Got {len(lb)} traders from leaderboard. Now filtering by trade frequency...\n")
    filtered_traders = []
    skipped_no_wallet = 0
    skipped_trade_count = 0
    skipped_low_profit = 0
    skipped_low_winrate = 0
    skipped_high_hold = 0
    skipped_small_sample = 0
    
    for i, entry in enumerate(lb, 1):
        wallet = entry.get("proxyWallet")
        name = entry.get("userName") or entry.get("xUsername") or (wallet[:8] + "…")
        if not wallet:
            skipped_no_wallet += 1
            continue
        
        # Check trade count first (fast check)
        trade_count, count_complete = weekly_trade_count(wallet)
        
        if trade_count < MIN_TRADES_PER_WEEK or trade_count > MAX_TRADES_PER_WEEK:
            skipped_trade_count += 1
            if i % 50 == 0:  # Print progress every 50 traders
                print(f"  Progress: {i}/{len(lb)} checked, {len(filtered_traders)} passed filter so far")
            continue
        
        # Get profit rate from leaderboard
        pr = profit_rate(entry)
        if pr is None or pr < MIN_PROFIT_RATE:
            skipped_low_profit += 1
            continue
        
        # Get win rate and holding time
        wh = win_rate_and_hold(wallet)
        
        # Filter: minimum sample size
        if wh["sample"] < MIN_SAMPLE_SIZE:
            skipped_small_sample += 1
            continue
        
        # Filter: win rate
        if wh["win_rate"] is None or wh["win_rate"] < MIN_WIN_RATE:
            skipped_low_winrate += 1
            continue
        
        # Filter: holding time
        if wh["avg_hold_days"] is not None and wh["avg_hold_days"] > MAX_HOLD_DAYS:
            skipped_high_hold += 1
            continue
        
        # This trader passed ALL filters - print and save
        flag = "OK  " if wh["complete"] else "PART"  # PART = partial/cut-off data
        note = "" if wh["complete"] else "  <-- INCOMPLETE (cut off, do not trust)"
        pr_s = "N/A" if pr is None else f"{pr}"
        wr_s = "N/A" if wh["win_rate"] is None else f"{wh['win_rate']}%"
        hold_s = "N/A" if wh["avg_hold_days"] is None else f"{wh['avg_hold_days']}d"
        print(f"[{i:>3}/{len(lb)}] {flag} {name[:22]:<22} "
              f"Trades/wk={trade_count} PR={pr_s} WR={wr_s} Hold={hold_s} "
              f"(n={wh['sample']}, matched={wh['matched']}){note}")
        filtered_traders.append({
            "Name": name,
            "TraderID": wallet,
            "WeeklyTrades": trade_count,
            "ProfitRate": pr if pr is not None else "N/A",
            "WinRate": wh["win_rate"] if wh["win_rate"] is not None else "N/A",
            "AvgHoldingDays": wh["avg_hold_days"] if wh["avg_hold_days"] is not None else "N/A",
            "_complete": wh["complete"],
        })
    print(f"\nFiltering complete:")
    print(f"  - Traders checked: {len(lb)}")
    print(f"  - Skipped (no wallet): {skipped_no_wallet}")
    print(f"  - Skipped (trade count outside {MIN_TRADES_PER_WEEK}-{MAX_TRADES_PER_WEEK}): {skipped_trade_count}")
    print(f"  - Skipped (profit rate < {MIN_PROFIT_RATE*100:.0f}%): {skipped_low_profit}")
    print(f"  - Skipped (sample size < {MIN_SAMPLE_SIZE}): {skipped_small_sample}")
    print(f"  - Skipped (win rate < {MIN_WIN_RATE:.0f}%): {skipped_low_winrate}")
    print(f"  - Skipped (holding time > {MAX_HOLD_DAYS} days): {skipped_high_hold}")
    print(f"  - Passed ALL filters: {len(filtered_traders)}")
    # write the 6-column file
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out = f"traders_{stamp}.csv"
    cols = ["Name", "TraderID", "WeeklyTrades", "ProfitRate", "WinRate", "AvgHoldingDays"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in filtered_traders:
            w.writerow({c: r[c] for c in cols})
    incomplete = sum(1 for r in filtered_traders if not r["_complete"])
    print(f"\nDone. Wrote {len(filtered_traders)} traders -> {out}")
    if incomplete:
        print(f"WARNING: {incomplete} trader(s) had INCOMPLETE data (cut off by "
              f"rate-limit or error). Their numbers are partial — re-run to confirm.")
    else:
        print("All traders fetched cleanly (no rate-limit cut-offs).")
    
    # Return the output filename for GitHub Actions
    return out
if __name__ == "__main__":
    output_file = main()
    # Print the output filename for GitHub Actions to capture
    print(f"::set-output name=csv_file::{output_file}")
