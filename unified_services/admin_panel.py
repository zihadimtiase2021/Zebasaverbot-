"""Web control panel of the running bot:  https://<your-service>.onrender.com/admin

Unlock with WEBHOOK_SECRET, then switch the target channel and turn the source link / comments (and a few more
caption options) on or off. Every change is live immediately and saved to Saved Messages (see runtime_config.py):
no redeploy, no restart. Startup settings (keys, tokens, workers) are on /admin/settings. Standard library only;
served by WebhookServer.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qs

from unified_core.webui import SPIN, alert, e, layout, pw_field
from unified_services.runtime_config import NUMBERS, STRINGS, TOGGLES, RuntimeConfig

log = logging.getLogger("zeba.admin")

COOKIE = "zeba_admin"
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; "
       "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

SCRIPT = """
const T=document.getElementById('toast');let tt;
function toast(m,k){T.textContent=m;T.className='toast '+(k||'');T.style.display='block';clearTimeout(tt);tt=setTimeout(()=>{T.style.display='none'},2600)}
async function post(p,d){try{const r=await fetch(p,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(d)});
let j={};try{j=await r.json()}catch(_){}return {ok:r.ok&&j.ok!==false,j}}catch(_){return {ok:false,j:{error:'Network error'}}}}
document.querySelectorAll('[data-toggle]').forEach(cb=>cb.addEventListener('change',async()=>{const v=cb.checked;
const r=await post('/admin/toggle',{key:cb.dataset.toggle,value:v?'1':'0'});if(r.ok)toast('Saved','ok');else{cb.checked=!v;toast(r.j.error||'Failed','err')}}));
document.querySelectorAll('[data-number]').forEach(i=>i.addEventListener('change',async()=>{const r=await post('/admin/number',{key:i.dataset.number,value:i.value});
if(r.ok){i.value=r.j.value;toast('Saved','ok')}else toast(r.j.error||'Failed','err')}));
const wf=document.getElementById('wf');
function wmPrev(){const i=document.getElementById('wmp');if(i)i.src='/admin/wmpreview?'+Date.now()}
if(wf)wf.addEventListener('submit',async ev=>{ev.preventDefault();const b=wf.querySelector('button');b.disabled=true;
const d={WM_TEXT:wf.wm_text.value,WM_FONT:wf.wm_font.value,WM_TEXT_COLOR:wf.wm_color.value,WM_BG_COLOR:wf.wm_nobg.checked?'none':wf.wm_bg.value,WM_POSITION:wf.wm_pos.value};
const r=await post('/admin/wm',d);b.disabled=false;if(r.ok){toast('Watermark saved','ok');wmPrev()}else toast(r.j.error||'Failed','err')});
document.querySelectorAll('[data-number^=WM_],[data-toggle^=WM_]').forEach(el=>el.addEventListener('change',()=>setTimeout(wmPrev,600)));
const tf=document.getElementById('tf');
if(tf)tf.addEventListener('submit',async ev=>{ev.preventDefault();const b=tf.querySelector('button');b.disabled=true;
const d={channel_pick:tf.channel_pick?tf.channel_pick.value:'',channel_manual:tf.channel_manual.value};
const r=await post('/admin/target',d);b.disabled=false;
if(r.ok){document.getElementById('cur').textContent=r.j.title;document.getElementById('curid').textContent=r.j.id;tf.channel_manual.value='';toast('Now posting to '+r.j.title,'ok')}
else toast(r.j.error||'Failed','err')});
"""


class AdminPanel:
    def __init__(self, runtime: RuntimeConfig, queue, secret: str) -> None:
        self.rt = runtime
        self.queue = queue
        self.secret = (secret or "").strip()
        self._fails: list[float] = []
        self.accounts = None   # AccountManager, set by unified_main once the control bot is up (/admin/accounts)
        from unified_services.admin_accounts import WebAccountLogin
        self._acc_login = WebAccountLogin(self)

    # ------------------------------------------------------------------ plumbing
    def _token(self) -> str:
        return hmac.new(self.secret.encode(), b"zeba-admin-panel-v1", hashlib.sha256).hexdigest()

    def _authed(self, headers: dict) -> bool:
        cookies = {}
        for part in headers.get("cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        return hmac.compare_digest(cookies.get(COOKIE, "").encode(), self._token().encode())

    @staticmethod
    def _html(body: str, status: int = 200, extra: dict | None = None):
        data = body.encode()
        hdr = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex",
               "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": CSP, **(extra or {})}
        return status, hdr, data

    @staticmethod
    def _json(obj: dict, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}, data

    def _redirect(self, to: str, extra: dict | None = None):
        return 303, {"Location": to, "Cache-Control": "no-store", **(extra or {})}, b""

    def _locked_out(self) -> bool:
        now = time.time()
        self._fails = [t for t in self._fails if now - t < 60]
        return len(self._fails) >= 8

    # ------------------------------------------------------------------ entry point
    async def handle(self, method: str, path: str, headers: dict, body: bytes, ip: str):
        try:
            return await self._handle(method, path.rstrip("/") or "/", headers, body)
        except Exception:
            log.exception("admin panel error")
            return self._json({"ok": False, "error": "internal error"}, 500)

    async def _handle(self, method: str, path: str, headers: dict, body: bytes):
        if not self.secret:
            return self._json({"ok": False, "error": "admin panel is disabled (set WEBHOOK_SECRET)"}, 404)
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "ignore"), keep_blank_values=True).items()}

        if path == "/admin/unlock" and method == "POST":
            if self._locked_out():
                return self._html(self._unlock_page("Too many attempts. Wait a minute."), 429)
            if not hmac.compare_digest(form.get("pw", "").encode(), self.secret.encode()):
                self._fails.append(time.time())
                await asyncio.sleep(1.5)
                return self._html(self._unlock_page("Wrong password."), 401)
            return self._redirect("/admin", {"Set-Cookie": f"{COOKIE}={self._token()}; Path=/admin; Max-Age=43200; HttpOnly; Secure; SameSite=Strict"})

        if not self._authed(headers):
            if method == "POST":
                return self._json({"ok": False, "error": "Locked. Reload the page and unlock again."}, 401)
            return self._html(self._unlock_page())

        if path == "/admin/accounts" or path.startswith("/admin/accounts/"):
            kind, html = await self._acc_login.handle(method, path, form)
            return self._html(html) if kind == "page" else self._redirect("/admin/accounts")
        if path == "/admin" and method == "GET":
            return self._html(await self._dashboard())
        if path == "/admin/settings" and method == "GET":
            from unified_services import admin_settings
            return self._html(admin_settings.page())
        if path == "/admin/settings/save" and method == "POST":
            from unified_services import admin_settings
            return self._html(await admin_settings.save(form))
        if path == "/admin/settings/relogin" and method == "POST":
            from unified_services import admin_settings
            return self._html(await admin_settings.relogin())
        if path == "/admin/wmpreview" and method == "GET":
            from unified_services.watermark import sample_preview
            data = await asyncio.to_thread(sample_preview, self.rt.cfg)
            return 200, {"Content-Type": "image/jpeg", "Cache-Control": "no-store"}, data
        if method != "POST":
            return self._json({"ok": False, "error": "not found"}, 404)

        if path == "/admin/logout":
            return self._redirect("/admin", {"Set-Cookie": f"{COOKIE}=; Path=/admin; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
        if path == "/admin/toggle":
            key = form.get("key", "")
            if key not in TOGGLES:
                return self._json({"ok": False, "error": "unknown setting"}, 400)
            await self.rt.set_toggle(key, form.get("value") == "1")
            return self._json({"ok": True})
        if path == "/admin/number":
            key = form.get("key", "")
            raw = (form.get("value") or "").strip()
            if key not in NUMBERS or not raw.lstrip("-").isdigit():
                return self._json({"ok": False, "error": "enter a whole number"}, 400)
            return self._json({"ok": True, "value": await self.rt.set_number(key, int(raw))})
        if path == "/admin/wm":
            vals = {k: form.get(k, "") for k in STRINGS if k in form}
            try:
                await self.rt.set_many(vals)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            return self._json({"ok": True})
        if path == "/admin/target":
            chan = (form.get("channel_manual") or "").strip() or (form.get("channel_pick") or "").strip()
            if not chan:
                return self._json({"ok": False, "error": "Choose a channel or enter its id / @username."}, 400)
            try:
                title = await self.rt.set_target(chan)
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            return self._json({"ok": True, "title": title, "id": self.rt.target_info()["id"]})
        return self._json({"ok": False, "error": "not found"}, 404)

    # ------------------------------------------------------------------ pages
    def _unlock_page(self, msg: str = "") -> str:
        return layout(f"""<div class="card"><h2>Control panel</h2>
