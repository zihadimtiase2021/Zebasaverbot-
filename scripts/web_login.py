"""Zeba Setup Panel - configure the bot from the browser (no terminal, no manual env editing).

Started by deploy/entrypoint.sh when SESSION_STRING is missing (or FORCE_SETUP=1). The Telegram login runs on pyrofork;
the SESSION_STRING it produces is a Telethon StringSession (what the bot reads).

Flow:  Unlock (WEBHOOK_SECRET)  ->  1 Connect Telegram (phone, code, 2FA)  ->  2 Configure (channel, keys, performance)
       ->  3 Launch: values are written to the Render service environment through the Render API and the service is
       redeployed, so the settings survive restarts on the free plan (no disk).
Needs once in Render -> Environment:  API_ID, API_HASH, WEBHOOK_SECRET  and (for saving)  RENDER_API_KEY.
RENDER_SERVICE_ID is provided by Render itself. Without RENDER_API_KEY the panel shows the values to copy by hand,
or can start the bot right away (until the next restart).
Secrets are never written to logs or disk.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs

from unified_core import pyro_login as pl
from unified_core.webui import CSS, JS, LOGO, SPIN, alert, e, kv, layout, pw_field  # noqa: F401  (shared look & feel)

PORT = int(os.environ.get("PORT") or 7860)
SECRET = os.environ.get("WEBHOOK_SECRET", "")
API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH", "")
RENDER_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE = os.environ.get("RENDER_SERVICE_ID", "")
RENDER_BASE = os.environ.get("RENDER_API_BASE", "https://api.render.com/v1").rstrip("/")
COOKIE = "zeba_auth"
MAX_BODY = 256 * 1024

state: dict = {"client": None, "phone": None, "hash": None, "session": None, "channels": [], "stage": "phone",
               "need_2fa": False, "fails": []}


# --------------------------------------------------------------------------------------------- pages
def p_unlock(msg: str = "") -> str:
    return layout(f"""<div class="card"><h2>Unlock setup</h2>
<p class="sub">Enter your <b>WEBHOOK_SECRET</b> (the one you added in Render Environment) to continue.</p>{alert(msg)}
<form method="post" action="/unlock">{pw_field("pw", "Setup password", required=True, extra="autofocus")}
<button type="submit">Unlock {SPIN}</button></form></div>""")


def p_no_secret() -> str:
    return layout(f"""<div class="card"><h2>One more step</h2>
{alert("WEBHOOK_SECRET is not set.", "warn")}<p class="sub">In Render open <b>Environment</b>, add a variable named
<code>WEBHOOK_SECRET</code> with a long random value, save, and reload this page.</p></div>""")


def p_phone(msg: str = "") -> str:
    return layout(f"""<div class="card"><h2>Connect your Telegram account</h2>
<p class="sub">The bot posts as this account. A login code will arrive in your Telegram app.</p>{alert(msg)}
<form method="post" action="/send"><label for="phone">Phone number</label>
<input id="phone" name="phone" type="tel" inputmode="tel" placeholder="+8801XXXXXXXXX" autocomplete="tel" required autofocus>
<p class="hint">Include the country code, for example +880 for Bangladesh.</p>
<button type="submit">Send login code {SPIN}</button></form>
<p class="hint">Never forward the code to anyone: Telegram cancels codes that are shared.</p></div>""", 1)


def p_code(msg: str = "") -> str:
    need = state["need_2fa"]
    twofa = pw_field("pw2", "Two-step verification password", "Your Telegram cloud password." if need else
                     "Only if you enabled two-step verification; otherwise leave empty.", required=need)
    return layout(f"""<div class="card"><h2>Enter the login code</h2>
