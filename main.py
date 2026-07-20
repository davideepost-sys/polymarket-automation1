import os
import json
from urllib.request import Request, urlopen
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

def get_ai_response(prompt, history):
    api_key = os.environ.get("GROQ_API_KEY")
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages = [{"role": "system", "content": "Du är en smart trading-assistent. Svara kort och proffsigt på svenska."}]
    messages.extend(history )
    messages.append({"role": "user", "content": prompt})
    
    data = json.dumps({"model": "llama-3.3-70b-versatile", "messages": messages}).encode()
    req = Request(url, data=data, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urlopen(req) as resp:
        return json.loads(resp.read().decode())["choices"][0]["message"]["content"]

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = context.user_data.get("history", [])
    response = get_ai_response(update.message.text, history)
    await update.message.reply_text(response)
    history.append({"role": "user", "content": update.message.text})
    history.append({"role": "assistant", "content": response})
    context.user_data["history"] = history[-10:] # Kommer ihåg de 10 senaste sakerna

if __name__ == "__main__":
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.run_polling()
