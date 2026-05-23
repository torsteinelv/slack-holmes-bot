import os
import json
import logging
import requests
import threading
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# --- Konfigurer strukturert logging for Kubernetes ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# --- Konfigurasjon ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
HOLMES_URL = os.environ.get("HOLMES_URL", "http://robusta-holmes.robusta.svc.cluster.local:80")
MODEL = os.environ.get("MODEL", "model1")

# Initialiser Bolt-appen
app = App(token=SLACK_BOT_TOKEN)

def get_thread_context(client, channel, thread_ts):
    """Henter alle meldinger i en tråd og formaterer dem som kontekst for Holmes."""
    try:
        logging.info(f"Henter trådhistorikk for kanal {channel}, tråd {thread_ts}")
        result = client.conversations_replies(channel=channel, ts=thread_ts)
        messages = result.get("messages", [])
        
        context_lines = []
        for msg in messages:
            role = "Holmes (Meg)" if ("bot_id" in msg or msg.get("subtype") == "bot_message") else "Bruker"
            text = msg.get("text", "").strip()
            if text:
                context_lines.append(f"{role}: {text}")
        
        return "\n".join(context_lines)
    except Exception as e:
        logging.error(f"Kunne ikke hente trådhistorikk: {e}")
        return ""

def update_slack_status(client, channel, ts, tasks, action):
    """Bygger og oppdaterer fremdriftsstatusen i Slack-meldingen underveis."""
    status_text = "🔍 *Holmes feilsøker saken...*\n"
    if tasks:
        status_text += f"\n📊 *Fremdrift på undersøkelsen:*\n```\n{tasks}\n```\n"
    if action:
        status_text += f"\n{action}"
        
    try:
        client.chat_update(channel=channel, ts=ts, text=status_text)
    except Exception as e:
        logging.error(f"Feil ved oppdatering av Slack-status: {e}")

def find_table_in_json(obj):
    """Søker rekursivt igjennom et JSON-objekt for å finne ASCII-tabellen fra TodoWrite."""
    if isinstance(obj, str) and "+--" in obj and "Status" in obj:
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            res = find_table_in_json(v)
            if res:
                return res
    if isinstance(obj, list):
        for item in obj:
            res = find_table_in_json(item)
            if res:
                return res
    return None

