import os
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# --- Konfigurasjon ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
HOLMES_URL = os.environ.get("HOLMES_URL", "http://robusta-holmes.robusta.svc.cluster.local:80")
MODEL = os.environ.get("MODEL", "model1")

# Initialiser Bolt-appen
app = App(token=SLACK_BOT_TOKEN)

def ask_holmes(query):
    """Sender spørsmål til Holmes GPT API og returnerer analysen."""
    try:
        print(f"Sender til Holmes: {query}")
        response = requests.post(
            f"{HOLMES_URL}/api/chat",
            json={
                "ask": query,
                "model": MODEL
            },
            timeout=180 # AI-undersøkelser kan ta tid
        )
        response.raise_for_status()
        data = response.json()
        return data.get("analysis", "Fikk ingen analyse fra Holmes.")
    except Exception as e:
        return f"❌ Feil ved kontakt med Holmes: {str(e)}"

# Denne fanger opp alle meldinger i kanaler boten er medlem av
@app.message("") 
def handle_all_messages(message, say):
    # SIKKERHETSSJEKK: Ikke svar hvis meldingen kommer fra en bot (inkludert seg selv)
    if "bot_id" in message or message.get("subtype") == "bot_message":
        return

    user_query = message.get("text", "").strip()
    if not user_query:
        return

    # Vis brukeren at vi jobber (valgfritt, men hyggelig)
    # say(text="🔍 Tenker...", thread_ts=message.get("ts"))

    # Hent svar fra Holmes
    answer = ask_holmes(user_query)
    
    # Svar i samme tråd
    say(text=answer, thread_ts=message.get("ts"))

# Støtte for direkte @mentions (hvis du skulle trenge det i andre kanaler)
@app.event("app_mention")
def handle_app_mentions(event, say):
    user_query = event.get("text", "").split(">")[-1].strip()
    if user_query:
        answer = ask_holmes(user_query)
        say(text=answer, thread_ts=event.get("ts"))

if __name__ == "__main__":
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        print("CRITICAL ERROR: Mangler SLACK_BOT_TOKEN eller SLACK_APP_TOKEN")
        exit(1)
    
    print(f"🚀 Starter Holmes Slack Bot (Mode: All-Access)")
    print(f"Connecting to Holmes at: {HOLMES_URL}")
    
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