<p class="sub">Enter your <b>WEBHOOK_SECRET</b> to manage the bot.</p>{alert(msg)}
<form method="post" action="/admin/unlock">{pw_field("pw", "Password", required=True, extra="autofocus")}
<button type="submit">Unlock {SPIN}</button></form></div>""", subtitle="Control panel")

    async def _dashboard(self) -> str:
        snap = self.rt.snapshot()
        q = self.queue.snapshot()
        try:
            chans = await asyncio.wait_for(self.rt.list_channels(), 8)
        except Exception:
            chans = []
        cur = snap["target"]
        opts = "".join(f'<option value="{e(c["id"])}"{" selected" if c["id"] == cur["id"] else ""}>{e(c["name"])}</option>'
                       for c in chans)
        picker = (f'<label>Channel</label><select name="channel_pick"><option value="">Choose a channel...</option>{opts}</select>'
                  if chans else '<p class="hint">No channel list available right now - enter the id below.</p>')
        def trow(key, label, desc):
            return (f'<div class="row"><div><div class="t">{e(label)}</div><div class="d">{e(desc)}</div></div>'
                    f'<label class="sw"><input type="checkbox" data-toggle="{key}" {"checked" if snap["toggles"][key] else ""}><span></span></label></div>')

        def nrow(key, label, lo, hi, desc):
            return (f'<div class="row"><div><div class="t">{e(label)}</div><div class="d">{e(desc)}</div></div>'
                    f'<input class="num" type="number" inputmode="numeric" min="{lo}" max="{hi}" value="{snap["numbers"][key]}" data-number="{key}"></div>')

        rows = "".join(trow(k, l, d) for k, (l, d) in TOGGLES.items() if not k.startswith("WM_"))
        nrows = "".join(nrow(k, l, lo, hi, f"Used when comments are on ({lo}-{hi}).")
                        for k, (l, lo, hi) in NUMBERS.items() if not k.startswith("WM_"))
        st = snap["strings"]
        from unified_services.watermark import POSITIONS, list_fonts
        fonts = "".join(f'<option value="{e(f)}">' for f in list_fonts(getattr(self.rt.cfg, "DATA_DIR", None))[:200])
        pos = "".join(f'<option value="{k}"{" selected" if st["WM_POSITION"] == k else ""}>{e(v)}</option>' for k, v in POSITIONS.items())
        nobg = st["WM_BG_COLOR"] == "none"
        wm_rows = "".join(trow(k, l, d) for k, (l, d) in TOGGLES.items() if k.startswith("WM_")) + "".join(
            nrow(k, l, lo, hi, f"{lo}-{hi}") for k, (l, lo, hi) in NUMBERS.items() if k.startswith("WM_"))
        wm_card = f"""<div class="card" style="margin-top:14px"><h2>Watermark</h2>