def process_message_async(client, message, say):
    """Kjører asynkront i bakgrunnen. Håndterer strømming, oppgavelister og sluttresultat."""
    channel = message.get("channel")
    ts = message.get("ts")
    thread_ts = message.get("thread_ts", ts)
    
    user_query = message.get("text", "").strip()
    if message.get("type") == "app_mention" or "app_mention" in message.get("subtype", ""):
        user_query = user_query.split(">")[-1].strip()

    if not user_query:
        return

    logging.info(f"Ny forespørsel mottatt fra bruker i kanal {channel}. Tekst: '{user_query}'")

    # 1. Send initial statusmelding til Slack
    try:
        placeholder = say(text="🔍 _Holmes oppretter en feilsøkingsplan... Vær vennlig å vente._", thread_ts=thread_ts)
        placeholder_ts = placeholder.get("ts")
    except Exception as e:
        logging.error(f"Feil ved sending av initial status til Slack: {e}")
        return

    # 2. Hent trådkontekst hvis relevant
    if message.get("thread_ts"):
        history = get_thread_context(client, channel, thread_ts)
        prompt = (
            f"Du er en AI-assistent i en pågående Slack-samtale. Her er historikken så langt:\n"
            f"{history}\n\n"
            f"Svar på det siste spørsmålet/oppfølgingen basert på denne konteksten."
        )
    else:
        prompt = user_query

    current_tasks = ""
    current_action = ""
    final_answer = "Fikk ingen endelig analyse fra Holmes."

    # 3. Stream fra Holmes-API-et
    try:
        logging.info(f"Kobler til Holmes API på {HOLMES_URL}/api/chat (Model: {MODEL})")
        response = requests.post(
            f"{HOLMES_URL}/api/chat",
            json={
                "ask": prompt,
                "model": MODEL,
                "stream": True
            },
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=600
        )
        response.raise_for_status()
        logging.info("Vellykket tilkobling opprettet mot Holmes SSE-strøm.")

        current_event = None

        for line in response.iter_lines():
            if not line:
                continue
            
            decoded_line = line.decode('utf-8').strip()
            
            if decoded_line.startswith("event:"):
                current_event = decoded_line.split(":", 1)[1].strip()
            elif decoded_line.startswith("data:"):
                data_str = decoded_line.split(":", 1)[1].strip()
                try:
                    data = json.loads(data_str)
                    
                    # --- DYNAMISK TABELL-SKANNING ---
                    # Sjekker om denne datapakken inneholder oppgavelisten, uavhengig av struktur og feltnavn
                    if "+--" in data_str and "Status" in data_str:
                        found_table = find_table_in_json(data)
                        if found_table:
                            current_tasks = found_table.strip()
                            logging.info(f"Suksess: Fant og oppdaterte oppgaveliste via global skanning ({len(current_tasks)} tegn).")

                    # --- HÅNDTERING AV EVENTS ---
                    if current_event == "start_tool_calling":
                        tool_name = data.get("tool_name", "")
                        description = data.get("description", "Undersøker...")
                        logging.info(f"[Holmes Event] Starter verktøy: {tool_name}")
                        
                        if tool_name == "TodoWrite":
                            current_action = "📝 _Oppdaterer planen over oppgaver..._"
                        else:
                            current_action = f"🔨 _Kjører verktøy:_ `{tool_name}`\n📋 {description}"
                    
                    elif current_event == "ai_message":
                        thought = data.get("message", "")
                        if thought:
                            logging.info(f"[Holmes Event] AI Tenker: {thought}")
                            current_action = f"💭 _Tenker:_ {thought}"
                    
                    elif current_event == "ai_answer_end":
                        logging.info("[Holmes Event] Endelig analyse mottatt fra Holmes.")
                        final_answer = data.get("analysis", final_answer)
                    
                    # Oppdaterer Slack-meldingen live for hvert event i strømmen
                    update_slack_status(client, channel, placeholder_ts, current_tasks, current_action)
                        
                except Exception as parse_err:
                    logging.error(f"Kunne ikke tolke live-data JSON: {parse_err}")

        # 4. Erstatt statusen med det endelige svaret i Slack når loopen avsluttes
        logging.info(f"Oppdaterer Slack-melding {placeholder_ts} med endelig svar.")
        client.chat_update(channel=channel, ts=placeholder_ts, text=final_answer)
        logging.info("Forespørsel ferdig behandlet og fullført.")

    except Exception as e:
        error_msg = f"❌ Det oppstod en feil under undersøkelsen: {str(e)}"
        logging.error(f"Kritisk feil under kommunikasjon med Holmes: {str(e)}")
        client.chat_update(channel=channel, ts=placeholder_ts, text=error_msg)

# --- Slack Event-håndtering ---
@app.message("") 
def handle_all_messages(message, client, say):
    if "bot_id" in message or message.get("subtype") == "bot_message":
        return
    threading.Thread(target=process_message_async, args=(client, message, say)).start()

@app.event("app_mention")
def handle_app_mentions(event, client, say):
    threading.Thread(target=process_message_async, args=(client, event, say)).start()

if __name__ == "__main__":
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        logging.critical("Mangler SLACK_BOT_TOKEN eller SLACK_APP_TOKEN i miljøvariabler!")
        exit(1)
    
    logging.info("🚀 Starter Holmes Slack Bot (Mode: Enterprise Live Stream & Task Checklist)")
    logging.info(f"Kobler til Holmes på adresse: {HOLMES_URL}")
    
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
