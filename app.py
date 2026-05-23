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

def update_slack_status(client, channel, ts, tasks_table, action):
    """Bygger og oppdaterer fremdriftsstatusen i Slack-meldingen."""
    status_text = "🔍 *Holmes feilsøker saken...*\n"
    
    # Tegner tabellen øverst hvis den er funnet og generert
    if tasks_table:
        status_text += f"\n```\n{tasks_table}\n```\n"
        
    # Tegner handlingen nederst
    if action:
        status_text += f"\n{action}"
        
    try:
        client.chat_update(channel=channel, ts=ts, text=status_text)
    except Exception as e:
        logging.error(f"Feil ved oppdatering av Slack-status: {e}")

def build_markdown_table(tasks_list):
    """Tar en JSON-liste med oppgaver og bygger en pen ASCII-tabell for Slack."""
    if not tasks_list or not isinstance(tasks_list, list):
        return None
        
    table = "+----+------------------------------------------------------+-----------------+\n"
    table += "| ID | Content                                              | Status          |\n"
    table += "+----+------------------------------------------------------+-----------------+\n"
    
    for i, task in enumerate(tasks_list):
        # Henter ut feltene uavhengig av nøyaktig hva Holmes kaller dem
        task_id = str(task.get('id', task.get('ID', i+1)))
        content = str(task.get('content', task.get('description', task.get('task', ''))))
        status = str(task.get('status', task.get('state', 'pending'))).lower()
        
        # Formaterer statusen slik vi kjenner den fra loggene
        if status in ['completed', 'done', 'true', 'success']:
            status_fmt = "[✓] completed"
        elif status in ['in_progress', 'progressing', 'running', 'active']:
            status_fmt = "[~] in_progress"
        else:
            status_fmt = "[ ] pending"
            
        # Tilpasser bredden slik at tabellen aldri brekker i Slack
        if len(content) > 52:
            content = content[:49] + "..."
            
        table += f"| {task_id:<2} | {content:<52} | {status_fmt:<15} |\n"
        
    table += "+----+------------------------------------------------------+-----------------+"
    return table

def extract_tasks_from_json(obj):
    """Leter rekursivt etter selve listen med oppgaver i Holmes' JSON-struktur."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            # Sjekker om verdien er en liste med oppgave-dictionaries
            if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                if any(k in val[0] for k in ['status', 'state']) and any(k in val[0] for k in ['content', 'description', 'task']):
                    return val
        for val in obj.values():
            res = extract_tasks_from_json(val)
            if res: return res
            
    elif isinstance(obj, list):
        for item in obj:
            res = extract_tasks_from_json(item)
            if res: return res
            
    elif isinstance(obj, str):
        # LLM-er sender ofte JSON-argumenter som en innkapslet (escaped) tekststreng
        try:
            parsed = json.loads(obj)
            return extract_tasks_from_json(parsed)
        except:
            pass
            
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
        logging.error(f"Feil ved sending av initial status: {e}")
        return

    # 2. Hent trådkontekst hvis relevant
    if message.get("thread_ts"):
        history = get_thread_context(client, channel, thread_ts)
        prompt = (f"Du er en AI-assistent i en pågående Slack-samtale. Her er historikken:\n"
                  f"{history}\n\nSvar på det siste spørsmålet/oppfølgingen basert på denne konteksten.")
    else:
        prompt = user_query

    current_tasks_table = ""
    current_action = "Oppretter kobling til klyngen..."
    final_answer = "Fikk ingen endelig analyse fra Holmes."

    # 3. Stream fra Holmes-API-et med smart timeout
    try:
        logging.info(f"Starter live-analyse for query: '{user_query[:50]}...'")
        response = requests.post(
            f"{HOLMES_URL}/api/chat",
            json={"ask": prompt, "model": MODEL, "stream": True},
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=(15, 120)  # 15 sek for å koble til, 120 sek max stillhet mellom events
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
                    
                    # --- DYNAMISK TABELL-BYGGER ---
                    # Henter ut den rå JSON-listen og tegner tabellen manuelt
                    raw_tasks = extract_tasks_from_json(data)
                    if raw_tasks:
                        new_table = build_markdown_table(raw_tasks)
                        if new_table and new_table != current_tasks_table:
                            current_tasks_table = new_table
                            logging.info("Tegnet ny oppdatert tabell fra JSON-data!")

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
                    
                    # Pusher skjermbildet til Slack for hvert lille steg!
                    update_slack_status(client, channel, placeholder_ts, current_tasks_table, current_action)
                        
                except Exception as parse_err:
                    logging.error(f"Tolkningsfeil: {parse_err}")

        # Erstatt med endelig svar når strømmen er ferdig
        client.chat_update(channel=channel, ts=placeholder_ts, text=final_answer)
        logging.info("Suksess: Feilsøking fullført og endelig svar sendt til Slack.")

    except requests.exceptions.Timeout:
        error_msg = "❌ Det oppstod et tidsavbrudd: Holmes har ikke sendt noen oppdateringer på 2 minutter. Sjekk `kubectl logs` for Holmes-serveren."
        logging.error("Tidsavbrudd (Timeout) under feilsøking.")
        client.chat_update(channel=channel, ts=placeholder_ts, text=error_msg)
    except Exception as e:
        error_msg = f"❌ Det oppstod en feil under undersøkelsen: {str(e)}"
        logging.error(f"Feil under prosessering av melding: {str(e)}")
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
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        logging.critical("Mangler SLACK_BOT_TOKEN eller SLACK_APP_TOKEN!")
        exit(1)
        
    logging.info("🚀 Starter Holmes Slack Bot (Mode: Enterprise Async m/ Live JSON Table Rendering)")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
