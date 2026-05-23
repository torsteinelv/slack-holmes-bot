import os
import json
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

def update_slack_status(client, channel, ts, tasks, action):
    """Bygger og oppdaterer fremdriftsstatusen i Slack-meldingen underveis."""
    status_text = "🔍 *Holmes feilsøker saken...*\n"
    
    # Hvis vi har mottatt en oppgaveliste fra TodoWrite, vis den i en kodeblokk
    if tasks:
        status_text += f"\n📊 *Fremdrift på undersøkelsen:*\n```\n{tasks}\n```\n"
    
    # Vis hva agenten gjør eller tenker akkurat nå i bunnen
    if action:
        status_text += f"\n{action}"
        
    try:
        client.chat_update(channel=channel, ts=ts, text=status_text)
    except Exception as e:
        print(f"Feil ved oppdatering av Slack-status: {e}")

def process_message_async(client, message, say):
    """Kjører asynkront i bakgrunnen. Håndterer strømming, oppgavelister og sluttresultat."""
    channel = message.get("channel")
    ts = message.get("ts")
    thread_ts = message.get("thread_ts", ts)
    
    # Vasker teksten hvis det er en app_mention
    user_query = message.get("text", "").strip()
    if message.get("type") == "app_mention" or "app_mention" in message.get("subtype", ""):
        user_query = user_query.split(">")[-1].strip()

    if not user_query:
        return

    # 1. Send den første statusmeldingen som vi skal oppdatere underveis
    try:
        placeholder = say(text="🔍 _Holmes oppretter en feilsøkingsplan... Vær vennlig å vente._", thread_ts=thread_ts)
        placeholder_ts = placeholder.get("ts")
    except Exception as e:
        print(f"Feil ved sending av initial status: {e}")
        return

    # 2. Hent trådkontekst hvis brukeren svarer i en eksisterende tråd
    if message.get("thread_ts"):
        print(f"Bygger trådkontekst for tråd: {thread_ts}")
        history = get_thread_context(client, channel, thread_ts)
        prompt = (
            f"Du er en AI-assistent i en pågående Slack-samtale. Her er historikken så langt:\n"
            f"{history}\n\n"
            f"Svar på det siste spørsmålet/oppfølgingen basert på denne konteksten."
        )
    else:
        prompt = user_query

    # Tilstandsvariabler for å huske statusen mellom live-events
    current_tasks = ""
    current_action = ""
    final_answer = "Fikk ingen endelig analyse fra Holmes."

    # 3. Send forespørsel til Holmes med stream=True både i JSON-bodyen og i HTTP-kallet
    try:
        print(f"Starter live-analyse for: {prompt}")
        response = requests.post(
            f"{HOLMES_URL}/api/chat",
            json={
                "ask": prompt,
                "model": MODEL,
                "stream": True  # VIKTIG: Forteller Holmes-API-et at det skal strømme SSE-events
            },
            headers={"Accept": "text/event-stream"},
            stream=True,  # Viktig for at requests skal lese bit for bit i sanntid
            timeout=600
        )
        response.raise_for_status()

        current_event = None

        # Les strømmen linje for linje fra Holmes-serveren etter hvert som den jobber
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
                    
                    # EVENT: Holmes starter et nytt verktøy (f.eks. en kubectl-kommando)
                    if current_event == "start_tool_calling":
                        tool_name = data.get("tool_name", "")
                        description = data.get("description", "Undersøker...")
                        
                        if tool_name == "TodoWrite":
                            current_action = "📝 _Oppdaterer planen over oppgaver..._"
                        else:
                            current_action = f"🔨 _Kjører verktøy:_ `{tool_name}`\n📋 {description}"
                        
                        update_slack_status(client, channel, placeholder_ts, current_tasks, current_action)
                    
                    # EVENT: Et verktøy ble ferdig. Her fanger vi opp oppgaveliste-tabellen fra TodoWrite
                    elif current_event == "tool_calling_result":
                        tool_name = data.get("name") or data.get("tool_name")
                        result_obj = data.get("result", {})
                        
                        if tool_name == "TodoWrite" and isinstance(result_obj, dict):
                            tool_data = result_obj.get("data")
                            # Sjekker at det faktisk er ASCII-tabellen vi har fått tak i
                            if isinstance(tool_data, str) and "+--" in tool_data:
                                current_tasks = tool_data.strip()
                                update_slack_status(client, channel, placeholder_ts, current_tasks, current_action)
                    
                    # EVENT: Holmes formulerer en tanke eller observasjon underveis
                    elif current_event == "ai_message":
                        thought = data.get("message", "")
                        if thought:
                            current_action = f"💭 _Tenker:_ {thought}"
                            update_slack_status(client, channel, placeholder_ts, current_tasks, current_action)
                    
                    # EVENT: Analysen er helt ferdig og fullført
                    elif current_event == "ai_answer_end":
                        final_answer = data.get("analysis", final_answer)
                        
                except Exception as parse_err:
                    print(f"Kunne ikke tolke live-data: {parse_err}")

        # 4. Når loopen er ferdig, erstatter vi statusen/tabellen med det endelige svaret.
        # (Hvis du ønsker å beholde tabellen i toppen av det endelige svaret, kan du endre text til f"```\n{current_tasks}\n```\n\n{final_answer}")
        client.chat_update(channel=channel, ts=placeholder_ts, text=final_answer)

    except Exception as e:
        error_msg = f"❌ Det oppstod en feil under undersøkelsen: {str(e)}"
        client.chat_update(channel=channel, ts=placeholder_ts, text=error_msg)

# --- Slack Event-håndtering ---
@app.message("") 
def handle_all_messages(message, client, say):
    if "bot_id" in message or message.get("subtype") == "bot_message":
        return
    # Frikoble Slack umiddelbart ved å dytte jobben over i en egen Python-tråd
    threading.Thread(target=process_message_async, args=(client, message, say)).start()

@app.event("app_mention")
def handle_app_mentions(event, client, say):
    # Gjenbruker samme asynkrone logikk for @mentions
    threading.Thread(target=process_message_async, args=(client, event, say)).start()

if __name__ == "__main__":
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        print("CRITICAL ERROR: Mangler SLACK_BOT_TOKEN eller SLACK_APP_TOKEN")
        exit(1)
    
    print("🚀 Starter Holmes Slack Bot (Mode: Enterprise Live Stream & Task Checklist)")
    print(f"Connecting to Holmes at: {HOLMES_URL}")
    
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
