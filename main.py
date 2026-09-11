import csv
import html
import json
import logging
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CONVERSATION_STATE = 1
PROJECT_DIR = Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / "latest_traders.csv"
GITHUB_REPO_OWNER = "davideepost-sys"
GITHUB_REPO_NAME = "polymarket-automation1"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-20b"
USER_AGENT = "PolyGunAssistant/1.0"
RAW_CSV_URL = (
    "https://raw.githubusercontent.com/"
    "davideepost-sys/polymarket-automation1/main/latest_traders.csv"
)
MAX_HISTORY_ITEMS = 10
TELEGRAM_MESSAGE_LIMIT = 3800

AI_SYSTEM_PROMPT = """Du är PolyGun Assistant, en svensk AI-assistent för analys av Polymarket-traderdata.

Viktiga regler:
1. När traderdata finns i kontexten är den aktuella CSV-filen källan till siffrorna.
2. Hitta aldrig på ett saknat värde. Skriv N/A när ett fält saknas.
3. Blanda inte ihop tekniskt wallet/adress-ID med ett verifierat username.
4. Förklara att RR betyder Risk/Reward, PR ProfitRate, WR WinRate och Hold genomsnittlig holdtid.
5. Analysera endast när användaren uttryckligen ber om analys, jämförelse, förklaring eller scenario.
6. Ett scenario med 100 trades är fiktivt. Visa antaganden och räkna bara med startkapital och fast summa eller procent per trade som användaren anger.
7. Ge inte garanti om vinst eller stabil framtida utveckling. Detta är analys, inte garanterad finansiell rådgivning.
8. Om frågan inte kan besvaras från CSV:n, säg exakt vilken information som saknas.
"""


def read_latest_traders():
    """Read fresh public GitHub CSV, with local server CSV as fallback."""
    try:
        request = Request(
            RAW_CSV_URL,
            headers={"User-Agent": USER_AGENT},
            method="GET",
        )
        with urlopen(request, timeout=10) as response:
            text = response.read().decode("utf-8-sig")
        rows = list(csv.DictReader(text.splitlines()))
        if rows:
            try:
                CSV_PATH.write_text(text, encoding="utf-8")
            except OSError:
                logger.warning("Kunde inte uppdatera lokal CSV-cache")
            return rows
    except Exception as error:
        logger.warning("GitHub-CSV kunde inte hämtas: %s", error)

    if not CSV_PATH.exists():
        return []

    try:
        with CSV_PATH.open("r", newline="", encoding="utf-8-sig") as file:
            return list(csv.DictReader(file))
    except (OSError, csv.Error) as error:
        logger.warning("Kunde inte läsa lokal CSV %s: %s", CSV_PATH, error)
        return []


def value(row, column, fallback="N/A"):
    raw = row.get(column, "")
    return raw if raw not in (None, "") else fallback


def is_real_username(name):
    if not name:
        return False
    return not (name.startswith("0x") or "-" in name and name[:2] == "0x")


def trader_link_or_id(row):
    name = value(row, "Name")
    safe_name = html.escape(name)
    trader_id = value(row, "TraderID")

    if is_real_username(name):
        return (
            f'<a href="https://polymarket.com/@{safe_name}">'
            f"<b>{safe_name}</b></a>"
        )

    return f"<b>{safe_name}</b>\n<code>{html.escape(trader_id)}</code>"


def format_trader(index, row):
    marker = " <b>TOP 3</b>" if index <= 3 else ""
    return (
        f"{index}. {trader_link_or_id(row)}{marker}\n"
        f"ProfitRate: {value(row, 'ProfitRate')} | "
        f"WinRate: {value(row, 'WinRate')} | "
        f"Risk/Reward (RR): {value(row, 'RR')}\n"
        f"Genomsnittlig vinst: {value(row, 'AvgWin')} | "
        f"Genomsnittlig förlust: {value(row, 'AvgLoss')} | "
        f"Genomsnittlig holdtid: {value(row, 'AvgHoldingDays')} dagar\n"
    )


def split_messages(header, entries):
    parts = []
    current = header

    for entry in entries:
        if len(current) + len(entry) > TELEGRAM_MESSAGE_LIMIT:
            parts.append(current.rstrip())
            current = "<b>Fortsättning:</b>\n\n"
        current += entry + "\n"

    if current.strip():
        parts.append(current.rstrip())
    return parts