<p class="sub">Text watermark on photos / videos. Switch it on for the sources you want.</p>{wm_rows}
<form id="wf" data-ajax="1"><label>Text (new line allowed)</label><textarea name="wm_text" rows="2" maxlength="200">{e(st["WM_TEXT"])}</textarea>
<label>Font (name or font you uploaded with .wmfont)</label><input name="wm_font" list="fl" value="{e(st["WM_FONT"])}" autocomplete="off"><datalist id="fl">{fonts}</datalist>
<label>Text colour</label><input type="color" name="wm_color" value="{e(st["WM_TEXT_COLOR"])}">
<label>Background box colour</label><input type="color" name="wm_bg" value="{e(st["WM_BG_COLOR"] if not nobg else "#000000")}">
<label><input type="checkbox" style="width:auto" name="wm_nobg" {"checked" if nobg else ""}> No background box</label>
<label>Position</label><select name="wm_pos">{pos}</select>
<button type="submit">Save watermark {SPIN}</button></form>
<img id="wmp" src="/admin/wmpreview" alt="preview" style="width:100%;margin-top:12px;border-radius:10px"></div>"""
        stats = "".join(f"<div><b>{q.get(k, 0)}</b><small>{name}</small></div>"
                        for k, name in (("pending", "Queued"), ("active", "Active"), ("ok", "Done"), ("failed", "Failed")))
        body = f"""<div class="card"><div class="stat">{stats}</div></div>
<div class="card" style="margin-top:14px"><h2>Target channel</h2>
<p class="sub">Posting to <b id="cur">{e(cur["title"])}</b> <span class="pill" id="curid">{e(cur["id"])}</span></p>
<form id="tf" data-ajax="1">{picker}
<label>Or enter an id / @username / t.me link</label><input name="channel_manual" placeholder="-1001234567890 or @mychannel" autocomplete="off">
<p class="hint">The account must be an admin of the new channel. Posts already uploading finish in the old one. Comments need a linked discussion group.</p>
<button type="submit">Switch channel {SPIN}</button></form></div>
<div class="card" style="margin-top:14px"><h2>Post options</h2><p class="sub">Changes apply to the next post, instantly.</p>{rows}{nrows}</div>
{wm_card}
<div class="card" style="margin-top:14px"><h2>Accounts</h2><p class="sub">Add another Telegram account from the browser.</p>
<a href="/admin/accounts"><button class="ghost" type="button">Add / manage accounts</button></a></div>
<div class="card" style="margin-top:14px"><h2>Startup settings</h2><p class="sub">API keys, control bot token, admin ids, workers, X accounts, cookies, main account login.</p>
<a href="/admin/settings"><button class="ghost" type="button">Open startup settings</button></a></div>
<form method="post" action="/admin/logout"><button class="ghost" type="submit">Lock panel</button></form>"""
        return layout(body, subtitle="Control panel",
                      footer="Saved in your Telegram Saved Messages &middot; survives restarts &middot; no redeploy needed",
                      script=SCRIPT)
