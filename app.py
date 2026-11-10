import os
import sys
import json
import html
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
from flask import Flask, Response
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

# Configure unbuffered real-time logging for Render console + in-memory /dump
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("plane-bot")

# Timezone helpers (IST is UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

def get_current_timestamp():
    now_utc = datetime.now(UTC)
    now_ist = now_utc.astimezone(IST)
    return f"{now_ist.strftime('%Y-%m-%d %H:%M:%S IST')} ({now_utc.strftime('%H:%M:%S UTC')})"

# Track startup time, ping history, and recent event logs in memory
CONTAINER_START_TIME = get_current_timestamp()
ping_history = []
event_history = []

def log_event(message: str, level: str = "INFO"):
    """Logs immediately to Render stdout (flushed) and stores in memory for /dump."""
    ts = get_current_timestamp()
    entry = f"[{ts}] {message}"
    if level == "ERROR":
        logger.error(message)
    elif level == "WARNING":
        logger.warning(message)
    else:
        logger.info(message)
    sys.stdout.flush()

    event_history.append(entry)
    if len(event_history) > 200:
        event_history.pop(0)

# Environment Variables & Secrets
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
PLANE_API_KEY = os.environ.get("PLANE_API_KEY")
PLANE_BASE_URL = os.environ.get("PLANE_BASE_URL", "https://tickets.kasplo.com").rstrip("/")
WORKSPACE_SLUG = os.environ.get("PLANE_WORKSPACE_SLUG", "kasplo-prod")
PROJECT_ID = os.environ.get("PLANE_PROJECT_ID", "68b19e1d-0f8f-4b77-9dba-68697b7ecae1")
PROJECT_IDENTIFIER = os.environ.get("PLANE_PROJECT_IDENTIFIER", "KASPL")

TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID", "C0CGD83RDNW")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "C0CGD83RDNW")

PRANAVA_USER_ID = os.environ.get("PRANAVA_USER_ID", "U0C5JAMGZEE")
SAHIL_USER_ID = os.environ.get("SAHIL_USER_ID", "U0BKVFQH7NH")
SUJITH_USER_ID = os.environ.get("SUJITH_USER_ID", "U03KTDPJC05")

AUTHORIZED_USERS = {u for u in (PRANAVA_USER_ID, SAHIL_USER_ID, SUJITH_USER_ID) if u}
APPROVE_REACTIONS = {"ticket", "admission_tickets"}
REJECT_REACTIONS = {"x", "negative_squared_cross_mark"}
RECONSIDER_WINDOW_SECONDS = 30 * 60  # 30 minutes

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    log_event("Warning: SLACK_BOT_TOKEN or SLACK_APP_TOKEN is missing from environment!", "WARNING")

if not PLANE_API_KEY or not PROJECT_ID:
    log_event("Warning: PLANE_API_KEY or PLANE_PROJECT_ID is missing from environment!", "WARNING")

state_lock = Lock()
user_name_cache = {}

flask_app = Flask(__name__)
app = App(token=SLACK_BOT_TOKEN)


# =====================================================================
# 1. SLACK LOG CHANNEL STATE MANAGEMENT ("Ctrl+F" Persistence)
# =====================================================================
def get_state_key(channel_id: str, message_ts: str) -> str:
    return f"{channel_id}:{message_ts}"


def find_state_in_log_channel(state_key: str):
    """
    Searches LOG_CHANNEL_ID for an existing state record for this message_ts.
    Returns (log_message_ts, state_dict) or (None, default_state_dict).
    """
    default_state = {
        "key": state_key,
        "status": "NEW",          # NEW | PENDING | REJECTED | CANCELLED | CREATED
        "sahil_pinged": False,
        "rejected_at": None,
        "rejected_by": None,
        "plane_id": None,
        "plane_identifier": None,
        "updated_at": get_current_timestamp(),
    }
    if not LOG_CHANNEL_ID:
        return None, default_state

    marker = f"[PLANE_STATE:{state_key}]"
    try:
        cursor = None
        for _ in range(3):  # Scan up to 600 recent messages in LOG_CHANNEL_ID
            resp = app.client.conversations_history(
                channel=LOG_CHANNEL_ID,
                limit=200,
                cursor=cursor
            )
            for msg in resp.get("messages", []):
                text = msg.get("text", "")
                if marker in text:
                    if "```" in text:
                        raw_json = text.split("```")[1].strip()
                        if raw_json.startswith("json"):
                            raw_json = raw_json[4:].strip()
                        parsed = json.loads(raw_json)
                        default_state.update(parsed)
                        return msg["ts"], default_state
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        log_event(f"Error reading state from log channel ({LOG_CHANNEL_ID}): {e}", "ERROR")

    return None, default_state


