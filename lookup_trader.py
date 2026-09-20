#!/usr/bin/env python3
"""Direct PolyMarket trader lookup.

Accepts a username, wallet address, @username, or Polymarket profile URL.
It never applies the daily-list thresholds. It resolves the trader, reads a
large closed-position sample, calculates available metrics, and sends the
result to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
"""
import argparse
import html
import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
USER_AGENT = "PolyGunAssistant/3.0"
PAGE_SIZE = 50
CLOSED_LIMIT = 2000
ACTIVITY_LIMIT = 2500
MAX_RETRIES = 3
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def get_json(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(request, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            last = error
            if attempt + 1 < MAX_RETRIES:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"API-fel: {last}")


def clean_identifier(raw):
    value = raw.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    if "://" in value:
        parsed = urlparse(value)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if parts and parts[0] in {"@", "profile", "profiles"}:
            parts = parts[1:]
        if parts:
            value = parts[-1]
    return value.strip().lstrip("@").strip("/")


def resolve_wallet(raw_identifier):
    identifier = clean_identifier(raw_identifier)
    if WALLET_RE.fullmatch(identifier):
        return identifier, identifier

    data = get_json(GAMMA_API + "/public-search", {
        "q": identifier,
        "search_profiles": "true",
    })
    profiles = data.get("profiles") or []
    target = identifier.casefold()

    # Exact match first; never silently choose a similar username.
    for profile in profiles:
        for field in ("name", "pseudonym", "xUsername"):
            value = profile.get(field)
            if value and value.casefold().lstrip("@") == target:
                wallet = profile.get("proxyWallet") or profile.get("wallet")
                if wallet:
                    return wallet, value

    # A profile URL may resolve by slug even when public-search returns a
    # slightly different display field. Only accept a unique result.
    candidates = []
    for profile in profiles:
        wallet = profile.get("proxyWallet") or profile.get("wallet")
        if wallet:
            candidates.append((wallet, profile.get("name") or profile.get("pseudonym") or identifier))
    unique = {wallet: name for wallet, name in candidates}
    if len(unique) == 1:
        wallet, name = next(iter(unique.items()))
        return wallet, name
    return None, None


def fetch_pages(endpoint, params, limit):
    rows = []
    for offset in range(0, limit, PAGE_SIZE):
        page = get_json(DATA_API + endpoint, {
            **params,
            "limit": min(PAGE_SIZE, limit - offset),
            "offset": offset,
        })
        if not isinstance(page, list) or not page:
            break
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        time.sleep(0.08)
    return rows[:limit]


def number(value):
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def timestamp(value):
    parsed = number(value)
    if parsed is not None:
        return parsed / 1000 if parsed > 10_000_000_000 else parsed
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def analyze(wallet):
    closed = fetch_pages(
        "/closed-positions",
        {"user": wallet, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        CLOSED_LIMIT,
    )
    activity = fetch_pages(
        "/activity",
        {"user": wallet, "type": "TRADE", "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        ACTIVITY_LIMIT,
    )

    pnls = [number(row.get("realizedPnl")) for row in closed]
    pnls = [value for value in pnls if value is not None]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    decided = len(wins) + len(losses)

    # Match each close with the trader's own BUY timestamps per asset.
    buys = defaultdict(deque)
    for row in sorted(activity, key=lambda item: timestamp(item.get("timestamp")) or 0):
        if str(row.get("side", "")).upper() != "BUY":
            continue
        asset = row.get("asset") or row.get("conditionId") or row.get("market")
        ts = timestamp(row.get("timestamp"))
        if asset and ts:
            buys[asset].append(ts)

    holds = []
    for row in sorted(closed, key=lambda item: timestamp(item.get("timestamp")) or 0):
        asset = row.get("asset") or row.get("conditionId") or row.get("market")
        close_ts = timestamp(row.get("timestamp"))
        if not asset or close_ts is None or not buys[asset]:
            continue
        while buys[asset] and buys[asset][0] > close_ts:
            buys[asset].popleft()
        if buys[asset]:
            hold = (close_ts - buys[asset].popleft()) / 86400
            if hold >= 0:
                holds.append(hold)

    newest = [timestamp(row.get("timestamp")) for row in closed]
    newest = [value for value in newest if value is not None]
    days = ((max(newest) - min(newest)) / 86400) if len(newest) >= 2 else None
    total = sum(pnls) if pnls else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    win_rate = (len(wins) / decided * 100) if decided else None
    rr = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss else None
    avg_hold = sum(holds) / len(holds) if holds else None

    recent_cutoff = time_now() - 7 * 86400
    weekly_trades = sum(1 for row in activity if (timestamp(row.get("timestamp")) or 0) >= recent_cutoff)

    return {
        "closed_count": len(closed),
        "activity_count": len(activity),
        "weekly_trades": weekly_trades,
        "decided": decided,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total": total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "rr": rr,
        "avg_hold": avg_hold,
        "hold_matches": len(holds),
        "days": days,
        "oldest": min(newest) if newest else None,
        "newest": max(newest) if newest else None,
    }


def time_now():
    return datetime.now(timezone.utc).timestamp()


def fmt(value, digits=2):
    return "N/A" if value is None else f"{value:.{digits}f}"


def format_result(identifier, name, wallet, result):
    if not result["closed_count"]:
        return f"<b>{html.escape(name or identifier)}</b>\nIngen stängd positionsdata hittades.\nTrader ID: <code>{html.escape(wallet)}</code>"
    return (
        f"<b>LOOKUP: {html.escape(name or identifier)}</b>\n"
        f"Trader ID: <code>{html.escape(wallet)}</code>\n\n"
        f"Weekly trades: {result['weekly_trades']}\n"
        f"Stängda positioner: {result['closed_count']}\n"
        f"Avgjorda positioner: {result['decided']} (vinster {result['wins']}, förluster {result['losses']})\n"
        f"Profit/Loss totalt: {fmt(result['total'])}\n"
        f"WinRate: {fmt(result['win_rate'], 1)}%\n"
        f"AvgWin: {fmt(result['avg_win'])}\n"
        f"AvgLoss: {fmt(result['avg_loss'])}\n"
        f"RR: {fmt(result['rr'], 3)}\n"
        f"Hold: {fmt(result['avg_hold'])} dagar ({result['hold_matches']} matchningar)\n"
        f"Historik: {fmt(result['days'], 1)} dagar\n"
        "Filter: inga daglistetrösklar använda."
    )


def send_telegram(message):
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram skickades inte: TELEGRAM_BOT_TOKEN eller TELEGRAM_CHAT_ID saknas.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode({"chat_id": chat_id, "text": message, "parse_mode": "HTML"}).encode()
    request = Request(url, data=payload, headers={"User-Agent": USER_AGENT}, method="POST")
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram-fel: {result}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("identifier", help="username, wallet-ID eller Polymarket-profillänk")
    parser.add_argument("--search-depth", default="2000", help="Behålls för workflow-kompatibilitet")
    args = parser.parse_args()
    try:
        wallet, name = resolve_wallet(args.identifier)
        if not wallet:
            message = f'Hittade ingen trader för "{html.escape(args.identifier)}".'
        else:
            message = format_result(args.identifier, name, wallet, analyze(wallet))
    except Exception as error:
        message = f"Lookup kunde inte slutföras: {html.escape(str(error))}"
    print(message)
    send_telegram(message)


if __name__ == "__main__":
    main()
