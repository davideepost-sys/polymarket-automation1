"""
send_digest.py — sends the daily "ny dag, nya möjligheter" message to
Telegram, built from the most recent traders_*.csv that scraper_2.py
produced in this same run.

Needs two environment variables (set as GitHub Secrets):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Profile link format confirmed working: https://polymarket.com/@<username>
If a trader has no real username (scraper_2.py falls back to a
shortened wallet like "0xabcd1234…"), we skip the link — a guessed
link would 404, and False > wrong for a "no false data" requirement.
"""
import os
import sys
import csv
import glob
import json
import html
from urllib.request import Request, urlopen
from urllib.parse import urlencode

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def latest_csv():
    files = sorted(glob.glob("traders_*.csv"))
    return files[-1] if files else None


def has_real_username(name):
    # scraper_2.py's fallback name looks like "0xabcd1234…" (wallet
    # prefix + ellipsis) when no username/xUsername exists.
    return not (name.startswith("0x") and name.endswith("…"))


def build_message(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return "Hej, ny dag nya möjligheter! Inga traders klarade filtren idag."

    lines = ["Hej, ny dag nya möjligheter! Här är dagens traders:\n"]
    for i, r in enumerate(rows, 1):
        name = r["Name"]
        safe_name = html.escape(name)
        if has_real_username(name):
            header = f'{i}. <a href="https://polymarket.com/@{name}"><b>{safe_name}</b></a>'
        else:
            header = f"{i}. <b>{safe_name}</b> (inget publikt användarnamn)"
        lines.append(
            f"{header}\n"
            f'   PR: {float(r["ProfitRate"])*100:.1f}%  '
            f'WR: {r["WinRate"]}%  '
            f'RR: {r["RR"]}  '
            f'Hold: {r["AvgHoldingDays"]}d  '
            f'Score: {r["Score"]}'
        )
    return "\n".join(lines)


def send(text, token, chat_id):
    url = TELEGRAM_API.format(token=token)
    data = urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = Request(url, data=data)
    with urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram send failed: {result}")
    return result


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars.")
        sys.exit(1)

    path = latest_csv()
    if not path:
        print("No traders_*.csv file found — nothing to send.")
        sys.exit(1)

    msg = build_message(path)
    send(msg, token, chat_id)
    print(f"Sent digest based on {path} ({len(msg)} chars).")


if __name__ == "__main__":
    main()
