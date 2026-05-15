import os
import requests
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeHandler

# Hent miljøvariabler
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
HOLMES_URL = os.environ.get("HOLMES_URL", "http://robusta-holmes.robusta.svc.cluster.local:80")
MODEL = os.environ.get("MODEL", "model1")

client = WebClient(token=SLACK_BOT_TOKEN)

def handle_message(event, say):
    user_query = event.get("text", "")
    # Fjern @-mention fra teksten
    clean_query = user_query.split(">")[-1].strip()
    
    if not clean_query:
        return

    print(f"Mottok spørsmål: {clean_query}")

    try:
        # Send forespørsel til Holmes GPT API
        response = requests.post(
            f"{HOLMES_URL}/api/chat",
            json={
                "ask": clean_query,
                "model": MODEL
            },
            timeout=120
        )
        response.raise_for_status()
        data = response.json()
        
        # Hent svaret (analysis) og post det tilbake i Slack-tråden
        answer = data.get("analysis", "Beklager, jeg fikk et tomt svar fra Holmes.")
        say(text=answer, thread_ts=event.get("ts"))

    except Exception as e:
        print(f"Feil ved kontakt med Holmes: {e}")
        say(text=f"Det oppstod en feil: {str(e)}", thread_ts=event.get("ts"))

@SocketModeHandler.handle("app_mention")
def mention_handler(body, say):
    handle_message(body["event"], say)

if __name__ == "__main__":
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        print("FEIL: Mangler Slack-tokens!")
        exit(1)
    
    print("Starter Holmes Slack Bot...")
    handler = SocketModeHandler(client, SLACK_APP_TOKEN)
    handler.start()
