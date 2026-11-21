# Deploy on Render (free) - step by step

> **Free plan reality check (Render, 2026):** a free web service has 512 MB RAM and 0.1 CPU, **sleeps after 15 minutes without
> inbound HTTP traffic** and has **no persistent disk** (files are wiped on every restart/redeploy). This guide keeps it awake
> with pings and stores everything that matters in environment variables. Expect: videos are processed slowly, and the bot
> forgets *which links it already posted*, unfinished batches and your `.ai` choice whenever Render restarts it.
> For a bot that never forgets: Render Starter ($7/mo, use `render.yaml`) or a VPS (see "Better free option" below).

## Part A - Prepare on your own computer (10 min)
1. Install the bot locally once (`python3 run.py --setup-only` is enough) or just have Python 3.10+.
2. Get `API_ID` and `API_HASH` at https://my.telegram.org -> *API development tools*. Put them in `.env`:
   ```
   API_ID=1234567
   API_HASH=0123456789abcdef0123456789abcdef
   ```
3. Create the Telegram login **as text** (a new session; your local bot, if any, keeps its own):
   ```bash
   python3 -m scripts.export_session
   ```
   Enter phone number -> code -> (2FA password). Copy the long text it prints. This is `SESSION_STRING`.
   **It gives full access to your Telegram account - treat it like a password. Never commit or share it.**
4. Decide `TARGET_CHANNEL`: the numeric id (`-1001234567890`) or `@username` of the channel where media is posted.
   The Telegram account from step 3 must be an **admin with "post messages"** there.
5. Translation (optional, free): get a key at https://ollama.com/settings/keys (`OLLAMA_API_KEY`) and/or
   https://openrouter.ai/keys (`OPENROUTER_API_KEY`, free models only).
6. Optional extras: `CONTROL_BOT_TOKEN` (a bot from @BotFather for notifications), `X_ACCOUNTS=user:auth_token:ct0,user2:auth_token:ct0`,
   `COOKIES_B64` (`base64 -w0 cookies.txt`; Mac: `base64 -i cookies.txt`).

## Part B - Put the code on GitHub (5 min)
1. Create a **private** repository on https://github.com/new (e.g. `zebasaverbot`).
2. Unzip the project, then in its folder:
   ```bash
   git init && git add . && git commit -m "zeba bot"
   git branch -M main
   git remote add origin https://github.com/YOUR_NAME/zebasaverbot.git
   git push -u origin main
   ```
   `.gitignore` already excludes `.env`, sessions and data. **Never commit `.env`, `*.session` or your session string.**

## Part C - Create the free service on Render (10 min)
1. Sign up / log in at https://dashboard.render.com and connect your GitHub account.
2. **New -> Web Service** -> choose your private repo -> *Connect*.
3. Settings:
   | Field | Value |
   |---|---|
   | Language / Runtime | **Docker** (Render finds the `Dockerfile`) |
   | Branch | `main` |
   | Instance Type | **Free** |
   | Health Check Path (Advanced) | `/health` |
   (Shortcut: *New -> Blueprint* with `render.free.yaml` as the Blueprint file fills everything below for you.)
4. **Environment Variables** (Advanced -> Add Environment Variable):
   | Key | Value |
   |---|---|
   | `API_ID`, `API_HASH` | from Part A |
   | `SESSION_STRING` | the text from Part A step 3 |
   | `TARGET_CHANNEL` | channel id or @username |
   | `ZEBA_SETUP_COMPLETE` | `1` |
   | `WEBHOOK_SECRET` | any long random text (e.g. 40+ random characters) |
   | `MAX_CONCURRENCY`, `UPLOAD_CONCURRENCY`, `COMMENT_CONCURRENCY` | `1` (free CPU is tiny) |
   | `MAX_VIDEO_HEIGHT` | `720` (less RAM/CPU for ffmpeg) |
   | `OLLAMA_API_KEY` / `OPENROUTER_API_KEY` | optional, translation |
   | `CONTROL_BOT_TOKEN`, `ADMIN_IDS`, `X_ACCOUNTS`, `COOKIES_B64` | optional |
5. Click **Create Web Service**. The first build takes ~5-10 minutes (Docker image with ffmpeg).
6. Open **Logs**. Success looks like: `Logged in as <your name>` ... `HTTP server listening` ... `Keep-alive ping every 600s`.
   Your URL is shown at the top: `https://zebasaverbot-xxxx.onrender.com`.
7. Check from Telegram (Saved Messages): send `.zstat`, then `.ai test` (tests every translation provider with the exact error if one fails),
   then a link like `.dl https://x.com/user/status/123`.

