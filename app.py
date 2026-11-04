import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from threading import Thread
from flask import Flask, Response, request
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Ensure SSL certificates are properly located across Linux/Render environments
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    CA_BUNDLE = certifi.where()
except ImportError:
    CA_BUNDLE = True

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

# Timezone helpers (IST is UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

def get_current_timestamp():
    now_utc = datetime.now(UTC)
    now_ist = now_utc.astimezone(IST)
    return f"{now_ist.strftime('%Y-%m-%d %H:%M:%S IST')} ({now_utc.strftime('%H:%M:%S UTC')})"

# Track startup time & ping history in memory
CONTAINER_START_TIME = get_current_timestamp()
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
            payload = {"name": "Production Complaint from Slack", "description_html": f"<p>{message}</p>"}
            
            requests.post(url, headers=headers, json=payload, verify=CA_BUNDLE, timeout=10)
        except Exception as e:
            print(f"Error handling reaction_added event: {e}")

# Lightweight Flask server to pass Render's port health check
@flask_app.route("/")
def health_check():
    now_str = get_current_timestamp()
    ping_history.append(now_str)
    # Prevent memory bloat by keeping only recent 100 logs
    if len(ping_history) > 100:
        ping_history.pop(0)
    return "Bot is running!"

@flask_app.route("/dump")
def dump_logs():
    current_time = get_current_timestamp()
    
    # Try creating an optional shareable dpaste link without crashing if it fails
    share_link = None
    if ping_history:
        try:
            response = requests.post(
                "https://dpaste.com/api/v2/",
                data={
                    "content": "\n".join([f"Ping at {p}" for p in ping_history]),
                    "expiry_days": 7
                },
                verify=CA_BUNDLE,
                timeout=5
            )
            if response.ok:
                share_link = response.text.strip()
        except Exception:
            pass  # Fail gracefully to keep dump page functioning

    lines = [
        "==================================================",
        "          PLANE-BOT PING & UPTIME LOGS            ",
        "==================================================",
        f"Server Time:       {current_time}",
        f"Container Started: {CONTAINER_START_TIME}",
        f"Total Pings:       {len(ping_history)} / 100 recorded",
    ]
    
    if share_link:
        lines.append(f"Shareable URL:     {share_link}")
    
    lines.append("==================================================\n")

    if not ping_history:
        lines.append("No ping history recorded yet in this container session.")
        lines.append("")
        lines.append("Notes:")
        lines.append("- Render free tier spins down containers during idle hours.")
        lines.append("- When cron-job.org pings '/' (every 13 mins), timestamps will record here.")
    else:
        lines.append("Recorded Pings (newest to oldest):")
        for idx, entry in enumerate(reversed(ping_history), 1):
            lines.append(f"[{idx:02d}] Ping successful at: {entry}")

    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")

def run_flask():
    # Render assigns a port dynamically via the PORT environment variable
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