def current_traders_messages():
    rows = read_latest_traders()
    if not rows:
        return [
            "<b>DAGENS TRADERS</b>\n"
            "Ingen aktuell latest_traders.csv kunde läsas."
        ]

    entries = [format_trader(index, row) for index, row in enumerate(rows, 1)]
    return split_messages("<b>DAGENS TRADERS</b>\n\n", entries)


def compact_csv_context(rows, max_rows=100):
    if not rows:
        return "CSV_STATUS: latest_traders.csv saknas eller är tom."

    columns = [
        "Name",
        "TraderID",
        "ProfitRate",
        "WinRate",
        "RR",
        "AvgWin",
        "AvgLoss",
        "AvgHoldingDays",
        "MarketCount",
        "Score",
    ]

    lines = [
        "CSV_STATUS: aktuell latest_traders.csv från PolyGun scraper/GitHub",
        f"CSV_ROWS_TOTAL: {len(rows)}",
        "Saknade värden är N/A och får inte ersättas eller gissas.",
        "DATA:",
    ]

    for index, row in enumerate(rows[:max_rows], 1):
        values = [f"{column}={value(row, column)}" for column in columns]
        lines.append(f"{index}. " + " | ".join(values))

    if len(rows) > max_rows:
        lines.append(f"Ytterligare {len(rows) - max_rows} rader finns i CSV:n men visas inte i denna kontext.")

    return "\n".join(lines)


def question_needs_trader_context(prompt):
    lowered = prompt.lower()
    keywords = (
        "trader", "csv", "top", "rank", "ranking", "profitrate", "profit rate",
        "winrate", "win rate", "rr", "risk/reward", "hold", "hålltid", "vinst",
        "förlust", "copytrade", "copytrade", "jämför", "jämfö", "scenario",
        "100 trades", "alla", "vilka", "bäst", "värden",
    )
    return any(keyword in lowered for keyword in keywords)


def get_ai_response(prompt, system_prompt=AI_SYSTEM_PROMPT, chat_history=None):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "AI-fel: GROQ_API_KEY saknas på servern."

    messages = [{"role": "system", "content": system_prompt}]
    if chat_history:
        messages.extend(chat_history[-MAX_HISTORY_ITEMS:])
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 1400,
    }

    request = Request(
        GROQ_URL,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result.get("choices", [{}])[0].get("message", {}).get("content")
        return content.strip() if content else "AI-fel: Groq returnerade inget textsvar."
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8", "replace"))
            message = body.get("error", {}).get("message", error.reason)
        except Exception:
            message = error.reason
        return f"AI-fel HTTP {error.code}: {message}"
    except (URLError, TimeoutError) as error:
        return f"AI-fel: anslutningen till Groq misslyckades: {error}"
    except Exception as error:
        logger.exception("AI-anrop misslyckades")
        return f"AI-fel: {error}"


def trigger_github_workflow(workflow_id, inputs=None):
    github_token = os.environ.get("HEY_GITHUB_PAT")
    if not github_token:
        return False, "Kan inte starta körningen: HEY_GITHUB_PAT saknas."

    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/"
        f"{GITHUB_REPO_NAME}/actions/workflows/{workflow_id}/dispatches"
    )
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"ref": "main", "inputs": inputs or {}}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=20)
        response.raise_for_status()
        return True, "Workflow triggad. Kontrollera senare med /status_daily."
    except Exception as error:
        return False, f"Workflow kunde inte startas: {error}"


def get_latest_workflow_run_status(workflow_id):
    github_token = os.environ.get("HEY_GITHUB_PAT")
    if not github_token:
        return "HEY_GITHUB_PAT saknas på servern."

    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/"
        f"{GITHUB_REPO_NAME}/actions/workflows/{workflow_id}/runs"
    )
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        runs = response.json().get("workflow_runs", [])
        if not runs:
            return "Inga körningar hittades."
        run = runs[0]
        return (
            f"Status: {run.get('status')} | Slutsats: {run.get('conclusion')}\n"
            f"{run.get('html_url')}"
        )
    except Exception as error:
        return f"Status kunde inte hämtas: {error}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["chat_history"] = []
    await update.message.reply_text(
        "PolyGun Assistant är redo.\n\n"
        "Jag läser aktuell traderdata när du uttryckligen frågar om den.\n"
        "Exempel: jämför trader 1 och 2, förklara RR 0,65 eller simulera 100 fiktiva trades.\n\n"
        "Använd /help för kommandon och /clear för att rensa kort chatthistorik."
    )
    return CONVERSATION_STATE


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Kommandon:\n"
        "/start — börja om och rensa kort chatthistorik\n"
        "/clear — rensa kort chatthistorik\n"
        "/top — visa aktuell CSV direkt\n"
        "/toptraders — samma som /top\n"
        "/run [antal] — starta GitHub-körning, standard 500\n"
        "/lookup <trader> — slå upp trader\n"
        "/run_lookup <trader> — starta lookup-workflow\n"
        "/status_daily — status för senaste körning\n"
        "/status_lookup — status för senaste lookup\n\n"
        "AI-frågor skrivs som vanlig text. Exempel:\n"
        "Förklara RR 0,65.\n"
        "Jämför trader 1 och trader 2.\n"
        "Simulera 100 trades med 1000 USDC och 10 USDC per trade."
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["chat_history"] = []
    await update.message.reply_text("Kort chatthistorik rensad.")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for part in current_traders_messages():
        await update.message.reply_text(part, parse_mode="HTML", disable_web_page_preview=True)


