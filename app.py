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

app = App(token=SLACK_BOT_TOKEN)

def get_thread_context(client, channel, thread_ts):
    """Henter trådhistorikk for kontekst."""
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts)
        messages = result.get("messages", [])
        context_lines = []
        for msg in messages:
            role = "Holmes" if ("bot_id" in msg or msg.get("subtype") == "bot_message") else "Bruker"
            text = msg.get("text", "").strip()
            if text:
                context_lines.append(f"{role}: {text}")
        return "\n".join(context_lines)
    except Exception as e:
        logging.error(f"Kunne ikke hente trådhistorikk: {e}")
        return ""

def update_slack_status(client, channel, ts, tasks, action):
    """Bygger og oppdaterer fremdriftsstatusen i Slack-meldingen."""
    status_text = "🔍 *Holmes feilsøker saken...*\n"
    
    # Tegner tabellen øverst hvis den er funnet
    if tasks:
        status_text += f"\n```\n{tasks}\n```\n"
        
    # Tegner handlingen nederst
    if action:
        status_text += f"\n{action}"
        
    try:
        client.chat_update(channel=channel, ts=ts, text=status_text)
    except Exception as e:
        logging.error(f"Feil ved oppdatering av Slack-status: {e}")

def extract_table(text):
    """Hjelpefunksjon som trekker ut ASCII-tabellen fra en tekststreng."""
    # Sørger for at vi takler escaped linjeskift (\n) som ofte kommer fra JSON-verdier
    if '\\n' in text and '\n' not in text:
        lines = text.split('\\n')
    else:
        lines = text.split('\n')
        
    table_lines = []
    in_table = False
    
    for line in lines:
        stripped = line.strip()
        # En typisk Markdown/ASCII tabell starter og slutter med +-- eller har | rammer
        if stripped.startswith('+--') or stripped.startswith('|'):
            in_table = True
            table_lines.append(line.rstrip())
        elif in_table and stripped:
            # Vi traff tekst som ikke hører til tabellen, så vi avbryter loopen
            break
            
    # Returner kun hvis vi fant en faktisk tabell (minimum header + linje + data)
    if len(table_lines) >= 3:
        return "\n".join(table_lines)
    return None

def find_table_in_json(obj):
    """Søker rekursivt igjennom et JSON-objekt for å finne ASCII-tabellen, uavhengig av formatering."""
    if isinstance(obj, str):
        # 1. Prøv å se om strengen inneholder en tabell direkte
        table = extract_table(obj)
        if table:
            return table
            
        # 2. Holmes sender ofte tool-argumenter (som TodoWrite) som en indre JSON-streng. Prøv å pakke ut!
        try:
            inner_obj = json.loads(obj)
            res = find_table_in_json(inner_obj)
            if res: return res
        except:
            pass
            
    elif isinstance(obj, dict):
        for v in obj.values():
            res = find_table_in_json(v)
            if res: return res
            
    elif isinstance(obj, list):
        for item in obj:
            res = find_table_in_json(item)
            if res: return res
            
    return None

def process_message_async(client, message, say):
    """Kjører asynkront i bakgrunnen."""
    channel = message.get("channel")
    ts = message.get("ts")
    thread_ts = message.get("thread_ts", ts)
    
    user_query = message.get("text", "").strip()
    if message.get("type") == "app_mention" or "app_mention" in message.get("subtype", ""):
        user_query = user_query.split(">")[-1].strip()

    if not user_query:
        return

    # 1. Send initial statusmelding til Slack
    try:
        placeholder = say(text="🔍 _Holmes oppretter en feilsøkingsplan..._", thread_ts=thread_ts)
        placeholder_ts = placeholder.get("ts")
    except Exception as e:
        logging.error(f"Feil ved sending av initial status til Slack: {e}")
        return

    if message.get("thread_ts"):
        history = get_thread_context(client, channel, thread_ts)
        prompt = (f"Du er en AI-assistent i en pågående Slack-samtale. Her er historikken:\n"
                  f"{history}\n\nSvar på det siste spørsmålet/oppfølgingen basert på denne konteksten.")
    else:
        prompt = user_query

    current_tasks = ""
    current_action = "Oppretter kobling til klyngen..."
    final_answer = "Fikk ingen endelig analyse fra Holmes."

    try:
        response = requests.post(
            f"{HOLMES_URL}/api/chat",
            json={"ask": prompt, "model": MODEL, "stream": True},
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=600
        )
        response.raise_for_status()

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
                    
                    # --- DYNAMISK TABELL-SKANNING PÅ HVERT ENESTE EVENT ---
                    found_table = find_table_in_json(data)
                    if found_table:
                        if current_tasks != found_table.strip():
                            current_tasks = found_table.strip()
                            logging.info(f"Tabell oppdatert! (Lengde: {len(current_tasks)})")

                    # --- OPPDATERING AV HANDLING PÅGÅR ---
                    if current_event == "start_tool_calling":
                        tool_name = data.get("tool_name", "")
                        description = data.get("description", "Undersøker...")
                        
                        if tool_name == "TodoWrite":
                            current_action = "📝 _Oppdaterer planen over oppgaver..._"
                        else:
                            current_action = f"🔨 _Kjører verktøy:_ `{tool_name}`\n📋 {description}"
                    
                    elif current_event == "ai_message":
                        thought = data.get("message", "")
                        if thought:
                            current_action = f"💭 _Tenker:_ {thought}"
                    
                    elif current_event == "ai_answer_end":
                        final_answer = data.get("analysis", final_answer)
                    
                    # PUSH OPPDATERINGEN TIL SLACK UMiddelbart
                    update_slack_status(client, channel, placeholder_ts, current_tasks, current_action)
                        
                except Exception as parse_err:
                    logging.error(f"Tolkningsfeil: {parse_err}")

        # Erstatt med endelig svar når strømmen er ferdig
        client.chat_update(channel=channel, ts=placeholder_ts, text=final_answer)

    except Exception as e:
        error_msg = f"❌ Det oppstod en feil under undersøkelsen: {str(e)}"
        client.chat_update(channel=channel, ts=placeholder_ts, text=error_msg)

@app.message("") 
def handle_all_messages(message, client, say):
    if "bot_id" in message or message.get("subtype") == "bot_message":
        return
    threading.Thread(target=process_message_async, args=(client, message, say)).start()

@app.event("app_mention")
def handle_app_mentions(event, client, say):
    threading.Thread(target=process_message_async, args=(client, event, say)).start()

if __name__ == "__main__":
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
