import os
import requests
from datetime import datetime
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Load environment variables from .env file
load_dotenv()

# Environment Variables & Secrets
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
PLANE_API_KEY = os.environ.get("PLANE_API_KEY")
WORKSPACE_SLUG = os.environ.get("PLANE_WORKSPACE_SLUG", "kasplo-prod")
PROJECT_ID = os.environ.get("PLANE_PROJECT_ID")

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    print("Warning: SLACK_BOT_TOKEN or SLACK_APP_TOKEN is missing from environment!")

if not PLANE_API_KEY or not PROJECT_ID:
    print("Warning: PLANE_API_KEY or PLANE_PROJECT_ID is missing from environment!")

ping_history = []

flask_app = Flask(__name__)
app = App(token=SLACK_BOT_TOKEN)

@app.event("reaction_added")
def handle_reaction_added(event, say):
    reaction = event.get("reaction")
    item = event.get("item")
    
    if reaction == "ticket" and item:
        try:
            result = app.client.conversations_history(
                channel=item["channel"],
                latest=item["ts"],
                inclusive=True,
                limit=1
            )
            messages = result.get("messages", [])
            if not messages:
                return
            message = messages[0].get("text", "")
            
            url = f"https://tickets.kasplo.com/api/v1/workspaces/{WORKSPACE_SLUG}/projects/{PROJECT_ID}/issues/"
            headers = {"Content-Type": "application/json", "x-api-key": PLANE_API_KEY}
            payload = {"name": "Customer Complaint from Slack", "description_html": f"<p>{message}</p>"}
            
            requests.post(url, headers=headers, json=payload, timeout=10)
        except Exception as e:
            print(f"Error handling reaction_added event: {e}")

# Lightweight Flask server to pass Render's port health check
@flask_app.route("/")
def health_check():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ping_history.append(f"Ping successful at: {now_str}")
    # Prevent memory bloat by keeping only recent logs
    if len(ping_history) > 100:
        ping_history.pop(0)
    return "Bot is running!"

@flask_app.route("/dump")
def dump_logs():
    if not ping_history:
        return "No ping history recorded yet."
    
    log_text = "\n".join(ping_history)
    
    try:
        # Push the text to dpaste (no API key required)
        response = requests.post(
            "https://dpaste.com/api/v2/",
            data={
                "content": log_text,
                "expiry_days": 7
            },
            timeout=10
        )
        if response.ok:
            return f"Logs dumped to: {response.text.strip()}"
        return f"Failed to dump logs to dpaste: HTTP {response.status_code}", 502
    except Exception as e:
        return f"Error dumping logs: {e}", 500

def run_flask():
    # Render assigns a port dynamically via the PORT environment variable
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