async def lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        context.user_data["awaiting_lookup"] = True
        await update.message.reply_text(
            "Då ska vi se...\n"
            "Behöver bara ett namn eller trader-ID, t.ex: DINTRADER123."
        )
        return
    try:
        import lookup_trader
        result = lookup_trader.get_trader_analysis(" ".join(context.args))
    except Exception as error:
        result = f"Lookup kunde inte köras: {error}"
    await update.message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)


async def run_daily_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pool_size = 500
    if context.args:
        try:
            pool_size = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Använd exempelvis /run 500. Antalet måste vara ett heltal.")
            return

    if pool_size < 1 or pool_size > 1000:
        await update.message.reply_text("Antalet måste vara mellan 1 och 1000.")
        return

    ok, message = trigger_github_workflow(
        "daily_run_2.yml",
        {"pool_size": str(pool_size)},
    )
    await update.message.reply_text(message)


async def run_automation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_daily_analysis_command(update, context)


async def run_lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Använd: /run_lookup <username, wallet eller profilänk>")
        return
    ok, message = trigger_github_workflow(
        "lookup_trader.yml",
        {"trader": " ".join(context.args)},
    )
    await update.message.reply_text(message)


async def status_daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(get_latest_workflow_run_status("daily_run_2.yml"))


async def status_lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(get_latest_workflow_run_status("lookup_trader.yml"))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prompt = update.message.text.strip()

    if context.user_data.pop("awaiting_lookup", False):
        await update.message.reply_text("Toppen, ge mig en sekund så ska jag kolla...")
        try:
            import lookup_trader
            result = lookup_trader.get_trader_analysis(prompt)
        except Exception as error:
            result = f"Lookup kunde inte köras: {error}"
        await update.message.reply_text(result, parse_mode="HTML", disable_web_page_preview=True)
        return CONVERSATION_STATE

    history = context.user_data.get("chat_history", [])

    if question_needs_trader_context(prompt):
        rows = read_latest_traders()
        prompt_for_ai = (
            "Användaren frågar om aktuell traderdata. Läs CSV-kontexten nedan och svara på svenska.\n\n"
            f"{compact_csv_context(rows)}\n\n"
            f"ANVÄNDARENS FRÅGA:\n{prompt}"
        )
    else:
        prompt_for_ai = prompt

    response = get_ai_response(prompt_for_ai, chat_history=history)
    await update.message.reply_text(response)

    history.extend([
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": response},
    ])
    context.user_data["chat_history"] = history[-MAX_HISTORY_ITEMS:]
    return CONVERSATION_STATE


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN saknas")

    app = Application.builder().token(token).build()
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("lookup", lookup_command),
        ],
        states={
            CONVERSATION_STATE: [
                CommandHandler("help", help_command),
                CommandHandler("clear", clear_command),
                CommandHandler("top", top_command),
                CommandHandler("toptraders", top_command),
                CommandHandler("lookup", lookup_command),
                CommandHandler("run", run_daily_analysis_command),
                CommandHandler("run_automation", run_automation_command),
                CommandHandler("run_daily_analysis", run_daily_analysis_command),
                CommandHandler("run_lookup", run_lookup_command),
                CommandHandler("status_daily", status_daily_command),
                CommandHandler("status_lookup", status_lookup_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
            ]
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("lookup", lookup_command),
        ],
        allow_reentry=True,
    )
    app.add_handler(conversation)
    app.run_polling()


if __name__ == "__main__":
    main()
