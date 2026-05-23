import os
import requests
import threading
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
            timeout=600  # AI-undersøkelser kan ta tid
        )
        response.raise_for_status()
        data = response.json()
        return data.get("analysis", "Fikk ingen analyse fra Holmes.")
    except Exception as e:
        return f"❌ Feil ved kontakt med Holmes: {str(e)}"

def get_thread_context(client, channel, thread_ts):
    """Henter alle meldinger i en tråd og formaterer dem som kontekst for Holmes."""
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts)
        messages = result.get("messages", [])
        
        context_lines = []
        for msg in messages:
            # Sjekk om meldingen kom fra en bot eller en faktisk bruker
            role = "Holmes (Meg)" if ("bot_id" in msg or msg.get("subtype") == "bot_message") else "Bruker"
            text = msg.get("text", "").strip()
            if text:
                context_lines.append(f"{role}: {text}")
        
        return "\n".join(context_lines)
    except Exception as e:
        print(f"Kunne ikke hente trådhistorikk: {e}")
        return ""

def process_message_async(client, message, say):
    """Kjører i bakgrunnen: Sender status, henter historikk, spør Holmes, og oppdaterer svaret."""
    channel = message.get("channel")
    ts = message.get("ts")
    
    # Hvis dette allerede er en tråd, svarer vi i den. Hvis ikke, starter vi en ny tråd basert på meldingens ts.
    thread_ts = message.get("thread_ts", ts)
    
    # Vasker teksten litt om det er en app_mention
    user_query = message.get("text", "").strip()
    if message.get("type") == "app_mention" or "app_mention" in message.get("subtype", ""):
        user_query = user_query.split(">")[-1].strip()

    if not user_query:
        return

    # 1. Send en umiddelbar statusoppdatering i Slack (så brukeren ser at ting skjer)
    try:
        placeholder = say(text="🔍 _Holmes undersøker saken... Vennligst vent._", thread_ts=thread_ts)
        placeholder_ts = placeholder.get("ts")
    except Exception as e:
        print(f"Feil ved sending av statusmelding: {e}")
        return

    # 2. Sjekk om meldingen er en del av en tråd, og bygg kontekst/historikk
    if message.get("thread_ts"):
        print(f"Henter trådkontekst for tråd: {thread_ts}")
        history = get_thread_context(client, channel, thread_ts)
        
        # Vi pakker historikken pent inn i forespørselen til Holmes
        prompt = (
            f"Du er en AI-assistent i en pågående Slack-samtale. Her er historikken så langt:\n"
            f"{history}\n\n"
            f"Svar på det siste spørsmålet/oppfølgingen basert på denne konteksten."
        )
    else:
        prompt = user_query

    # 3. Gjør det tunge API-kallet mot Holmes (Dette kan ta minutter)
    answer = ask_holmes(prompt)

    # 4. Rediger placeholder-meldingen i Slack med det ekte svaret
    try:
        client.chat_update(
            channel=channel,
            ts=placeholder_ts,
            text=answer
        )
    except Exception as e:
        print(f"Feil ved oppdatering av melding ({e}). Sender som ny melding i stedet.")
        say(text=answer, thread_ts=thread_ts)

# Fang opp meldinger i kanaler
@app.message("") 
def handle_all_messages(message, client, say):
    if "bot_id" in message or message.get("subtype") == "bot_message":
        return

    # Frikoble Slack-tråden umiddelbart ved å dytte jobben over i en Python-bakgrunnstråd
    threading.Thread(target=process_message_async, args=(client, message, say)).start()

# Fang opp direkte @mentions
@app.event("app_mention")
def handle_app_mentions(event, client, say):
    # Gjenbruker samme asynkrone logikk
    threading.Thread(target=process_message_async, args=(client, event, say)).start()

if __name__ == "__main__":
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        print("CRITICAL ERROR: Mangler SLACK_BOT_TOKEN eller SLACK_APP_TOKEN")
        exit(1)
    
    print(f"🚀 Starter Holmes Slack Bot (Mode: Enterprise Async & Thread-Aware)")
    print(f"Connecting to Holmes at: {HOLMES_URL}")
    
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
