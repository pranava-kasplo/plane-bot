# Slack-to-Plane Bug Reporting Integration

## Overview
A custom Slack bot deployed to automatically capture customer complaints from the private `#production-tickets` channel and push them as new issues into the Kasplo Plane board ([tickets.kasplo.com](https://tickets.kasplo.com)).

---

## Infrastructure & Architecture

- **Application Core**: Python script utilizing `slack_bolt` (Socket Mode) to bypass incoming webhook URL requirements, paired with `requests` for Plane API routing.
- **Hosting**: Deployed as a Web Service on Render's free tier, connected directly to a private GitHub repository for automated CI/CD on new commits.
- **Health & Keep-Alive**: A lightweight Flask server runs concurrently on port 3000 to satisfy Render's port-binding requirements.
- **Scheduling**: A [cron-job.org](https://cron-job.org) task pings the Flask `/` endpoint every 13 minutes from 08:00 to 21:52 IST (Monday–Friday) to prevent the Render container from spinning down during business hours. The job is configured to tolerate Render's 30-second morning cold starts without disabling the schedule.

---

## Workflow Mechanics

1. A team member adds the `:ticket:` emoji reaction to any message in `#production-tickets`.
2. Slack immediately pushes the `reaction_added` event payload to the Render container via WebSockets.
3. The script verifies the event, uses the `groups:history` OAuth scope to retrieve the original message text, and executes a `POST` request to Kasplo's self-hosted Plane API (`kasplo-prod` workspace, project `68b19e1d...`).
4. The ticket is immediately generated in Plane with the message text formatted in HTML.

---

## Configuration & Environment Variables

The integration relies on the following secrets stored securely in the Render dashboard:

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | App-Level Token for Socket Mode (`xapp-...`) |
| `PLANE_API_KEY` | Plane User API Key (`plane_api_...`) |
| `PORT` | *(Optional, defaulted by Render)* Port for Flask server (defaults to `3000`) |

---

## Logging & Diagnostics

Because Render's free tier lacks persistent disk storage, ping history is kept in short-term memory (up to 100 records). 

A custom `/dump` endpoint is configured on the Flask server:
- Navigating to `https://plane-bot.onrender.com/dump` in a web browser directly displays formatted diagnostic logs (current server time, container startup time, and all recent ping timestamps in both IST and UTC).
- Automatically attempts to generate an optional 7-day shareable URL via [dpaste.com](https://dpaste.com) without blocking or failing page output if external network calls are restricted.

