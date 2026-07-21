import os
import logging
import csv
import json
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import requests

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

import scraper_2
import lookup_trader

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

CONVERSATION_STATE = 1

# --- Din Konfiguration ---
GITHUB_REPO_OWNER = "davideepost-sys"
GITHUB_REPO_NAME = "polymarket-automation1"

def get_ai_response(prompt, system_prompt="Du är en hjälpsam AI-assistent.", chat_history=None):
    api_key = os.environ.get("GROQ_API_KEY")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages = [{"role": "system", "content": system_prompt}]
    if chat_history: messages.extend(chat_history )
    messages.append({"role": "user", "content": prompt})
    data = {"model": "llama-3.3-70b-versatile", "messages": messages, "temperature": 0.7, "max_tokens": 1024}
    req = Request(url, data=json.dumps(data).encode(), headers=headers)
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read().decode())["choices"][0]["message"]["content"]
    except Exception as e: return f"AI-fel: {e}"

def trigger_github_workflow(workflow_id, inputs=None):
    github_token = os.environ.get("HEY_GITHUB_PAT")
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/actions/workflows/{workflow_id}/dispatches"
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
    data = {"ref": "main", "inputs": inputs if inputs else {}}
    try:
        response = requests.post(url, headers=headers, json=data )
        response.raise_for_status()
        return True, "Workflow triggad!"
    except Exception as e: return False, f"Fel: {e}"

def get_latest_workflow_run_status(workflow_id):
    github_token = os.environ.get("HEY_GITHUB_PAT")
    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/actions/workflows/{workflow_id}/runs"
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(url, headers=headers )
        runs = response.json().get("workflow_runs")
        if runs:
            r = runs[0]
            return f"Status: {r.get('status')}, Slutsats: {r.get('conclusion')}. {r.get('html_url')}"
        return "Inga körningar hittades."
    except Exception as e: return f"Fel: {e}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Bot startad. Prata med mig eller använd /help.")
    context.user_data["chat_history"] = []
    return CONVERSATION_STATE

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/toptraders\n/lookup <trader>\n/run_daily_analysis\n/run_lookup <trader>\n/status_daily\n/status_lookup")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    history = context.user_data.get("chat_history", [])
    response = get_ai_response(update.message.text, chat_history=history)
    await update.message.reply_text(response)
    history.append({"role": "user", "content": update.message.text})
    history.append({"role": "assistant", "content": response})
    context.user_data["chat_history"] = history[-10:]
    return CONVERSATION_STATE

async def toptraders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Analyserar...")
    # Här anropas din scraper_2 logik
    await update.message.reply_text("Hämtar data...")

async def lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args: return
    await update.message.reply_text(lookup_trader.get_trader_analysis(context.args[0]))

async def run_daily_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, msg = trigger_github_workflow("daily_run_2.yml")
    await update.message.reply_text(msg)

async def run_lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args: return
    _, msg = trigger_github_workflow("lookup_trader.yml", {"trader": context.args[0]})
    await update.message.reply_text(msg)

async def status_daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(get_latest_workflow_run_status("daily_run_2.yml"))

async def status_lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(get_latest_workflow_run_status("lookup_trader.yml"))

def main() -> None:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={CONVERSATION_STATE: [
            CommandHandler("help", help_command),
            CommandHandler("toptraders", toptraders_command),
            CommandHandler("lookup", lookup_command),
            CommandHandler("run_daily_analysis", run_daily_analysis_command),
            CommandHandler("run_lookup", run_lookup_command),
            CommandHandler("status_daily", status_daily_command),
            CommandHandler("status_lookup", status_lookup_command),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
        ]},
        fallbacks=[CommandHandler("cancel", start)],
    )
    app.add_handler(conv)
    app.run_polling()

if __name__ == "__main__":
    main()
