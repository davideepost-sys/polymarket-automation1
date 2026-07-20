"""
lookup_trader.py — looks up ONE trader (by username or wallet address) and
runs the exact same analysis pipeline as scraper_2.py on them, so the
result is 100% consistent with the daily list — same filters, same
ProfitRate source (weekly leaderboard pnl/vol), same everything.

How it works:

Resolve the input to a wallet address.
If it already looks like a wallet (0x + 40 hex chars), use it as-is.
Otherwise, search Polymarket's public profile search for an exact
username match.
Search the WEEKLY leaderboard for that wallet by paginating through
it (same endpoint scraper_2.py uses) until found or until
search_depth is reached. This finds their real rank, whatever it is.
Run scraper_2.analyze_trader() — the SAME function the daily run
uses — on their leaderboard entry. No separate/duplicate logic, so
this can never drift out of sync with scraper_2.py as it evolves.
Send the result to Telegram.
Usage:
python3 lookup_trader.py <username_or_wallet> [search_depth]

search_depth: how many places into the weekly leaderboard to search
before giving up (default 2000, hard cap 10000 — searching further
costs more API calls/time, since each page is a separate request).
"""
import sys
import os
import re
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import scraper_2 # reuse the exact same fetch/analysis logic — single source of truth

GAMMA_API = "https://gamma-api.polymarket.com"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

DEFAULT_SEARCH_DEPTH = 2000
MAX_SEARCH_DEPTH = 10000
LEADERBOARD_PAGE_SIZE = 50 # matches scraper_2.get_leaderboard's page size

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$" )

SKIP_REASONS_SV = {
    "low_profit": f"profitrate under {scraper_2.MIN_PROFIT_RATE*100:.0f}%",
    "implausible_profit_rate": f"profitrate över {scraper_2.MAX_PROFIT_RATE*100:.0f}% (troligen felaktig data, inte äkta)",
    "trade_count": f"antal trades/vecka utanför {scraper_2.MIN_TRADES_PER_WEEK}-{scraper_2.MAX_TRADES_PER_WEEK}",
    "small_sample": f"färre än {scraper_2.MIN_SAMPLE_SIZE} avgjorda trades — för tunt underlag",
    "low_winrate": f"winrate under {scraper_2.MIN_WIN_RATE:.0f}%",
    "insufficient_hold_data": "för lite hold-tid-data för att lita på snittet",
    "unreliable_hold_data": f"hold-tid-datan täcker mindre än {scraper_2.MIN_HOLD_COVERAGE*100:.0f}% av trades — inte tillförlitlig",
    "too_fast_to_copy": f"håller positioner under {scraper_2.MIN_HOLD_DAYS*1440:.0f} minuter i snitt — för snabb för att kunna copy-tradas",
    "high_hold": f"håller positioner längre än {scraper_2.MAX_HOLD_DAYS} dagar i snitt",
    "risky_loss_ratio": f"snittförlusten är mer än {scraper_2.MAX_LOSS_TO_WIN_RATIO}x snittvinsten — för riskabelt riskförhållande",
}

def resolve_wallet(identifier):
    """Returns (wallet_address, matched_username) or (None, None)."""
    if WALLET_RE.match(identifier):
        return identifier, None
    
    url = f"{GAMMA_API}/public-search?" + urlencode({
        "q": identifier, "search_profiles": "true",
    })
    req = Request(url, headers={"User-Agent": scraper_2.USER_AGENT})
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Username-sökning misslyckades: {e}")
        return None, None
    
    profiles = data.get("profiles") or []
    ident_lower = identifier.lower()
    for p in profiles:
        for field in ("name", "pseudonym", "xUsername"):
            val = p.get(field)
            if val and val.lower() == ident_lower:
                return p.get("proxyWallet"), val
    return None, None

def find_in_leaderboard(wallet, search_depth):
    """Paginate the weekly leaderboard looking for this wallet.
    Returns (entry, rank) or (None, None) if not found within search_depth."""
    wallet_lower = wallet.lower()
    offset = 0
    while offset < search_depth:
        limit = min(LEADERBOARD_PAGE_SIZE, search_depth - offset)
        try:
            page = scraper_2.get_leaderboard_page(
                "WEEK", "PNL", limit, offset
            )
        except (scraper_2.RateLimited, scraper_2.FetchError) as e:
            print(f"Topplistan gick inte att hämta vid plats {offset}: {e}")
            break
        if not page:
            break
        for i, entry in enumerate(page):
            if (entry.get("proxyWallet") or "").lower() == wallet_lower:
                return entry, offset + i + 1
        if len(page) < limit:
            break
        offset += limit
    return None, None

def get_trader_analysis(identifier, search_depth=DEFAULT_SEARCH_DEPTH):
    wallet, matched_name = resolve_wallet(identifier)
    if not wallet:
        return f'Hittade ingen Polymarket-trader som matchar "{identifier}".'
    
    entry, rank = find_in_leaderboard(wallet, search_depth)
    
    if not entry:
        return (
            f'Hittade inte "{identifier}" inom topp {search_depth} i veckans topplista.\n'
            f"Antingen har de för lite vinst/volym den här veckan för att synas alls, "
            f"eller ligger de längre ner — testa ett större sökdjup (max {MAX_SEARCH_DEPTH})."
        )
    
    name = entry.get("userName") or entry.get("xUsername") or (wallet[:8] + "…")
    result = scraper_2.analyze_trader(entry)
    
    if result is None:
        msg = f"{name} (plats {rank}) saknar wallet-data — kan inte analyseras."
    elif "skip" in result:
        reason = SKIP_REASONS_SV.get(result["skip"], result["skip"])
        msg = (
            f"<b>{name}</b> — plats {rank} i veckans topplista (sökt bland topp {search_depth}).\n"
            f"❌ Klarar INTE dina kriterier just nu.\n"
            f"Anledning: {reason}."
        )
    else:
        username_for_link = entry.get("userName")
        link_line = (
            f'\n🔗 <a href="https://polymarket.com/@{username_for_link}">Profil</a>'
            if username_for_link else ""
         )
        rr_display = result["RR"] if result["RR"] is not None else "∞ (inga förluster)"
        msg = (
            f"<b>{name}</b> — plats {rank} i veckans topplista (sökt bland topp {search_depth}).\n"
            f"✅ Klarar ALLA dina kriterier!\n\n"
            f"PR: {result['ProfitRate']*100:.1f}% | WR: {result['WinRate']}% | RR: {rr_display}\n"
            f"Hold: {result['AvgHoldingDays']}d | Trades/vecka: {result['WeeklyTrades']} | "
            f"Marknader: {result['MarketCount']}"
            f"{link_line}"
        )
    return msg

# The main function is removed as this file will be imported as a module.
# def main():
#    ...