<p class="sub">We sent a code to <b>{e(state["phone"])}</b> in the Telegram app.</p>{alert(msg)}
<form method="post" action="/verify"><label for="code">Login code</label>
<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8" placeholder="12345" {"" if need else "required autofocus"}>
{twofa}<button type="submit">Verify and continue {SPIN}</button></form>
<form method="post" action="/reset"><button class="ghost" type="submit">Use a different number</button></form></div>""", 1)


def p_settings(msg: str = "", v: dict | None = None) -> str:
    v = v or {}
    ch = state["channels"]
    opts = "".join(f'<option value="{e(c["id"])}"{" selected" if v.get("channel_pick") == c["id"] else ""}>{e(c["name"])} ({e(c["id"])})</option>'
                   for c in ch)
    sel = (f'<select name="channel_pick"><option value="">Choose a channel...</option>{opts}</select>'
           if ch else '<p class="hint">No channel with posting rights was found on this account.</p>')

    def val(k, d=""):
        return e(v.get(k, d))
    heights = "".join(f'<option value="{h}"{" selected" if val("MAX_VIDEO_HEIGHT", "720") == str(h) else ""}>{h}p</option>'
                      for h in (480, 720, 1080))
    return layout(f"""<div class="card"><h2>Configure the bot</h2>
<p class="sub">Logged in successfully. Choose where media is posted and tune the options.</p>{alert(msg)}
<form method="post" action="/save">
<fieldset><legend>Destination</legend><label>Target channel</label>{sel}
<label for="chan">Or enter a channel id / @username</label>
<input id="chan" name="channel_manual" placeholder="-1001234567890 or @mychannel" value="{val("channel_manual")}">
<p class="hint">The logged-in account must be an admin with permission to post messages.</p></fieldset>
<fieldset><legend>Translation (optional)</legend>
{pw_field("OLLAMA_API_KEY", "Ollama API key", "Free key: ollama.com/settings/keys")}
{pw_field("OPENROUTER_API_KEY", "OpenRouter API key", "Free models only: openrouter.ai/keys")}</fieldset>
<fieldset><legend>Notifications (optional)</legend>
{pw_field("CONTROL_BOT_TOKEN", "Control bot token", "Create a bot with @BotFather to get status messages.")}
<label for="adm">Admin Telegram user ids</label><input id="adm" name="ADMIN_IDS" inputmode="numeric" placeholder="123456789, 987654321" value="{val("ADMIN_IDS")}"></fieldset>
<details><summary>Advanced</summary>
<div class="grid"><div><label for="mc">Download workers</label><input id="mc" name="MAX_CONCURRENCY" type="number" min="1" max="8" value="{val("MAX_CONCURRENCY", "1")}"></div>
<div><label for="uc">Upload workers</label><input id="uc" name="UPLOAD_CONCURRENCY" type="number" min="1" max="8" value="{val("UPLOAD_CONCURRENCY", "1")}"></div>
<div><label for="cc">Comment workers</label><input id="cc" name="COMMENT_CONCURRENCY" type="number" min="1" max="8" value="{val("COMMENT_CONCURRENCY", "1")}"></div>
<div><label for="mh">Max video height</label><select id="mh" name="MAX_VIDEO_HEIGHT">{heights}</select></div></div>
<label for="xa">X accounts</label><textarea id="xa" name="X_ACCOUNTS" placeholder="user:auth_token:ct0,user2:auth_token:ct0">{val("X_ACCOUNTS")}</textarea>
<label for="cb">Cookies (base64)</label><textarea id="cb" name="COOKIES_B64" placeholder="base64 of cookies.txt">{val("COOKIES_B64")}</textarea></details>
<button type="submit">Save and launch {SPIN}</button></form>
<form method="post" action="/reset"><button class="ghost" type="submit">Start over</button></form></div>""", 2)


def p_launched(results: list[tuple[str, bool, str]]) -> str:
    rows = "".join(f'<li><span class="{"okc" if ok else "bad"}">{"&#10003;" if ok else "&#10005;"}</span><span><b>{e(k)}</b>'
                   f'{"" if ok else " - " + e(msg)}</span></li>' for k, ok, msg in results)
    return layout(f"""<div class="card center"><div class="big">&#128640;</div><h2>Saved. Deploying now.</h2>
