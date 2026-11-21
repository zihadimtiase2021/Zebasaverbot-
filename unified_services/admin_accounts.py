"""Add a Telegram account from the web panel:  https://<your-service>/admin/accounts

Same login steps as the setup panel (scripts/web_login.py): pyrofork login object -> send_code -> sign_in
(code, then the 2FA password when asked). It is the fallback for the /login command of the control bot; the account
starts as soon as the code is accepted and you get the session key. Lives behind the /admin unlock.
"""
from __future__ import annotations

import asyncio
import logging
import time

from unified_core import pyro_login as pl
from unified_core.utils import short_error
from unified_core.webui import SPIN, alert, e, layout, pw_field

log = logging.getLogger("zeba.admin.accounts")

TTL = 300  # seconds to finish a login


class WebAccountLogin:
    def __init__(self, panel) -> None:
        self.panel = panel
        self.st: dict = {}

    @property
    def manager(self):
        return getattr(self.panel, "accounts", None)

    async def _drop(self) -> None:
        client = self.st.get("client")
        self.st = {}
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    @staticmethod
    async def _drop_client(client) -> None:
        try:
            await client.disconnect()
        except Exception:
            pass

    # ------------------------------------------------------------------ routing
    async def handle(self, method: str, path: str, form: dict):
        """Returns ("page", html) or ("redirect", None)."""
        if self.st and time.time() - self.st.get("ts", 0) > TTL:
            await self._drop()
        if method == "GET":
            return "page", self.page(self.st.pop("flash", "") if self.st else "")
        action = path.rsplit("/", 1)[-1]
        if self.manager is None:
            return "page", self.page("Accounts are not available (the control bot is off).")
        try:
            if action == "send":
                msg = await self._send(form)
            elif action == "resend":
                msg = await self._resend()
            elif action == "verify":
                msg = await self._verify(form)
            else:
                await self._drop()
                msg = ""
        except Exception as exc:
            log.warning("web account login failed: %s", exc)
            await self._drop()
            msg = f"Failed ({short_error(exc)}). Start again."
        if msg:
            if self.st:
                self.st["flash"] = msg
            else:
                return "page", self.page(msg)
        return "redirect", None

    # ------------------------------------------------------------------ steps
    async def _send(self, form: dict) -> str:
        from unified_services.account_bot import parse_phone

        phone = parse_phone(form.get("phone", ""))
        if not phone:
            return "Enter the number with country code, like +8801XXXXXXXXX."
        if self.manager.total >= self._limit():
            return "Account limit reached. Log one out first."
        api_id_raw, api_hash = form.get("api_id", "").strip(), form.get("api_hash", "").strip()
        if api_id_raw and not (api_id_raw.isdigit() and 1 <= int(api_id_raw) <= 2147483647 and api_hash):
            return "APP ID must be a number up to 2147483647 (5-9 digits) and APP HASH is needed with it (or leave both empty)."
        api_id = int(api_id_raw) if api_id_raw else 0
        await self._drop()
        client = self.manager.new_login(api_id=api_id, api_hash=api_hash)
        try:
            await client.connect()
            sent = await client.send_code(phone)
        except Exception as exc:
            try:
                await client.disconnect()
            except Exception:
                pass
            return f"Could not send the code ({exc.__class__.__name__}). Check the number and API_ID / API_HASH."
        self.st = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "sent": sent, "ts": time.time(),
                   "need_2fa": False,
                   "api_id": api_id, "api_hash": api_hash}
        log.info("web login code requested: %s", sent.kind)
        return ""

    async def _resend(self) -> str:
        from unified_services.account_bot import _resend

        if not self.st:
            return "Session expired. Start again."
        try:
            sent = await _resend(self.st["client"], self.st["phone"], self.st["hash"])
        except Exception as exc:
            return f"Could not resend ({exc.__class__.__name__}): Telegram has no other way right now."
        self.st.update(hash=getattr(sent, "phone_code_hash", self.st["hash"]), sent=sent, ts=time.time())
        return ""

    async def _verify(self, form: dict) -> str:
        from unified_services.accounts import AccountError

        if not self.st:
            return "Session expired. Start again."
        client = self.st["client"]
        code = "".join(ch for ch in form.get("code", "") if ch.isdigit())
        pw = form.get("pw2", "")
        try:
            if pw and not code:
                await client.sign_in(password=pw)
            else:
                await client.sign_in(code=code)
        except pl.PasswordNeeded:
            if not pw:
                self.st["need_2fa"] = True
                return "Two-step verification is on. Enter the account's password."
            try:
                await client.sign_in(password=pw)
            except pl.PasswordInvalid:
                return "Wrong two-step password."
        except pl.PasswordInvalid:
            return "Wrong two-step password."
        except pl.CodeInvalid:
            return "That code is wrong. Check it and try again."
        except pl.CodeExpired:
            await self._drop()
            return "The code expired. Start again."
        except pl.NoAccount:
            await self._drop()
            return "There is no Telegram account on this number. Create it in the Telegram app first."
        except pl.FloodWait as exc:
            await self._drop()
            return f"Too many attempts - wait {exc.seconds} s."

        phone, api_id, api_hash = self.st["phone"], self.st.get("api_id", 0), self.st.get("api_hash", "")
        try:
            key = await client.export_session()
        except Exception as exc:
            await self._drop_client(client)
            return f"Logged in, but the session could not be exported ({short_error(exc)}). Start again."
        self.st = {}
        try:
            await client.disconnect()
        except Exception:
            pass
        # run the account on a normal long-lived client with the same key (same as /resume)
        client = self.manager.new_client(key, api_id=api_id, api_hash=api_hash)
        try:
            await client.connect()
            inst = await self.manager.add_client(client, phone=phone)
        except AccountError as exc:
            return f"{exc}" + (f" Session key (use /resume in the bot): {exc.key}" if exc.key else "")
        except Exception as exc:
            return f"Logged in, but the account did not start ({short_error(exc)}). Session key: {key}"
        self.st = {"done": inst, "ts": time.time()}
        return ""

    def _limit(self) -> int:
        from unified_core.config import settings
        return settings.MAX_ACCOUNTS

    # ------------------------------------------------------------------ pages
    def page(self, msg: str = "") -> str:
        from unified_services.account_bot import delivery_text, has_next

        st = self.st
        flash = alert(msg) if msg else ""
        if st.get("done") is not None:
            inst = st["done"]
            body = (f'<div class="card"><h2>Logged in</h2>{alert("Account started: " + inst.name, "ok")}'
                    f'<p class="sub">Session key - keep it secret, use <b>/resume &lt;key&gt;</b> in the bot to log in again:</p>'
                    f'<textarea rows="4" readonly onclick="this.select()">{e(inst.session)}</textarea>'
                    f'<form method="post" action="/admin/accounts/reset"><button type="submit">Done</button></form></div>')
            return layout(body, subtitle="Accounts")
        if st.get("client") is not None:
            sent = st["sent"]
            where = delivery_text(sent, st["phone"])
            pw = (pw_field("pw2", "Two-step password", required=True, extra="autofocus") if st.get("need_2fa")
                  else pw_field("pw2", "Two-step password (only if the account has one)"))
            resend = ('<form method="post" action="/admin/accounts/resend"><button class="ghost" type="submit">'
                      'Send the code another way</button></form>') if has_next(sent) else ""
            body = (f'<div class="card"><h2>Enter the code</h2>{flash}<p class="sub">{where}</p>'
                    f'<form method="post" action="/admin/accounts/verify">'
                    f'<label>Login code</label><input name="code" inputmode="numeric" autocomplete="off" autofocus>'
                    f'{pw}<button type="submit">Log in {SPIN}</button></form>{resend}'
                    f'<form method="post" action="/admin/accounts/reset"><button class="ghost" type="submit">Cancel</button></form></div>')
            return layout(body, subtitle="Accounts")
        rows = ""
        if self.manager is not None:
            rows = "".join(f"<li>{e(r['n'])}. {e(r['name'] or r['id'])}{' (primary)' if r['primary'] else ''}</li>"
                           for r in self.manager.listing())
        body = (f'<div class="card"><h2>Add a Telegram account</h2>{flash}'
                f'<p class="sub">Phone number with country code. The code arrives in the Telegram app of that account '
                f'(chat "Telegram") or by SMS.</p>'
                f'<form method="post" action="/admin/accounts/send">'
                f'<label>APP ID (optional - empty = the bot\'s own)</label><input name="api_id" inputmode="numeric" autocomplete="off">'
                f'<label>APP HASH (optional)</label><input name="api_hash" autocomplete="off"><label>Phone</label>'
                f'<input name="phone" placeholder="+8801XXXXXXXXX" inputmode="tel" autocomplete="off" autofocus>'
                f'<button type="submit">Send code {SPIN}</button></form>'
                f'<ul>{rows}</ul><p class="hint"><a href="/admin">Back to the panel</a></p></div>')
        return layout(body, subtitle="Accounts")
