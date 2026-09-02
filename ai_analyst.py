import csv
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-20b"
USER_AGENT = "PolyGunAssistant/1.0"
REQUEST_TIMEOUT_SECONDS = 30


def _error_message(prefix, message):
    """Return a short, non-secret status marker for the digest workflow."""
    clean = " ".join(str(message).split())
    return f"AI_UNAVAILABLE: {prefix}: {clean[:300]}"


def get_ai_response_for_summary(
    prompt,
    system_prompt="Du är en expert på trading och Polymarket och sammanfattar dagens topptraders.",
):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return _error_message(
            "GROQ_API_KEY saknas",
            "kontrollera GitHub Secret GROQ_API_KEY",
        )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 1024,
    }

    request = Request(
        GROQ_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))

        content = result.get("choices", [{}])[0].get("message", {}).get("content")
        if not content:
            return _error_message(
                "tomt AI-svar",
                "Groq returnerade inget textinnehåll",
            )

        return content.strip()

    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8", "replace"))
            message = body.get("error", {}).get("message", error.reason)
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = error.reason

        return _error_message(f"HTTP {error.code}", message)

    except (URLError, TimeoutError) as error:
        return _error_message("anslutningsfel", error)

    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        return _error_message("ogiltigt Groq-svar", error)

    except Exception as error:
        return _error_message("oväntat fel", error)


def analyze_latest_traders_for_digest():
    csv_path = "latest_traders.csv"

    if not os.path.exists(csv_path):
        return _error_message("CSV saknas", csv_path)

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as file:
        traders = list(csv.DictReader(file))[:5]

    if not traders:
        return _error_message("CSV tom", csv_path)

    trader_data = json.dumps(traders, ensure_ascii=False, indent=2)

    prompt = (
        "Här är dagens topp-traders från Polymarket i CSV-format:\n"
        f"{trader_data}\n\n"
        "Ge en kort, professionell sammanfattning på svenska av vilka som ser mest lovande ut "
        "och varför. Använd endast uppgifterna i CSV:n. Fokusera på ProfitRate, WinRate, RR, "
        "AvgWin, AvgLoss och AvgHoldingDays. Hitta inte på saknade värden; skriv N/A när ett "
        "fält saknas. Skriv inte investeringsråd och kalla inte en trader säker eller garanterad."
    )

    return get_ai_response_for_summary(prompt)


if __name__ == "__main__":
    print(analyze_latest_traders_for_digest())