<p class="sub">All settings were stored in your Render service and a deploy was started. It takes 1-2 minutes. When it finishes
this page is replaced by the running bot. Check Render <b>Logs</b> for <code>Logged in as</code>, then send <code>.zstat</code>
in Telegram Saved Messages.</p></div>
<div class="card" style="margin-top:14px"><ul class="list">{rows}</ul></div>""", 3)


def p_manual(pairs: dict, msg: str = "") -> str:
    items = "".join(kv(k, v, i) for i, (k, v) in enumerate(pairs.items()))
    note = alert(msg, "warn") if msg else alert("RENDER_API_KEY is not set, so settings cannot be saved automatically.", "info")
    return layout(f"""<div class="card"><h2>Save these in Render</h2>{note}
<p class="sub">Render dashboard, then your service, then <b>Environment</b>, then add each variable and save. Add
<code>RENDER_API_KEY</code> once to save everything automatically next time.</p>{items}
<form method="post" action="/start"><button type="submit">Start the bot now {SPIN}</button></form>
<p class="hint">Starting now works until the next restart; after that this panel returns unless the variables are saved in Render.</p>
<form method="post" action="/back"><button class="ghost" type="submit">Back to settings</button></form></div>""", 3)


def p_starting() -> str:
    return layout("""<div class="card center"><div class="big">&#9889;</div><h2>Starting the bot</h2>
