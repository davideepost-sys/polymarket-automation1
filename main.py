import os
import logging
import csv
import json
from urllib.request import Request, urlopen
from urllib.parse import urlencode
import requests # För GitHub API-anrop

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

import scraper_2
import lookup_trader

# Aktivera loggning
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx" ).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Tillstånd för konversationshanteraren
CONVERSATION_STATE = 1

# --- Konfiguration ---
GITHUB_REPO_OWNER = "davideepost-sys"       # <--- ÄNDRAD till ditt användarnamn
GITHUB_REPO_NAME = "polymarket-automation1" # <--- ÄNDRAD till ditt repo-namn

# --- AI-integration (Groq) ---
def get_ai_response(prompt, system_prompt="Du är en hjälpsam AI-assistent.", chat_history=None):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY saknas.")
        return "Fel: GROQ_API_KEY saknas i miljövariablerna."

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt} )
    if chat_history:
        messages.extend(chat_history)
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": "llama-3.3-70b-versatile", # Använder den kraftfulla Llama 3.3 70B-modellen
        "messages": messages,
        "temperature": 0.7, # Justera för kreativitet (0.0-1.0)
        "max_tokens": 1024 # Begränsa svarslängden
    }

    req = Request(url, data=json.dumps(data).encode(), headers=headers)
    try:
        with urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"AI-fel: {e}")
        return f"Tyvärr, jag kunde inte kommunicera med AI:n just nu. Fel: {e}"

# --- GitHub Actions-integration ---
def trigger_github_workflow(workflow_id, inputs=None):
    github_token = os.environ.get("HEY_GITHUB_PAT")
    if not github_token:
        logger.error("HEY_GITHUB_PAT saknas.")
        return False, "Fel: HEY_GITHUB_PAT saknas i miljövariablerna."

    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/actions/workflows/{workflow_id}/dispatches"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "ref": "main", # Eller din standardgren, t.ex. "master"
        "inputs": inputs if inputs else {}
    }

    try:
        response = requests.post(url, headers=headers, json=data )
        response.raise_for_status() # Utlöser ett undantag för HTTP-fel
        return True, "Workflow triggad framgångsrikt!"
    except requests.exceptions.RequestException as e:
        logger.error(f"Fel vid triggning av GitHub Workflow: {e}")
        return False, f"Kunde inte trigga workflow. Fel: {e}"

def get_latest_workflow_run_status(workflow_id):
    github_token = os.environ.get("HEY_GITHUB_PAT")
    if not github_token:
        return "Fel: HEY_GITHUB_PAT saknas."

    url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/actions/workflows/{workflow_id}/runs"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        response = requests.get(url, headers=headers )
        response.raise_for_status()
        runs = response.json().get("workflow_runs")
        if runs:
            latest_run = runs[0]
            status = latest_run.get("status")
            conclusion = latest_run.get("conclusion")
            html_url = latest_run.get("html_url")
            return f"Senaste körningen ({latest_run.get('id')}) för workflow '{workflow_id}': Status: {status}, Slutsats: {conclusion}. <a href=\"{html_url}\">Se detaljer</a>"
        return f"Inga körningar hittades för workflow '{workflow_id}'."
    except requests.exceptions.RequestException as e:
        logger.error(f"Fel vid hämtning av workflow-status: {e}")
        return f"Kunde inte hämta workflow-status. Fel: {e}"

# --- Polymarket Data Integration ---
def get_latest_traders_summary():
    try:
        leaderboard_entries = scraper_2.get_leaderboard(scraper_2.POOL) 
        if not leaderboard_entries:
            return "Ingen Polymarket-data tillgänglig för analys just nu."

        traders_for_ai = []
        for entry in leaderboard_entries:
            analyzed_data = scraper_2.analyze_trader(entry)
            if analyzed_data and "skip" not in analyzed_data:
                traders_for_ai.append({
                    "Name": entry.get("userName") or entry.get("xUsername") or (entry.get("proxyWallet")[:8] + "…"),
                    "ProfitRate": analyzed_data.get("ProfitRate"),
                    "WinRate": analyzed_data.get("WinRate"),
                    "RR": analyzed_data.get("RR"),
                    "AvgHoldingDays": analyzed_data.get("AvgHoldingDays"),
                    "WeeklyTrades": analyzed_data.get("WeeklyTrades"),
                    "MarketCount": analyzed_data.get("MarketCount"),
                })

        if not traders_for_ai:
            return "Inga traders klarade analyskriterierna idag."

        trader_data_str = "\n".join([f"- {t['Name']}: PR {t['ProfitRate']*100:.1f}%, WR {t['WinRate']}%" for t in traders_for_ai])
        prompt = f"Här är dagens topp-traders från Polymarket:\n{trader_data_str}\n\nGe en kort, proffsig sammanfattning på svenska om vem som ser mest lovande ut och varför. Fokusera på WinRate och ProfitRate."
        
        return get_ai_response(prompt, system_prompt="Du är en expert på trading och Polymarket och sammanfattar dagens topp-traders.")
    except Exception as e:
        logger.error(f"Fel vid hämtning/analys av topp-traders: {e}")
        return f"Kunde inte hämta topp-traders just nu. Fel: {e}"

