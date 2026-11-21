"""Startup settings page of the running bot:  https://<your-service>.onrender.com/admin/settings

The live options (target channel, watermark, comments ...) are on /admin. Everything that is read only when the bot
starts - API keys, control bot token, admin ids, worker counts, X accounts, cookies, "log in the main account again" -
lives here, so the browser is enough: nothing has to be edited in the Render dashboard.

Saving writes the values into the Render service environment (RENDER_API_KEY) and starts a deploy, exactly like the
first-time setup panel. Without RENDER_API_KEY the page shows the values to paste into Render by hand.
Secrets are never echoed back: an empty secret field means "keep what is set".
"""
from __future__ import annotations

import asyncio
import os

from unified_core.webui import SPIN, alert, e, kv, layout, pw_field

SECRETS = ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "CONTROL_BOT_TOKEN", "X_ACCOUNTS", "COOKIES_B64")
WORKERS = (("MAX_CONCURRENCY", "Download workers"), ("UPLOAD_CONCURRENCY", "Upload workers"),
           ("COMMENT_CONCURRENCY", "Comment workers"))


def _set(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())


def _wl():
    """The setup panel module: reused for its Render API helpers (imported lazily)."""
    from scripts import web_login
    return web_login


def render_ready() -> bool:
    wl = _wl()
    return bool(wl.RENDER_KEY and wl.RENDER_SERVICE)


def collect(form: dict) -> tuple[dict, str]:
    """Only what the person filled in / changed; empty secret = unchanged."""
    pairs: dict[str, str] = {}
    for k in SECRETS:
        if (form.get(k) or "").strip():
            pairs[k] = form[k].strip()
    adm = (form.get("ADMIN_IDS") or "").replace(" ", "")
    if adm != os.environ.get("ADMIN_IDS", "").replace(" ", ""):
        if adm and not all(p.lstrip("-").isdigit() for p in adm.split(",") if p):
            return {}, "Admin ids must be numbers separated by commas."
        pairs["ADMIN_IDS"] = adm
    for k, _label in WORKERS:
        raw = (form.get(k) or "").strip()
        if not raw:
            continue
        if not raw.isdigit() or not 1 <= int(raw) <= 8:
            return {}, "Worker counts must be between 1 and 8."
        if raw != os.environ.get(k, ""):
            pairs[k] = raw
    height = form.get("MAX_VIDEO_HEIGHT") or ""
    if height in ("480", "720", "1080") and height != os.environ.get("MAX_VIDEO_HEIGHT", ""):
        pairs["MAX_VIDEO_HEIGHT"] = height
    for k in ("KEEPALIVE_ENABLED",):
        want = "true" if form.get(k) == "1" else "false"
        if want != os.environ.get(k, "true").lower():
            pairs[k] = want
    return pairs, ""


def page(msg: str = "", kind: str = "err") -> str:
    def cur(k: str, d: str = "") -> str:
        return e(os.environ.get(k, d))

    def secret(k: str, label: str, hint: str) -> str:
        state = "set - leave empty to keep it" if _set(k) else "not set"
        return pw_field(k, label, f"{hint} <b>Now: {state}.</b>")

    heights = "".join(
        f'<option value="{h}"{" selected" if os.environ.get("MAX_VIDEO_HEIGHT", "720") == str(h) else ""}>{h}p</option>'
        for h in (480, 720, 1080))
    workers = "".join(
        f'<div><label for="{k}">{label}</label><input id="{k}" name="{k}" type="number" min="1" max="8" '
        f'value="{cur(k, "1")}"></div>' for k, label in WORKERS)
    keep = os.environ.get("KEEPALIVE_ENABLED", "true").lower() not in ("0", "false", "no", "off")
    note = ("" if render_ready() else alert(
        "RENDER_API_KEY is not set, so changes cannot be saved automatically: you will get the values to paste "
        "into Render -> Environment.", "info"))
    body = f"""<div class="card"><h2>Startup settings</h2>
<p class="sub">These are read when the bot starts, so saving redeploys the service (1-2 minutes, the panel is back after).
Live options (channel, watermark, comments) are on the <a href="/admin">control panel</a>.</p>{alert(msg, kind)}{note}
<form method="post" action="/admin/settings/save">
<fieldset><legend>Translation</legend>
{secret("OLLAMA_API_KEY", "Ollama API key", "Free key: ollama.com/settings/keys.")}
{secret("OPENROUTER_API_KEY", "OpenRouter API key", "Free models only: openrouter.ai/keys.")}</fieldset>
<fieldset><legend>Control bot</legend>
{secret("CONTROL_BOT_TOKEN", "Control bot token", "From @BotFather (/newbot). Gives /login, /accounts and status messages.")}
<label for="adm">Admin Telegram user ids</label><input id="adm" name="ADMIN_IDS" inputmode="numeric" placeholder="123456789, 987654321" value="{cur("ADMIN_IDS")}"></fieldset>
<fieldset><legend>Performance</legend>
<div class="grid">{workers}<div><label for="mh">Max video height</label><select id="mh" name="MAX_VIDEO_HEIGHT">{heights}</select></div></div>
<label><input type="checkbox" style="width:auto" name="KEEPALIVE_ENABLED" value="1" {"checked" if keep else ""}> Keep-alive ping (stops a free Render instance from sleeping)</label></fieldset>
<fieldset><legend>Downloads</legend>
{secret("X_ACCOUNTS", "X accounts", "Format user:auth_token:ct0,user2:auth_token:ct0")}
{secret("COOKIES_B64", "Cookies (base64)", "Base64 of cookies.txt.")}</fieldset>
<button type="submit">Save and redeploy {SPIN}</button></form></div>
<div class="card" style="margin-top:14px"><h2>Main Telegram account</h2>
<p class="sub">Session expired or you want a different main account? This restarts the service into the login panel, where you
sign in with phone + code (+2FA) and pick the channel again.</p>
<form method="post" action="/admin/settings/relogin" onsubmit="return confirm('Restart into the login panel?')">
<button class="ghost" type="submit">Log in the main account again</button></form></div>
<p class="hint"><a href="/admin">Back to the control panel</a></p>"""
    return layout(body, subtitle="Startup settings", footer="Secrets are never shown again &middot; empty = keep")