def save_state_to_log_channel(state_key: str, state: dict, existing_log_ts: str = None, event_note: str = ""):
    """Writes or updates the machine-readable state message in LOG_CHANNEL_ID."""
    state["updated_at"] = get_current_timestamp()
    log_event(f"State update for {state_key}: status={state.get('status')} | {event_note}")

    if not LOG_CHANNEL_ID:
        return None

    marker = f"[PLANE_STATE:{state_key}]"
    pretty_json = json.dumps(state, indent=2)
    text = (
        f"*{marker}* — Status: `{state.get('status')}`\n"
        f"Latest Event: {event_note}\n"
        f"```json\n{pretty_json}\n```"
    )
    try:
        if existing_log_ts:
            app.client.chat_update(
                channel=LOG_CHANNEL_ID,
                ts=existing_log_ts,
                text=text
            )
            return existing_log_ts
        else:
            res = app.client.chat_postMessage(
                channel=LOG_CHANNEL_ID,
                text=text
            )
            return res.get("ts")
    except Exception as e:
        log_event(f"Failed to write state to log channel: {e}", "ERROR")
        return existing_log_ts


# =====================================================================
# 2. SLACK HELPERS: USER NAMES, REACTIONS & THREAD HISTORY
# =====================================================================
def get_user_display_name(user_id: str) -> str:
    if not user_id:
        return "Unknown User"
    if user_id in user_name_cache:
        return user_name_cache[user_id]
    try:
        res = app.client.users_info(user=user_id)
        user_obj = res.get("user", {})
        profile = user_obj.get("profile", {})
        name = (
            profile.get("real_name")
            or profile.get("display_name")
            or user_obj.get("name")
            or user_id
        )
        user_name_cache[user_id] = name
        return name
    except Exception as e:
        log_event(f"Could not resolve user name for {user_id}: {e}", "WARNING")
        return f"<@{user_id}>"


def check_message_context(channel_id: str, message_ts: str):
    """
    Returns (is_reply, parent_thread_ts, is_bot_state_log).
    Checks both whether the message is a thread reply and whether it's a [PLANE_STATE:...] log message.
    """
    try:
        res = app.client.conversations_replies(
            channel=channel_id,
            ts=message_ts,
            limit=1
        )
        messages = res.get("messages", [])
        if messages:
            msg = messages[0]
            text = msg.get("text", "")
            is_bot_state_log = "[PLANE_STATE:" in text
            thread_ts = msg.get("thread_ts")
            if thread_ts and thread_ts != message_ts:
                return True, thread_ts, is_bot_state_log
            return False, message_ts, is_bot_state_log
    except Exception as e:
        log_event(f"Error checking message context for {message_ts}: {e}", "WARNING")
    return False, message_ts, False


def get_current_reactions_on_message(channel_id: str, message_ts: str):
    """
    Returns (approved_user_ids, rejected_user_ids) currently active on the parent message.
    """
    approved_users = set()
    rejected_users = set()
    try:
        res = app.client.reactions_get(
            channel=channel_id,
            timestamp=message_ts,
            full=True
        )
        msg = res.get("message", {})
        for r in msg.get("reactions", []):
            r_name = r.get("name")
            users = set(r.get("users", []))
            if r_name in APPROVE_REACTIONS:
                approved_users.update(users)
            elif r_name in REJECT_REACTIONS:
                rejected_users.update(users)
    except Exception as e:
        log_event(f"Error fetching reactions for {message_ts}: {e}", "ERROR")
    return approved_users, rejected_users


