import csv
import glob
import html
import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
AI_UNAVAILABLE_PREFIX = "AI_UNAVAILABLE:"
TELEGRAM_MESSAGE_LIMIT = 3800


def latest_csv():
    files = sorted(glob.glob("traders_*.csv"))
    return files[-1] if files else None


def has_real_username(name):
    return bool(name) and not (
        name.startswith("0x") and (name.endswith("…") or "-" in name)
    )


def read_ai_summary():
    path = "ai_summary.txt"
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as file:
        summary = file.read().strip()

    if not summary or summary.startswith(AI_UNAVAILABLE_PREFIX):
        return None

    return summary


def safe_value(row, column, fallback="N/A"):
    value = row.get(column, "")
    return value if value not in (None, "") else fallback


def format_trader(index, row):
    name = safe_value(row, "Name")
    safe_name = html.escape(name)

    if has_real_username(name):
        header = (
            f'{index}. <a href="https://polymarket.com/@{safe_name}">'
            f"<b>{safe_name}</b></a>"
        )
    else:
        header = f"{index}. <b>{safe_name}</b>"

    marker = " <b>TOP 3</b>" if index <= 3 else ""

    return (
        f"{header}{marker}\n"
        f" PR: {safe_value(row, 'ProfitRate')} | "
        f"WR: {safe_value(row, 'WinRate')} | "
        f"RR: {safe_value(row, 'RR')} | "
        f"ØV: {safe_value(row, 'AvgWin')} | "
        f"ØF: {safe_value(row, 'AvgLoss')} | "
        f"Hold: {safe_value(row, 'AvgHoldingDays')}d\n"
    )


def build_message_parts(path):
    with open(path, newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))

    ai_summary = read_ai_summary()
    if ai_summary:
        intro = f"<b>AI-ANALYS:</b>\n{html.escape(ai_summary)}\n\n"
    else:
        intro = (
            "<b>AI-ANALYS:</b> kunde inte hämtas denna körning. "
            "Traderdata nedan är fortfarande tillgänglig.\n\n"
        )

    if not rows:
        return [intro + "<b>DAGENS TRADERS:</b>\nInga traders klarade filtren."]

    parts = []
    current = intro + "<b>DAGENS TRADERS:</b>\n\n"

    for index, row in enumerate(rows, 1):
        trader_text = format_trader(index, row)
        if len(current) + len(trader_text) > TELEGRAM_MESSAGE_LIMIT:
            parts.append(current.rstrip())
            current = "<b>DAGENS TRADERS — fortsättning:</b>\n\n"
        current += trader_text + "\n"

    if current.strip():
        parts.append(current.rstrip())

    return parts


def send(text, token, chat_id):
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN eller TELEGRAM_CHAT_ID saknas")

    url = TELEGRAM_API.format(token=token)
    data = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = Request(
        url,
        data=data,
        headers={"User-Agent": "PolyGunAssistant/1.0"},
        method="POST",
    )

    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(f"Telegram send failed: {result}")


def main():
    path = latest_csv()
    if not path:
        print("Ingen traders_*.csv hittades — inget digestmeddelande skickat.")
        sys.exit(1)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    parts = build_message_parts(path)

    for part in parts:
        send(part, token, chat_id)

    print(f"Digest skickad från {path} i {len(parts)} Telegrammeddelande(n)")


if __name__ == "__main__":
    main()
