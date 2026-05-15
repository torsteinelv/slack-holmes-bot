import os
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Hent miljøvariabler
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
HOLMES_URL = os.environ.get("HOLMES_URL", "http://robusta-holmes.robusta.svc.cluster.local:80")
MODEL = os.environ.get("MODEL", "model1")

# Initialiser Bolt-appen
app = App(token=SLACK_BOT_TOKEN)

@app.event("app_mention")
def handle_mention(event, say):
    user_query = event.get("text", "")
    # Fjern @-mention fra teksten (f.eks. <@U12345> hva skjer? -> hva skjer?)
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
        
        answer = data.get("analysis", "Beklager, jeg fikk et tomt svar fra Holmes.")
        say(text=answer, thread_ts=event.get("ts"))

    except Exception as e:
        print(f"Feil ved kontakt med Holmes: {e}")
        say(text=f"Det oppstod en feil: {str(e)}", thread_ts=event.get("ts"))

if __name__ == "__main__":
    if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
        print("FEIL: Mangler Slack-tokens!")
        exit(1)
    
    print("Starter Holmes Slack Bot med SocketModeHandler...")
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    handler.start()