def fetch_full_thread_and_files(channel_id: str, parent_ts: str):
    """
    Fetches all human messages in the thread + collects all Slack files attached in the thread.
    Returns (title_text, description_html, files_list).
    """
    messages = []
    cursor = None
    while True:
        res = app.client.conversations_replies(
            channel=channel_id,
            ts=parent_ts,
            limit=200,
            cursor=cursor
        )
        messages.extend(res.get("messages", []))
        cursor = res.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    parent_text = messages[0].get("text", "Customer Complaint from Slack").strip() if messages else "Customer Complaint from Slack"
    first_line = parent_text.split("\n")[0][:100].strip()
    issue_title = first_line if first_line else "Customer Complaint from Slack"

    html_parts = [
        "<h3>Slack Thread Transcript</h3>",
        f"<p><strong>Channel:</strong> <code>{channel_id}</code> | <strong>Thread TS:</strong> <code>{parent_ts}</code></p>",
        "<hr/>"
    ]
    files_to_upload = []

    for msg in messages:
        # Skip messages sent by this bot (approval prompts, warnings, state logs, etc.)
        if msg.get("bot_id") or msg.get("subtype") == "bot_message" or "[PLANE_STATE:" in msg.get("text", ""):
            continue

        user_id = msg.get("user", "")
        author_name = html.escape(get_user_display_name(user_id))
        msg_ts = float(msg.get("ts", 0))
        msg_time = datetime.fromtimestamp(msg_ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S IST")
        raw_text = msg.get("text", "")
        escaped_text = html.escape(raw_text).replace("\n", "<br/>")

        html_parts.append(
            f"<p><strong>{author_name}</strong> <em>({msg_time})</em>:<br/>{escaped_text}</p>"
        )

        for f in msg.get("files", []):
            file_url = f.get("url_private_download") or f.get("url_private")
            file_name = f.get("name") or f.get("title") or "attachment"
            if file_url:
                files_to_upload.append({
                    "id": f.get("id"),
                    "name": file_name,
                    "mimetype": f.get("mimetype", "application/octet-stream"),
                    "size": f.get("size", 0),
                    "url": file_url,
                    "permalink": f.get("permalink", file_url),
                })

    return issue_title, "\n".join(html_parts), files_to_upload


# =====================================================================
# 3. PLANE API: WORK ITEM CREATION & ATTACHMENT UPLOAD
# =====================================================================
def upload_attachment_to_plane(issue_id: str, file_info: dict) -> bool:
    """
    Downloads a private Slack file using SLACK_BOT_TOKEN and uploads it to the Plane work item.
    Supports both Plane's presigned S3 upload flow and direct multipart upload.
    """
    file_url = file_info["url"]
    file_name = file_info["name"]
    mime_type = file_info["mimetype"]

    try:
        log_event(f"Downloading Slack attachment '{file_name}' ({file_info['size']} bytes)...")
        slack_resp = requests.get(
            file_url,
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            verify=CA_BUNDLE,
            timeout=30
        )
        if not slack_resp.ok:
            log_event(f"Failed to download Slack file '{file_name}': HTTP {slack_resp.status_code}", "WARNING")
            return False
        file_bytes = slack_resp.content
        file_size = len(file_bytes)
    except Exception as e:
        log_event(f"Exception downloading Slack file '{file_name}': {e}", "WARNING")
        return False

    plane_headers = {"x-api-key": PLANE_API_KEY}

    for endpoint_suffix in [
        f"work-items/{issue_id}/attachments/",
        f"issues/{issue_id}/issue-attachments/",
    ]:
        cred_url = f"{PLANE_BASE_URL}/api/v1/workspaces/{WORKSPACE_SLUG}/projects/{PROJECT_ID}/{endpoint_suffix}"
        try:
            # Attempt 1: Presigned credentials JSON request
            cred_resp = requests.post(
                cred_url,
                headers={**plane_headers, "Content-Type": "application/json"},
                json={"name": file_name, "size": file_size, "type": mime_type},
                verify=CA_BUNDLE,
                timeout=15
            )
            if cred_resp.ok:
                cred_data = cred_resp.json()
                upload_data = cred_data.get("upload_data", {})
                asset_id = cred_data.get("asset_id") or cred_data.get("id")
                if upload_data and upload_data.get("url"):
                    s3_url = upload_data["url"]
                    s3_fields = upload_data.get("fields", {})
                    s3_resp = requests.post(
                        s3_url,
                        data=s3_fields,
                        files={"file": (file_name, file_bytes, mime_type)},
                        verify=CA_BUNDLE,
                        timeout=30
                    )
                    if s3_resp.status_code in (200, 201, 204) and asset_id:
                        patch_url = f"{cred_url}{asset_id}/"
                        requests.patch(
                            patch_url,
                            headers={**plane_headers, "Content-Type": "application/json"},
                            json={"is_uploaded": True},
                            verify=CA_BUNDLE,
                            timeout=10
                        )
                        log_event(f"Uploaded '{file_name}' to Plane via presigned URL (asset_id={asset_id}).")
                        return True

            # Attempt 2: Direct multipart/form-data upload
            multi_resp = requests.post(
                cred_url,
                headers=plane_headers,
                data={"asset_name": file_name},
                files={"asset": (file_name, file_bytes, mime_type)},
                verify=CA_BUNDLE,
                timeout=30
            )
            if multi_resp.ok:
                log_event(f"Uploaded '{file_name}' to Plane via direct multipart POST.")
                return True
        except Exception as e:
            log_event(f"Attachment upload attempt on {endpoint_suffix} failed: {e}", "WARNING")

    return False


def create_plane_work_item(channel_id: str, parent_ts: str, approved_by_names: str):
    """
    Pulls the full thread, creates the work item in Plane, uploads any attachments,
    and returns (plane_issue_uuid, human_readable_identifier).
    """
    title, description_html, files = fetch_full_thread_and_files(channel_id, parent_ts)

    if files:
        description_html += "<hr/><h4>Attachments</h4><ul>"
        for f in files:
            safe_name = html.escape(f["name"])
            safe_link = html.escape(f["permalink"])
            description_html += f'<li><a href="{safe_link}">{safe_name}</a></li>'
        description_html += "</ul>"

    description_html += f"<hr/><p><em>Approved in Slack by: {html.escape(approved_by_names)}</em></p>"

    url = f"{PLANE_BASE_URL}/api/v1/workspaces/{WORKSPACE_SLUG}/projects/{PROJECT_ID}/issues/"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": PLANE_API_KEY
    }
    payload = {
        "name": title,
        "description_html": description_html
    }

    log_event(f"Sending POST to Plane API: {url} (title='{title}')")
    resp = requests.post(url, headers=headers, json=payload, verify=CA_BUNDLE, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    issue_uuid = data.get("id")
    seq_id = data.get("sequence_id")
    identifier = f"{PROJECT_IDENTIFIER}-{seq_id}" if seq_id else (issue_uuid or "CREATED")

    log_event(f"Plane work item created successfully: {identifier} (UUID={issue_uuid})")

    for f in files:
        upload_attachment_to_plane(issue_uuid, f)

    return issue_uuid, identifier


# =====================================================================
# 4. CORE APPROVAL & REJECTION STATE MACHINE
# =====================================================================
@app.event("reaction_added")
def handle_reaction_added(event, say):
    reaction = event.get("reaction")
    user_id = event.get("user")
    item = event.get("item", {})
    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if channel_id != TARGET_CHANNEL_ID:
        return

    if reaction not in APPROVE_REACTIONS and reaction not in REJECT_REACTIONS:
        return

    user_name = get_user_display_name(user_id)
    log_event(f"Reaction ':{reaction}:' added by {user_name} ({user_id}) on msg {message_ts}")

    # 1. Ignore unauthorized users
    if user_id not in AUTHORIZED_USERS:
        log_event(f"Ignored reaction ':{reaction}:' from unauthorized user {user_name} ({user_id}).")
        return

    # 2. Check if the message is a thread reply or a [PLANE_STATE:...] log message
    is_reply, parent_ts, is_bot_state_log = check_message_context(channel_id, message_ts)
    if is_bot_state_log:
        log_event(f"Ignored reaction on bot's own state log message ({message_ts}).")
        return

    if is_reply:
        log_event(f"User {user_name} reacted to a thread reply ({message_ts}) instead of parent ({parent_ts}).")
        app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=parent_ts,
            text=f"⚠️ <@{user_id}> please react to the *original message* at the top of this thread, not to a reply."
        )
        return

    with state_lock:
        state_key = get_state_key(channel_id, message_ts)
        log_ts, state = find_state_in_log_channel(state_key)

        # 3. If work item was already created in Plane, never duplicate it
        if state.get("status") == "CREATED":
            log_event(f"Ignored reaction on {message_ts}: Work item already created ({state.get('plane_identifier')}).")
            return

        # 4. Check if a previous rejection is older than 30 minutes
        if state.get("status") == "REJECTED" and state.get("rejected_at"):
            elapsed = time.time() - float(state["rejected_at"])
            if elapsed > RECONSIDER_WINDOW_SECONDS:
                log_event(f"Reaction on {message_ts} ignored: 30-minute reconsideration window expired ({int(elapsed)}s elapsed).")
                app.client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=message_ts,
                    text=f"⏳ <@{user_id}> the 30-minute window to reconsider this rejected ticket has expired."
                )
                return

        # 5. Inspect live reactions on the parent message
        approved_users, rejected_users = get_current_reactions_on_message(channel_id, message_ts)
        auth_approved = approved_users.intersection(AUTHORIZED_USERS)
        auth_rejected = rejected_users.intersection(AUTHORIZED_USERS)

        # 6. Handle Rejection (❌)
        if reaction in REJECT_REACTIONS:
            state["status"] = "REJECTED"
            state["rejected_at"] = time.time()
            state["rejected_by"] = user_id
            save_state_to_log_channel(
                state_key,
                state,
                log_ts,
                event_note=f"Rejected by {user_name} ({user_id})"
            )
            app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=message_ts,
                text=(
                    f"❌ <@{user_id}> has rejected the ticket request.\n"
                    f"_(If you change your mind within 30 minutes, remove ❌ and react with 🎫)_"
                )
            )
            return

        # If someone still has an active ❌ reaction on the message, block approval until ❌ is removed
        if auth_rejected:
            rejector_mentions = ", ".join(f"<@{u}>" for u in auth_rejected)
            log_event(f"Approval paused on {message_ts}: active ❌ reaction present from {rejector_mentions}.")
            app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=message_ts,
                text=f"⚠️ Cannot approve while an ❌ reaction from {rejector_mentions} is still active on the message. Remove ❌ first to reconsider."
            )
            return

        # 7. Handle Approval (🎫)
        sujith_approved = bool(SUJITH_USER_ID and SUJITH_USER_ID in auth_approved)
        pranava_approved = bool(PRANAVA_USER_ID and PRANAVA_USER_ID in auth_approved)
        sahil_approved = bool(SAHIL_USER_ID and SAHIL_USER_ID in auth_approved)

        # Case A: Super-approver (Sujith) OR Both Pranava + Sahil approved
        if sujith_approved or (pranava_approved and sahil_approved):
            approver_names = ", ".join(get_user_display_name(u) for u in auth_approved)
            try:
                issue_uuid, identifier = create_plane_work_item(channel_id, message_ts, approver_names)
                state["status"] = "CREATED"
                state["plane_id"] = issue_uuid
                state["plane_identifier"] = identifier
                save_state_to_log_channel(
                    state_key,
                    state,
                    log_ts,
                    event_note=f"Created Plane work item {identifier} (approved by {approver_names})"
                )
                approver_mentions = " & ".join(f"<@{u}>" for u in auth_approved)
                app.client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=message_ts,
                    text=(
                        f"✅ Ticket approved by {approver_mentions}!\n"
                        f"*Plane Work Item Created:* `{identifier}` (ID: `{issue_uuid}`)"
                    )
                )
            except Exception as e:
                log_event(f"Failed to create Plane issue for {message_ts}: {e}", "ERROR")
                app.client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=message_ts,
                    text=f"⚠️ Approval threshold met, but failed to create work item in Plane: `{e}`"
                )
            return

        # Case B: Pranava reacted first -> tag Sahil for approval
        if pranava_approved and not sahil_approved:
            state["status"] = "PENDING"
            if not state.get("sahil_pinged"):
                state["sahil_pinged"] = True
                app.client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=message_ts,
                    text=(
                        f"👋 <@{SAHIL_USER_ID}>, <@{PRANAVA_USER_ID}> has requested to add this thread to Plane.\n"
                        f"Please react to the *original message above* with 🎫 to approve or ❌ to reject."
                    )
                )
            save_state_to_log_channel(
                state_key,
                state,
                log_ts,
                event_note="Pranava approved with 🎫; waiting for Sahil"
            )
            return

        # Case C: Sahil reacted first -> wait silently until Pranava (or Sujith) adds 🎫
        if sahil_approved and not pranava_approved:
            state["status"] = "PENDING"
            save_state_to_log_channel(
                state_key,
                state,
                log_ts,
                event_note="Sahil approved first with 🎫; waiting silently for Pranava"
            )
            return


