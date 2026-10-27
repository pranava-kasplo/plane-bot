import os
import requests
from threading import Thread
from flask import Flask
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Initialize app using environment variables
app = App(token=os.environ.get("xoxb-your-slack-bot-token"))

@app.event("reaction_added")
def handle_reaction_added(event, say):
    reaction = event.get("reaction")
    item = event.get("item")
    
    if reaction == "ticket":
        result = app.client.conversations_history(
            channel=item["channel"],
            latest=item["ts"],
            inclusive=True,
            limit=1
        )
        message = result["messages"][0]["text"]
        
        plane_api_key = os.environ.get("plane_api_your_key_here")
        workspace_slug = "kasplo-prod" # Update this
        project_id = "68b19e1d-0f8f-4b77-9dba-68697b7ecae1"         # Update this
        
        url = f"https://tickets.kasplo.com/api/v1/workspaces/{workspace_slug}/projects/{project_id}/issues/"
        headers = {"Content-Type": "application/json", "x-api-key": plane_api_key}
        payload = {"name": "Customer Complaint from Slack", "description_html": f"<p>{message}</p>"}
        
        requests.post(url, headers=headers, json=payload)

# Dummy Flask server to pass Render's port health check
flask_app = Flask(__name__)
@flask_app.route("/")
def health_check():
    return "Bot is running!"

def run_flask():
    # Render assigns a port dynamically via the PORT environment variable
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    SocketModeHandler(app, os.environ.get("xapp-your-slack-app-token")).start()