def lookup_trader_info_with_ai(trader_identifier):
    try:
        analysis_result = lookup_trader.get_trader_analysis(trader_identifier)
        
        ai_prompt = f"Här är en analys av en Polymarket-trader:\n{analysis_result}\n\nGe en kort, proffsig kommentar på svenska om denna trader. Är det en bra kandidat för copy-trading baserat på informationen?"
        ai_comment = get_ai_response(ai_prompt, system_prompt="Du är en expert på trading och Polymarket och kommenterar trader-analyser.")
        
        return f"{analysis_result}\n\n<b>AI Kommentar:</b>\n{ai_comment}"
    except Exception as e:
        logger.error(f"Fel vid sökning/analys av trader: {e}")
        return f"Kunde inte söka efter trader just nu. Fel: {e}"

# --- Telegram Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skickar ett meddelande vid /start och sätter upp konversationstillstånd."""
    await update.message.reply_text(
        "Hej! Jag är din smarta Polymarket AI-assistent. Jag kan svara på frågor om trading, Polymarket, eller bara snacka lite. Jag kan också hjälpa dig att trigga GitHub Actions. Vad kan jag hjälpa dig med?"
    )
    context.user_data["chat_history"] = [] # Initiera chatthistorik
    return CONVERSATION_STATE

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skickar ett meddelande vid /help-kommandot."""
    await update.message.reply_text(
        "Jag kan svara på frågor om Polymarket, tradingstrategier, eller allmänna frågor. "
        "Du kan också fråga mig om dagens topp-traders med kommandot /toptraders. "
        "För att söka efter en specifik trader, skriv /lookup <trader_namn_eller_wallet>.\n\n"
        "För GitHub Actions kan du använda: "
        "/run_daily_analysis - för att trigga den dagliga analysen manuellt.\n"
        "/run_lookup <trader> [search_depth] - för att trigga en manuell trader-sökning.\n"
        "/status_daily - för att se status på den dagliga analysen.\n"
        "/status_lookup - för att se status på den manuella trader-sökningen.\n\n"
        "Skriv bara vad du vill prata om för en fri konversation!"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Hanterar inkommande meddelanden och skickar dem till AI:n."""
    user_message = update.message.text
    chat_history = context.user_data.get("chat_history", [])

    # Behåll chatthistoriken till en rimlig längd för att undvika att överskrida tokenbegränsningar
    if len(chat_history) > 10:
        chat_history = chat_history[-10:] 

    # Lägg till användarmeddelande i historiken
    chat_history.append({"role": "user", "content": user_message})

    # Hämta AI-svar med chatthistorik för kontext
    ai_response = get_ai_response(user_message, chat_history=chat_history)
    await update.message.reply_text(ai_response)

    # Lägg till AI-svar i historiken
    chat_history.append({"role": "assistant", "content": ai_response})
    context.user_data["chat_history"] = chat_history # Uppdatera chatthistoriken

    return CONVERSATION_STATE

async def toptraders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skickar en sammanfattning av topp-traders med AI."""
    await update.message.reply_text("Hämtar och analyserar dagens topp-traders, ett ögonblick...")
    summary = get_latest_traders_summary()
    await update.message.reply_text(summary)

async def lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Söker upp en specifik trader med AI."""
    if not context.args:
        await update.message.reply_text("Vänligen ange ett användarnamn eller en wallet-adress efter /lookup. Ex: /lookup JohnDoe")
        return
    
    trader_identifier = " ".join(context.args)
    await update.message.reply_text(f"Söker efter \'{trader_identifier}\' och ber AI:n kommentera, ett ögonblick...")
    info = lookup_trader_info_with_ai(trader_identifier)
    await update.message.reply_text(info)

async def run_daily_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Triggar den dagliga Polymarket-analysen på GitHub...")
    success, message = trigger_github_workflow("daily_run_2.yml") # Använd ditt workflow-filnamn
    await update.message.reply_text(message)

async def run_lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Vänligen ange ett användarnamn eller en wallet-adress efter /run_lookup. Ex: /run_lookup JohnDoe")
        return
    
    trader_identifier = context.args[0]
    search_depth = context.args[1] if len(context.args) > 1 else "2000"

    inputs = {"trader": trader_identifier, "search_depth": search_depth}
    await update.message.reply_text(f"Triggar manuell trader-sökning för '{trader_identifier}' på GitHub...")
    success, message = trigger_github_workflow("lookup_trader.yml", inputs=inputs) # Använd ditt workflow-filnamn
    await update.message.reply_text(message)

async def status_daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hämtar status för den dagliga analysen...")
    status_message = get_latest_workflow_run_status("daily_run_2.yml")
    await update.message.reply_text(status_message)

async def status_lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hämtar status för manuell trader-sökning...")
    status_message = get_latest_workflow_run_status("lookup_trader.yml")
    await update.message.reply_text(status_message)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Avbryter och avslutar konversationen."""
    await update.message.reply_text(
        "Hejdå! Hoppas vi ses snart igen."
    )
    context.user_data.clear() # Rensa chatthistorik
    return ConversationHandler.END

def main() -> None:
    """Starta botten."""
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN saknas. Vänligen sätt miljövariabeln.")
        return

    application = Application.builder().token(telegram_bot_token).build()

    # Konversationshanterare för ihållande chatt
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CONVERSATION_STATE: [
                CommandHandler("help", help_command),
                CommandHandler("toptraders", toptraders_command),
                CommandHandler("lookup", lookup_command),
                CommandHandler("run_daily_analysis", run_daily_analysis_command),
                CommandHandler("run_lookup", run_lookup_command),
                CommandHandler("status_daily", status_daily_command),
                CommandHandler("status_lookup", status_lookup_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    # Kör botten tills användaren trycker Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Installera requests-biblioteket om det saknas
    try:
        import requests
    except ImportError:
        print("Requests-biblioteket är inte installerat. Installerar...")
        os.system("pip install requests")
        import requests

    main()
