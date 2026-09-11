import csv
import glob
import html
import json
import os
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MESSAGE_LIMIT = 3800

AI_GUIDE = (
    "<b>AI ÄR TILLGÄNGLIG FÖR YTTERLIGARE HJÄLP</b>\n"
    "AI:n analyserar inte automatiskt denna lista. Be uttryckligen om hjälp i chatten.\n\n"
    "Exempel på frågor:\n"
    "• <i>Simulera 100 fiktiva copytrades med trader nr 1. "
    "Startkapital 1000 USDC och 10 USDC per trade.</i>\n"
    "• <i>Simulera trader nr 1 och nr 2 med 2 % av plånboken per trade.</i>\n"
    "• <i>Förklara varför RR 0,65 kan vara riskfyllt och ge ett exempel.</i>\n"
    "• <i>Jämför trader nr 1 och nr 2 utifrån PR, WR, RR, genomsnittlig vinst, "
    "genomsnittlig förlust och holdtid.</i>\n\n"
    "Skriv alltid startkapital och fast summa eller procent per trade vid simulering. "
    "Scenarierna är fiktiva och är inte en prognos.\n"
)


def latest_csv():
    files = sorted(glob.glob("traders_*.csv"))
    return files[-1] if files else None


def has_real_username(name):
    return bool(name) and not (
        name.startswith("0x") and (name.endswith("…") or "-" in name)
    )


def safe_value(row, column, fallback="N/A"):
    value = row.get(column, "")
    return value if value not in (None, "") else fallback


def format_trader(index, row):
    name = safe_value(row, "Name")
    safe_name = html.escape(name)

    if has_real_username(name):
        header = (
            f'<b>{index}. <a href="https://polymarket.com/@{safe_name}">'
            f"{safe_name}</a></b>"
        )
    else:
        header = f"<b>{index}. {safe_name}</b>"

    marker = " <b>TOP 3</b>" if index <= 3 else ""

    return (
        f"{header}{marker}\n"
        f"  ProfitRate: {safe_value(row, 'ProfitRate')} | "
        f"WinRate: {safe_value(row, 'WinRate')} | "
        f"Risk/Reward (RR): {safe_value(row, 'RR')}\n"
        f"  Genomsnittlig vinst: {safe_value(row, 'AvgWin')} | "
        f"Genomsnittlig förlust: {safe_value(row, 'AvgLoss')} | "
        f"Hold: {safe_value(row, 'Hold')}\n"
    )


def build_message_parts(path):
    with open(path, newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))

    intro = AI_GUIDE
    if not rows:
        return [intro + "\n<b>DAGENS TRADERS:</b>\nInga traders klarade de befintliga filtren."]

    parts = []
    current = intro + "\n<b>DAGENS TRADERS:</b>\n\n"

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

    data = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = Request(
        TELEGRAM_API.format(token=token),
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

    for part in build_message_parts(path):
        send(part, token, chat_id)

    print(f"Digest skickad från {path}")


if __name__ == "__main__":
    main()