@app.event("reaction_removed")
def handle_reaction_removed(event, say):
    reaction = event.get("reaction")
    user_id = event.get("user")
    item = event.get("item", {})
    channel_id = item.get("channel")
    message_ts = item.get("ts")

    if channel_id != TARGET_CHANNEL_ID or user_id not in AUTHORIZED_USERS:
        return

    if reaction not in APPROVE_REACTIONS and reaction not in REJECT_REACTIONS:
        return

    user_name = get_user_display_name(user_id)
    log_event(f"Reaction ':{reaction}:' removed by {user_name} ({user_id}) on msg {message_ts}")

    is_reply, _, is_bot_state_log = check_message_context(channel_id, message_ts)
    if is_reply or is_bot_state_log:
        return

    with state_lock:
        state_key = get_state_key(channel_id, message_ts)
        log_ts, state = find_state_in_log_channel(state_key)

        if state.get("status") == "CREATED":
            return

        # If Pranava removes 🎫 while the request is still pending a response, cancel the request
        if reaction in APPROVE_REACTIONS and user_id == PRANAVA_USER_ID and state.get("status") == "PENDING":
            state["status"] = "CANCELLED"
            state["sahil_pinged"] = False
            save_state_to_log_channel(
                state_key,
                state,
                log_ts,
                event_note=f"Cancelled by {user_name} removing 🎫 while pending"
            )
            app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=message_ts,
                text=f"🚫 <@{PRANAVA_USER_ID}> removed their 🎫 reaction. Pending ticket request has been cancelled."
            )
            return

        # If someone removes ❌ within the 30-minute window, move status back to PENDING
        if reaction in REJECT_REACTIONS and state.get("status") == "REJECTED":
            rejected_at = state.get("rejected_at")
            if rejected_at and (time.time() - float(rejected_at)) <= RECONSIDER_WINDOW_SECONDS:
                state["status"] = "PENDING"
                save_state_to_log_channel(
                    state_key,
                    state,
                    log_ts,
                    event_note=f"{user_name} removed ❌ within 30m window; back to PENDING"
                )