def saved_page(results: list[tuple[str, bool, str]]) -> str:
    rows = "".join(f'<li><span class="{"okc" if ok else "bad"}">{"&#10003;" if ok else "&#10005;"}</span><span><b>{e(k)}</b>'
                   f'{"" if ok else " - " + e(msg)}</span></li>' for k, ok, msg in results)
    return layout(f"""<div class="card center"><div class="big">&#128640;</div><h2>Saved. Deploying now.</h2>
<p class="sub">Render is redeploying (1-2 minutes). This page comes back by itself; reload it then.</p></div>
<div class="card" style="margin-top:14px"><ul class="list">{rows}</ul></div>""", subtitle="Startup settings")


def manual_page(pairs: dict, msg: str = "") -> str:
    items = "".join(kv(k, v, i) for i, (k, v) in enumerate(pairs.items()))
    return layout(f"""<div class="card"><h2>Save these in Render</h2>{alert(msg, "warn") if msg else ""}
<p class="sub">Render dashboard, your service, <b>Environment</b>, add each variable, save. Add <code>RENDER_API_KEY</code>
once and this page saves everything for you next time.</p>{items}
<p class="hint"><a href="/admin/settings">Back</a></p></div>""", subtitle="Startup settings")


async def save(form: dict) -> str:
    pairs, err = collect(form)
    if err:
        return page(err)
    if not pairs:
        return page("Nothing changed.", "info")
    if not render_ready():
        return manual_page(pairs)
    results = await asyncio.to_thread(_wl().save_to_render, pairs)
    if all(ok for _, ok, _ in results):
        return saved_page(results)
    bad = "; ".join(f"{k}: {m}" for k, ok, m in results if not ok)
    return manual_page(pairs, f"Automatic save failed ({bad}). Check RENDER_API_KEY, or paste the values below.")


def _force_setup() -> list[tuple[str, bool, str]]:
    wl = _wl()
    out: list[tuple[str, bool, str]] = []
    try:
        wl.render_call("PUT", f"/services/{wl.RENDER_SERVICE}/env-vars/FORCE_SETUP", {"value": "1"})
        out.append(("FORCE_SETUP", True, ""))
        wl.render_call("POST", f"/services/{wl.RENDER_SERVICE}/deploys", {"clearCache": "do_not_clear"})
        out.append(("Deploy started", True, ""))
    except Exception as exc:
        out.append(("Render API", False, exc.__class__.__name__))
    return out


async def relogin() -> str:
    if not render_ready():
        return manual_page({"FORCE_SETUP": "1"}, "RENDER_API_KEY is not set. Add FORCE_SETUP=1 in Render -> Environment "
                                                  "(the service restarts into the login panel).")
    results = await asyncio.to_thread(_force_setup)
    if all(ok for _, ok, _ in results):
        return saved_page(results)
    return manual_page({"FORCE_SETUP": "1"}, "Could not change Render automatically. Add FORCE_SETUP=1 by hand.")
