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

app = App(token=SLACK_BOT_TOKEN)

def get_thread_context(client, channel, thread_ts):
    """Henter trådhistorikk for kontekst."""
    try:
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
        print(f"Kunne ikke hente trådhistorikk: {e}")
        return ""

def process_message_async(client, message, say):
    """Lytter på Holmes SSE-strøm og oppdaterer Slack-meldingen i sanntid."""
    channel = message.get("channel")
    ts = message.get("ts")
    thread_ts = message.get("thread_ts", ts)
    
    user_query = message.get("text", "").strip()
    if message.get("type") == "app_mention" or "app_mention" in message.get("subtype", ""):
        user_query = user_query.split(">")[-1].strip()

    if not user_query:
        return

    # 1. Send den første statusmeldingen som vi skal gjenbruke og oppdatere
    try:
        placeholder = say(text="🔍 _Holmes kobler til clusteret og starter undersøkelsen..._", thread_ts=thread_ts)
        placeholder_ts = placeholder.get("ts")
    except Exception as e:
        print(f"Feil ved sending av initial status: {e}")
        return

    # 2. Bygg kontekst/historikk hvis det er en tråd
    if message.get("thread_ts"):
        history = get_thread_context(client, channel, thread_ts)
        prompt = (
            f"Du er en AI-assistent i en pågående Slack-samtale. Her er historikken så langt:\n"
            f"{history}\n\n"
            f"Svar på det siste spørsmålet/oppfølgingen basert på denne konteksten."
        )
    else:
        prompt = user_query

    # 3. Start strømming fra Holmes ved å bruke stream=True og rett Header
    try:
        print(f"Starter live-analyse for: {prompt}")
        response = requests.post(
            f"{HOLMES_URL}/api/chat",
            json={
                "ask": prompt,
                "model": MODEL
            },
            headers={"Accept": "text/event-stream"},  # Ber Holmes om å strømme events (SSE)
            stream=True,                             # Viktig for å lese bit for bit
            timeout=600
        )
        response.raise_for_status()

        current_event = None
        final_answer = "Fikk ingen endelig analyse fra Holmes."

        # Les strømmen linje for linje etter hvert som Holmes jobber
        for line in response.iter_lines():
            if not line:
                continue
            
            decoded_line = line.decode('utf-8').strip()
            
            # SSE-formater kommer som "event: navn" etterfulgt av "data: {JSON}"
            if decoded_line.startswith("event:"):
                current_event = decoded_line.split(":", 1)[1].strip()
            elif decoded_line.startswith("data:"):
                data_str = decoded_line.split(":", 1)[1].strip()
                try:
                    data = json.loads(data_str)
                    
                    # Hva gjør Holmes akkurat nå? (F.eks. kjører en kubectl-kommando)
                    if current_event == "start_tool_calling":
                        tool_name = data.get("tool_name", "et verktøy")
                        description = data.get("description", "Undersøker...")
                        
                        status_text = f"🔍 *Holmes feilsøker saken...*\n🔨 _Kjører:_ `{tool_name}`\n📋 {description}"
                        client.chat_update(channel=channel, ts=placeholder_ts, text=status_text)
                    
                    # Har Holmes noen foreløpige tanker underveis?
                    elif current_event == "ai_message":
                        thought = data.get("message", "")
                        if thought:
                            status_text = f"🔍 *Holmes analyserer...*\n💭 _Tenker:_ {thought}"
                            client.chat_update(channel=channel, ts=placeholder_ts, text=status_text)
                    
                    # Er Holmes helt ferdig? Hent det endelige svaret.
                    elif current_event == "ai_answer_end":
                        final_answer = data.get("analysis", final_answer)
                        
                except Exception as parse_err:
                    print(f"Kunne ikke tolke live-data: {parse_err}")

        # 4. Når loopen er ferdig, overskriver vi statusmeldingen med det endelige svaret!
        client.chat_update(channel=channel, ts=placeholder_ts, text=final_answer)

    except Exception as e:
        error_msg = f"❌ Det oppstod en feil under undersøkelsen: {str(e)}"
        client.chat_update(channel=channel, ts=placeholder_ts, text=error_msg)

# --- Slack Event Handlere ---
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
        print("CRITICAL ERROR: Mangler tokens")
        exit(1)
    
    print("🚀 Starter Holmes Slack Bot med sanntids statusoppdatering...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