# =====================================================================
# 5. RENDER HEALTH CHECK & DIAGNOSTIC DUMP ROUTES
# =====================================================================
@flask_app.route("/")
def health_check():
    now_str = get_current_timestamp()
    ping_history.append(now_str)
    if len(ping_history) > 100:
        ping_history.pop(0)
    return "Bot is running!"


@flask_app.route("/dump")
def dump_logs():
    current_time = get_current_timestamp()

    lines = [
        "==================================================",
        "          PLANE-BOT PING & EVENT LOGS             ",
        "==================================================",
        f"Server Time:       {current_time}",
        f"Container Started: {CONTAINER_START_TIME}",
        f"Total Pings:       {len(ping_history)} / 100 recorded",
        f"Total Events:      {len(event_history)} / 200 recorded",
        "==================================================\n",
        "--- RECENT BOT EVENTS (newest to oldest) ---",
    ]

    if not event_history:
        lines.append("No Slack/Plane events recorded in this container session yet.")
    else:
        for idx, entry in enumerate(reversed(event_history), 1):
            lines.append(f"[{idx:03d}] {entry}")

    lines.append("\n--- RECENT KEEP-ALIVE PINGS (newest to oldest) ---")
    if not ping_history:
        lines.append("No ping history recorded yet in this container session.")
    else:
        for idx, entry in enumerate(reversed(ping_history), 1):
            lines.append(f"[{idx:02d}] Ping successful at: {entry}")

    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


def run_flask():
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    log_event("Starting Plane Bot container (Flask + Slack SocketMode)...")
    Thread(target=run_flask, daemon=True).start()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()