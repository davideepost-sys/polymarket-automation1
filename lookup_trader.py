#!/usr/bin/env python3
"""Simple PolyMarket trader lookup by username or wallet/trader ID.

This lookup does not require the trader to be in the daily CSV or weekly
leaderboard. It resolves a username through public profile search, then reads
closed positions directly and reports a compact result.
"""
import html
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
USER_AGENT = "PolyGunAssistant/2.0"
PAGE_SIZE = 50
INITIAL_POSITIONS = 300
MAX_POSITIONS = 1000
MIN_TIME_COVERAGE_DAYS = 14.0
MAX_RETRIES = 3
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def get_json(url, params=None):
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            last = error
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"API-fel: {last}")


def resolve_wallet(identifier):
    identifier = identifier.strip()
    if WALLET_RE.fullmatch(identifier):
        return identifier, identifier
    data = get_json(GAMMA_API + "/public-search", {
        "q": identifier, "search_profiles": "true",
    })
    profiles = data.get("profiles") or []
    target = identifier.casefold().lstrip("@")
    for profile in profiles:
        for field in ("name", "pseudonym", "xUsername"):
            value = profile.get(field)
            if value and value.casefold().lstrip("@") == target:
                wallet = profile.get("proxyWallet") or profile.get("wallet")
                if wallet:
                    return wallet, value
    return None, None


def fetch_positions(wallet, limit):
    rows = []
    for offset in range(0, limit, PAGE_SIZE):
        page = get_json(DATA_API + "/closed-positions", {
            "user": wallet,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
            "limit": PAGE_SIZE,
            "offset": offset,
        })
        if not isinstance(page, list) or not page:
            break
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        time.sleep(0.12)
    return rows[:limit]


def number(value):
    try:
        value = float(value)
        return value if value == value else None
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
    rows = fetch_positions(wallet, INITIAL_POSITIONS)
    points = [(timestamp(r.get("timestamp")), number(r.get("realizedPnl"))) for r in rows]
    points = sorted((t, p) for t, p in points if t is not None and p is not None)
    mode = "300"
    initial_span = None
    if points:
        initial_span = (points[-1][0] - points[0][0]) / 86400
    if initial_span is None or initial_span < MIN_TIME_COVERAGE_DAYS:
        rows = fetch_positions(wallet, MAX_POSITIONS)
        mode = "upp till 1000"
        points = [(timestamp(r.get("timestamp")), number(r.get("realizedPnl"))) for r in rows]
        points = sorted((t, p) for t, p in points if t is not None and p is not None)
    if not points:
        return {"mode": mode, "count": 0}
    by_day = defaultdict(float)
    for t, pnl in points:
        by_day[datetime.fromtimestamp(t, timezone.utc).date().isoformat()] += pnl
    days = sorted(by_day)
    daily = [by_day[d] for d in days]
    cumulative = []
    total = 0.0
    for value in daily:
        total += value
        cumulative.append(total)
    n = len(cumulative)
    xm = (n - 1) / 2
    ym = sum(cumulative) / n
    ssx = sum((i - xm) ** 2 for i in range(n)) or 1
    ssy = sum((v - ym) ** 2 for v in cumulative)
    cov = sum((i - xm) * (v - ym) for i, v in enumerate(cumulative))
    slope = cov / ssx
    r2 = cov * cov / (ssx * ssy) if ssy else 0.0
    peak = 0.0
    drawdown = 0.0
    for value in cumulative:
        peak = max(peak, value)
        drawdown = max(drawdown, peak - value)
    return {
        "mode": mode,
        "count": len(points),
        "days": (points[-1][0] - points[0][0]) / 86400,
        "unique_days": len(days),
        "oldest": datetime.fromtimestamp(points[0][0], timezone.utc).strftime("%Y-%m-%d"),
        "newest": datetime.fromtimestamp(points[-1][0], timezone.utc).strftime("%Y-%m-%d"),
        "total": total,
        "slope": slope,
        "r2": max(0.0, min(1.0, r2)),
        "drawdown": drawdown,
        "positive_days": sum(v > 0 for v in daily) / len(daily),
    }


def get_trader_analysis(identifier):
    wallet, name = resolve_wallet(identifier)
    if not wallet:
        return f'Hittade ingen trader för "{html.escape(identifier)}".'
    result = analyze(wallet)
    shown_name = name or wallet
    if not result.get("count"):
        return f"<b>{html.escape(shown_name)}</b>\nIngen giltig stängd positionsdata hittades."
    return (
        f"<b>{html.escape(shown_name)}</b>\n"
        f"Trader ID: <code>{html.escape(wallet)}</code>\n\n"
        f"Kurvunderlag: {result['count']} positioner ({result['mode']})\n"
        f"Historik: {result['days']:.1f} dagar, {result['unique_days']} handelsdagar\n"
        f"Från: {result['oldest']} till {result['newest']}\n"
        f"Total stängd PnL: {result['total']:.2f}\n"
        f"Trendlinje: {'uppåtgående' if result['slope'] > 0 else 'inte uppåtgående'}\n"
        f"Trend-R²: {result['r2']:.3f}\n"
        f"Positiva dagar: {result['positive_days'] * 100:.1f}%\n"
        f"Max drawdown: {result['drawdown']:.2f}\n\n"
        "PR/WR/RR/Hold: N/A i denna enkla direktkurv-lookup."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Användning: python3 lookup_trader.py <username eller trader-ID>")
        raise SystemExit(1)
    print(get_trader_analysis(" ".join(sys.argv[1:])))