<p class="sub">The panel is closing and the bot is starting. Check the Render <b>Logs</b> for <code>Logged in as</code> in about a minute.</p></div>""", 3)


# --------------------------------------------------------------------------------------------- http
def respond(body: str, status: str = "200 OK", extra: dict | None = None) -> bytes:
    data = body.encode()
    hdr = {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(data)), "Cache-Control": "no-store",
           "X-Robots-Tag": "noindex", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff", "Connection": "close",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                      "img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"}
    hdr.update(extra or {})
    return (f"HTTP/1.1 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n").encode() + data


def redirect(to: str = "/", extra: dict | None = None) -> bytes:
    return respond("", "303 See Other", {"Location": to, **(extra or {})})


def token() -> str:
    return hmac.new(SECRET.encode(), b"zeba-setup-panel-v1", hashlib.sha256).hexdigest()


def authed(cookies: dict) -> bool:
    return bool(SECRET) and hmac.compare_digest(cookies.get(COOKIE, "").encode(), token().encode())


def parse_cookies(raw: str) -> dict:
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        hdrs = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        n = min(int(hdrs.get("content-length", 0) or 0), MAX_BODY)
        body = await asyncio.wait_for(reader.readexactly(n), 20) if n else b""
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "ignore"), keep_blank_values=True).items()}
        out = await route(method, path.split("?", 1)[0], form, parse_cookies(hdrs.get("cookie", "")))
    except Exception:
        out = respond(layout('<div class="card"><h2>Request error</h2><p class="sub">Please go back and try again.</p></div>'),
                      "400 Bad Request")
    writer.write(out)
    try:
        await writer.drain()
    finally:
        writer.close()


def locked_out() -> bool:
    now = time.time()
    state["fails"] = [t for t in state["fails"] if now - t < 60]
    return len(state["fails"]) >= 8


def stage_page(msg: str = "") -> str:
    st = state["stage"]
    if st == "code":
        return p_code(msg)
    if st == "settings":
        return p_settings(msg, state.get("form"))
    return p_phone(msg)


# ---- Render API
def render_call(method: str, path: str, payload: dict | None = None) -> None:
    req = urllib.request.Request(f"{RENDER_BASE}{path}", method=method,
                                 data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {RENDER_KEY}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        r.read()


def save_to_render(pairs: dict) -> list[tuple[str, bool, str]]:
    """Set each variable one by one (never the whole-environment PUT, which would delete the others), then deploy."""
    results: list[tuple[str, bool, str]] = []
    for k, v in pairs.items():
        try:
            render_call("PUT", f"/services/{RENDER_SERVICE}/env-vars/{k}", {"value": v})
            results.append((k, True, ""))
        except urllib.error.HTTPError as exc:
            results.append((k, False, f"Render API {exc.code}"))
        except Exception as exc:
            results.append((k, False, exc.__class__.__name__))
    try:  # leave "re-run setup" mode if it was on
        render_call("DELETE", f"/services/{RENDER_SERVICE}/env-vars/FORCE_SETUP")
    except Exception:
        pass
    if all(ok for _, ok, _ in results):
        try:
            render_call("POST", f"/services/{RENDER_SERVICE}/deploys", {"clearCache": "do_not_clear"})
            results.append(("Deploy started", True, ""))
        except urllib.error.HTTPError as exc:
            results.append(("Deploy", False, f"Render API {exc.code}"))
        except Exception as exc:
            results.append(("Deploy", False, exc.__class__.__name__))
    return results


def collect(form: dict) -> tuple[dict, str]:
    chan = (form.get("channel_manual") or "").strip() or (form.get("channel_pick") or "").strip()
    if not chan:
        return {}, "Choose a target channel or enter its id."
    pairs = {"SESSION_STRING": state["session"], "TARGET_CHANNEL": chan, "ZEBA_SETUP_COMPLETE": "1",
             "KEEPALIVE_ENABLED": "true"}
    for k in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "CONTROL_BOT_TOKEN", "X_ACCOUNTS", "COOKIES_B64"):
        if (form.get(k) or "").strip():
            pairs[k] = form[k].strip()
    adm = (form.get("ADMIN_IDS") or "").replace(" ", "")
    if adm:
        if not all(p.lstrip("-").isdigit() for p in adm.split(",") if p):
            return {}, "Admin ids must be numbers separated by commas."
        pairs["ADMIN_IDS"] = adm
    for k in ("MAX_CONCURRENCY", "UPLOAD_CONCURRENCY", "COMMENT_CONCURRENCY"):
        raw = (form.get(k) or "1").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 8:
            return {}, "Worker counts must be between 1 and 8."
        pairs[k] = raw
    pairs["MAX_VIDEO_HEIGHT"] = form.get("MAX_VIDEO_HEIGHT") if form.get("MAX_VIDEO_HEIGHT") in ("480", "720", "1080") else "720"
    return pairs, ""


async def list_channels(session: str) -> tuple[bool, list[dict]]:
    """Open the fresh login with Telethon (the bot's library): (usable?, channels this account can post to)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    out: list[dict] = []
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return False, out
        try:
            async for d in client.iter_dialogs(limit=400):
                ent = d.entity
                if not getattr(ent, "broadcast", False):
                    continue
                rights = getattr(ent, "admin_rights", None)
                if getattr(ent, "creator", False) or (rights and getattr(rights, "post_messages", False)):
                    out.append({"id": f"-100{ent.id}", "name": d.name})
        except Exception:
            pass
        return True, out
    except Exception:
        return False, out
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def route(method: str, path: str, form: dict, cookies: dict) -> bytes:
    if path in ("/health", "/healthz"):
        return respond('{"status": "ok"}', extra={"Content-Type": "application/json"})
    if not SECRET:
        return respond(p_no_secret())

    if path == "/unlock" and method == "POST":
        if locked_out():
            return respond(p_unlock("Too many attempts. Wait a minute."), "429 Too Many Requests")
        if not hmac.compare_digest(form.get("pw", "").encode(), SECRET.encode()):
            state["fails"].append(time.time())
            await asyncio.sleep(1.5)
            return respond(p_unlock("Wrong password."), "401 Unauthorized")
        return redirect("/", {"Set-Cookie": f"{COOKIE}={token()}; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=Strict"})

    if not authed(cookies):
        return respond(p_unlock())
    if method == "GET":
        return respond(stage_page())

    if path == "/reset":
        if state["client"]:
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        state.update(client=None, phone=None, hash=None, session=None, channels=[], stage="phone", need_2fa=False, form=None)
        return redirect()

    if path == "/send":
        phone = form.get("phone", "").strip().replace(" ", "")
        if not phone.startswith("+") or not phone[1:].isdigit():
            return respond(p_phone("Enter the number with country code, like +8801XXXXXXXXX."))
        try:
            if state["client"]:
                await state["client"].disconnect()
            client = pl.PyroLogin(API_ID, API_HASH)
            await client.connect()
            sent = await client.send_code(phone)
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            return respond(p_phone(f"Could not send the code ({exc.__class__.__name__}). Check API_ID / API_HASH and the number."))
        state.update(client=client, phone=phone, hash=sent.phone_code_hash, stage="code", need_2fa=False)
        return redirect()

    if path == "/verify":
        client = state["client"]
        if client is None:
            return respond(p_phone("Session expired. Start again."))
        code = form.get("code", "").strip().replace(" ", "")
        pw2 = form.get("pw2", "")
        try:
            if pw2 and not code:
                await client.sign_in(password=pw2)
            else:
                await client.sign_in(code=code)
        except pl.PasswordNeeded:
            if not pw2:
                state["need_2fa"] = True
                return respond(p_code("Two-step verification is on. Enter your password."))
            try:
                await client.sign_in(password=pw2)
            except pl.PasswordInvalid:
                return respond(p_code("Wrong two-step password."))
            except Exception as exc:
                return respond(p_code(f"Login failed ({exc.__class__.__name__})."))
        except pl.PasswordInvalid:
            return respond(p_code("Wrong two-step password."))
        except (pl.CodeInvalid, pl.CodeExpired):
            return respond(p_code("That code is wrong or expired. Go back and request a new one."))
        except Exception as exc:
            return respond(p_code(f"Login failed ({exc.__class__.__name__})."))
        try:
            session = await client.export_session()
        except Exception as exc:
            return respond(p_code(f"Logged in, but the session could not be exported ({exc.__class__.__name__})."))
        try:
            await client.disconnect()
        except Exception:
            pass
        state.update(client=None)
        usable, channels = await list_channels(session)
        if not usable:
            state.update(stage="phone", phone=None, hash=None, session=None, channels=[])
            return respond(p_phone("Logged in, but the session could not be used by the bot. Start again."))
        state.update(session=session, channels=channels, stage="settings")
        return redirect()

    if path == "/save" and state["stage"] == "settings":
        state["form"] = {k: v for k, v in form.items() if k not in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "CONTROL_BOT_TOKEN")}
        pairs, err = collect(form)
        if err:
            return respond(p_settings(err, state["form"]))
        state["pairs"] = pairs
        if RENDER_KEY and RENDER_SERVICE:
            results = await asyncio.to_thread(save_to_render, pairs)
            if all(ok for _, ok, _ in results):
                return respond(p_launched(results))
            bad = "; ".join(f"{k}: {m}" for k, ok, m in results if not ok)
            return respond(p_manual(pairs, f"Automatic save failed ({bad}). Check RENDER_API_KEY, or copy the values below."))
        return respond(p_manual(pairs))

    if path == "/back":
        return respond(p_settings("", state.get("form")))

    if path == "/start" and state.get("pairs"):
        os.environ.update(state["pairs"])
        loop = asyncio.get_running_loop()
        loop.call_later(1.5, lambda: os.execv(sys.executable, [sys.executable, "run.py"]))
        return respond(p_starting())

    return respond(layout('<div class="card"><h2>Not found</h2></div>'), "404 Not Found")


def _ping(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=20).read()
    except Exception:
        pass


async def keepalive() -> None:
    """A free Render instance sleeps without traffic: ping our own public /health now and then."""
    base = (os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base or os.environ.get("KEEPALIVE_ENABLED", "true").lower() in ("0", "false", "no", "off"):
        return
    await asyncio.sleep(30)
    while True:
        await asyncio.to_thread(_ping, base + "/health")
        await asyncio.sleep(600)


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    asyncio.create_task(keepalive())
    print(f"[setup-panel] SESSION_STRING not set - setup panel is live on port {PORT}. Open the service URL.", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