## Part D - Keep it awake (5 min)
The bot pings its own `/health` every 10 minutes (`KEEPALIVE_*`). That only works while it is awake, so add an outside monitor as backup
(it also wakes the service if Render did put it to sleep):
1. https://uptimerobot.com -> free account -> **Add New Monitor**.
2. Type **HTTP(s)**, URL `https://YOUR-SERVICE.onrender.com/health`, interval **5 minutes** -> Create.
Free instance hours: 750 per month per workspace; one service running 24/7 uses ~720, so run only **one** free service.

## Part E - Use the webhook (from any other site)
```bash
curl -X POST https://YOUR-SERVICE.onrender.com/webhook \
  -H "Authorization: Bearer YOUR_WEBHOOK_SECRET" -H "Content-Type: application/json" \
  -d '{"url": "https://x.com/user/status/123", "caption": "optional custom caption"}'
# -> {"ok": true, "queued": 1, "urls": [...], "duplicates": [], "invalid": []}
```
Body options: `{"url","caption","force"}` · `{"urls": [...]}` · `{"items": [{"url","caption"}, "https://..."]}` · form `url=...` · plain text with links.
Auth alternatives: `X-Webhook-Secret: <secret>` or HMAC `X-Signature: sha256=<hex HMAC-SHA256(secret, raw body)>`.
```python
import requests
requests.post("https://YOUR-SERVICE.onrender.com/webhook",
              headers={"Authorization": "Bearer YOUR_WEBHOOK_SECRET"}, json={"url": "https://youtu.be/xyz"}, timeout=15)
```
```js
await fetch("https://YOUR-SERVICE.onrender.com/webhook", { method: "POST",
  headers: { Authorization: "Bearer YOUR_WEBHOOK_SECRET", "Content-Type": "application/json" },
  body: JSON.stringify({ url: "https://www.instagram.com/reel/ABC/" }) });
```
Zapier / Make / n8n / IFTTT: "Webhooks / HTTP request" action -> POST + the header above + JSON body.
Call it from your **server**, not from public browser JavaScript (the secret would be visible). Only sites supported by `.dl` are accepted.
Replies: `202` queued · `400` bad link · `401` wrong secret · `404` webhook disabled (no secret) · `413` body > 64 KB ·
`429` rate limit (60/min; 10 wrong secrets/min per address) · `503` queue full. `GET /status` (same auth) shows queue counters.
After a Render sleep the first request needs ~1 minute to wake the service - retry or let the monitor keep it awake.

## What resets on the free plan (no disk)
| Kept (it is in environment variables) | Lost on every restart |
|---|---|
| Telegram login (`SESSION_STRING`), channel, API keys, X accounts (`X_ACCOUNTS`), cookies (`COOKIES_B64`) | list of already-posted links (a link can be posted again), unfinished batch progress, queued webhook links, `.ai` choice (set `TRANSLATE_PROVIDER` in env instead), accounts added with `.xadd` |
Everything else works as on a normal install. Put permanent choices into environment variables, not into chat commands.

## Better free option: Oracle Cloud Always Free VM (always on, real disk)
Oracle's free Ampere VM was cut in 2026 to **2 OCPU / 12 GB RAM**; signup needs a card for verification (not charged), capacity in some
regions is hard to get, and idle instances can be reclaimed (the bot keeps it busy). If you get one (Ubuntu 22.04/24.04 ARM):
```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
git clone https://github.com/YOUR_NAME/zebasaverbot.git && cd zebasaverbot
docker compose run --rm zebabot      # first start: answers the questions in the terminal (login code etc.)
docker compose up -d --build         # then runs in the background, restarts after reboot
```
No `SESSION_STRING` needed there; batches, duplicates and X accounts persist in the Docker volume. Open the firewall port only if you want the webhook
(`WEBHOOK_SECRET` + `WEBHOOK_PORT=8080`; put a free Cloudflare Tunnel or Caddy in front for HTTPS).

## Upgrade path on Render
Switch the instance type to **Starter**, add a disk mounted at `/data` (or deploy with `render.yaml`): then nothing is lost on restarts.

## Troubleshooting
- **Build ok but deploy "no open port"**: the bot exited. Open Logs: missing `API_ID`/`API_HASH`/`TARGET_CHANNEL`/`SESSION_STRING`, or the session was revoked -> run `scripts.export_session` again.
- **`AuthKeyDuplicated`**: the same `SESSION_STRING` runs in two places. Create one session string per deployment.
- **Out of memory / restarts on big videos**: keep `MAX_VIDEO_HEIGHT=720`, `MAX_CONCURRENCY=1`, or move to Starter/VPS.
- **No translation**: send `.ai test` (or locally `python3 run.py --test-translate`) - it prints each provider's exact error.
- **Bot silent for ~1 min after idle**: the instance was asleep; add the UptimeRobot monitor (Part D).
