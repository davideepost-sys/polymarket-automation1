import os
import sys
import csv
import glob
import json
import html
from urllib.request import Request, urlopen
from urllib.parse import urlencode

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

def latest_csv( ):
    files = sorted(glob.glob("traders_*.csv"))
    return files[-1] if files else None

def has_real_username(name):
    return not (name.startswith("0x") and name.endswith("…"))

def build_message(path):
    msg_parts = []
    
    # Lägg till AI-sammanfattning om den finns
    if os.path.exists("ai_summary.txt"):
        with open("ai_summary.txt", "r") as f:
            msg_parts.append(f"<b>🤖 AI ANALYS:</b>\n{f.read()}\n")

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    
    if not rows:
        msg_parts.append("Hej, ny dag nya möjligheter! Inga traders klarade filtren idag.")
    else:
        msg_parts.append("<b>📊 DAGENS TRADERS:</b>\n")
        for i, r in enumerate(rows, 1):
            name = r["Name"]
            safe_name = html.escape(name)
            if has_real_username(name):
                header = f'{i}. <a href="https://polymarket.com/@{name}"><b>{safe_name}</b></a>'
            else:
                header = f"{i}. <b>{safe_name}</b>"
            msg_parts.append(
                f"{header}\n"
                f' PR: {float(r["ProfitRate"] )*100:.1f}% | WR: {r["WinRate"]}% | RR: {r["RR"]}\n'
            )
    return "\n".join(msg_parts)

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

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    path = latest_csv()
    if not path:
        sys.exit(1)
    
    msg = build_message(path)
    send(msg, token, chat_id)

if __name__ == "__main__":
    main()
