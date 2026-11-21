# Zeba Saver Bot (production build)

Telegram userbot that downloads media (X/Twitter, Instagram, YouTube, TikTok, Facebook, Pinterest, Threads, t.me)
from links you send, reposts it to your channel with an (optional) AI Bangla translation, and posts the source's
top comments as replies under the post.

## Features
- Sources: X (twscrape → FxTwitter → yt-dlp), t.me message links, everything else via yt-dlp
- Albums (auto-split into groups of 10), photo/video detection, webp → jpg, oversize photo resize
- Real AI caption translation: **Ollama Cloud** (free API key, best model picked automatically) or any OpenAI-compatible API; original caption kept, safe length handling (1024 / 2048 Premium)
- Comments posted through the channel's **discussion thread** (`comment_to`), with tag-search fallback in `COMMENT_CHAT`
- Job queue with N workers, retries, FloodWait handling, per-job timeout, guaranteed temp cleanup
- **Fast:** X posts via FxTwitter first (one request), parallel media downloads, bigger upload chunks, comments fetched and
  posted in the background so the next link starts right after the post is live
- **Multiple X accounts:** twscrape rotates between them; when all are rate-limited the bot does not wait, it falls back
  to FxTwitter / yt-dlp (which also rotates through the accounts' cookies). `.xacc` shows who is limited
- **Resumable batches:** every link of a `.json`/`.txt` batch is stored; after a crash/restart the batch continues with the
  unfinished links only (no re-sending the file, nothing posted twice)
- **Runs in the background:** `python3 run.py --background`, with `--status` / `--stop`
- **Duplicate protection** (SQLite job history; `.dl -f` forces a re-post)
- Batch mode: `.json` / `.txt` files with URLs + live dashboard
- Status/failure notifications go to your **control bot chat** (press /start in the bot once); Saved Messages is the fallback
- Admin-only control bot (auto mode, stats, restart); loop protection so the bot never re-processes its own posts
- Graceful shutdown, rotating logs, Docker + systemd ready

## Commands (type them in any chat from your own account)
| Command | Action |
|---|---|
| `.dl <url>` / `.dl -f <url>` | download & repost (`-f` = ignore duplicate check) |
| `.dl <url> My own caption` | repost with your caption exactly as written (no scraped caption, no translation); works with `-f` too |
| `.dlauto on` / `.dlauto off` | in this chat every link you send (with or without `.dl`) is downloaded and posted **back into this same chat** (not the channel) |
| `.autodl` / `.autodl off` | auto-scrape links in this chat on/off, posts go to your **channel** |
| `.fwdsrc` / `.fwddst` | **chat → chat copier**: send `.fwdsrc` inside the source chat, `.fwddst` inside the destination chat (see below) |
| `.fwd @source @destination` | same route in one line · `.fwd list` · `.fwd watch [n]` / `.fwd unwatch [n]` · `.fwd all [n]` (Forward all) · `.fwd progress` · `.fwd stop [n]` · `.fwd links on\|off` · `.fwd del n` · `.fwd clear` · `.fwd id 123` |
| `.zstat` | queue status |
| `.ai` | which translation model is in use (and whether it is paused by a limit) |
| `.xacc` | X accounts and their rate-limit status |
| `.xadd <user> <auth_token> <ct0>` | add another X account (the message is deleted immediately; use Saved Messages) |
| `.xdel <user>` / `.xunlock` | remove an X account / clear rate-limit locks |
| send `.json` / `.txt` with URLs | batch (in an auto chat, or give it the caption `.batch`) |
| links in **Saved Messages** | always auto |

## Chat → chat message copier (`.fwdsrc` / `.fwddst`)
Copies **every new message** of one chat/channel/group into another one, also from **private** chats and chats where
Telegram's forwarding is disabled: it never uses "forward", the media is **downloaded and uploaded again** as a new message
(photos, videos, round videos, voice, audio, GIFs, files, albums, stickers, text with its formatting). Your account only
needs to be a member of the source and be allowed to post in the destination.
1. In the **source** chat send `.fwdsrc` (the answer disappears after 20 s)
2. In the **destination** chat send `.fwddst` → the route exists. (Or in one line: `.fwd @source @destination`, `.fwd -1001234 -1005678`, `.fwd https://t.me/c/123/1 @dest`.)
3. Every copy ends with a **serial number** that counts up, e.g. `ZF-000123` (tap to copy). `.fwd id 123` shows which source message it came from and where the copy is.

### Watch, Forward all, no duplicates, no links
* **👁 Watch** - a route is watching by default: every new message of the source is copied by itself. `.fwd unwatch [n]` pauses it, `.fwd watch [n]` resumes (menu: 🔁 Forward → tap the route).
* **📥 Forward all** - `.fwd all [n]` (menu: 🔁 Forward → 📥 All) copies the **whole history** of the source, oldest first, albums stay albums.
  Progress: `.fwd progress` (or 📊 Progress), stop: `.fwd stop [n]`. A summary is sent to your control chat when it ends.
* **Never twice** - the bot remembers per route which source messages are already copied (kept as id ranges in Saved Messages **and** in the
  database, so a free Render restart does not make it forget). Forward all skips those and sends only what is missing; messages that
  failed stay open and are retried by the next `.fwd all`. `.fwd reset n` forgets a route's history (to copy everything again on purpose).
* **🔗 Links removed** - every link (`https://…`, `www.…`, `t.me/…`, bare domains, hidden links) is cut out of the copied text; a message that
  only held a link is not copied. Switch: `.fwd links on|off` (default `FWD_STRIP_LINKS=true`). The **ban list** is applied afterwards.

Several routes are fine (one source → many destinations too). Routes are stored in Saved Messages with the other runtime settings,
so they survive Render restarts. Notes: only **new** messages are copied (not old history, edits or deletions); polls, locations and
contacts are skipped; files over your upload limit are skipped with a notification; copies are never copied again, and
the reverse route (A→B plus B→A) is refused so two chats can't copy each other forever. `FWD_ENABLED=false` switches it off,
`FWD_DELAY` (default 0.7 s) is the pause between two copies, `FWD_ID_PREFIX` changes `ZF`.

### Find a product + private notes (control bot only)
Every copied message (= "product") gets a hidden record that **only admins see, in the control bot chat** - the channel only
ever sees the copy with its serial number. Saved automatically: source chat, the **original caption** (before links / ban words were
cut out), number of files, which account copied it, time, links to the source message and the copy.

| Command | What it does |
|---|---|
| `/find 42` (or `/find ZF-000042`) | full details of that product, with its private note |
| `/find some words` | full details of every match (serial, source name, original caption, note) - the 5 newest in full, the rest as a list |
| `/addnote 42 price 15$` | adds a private note (a new line when the product already has one; max 1500 characters) |
| `/removenote 42` | removes the note of that product |
| `/defnote` | lists every source group / channel with its **default note** |
| `/defnote 2 Supplier Rahim, COD` | sets the default note of source no. 2 - it goes on every **new** product copied from there |
| `/defnote 2 -` | deletes that default note (products already made keep theirs) |

Default notes are per **source** group / channel (the place the product comes from), so every source can have its own.

**Backup in Saved Messages:** serial numbers with their source / copy ids, details, notes and default notes are also kept as ONE file
(`zeba_fwd_backup.json.gz`, caption `#zeba_fwd_backup`) in the Saved Messages of the main account. It is rewritten ~20 s after a note
changes (and at most every ~5 min while copies are being made, and on shutdown); only the newest file stays. At start-up whatever the
database is missing is put back (nothing that exists is changed), so a Render restart does not lose them. An empty database never
overwrites the backup. **Do not delete that message.** Copies without a note you wrote are forgotten after 90 days; a copy with your own
note is kept for ever.

## Quick start (one command)
```bash
python3 run.py          # or ./run.sh  (Windows: run.bat)
```
That is all. The first time it:
1. creates a private virtualenv and installs everything (ffmpeg is bundled, nothing to apt-install),
2. asks for each missing value and **saves every answer immediately** (Ctrl+C is safe, just run it again):
   Telegram `API_ID` / `API_HASH` → phone, login code and 2FA password → **pick your channel from a list**
   (or create one; the discussion group is auto-detected) → optional control-bot token → optional AI
   translation provider/key (tested on the spot) → optional X `auth_token` / `ct0` cookies (or a `cookies.txt`)
   → optional browser cookies for yt-dlp,
3. starts the bot and keeps it running (auto-restart on crash).

Next runs start instantly and only ask if something is missing or broken (revoked session, unreachable channel).
Change anything later with `python3 run.py --setup`. Run it on a server in the background with
`tmux`/`screen`, `nohup python3 run.py &`, Docker, or systemd (below).

Other flags: `--setup-only` (wizard only), `--once` (no auto-restart).

### Deploy on Render + webhook API
`render.free.yaml` (FREE: sleeps, no disk - kept awake by built-in pings) and `render.yaml` (Starter: always on + disk) are included, with Docker and a health check at `/health`. Step by step guide, plan
comparison (free sleeps!) and the **webhook** (`POST /webhook` to queue downloads from any other site, with curl / Python /
Node examples) are in **[RENDER_DEPLOY.md](RENDER_DEPLOY.md)**. Telegram login on a host without terminal: `python3 -m scripts.export_session`.

### Run in the background
```bash
python3 run.py --background   # first time: asks the questions in the terminal, then detaches
python3 run.py --status
python3 run.py --stop         # graceful: running jobs finish, unfinished batches continue next start
tail -f data/logs/zeba.log
```
It keeps running after you close the terminal and restarts itself if it crashes. Only one copy can run per data
folder (two copies on one Telegram session would log each other out). Docker and systemd (below) do the same job
and start on boot.

### Translation with Ollama Cloud (free key)
1. Create a key at https://ollama.com/settings/keys
2. Put it in `.env` as `OLLAMA_API_KEY=...` (or run `python3 run.py --setup` and choose "Ollama Cloud"), then restart.
3. `OLLAMA_MODEL=auto` (default): the live model list (`https://ollama.com/api/tags`) is ranked and the best model is
   used; if one is not allowed on your plan, is retired or refuses, the next one is tried automatically. `.ai` shows
   the model in use. Pin one with `OLLAMA_MODEL=glm-5.2` if you prefer.
- Uncensored: Ollama Cloud only hosts the official library models, so normally **no uncensored/abliterated model is
  available there**. The bot ranks any uncensored/abliterated/dolphin model first if one ever appears in the list, and
  otherwise picks the most permissive general model (gpt-oss is used last). The translation prompt also tells the model
  not to censor or refuse, and an answer without Bangla text (a refusal) is retried with the next model.
  For a truly uncensored model run Ollama locally and set `TRANSLATE_PROVIDER=ollama`,
  `OLLAMA_BASE_URL=http://localhost:11434` (no key needed): auto mode then prefers an installed abliterated model.
- Free plan = light usage, one cloud model at a time, limits reset every 5 hours / 7 days. When the limit is reached
  translation pauses automatically (posts keep going without translation) and resumes by itself.

### Switch the translation provider any time (`.ai`)
| Command | Effect |
|---|---|
| `.ai` | show the active provider / model / pause status |
| `.ai ollama` | Ollama Cloud (or local Ollama), best model chosen automatically |
| `.ai openai` | your OpenAI-compatible API (OpenAI, Groq, Gemini, OpenRouter...) |
| `.ai g4f` | GPT4Free (optional, unofficial - see below) |
| `.ai off` / `.ai auto` | no translation / back to whatever `.env` says |
The choice is remembered across restarts. `TRANSLATE_FALLBACK_G4F=true` additionally uses GPT4Free only when the chosen
provider fails or hit its limit.

GPT4Free (g4f) is installed best-effort by `run.py` (if not: `.venv/bin/pip install -U g4f`). It relies on unofficial public
endpoints, so quality and uptime vary and captions go to third parties. Every g4f call has a hard timeout
(`G4F_TIMEOUT`, 30 s), answers without Bangla are rejected, and 3 failures in a row pause it for 5 minutes, so it can
never block a post.

### Several X accounts (fallback when one hits its limit)
Add as many as you like - the wizard (`python3 run.py --setup`) asks "Add another X account?", or use
`.xadd` in Saved Messages, or `python -m scripts.add_x_account --username other --cookies "auth_token=...; ct0=..."`.
twscrape picks a free account for every request and moves on when one is limited. If every account is limited the bot
skips X login straight away (FxTwitter / yt-dlp) instead of waiting. After `X_COOLDOWN_SECONDS` (default 900) a limited
account is used again.

### Batches survive restarts
Each batch is saved in the database the moment it starts, and every finished link is marked. If the bot stops after
10 of 50 links, the next start continues with link 11 and edits the same dashboard message. Set `RESUME_BATCHES=false` to
disable. Comments that were still being posted at the moment of a hard kill are not retried (the posts themselves are safe).

### Docker
```bash
docker compose run --rm zebabot      # first time: answers the questions, then runs (Ctrl+C to stop)
docker compose up -d --build         # afterwards, in the background
```
Settings, session, databases and cookies live in the `zeba-data` volume.

### systemd
```bash
python3 run.py --setup-only          # answer the questions once
sudo cp deploy/zebabot.service /etc/systemd/system/ && sudo systemctl enable --now zebabot
```

### Migrating from the old version
`python -m scripts.migrate_legacy /path/to/old/zebasaverbot` copies session, DBs, cookies and `.env` into place;
`python3 run.py` then only asks for what is still missing.

## Disk cleanup guarantee
Every job downloads into its own temporary folder under `data/tmp/`:
- the moment the media is **uploaded to Telegram**, that folder (media, thumbnails, cookie copy) is deleted - before comments are posted;
- on failure, timeout or cancel it is deleted too;
- leftovers from a crash/kill are wiped at startup and shutdown, and a janitor removes anything stale every 10 minutes;
- yt-dlp's cache is disabled, so nothing else is written.

## Batch files with custom captions
Send a `.json` / `.txt` file of links. In JSON, give an item a `custom_caption` and that text becomes the post's
caption exactly as written (the scraped caption and AI translation are skipped for that item):
```json
[
  {"url": "https://x.com/user/status/123", "custom_caption": "My own caption"},
  {"url": "https://www.instagram.com/reel/ABC/"},
  "https://youtu.be/xyz"
]
```

## Important notes
- `TARGET_CHANNEL`: your account must be able to post there. Full ID (`-100…`) preferred; bare IDs are auto-tried with the prefix.
- Comments need the channel to have a **linked discussion group**; otherwise set `COMMENT_CHAT`.
- Global auto mode is **off by default** (it deletes link messages in every chat). Use `.autodl` per chat instead.
- Keep `.env`, `data/*.session`, `cookies.txt` and `accounts.db` private (they are git/docker-ignored).
  If an old backup was ever shared, rotate the bot token and terminate the old session in Telegram settings.
- YouTube may require a JS runtime (Node ≥ 20 or Deno) for yt-dlp; install it if YouTube downloads fail.
- Tests: `pip install -r requirements-dev.txt && pytest`

## Layout
```
unified_main.py            entry point (startup, shutdown, wiring)
unified_core/              config, database, logging, pure utils
unified_services/          pipeline, queue, downloader (+twitter, telegram), translator, handlers, control bot
(speed knobs in .env: MAX_CONCURRENCY=4, UPLOAD_CONCURRENCY=3, COMMENT_CONCURRENCY=2)
run.py                     one-command launcher (venv, deps, wizard, auto-restart)
unified_core/setup_wizard.py  first-run questions, Telegram login, channel picker
scripts/                   setup, ensure, login, add_x_account, migrate_legacy
deploy/zebabot.service     systemd unit
```


## Watermark
Text watermark for photos & videos (Pillow + ffmpeg). Commands (userbot `.wm ...`, control bot `/wm ...`, or `/admin` page):
`text`, `font`, `fonts`, `color`, `bg` (or `none`), `pos` (tl..br), `size`, `opacity`, `bgopacity`, `margin`, `on|off` (media sent to the bot),
`url on|off` (downloaded links), `fwd on|off` (forwarded media), `preview`, `reset`.
Send a `.ttf/.otf` with caption `.wmfont` (or `/wmfont` to the bot) to add your own font. For Bangla text pick a Bengali font (e.g. `.wm font Lohit-Bengali`).
Videos are re-encoded with ffmpeg, so they take longer on a free instance.

### Button menus (no commands needed)
Open your control bot and send `/start`. The main menu has **💧 Watermark**, **⚙️ Settings** (target channel, source link, comments, caption options),
**🌐 Translation** (provider) and **🔁 Forward** (pause / resume / delete / add routes). Text values (watermark text, custom colour, channel,
route chats) are asked in chat: tap the button, then send the value. Send a `.ttf/.otf` file to the bot to add a font, and any photo/video to get it back watermarked.

### Rotate, ban list, albums
* Watermark rotation: menu **💧 Watermark → 📏 Size / rotate**, or `.wm rotate 30` (-180..180, also works on videos).
* Forward ban list: menu **🔁 Forward → 🚫 Ban words**, or `.fwd ban word, https://spam.com, @name` · `.fwd bans` · `.fwd unban n|word|all`.
  Banned words / links / @names are cut out of every copied caption or text; the rest keeps its formatting. A hidden link whose address
  is banned is removed with its text. Default list: `FWD_BAN` in the environment (comma separated).
* Albums (grouped posts) are supported everywhere: forwarded albums, `.dl` links with several media, and albums sent to the bot / Saved
  Messages come back as one watermarked album.

## Several Telegram accounts (login / logout from the control bot)
The account in `SESSION_STRING` (or the session file) is the **primary** one. Any number of extra userbot accounts (up to
`MAX_ACCOUNTS`, default 5) can be added **from the control bot chat** - no terminal, no redeploy. Admins only.

| Command | What it does |
| --- | --- |
| `/login` (or `/login +8801XXXXXXXXX`) | phone → code → 2FA password (if the account has one) → account starts and you get a **session key** |
| `/accounts` | list with buttons **🚪 Logout** / **🗑 Terminate** and **➕ Add account** (also in the `/start` menu) |
| `/logout 2` | stop account no. 2 in this bot. Its Telegram session stays valid and the bot shows the key again |
| `/resume <key>` | log an account back in with its key - **no code needed** |
| `/terminate 2` | really end the Telegram session (asks to confirm). The key stops working |
| `/cancel` | abort a half-finished login |

* `/login` asks for your own APP ID + APP HASH first (my.telegram.org) - send `-` to use the bot's API. A fresh API pair gets the code reliably when the bot's own one is throttled.
* No code in the bot? Open `https://YOUR-SERVICE/admin/accounts` (unlock with WEBHOOK_SECRET): same login as the setup panel, the account starts right away.
* **Send the login code with spaces** (`1 2 3 4 5`). Telegram cancels a code that is sent as one plain number. Your messages with the
  code, the 2FA password and the `/resume` key are deleted from the chat right away.
* **The key is the account's session string.** Whoever has it controls the account - keep it secret, e.g. in Saved Messages.
  It is shown at login and again at logout. After a logout the server forgets it, so only you can bring the account back.
* The same account can never run twice (Telegram would kill a key used from two places at once); `/resume` of a running account is refused.
* Each extra account is a complete userbot: `.dl`, `.dlauto`, `.fwdsrc/.fwddst`, batches, `.target`, `.settings` ... work in **its** chats and
  its Saved Messages. Status messages in the control bot start with `👤 @username` so you see which account they belong to.
* **Shared:** watermark, translation, caption and forwarding-ban settings (change them from the control bot menus) and the
  duplicate check (a link already posted by one account is skipped for the others).
  **Per account:** the target channel (default = the main target at the time the account was added; change with `.target` in that
  account), forwarding routes and unfinished batches. Every extra account must be a member/admin of its target channel.
* **Which account forwards? (🔁 Forward menu)** Every account has its own forwarding routes. With more than one account running,
  the Forward screen of the control bot shows a **👤 Account** button: tap it to pick the account whose routes you see and
  change (routes, 📥 Forward all, progress, links, ban words all follow that choice).
  **➕ Add route** → source chat → destination chat → the bot checks every account (is it a member of both chats, may it post
  in the destination, is the route new for it):
  * two or more accounts can do it → you get a button per account: **choose which account reads the source and posts to the destination**;
  * only one can → that account is used (the message says so);
  * none can → the reason per account is shown and you can type the destination again.

  Typing `.fwd @source @destination` (or `.fwdsrc` / `.fwddst`) inside an account's own chat still creates the route for
  *that* account.
* **Surviving restarts:** accounts are stored in the database (`data/unified_database.db`) and come back at start-up. On a host
  without a persistent disk (Render free) put the keys into the environment instead: `EXTRA_SESSIONS=key1,key2`
  (an account that comes from `EXTRA_SESSIONS` returns after a restart even if you logged it out - remove its key there too).
* `EXTRA_ACCOUNT_WORKERS` (default 2) = download/upload workers per extra account.
* If Telegram ends a session from outside (Settings → Devices), the bot notices, drops the account and tells you.
