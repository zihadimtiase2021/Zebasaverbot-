"""Control-bot side of the multi-account feature: /login, /accounts, /logout, /terminate, /resume <key>."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from telethon import Button, errors, events

from unified_core import pyro_login as pl
from unified_core.config import settings
from unified_core.utils import esc, short_error
from unified_services.accounts import AccountError, AccountManager

log = logging.getLogger("zeba.accounts.bot")

LOGIN_TTL = 300          # seconds the admin has to finish a login
MAX_PENDING_LOGINS = 3   # half-finished logins at the same time
MAX_TRIES = 3            # wrong code / password attempts

ACCOUNTS_HELP = (
    "👥 <b>Accounts</b>\n"
    "• <code>/login</code> — add another Telegram account (APP ID → APP HASH → phone → code → 2FA password) · <code>/resend</code> if no code came\n"
    "• <code>/accounts</code> — list · buttons to log out\n"
    "• <code>/logout 2</code> — stop account no. 2 (its session stays valid, you get the key)\n"
    "• <code>/resume &lt;key&gt;</code> — log an account back in with its session key, no code needed\n"
    "• <code>/terminate 2</code> — really end the Telegram session (the key stops working)"
)


def parse_phone(text: str) -> str | None:
    t = re.sub(r"[\s\-().]", "", text or "")
    if re.fullmatch(r"\+?\d{7,15}", t):
        return t if t.startswith("+") else "+" + t
    return None


def parse_code(text: str) -> str | None:
    digits = re.sub(r"\D", "", text or "")
    return digits if 4 <= len(digits) <= 8 else None


def mask_phone(phone: str) -> str:
    p = (phone or "").lstrip("+")
    if len(p) <= 6:
        return "+" + p if p else ""
    return f"+{p[:2]}•••{p[-4:]}"


def delivery_text(sent, phone: str) -> str:
    """Where Telegram actually put the code (from its answer) - so the admin knows where to look."""
    kind = getattr(sent, "kind", None) or type(getattr(sent, "type", None)).__name__
    where = {
        "SentCodeTypeApp": ("in the <b>Telegram app</b> of that account: open the chat named <b>“Telegram”</b> "
                            "(official, blue tick) on the phone/desktop where the account is already logged in. "
                            "It is <b>not</b> sent by SMS and it does not come to this bot chat"),
        "SentCodeTypeSms": f"by <b>SMS</b> to <code>{esc(mask_phone(phone))}</code>",
        "SentCodeTypeFragmentSms": "as a <b>Fragment</b> message (anonymous number)",
        "SentCodeTypeFirebaseSms": "by <b>SMS</b> (Google verification)",
        "SentCodeTypeCall": "by a <b>phone call</b> that reads the code out",
        "SentCodeTypeFlashCall": "as a <b>missed call</b> (the code is the last digits of the caller number)",
        "SentCodeTypeMissedCall": "as a <b>missed call</b> (the code is the last digits of the caller number)",
        "SentCodeTypeEmailCode": "to the <b>e-mail</b> address saved on that account",
    }.get(kind, "via Telegram")
    return f"📨 <b>Code sent</b> {where}."


def has_next(sent) -> bool:
    return getattr(sent, "next_type", None) is not None


async def _resend(client, phone: str = "", code_hash: str = ""):
    """Ask for the code another way. `client` is a PyroLogin, which remembers the phone and the code hash."""
    return await client.resend()


def code_buttons(sent) -> list:
    rows = []
    if has_next(sent):
        rows.append([Button.inline("📩 Send the code another way", b"acc:resend")])
    rows.append([Button.inline("✖️ Cancel", b"acc:cancel")])
    return rows


def code_prompt(sent, phone: str) -> str:
    return (delivery_text(sent, phone) + "\n\n"
            "Send the code <b>with spaces between the digits</b>, e.g. <code>1 2 3 4 5</code> — "
            "Telegram cancels a code that is sent as one plain number.\n"
            "<i>Nothing arrived? Wait a minute, then use the button below (or /resend). /cancel to stop.</i>")


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    s = int(time.time() - ts)
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} d"


def render_accounts(manager: AccountManager) -> tuple[str, list]:
    rows = manager.listing()
    lines = [f"👥 <b>Accounts</b> ({len(rows)}/{settings.MAX_ACCOUNTS})", ""]
    buttons: list = []
    for r in rows:
        who = esc(r["name"] or str(r["id"])) + (f" (@{esc(r['username'])})" if r["username"] else "")
        if r["primary"]:
            lines.append(f"{r['n']}. 🔒 <b>{who}</b> — primary · running")
            continue
        extra = " · ".join(x for x in (mask_phone(r["phone"]), f"up {_ago(r['since'])}") if x)
        env = " · from EXTRA_SESSIONS" if r["source"] == "env" else ""
        lines.append(f"{r['n']}. ✅ <b>{who}</b> — {extra}{env}")
        short = (r["name"] or str(r["id"]))[:16]
        buttons.append([Button.inline(f"🚪 Logout {short}", f"acc:out:{r['id']}".encode()),
                        Button.inline("🗑 Terminate", f"acc:term:{r['id']}".encode())])
    if len(rows) == 1:
        lines.append("\nNo extra account yet.")
    lines.append("\n<i>Logout keeps the session valid and gives you a key; /resume &lt;key&gt; brings it back.</i>")
    if len(rows) < settings.MAX_ACCOUNTS:
        buttons.append([Button.inline("➕ Add account", b"acc:login")])
    buttons.append([Button.inline("🔙 Back", b"menu")])
    return "\n".join(lines), buttons


def key_block(key: str) -> str:
    return (f"🔑 <b>Session key</b> — whoever has it controls the account, keep it secret:\n"
            f"<code>{esc(key)}</code>")


def error_text(exc: AccountError) -> str:
    text = f"❌ {esc(str(exc))}"
    if exc.key:
        text += "\n\n" + key_block(exc.key) + "\nNothing was lost: fix the problem, then <code>/resume</code> this key."
    return text


def register_account_commands(app, manager: AccountManager, is_admin) -> None:
    bot = app.control_bot
    assert bot is not None
    states: dict[int, dict] = {}   # admin id -> {"step": phone|code|password, "client", "phone", "hash", "tries", "ts"}

    # ------------------------------------------------------------------ helpers
    async def drop_state(uid: int, disconnect: bool = True) -> None:
        st = states.pop(uid, None)
        if st and st.get("timer") is not None and st["timer"] is not asyncio.current_task():
            st["timer"].cancel()
        if st and disconnect and st.get("client") is not None:
            try:
                await st["client"].disconnect()   # never log_out: nothing was signed in, or the key is kept
            except Exception:
                pass

    async def expire(uid: int, ts: float) -> None:
        await asyncio.sleep(LOGIN_TTL)
        st = states.get(uid)
        if st and st["ts"] == ts:
            await drop_state(uid)
            try:
                await bot.send_message(uid, "⌛ Login timed out. Start again with /login.", parse_mode="html")
            except Exception:
                pass

    def arm(uid: int) -> None:
        st = states[uid]
        st["ts"] = time.time()
        if st.get("timer") is not None:
            st["timer"].cancel()
        st["timer"] = asyncio.create_task(expire(uid, st["ts"]))

    async def wipe(event) -> None:
        """Codes, passwords and keys must not stay in the chat."""
        try:
            await event.delete()
        except Exception:
            pass

    async def say_accounts(event, edit: bool) -> None:
        text, buttons = render_accounts(manager)
        if edit:
            try:
                await event.edit(text, buttons=buttons)
            except errors.MessageNotModifiedError:
                pass
        else:
            await event.respond(text, buttons=buttons)

    async def begin_login(event) -> None:
        uid = event.sender_id
        if manager.total >= settings.MAX_ACCOUNTS:
            return await event.respond(f"❌ Account limit reached ({settings.MAX_ACCOUNTS}). Log one out first.")
        if uid not in states and len(states) >= MAX_PENDING_LOGINS:
            return await event.respond("⏳ Too many logins in progress - try again in a few minutes.")
        await drop_state(uid)
        states[uid] = {"step": "api_id", "client": None, "phone": "", "hash": "", "tries": 0, "api_id": 0, "api_hash": ""}
        arm(uid)
        await event.respond(
            "1️⃣ <b>Add an account</b>\n\nSend your <b>APP ID</b> (my.telegram.org → API development tools).\n"
            "Send <code>-</code> to use this bot's own API instead.\n<i>/cancel to stop.</i>")

    async def finish(event, uid: int) -> None:
        st = states.get(uid)
        login_client, phone = st["client"], st["phone"]
        await drop_state(uid, disconnect=False)   # the client now belongs to the manager (or is closed below)
        # Login done with the plain client; run the account on a normal long-running client with the same key.
        try:
            key = await login_client.export_session()
        except Exception as exc:
            await login_client.disconnect()
            return await event.respond(f"❌ Logged in, but the session could not be exported: {esc(short_error(exc))}")
        try:
            await login_client.disconnect()
        except Exception:
            pass
        client = manager.new_client(key, api_id=st.get("api_id", 0), api_hash=st.get("api_hash", ""))
        try:
            await client.connect()
        except Exception as exc:
            return await event.respond(f"❌ Logged in, but could not reconnect: {esc(short_error(exc))}\n\n"
                                       + key_block(key) + "\nUse <code>/resume</code> with this key.")
        status = await event.respond("⏳ Starting the account…")
        try:
            inst = await manager.add_client(client, phone=phone)
        except AccountError as exc:
            return await status.edit(error_text(exc))
        n = list(manager.instances).index(inst.me_id) + 2
        await status.edit(
            f"✅ <b>Logged in: {esc(inst.name)}</b>" + (f" (@{esc(inst.username)})" if inst.username else "")
            + f" · no. {n}\n\n" + key_block(inst.session)
            + f"\n\n• Log out from here: <code>/logout {n}</code>\n"
              "• Log in again any time: <code>/resume &lt;key&gt;</code>\n"
              "<i>Save the key somewhere safe (e.g. Saved Messages). Anyone who has it can use the account.</i>")

    async def handle_step(event, st: dict, text: str | None = None) -> None:
        uid = event.sender_id
        text = (event.raw_text or "") if text is None else text
        step = st["step"]

        if step == "api_id":
            t = text.strip()
            if t in ("-", "0", "default"):
                st.update(step="phone", api_id=0, api_hash="")
                arm(uid)
                return await event.respond("3️⃣ Send the <b>phone number</b> with country code, e.g. "
                                           "<code>+8801XXXXXXXXX</code>")
            if parse_phone(t) and not t.isdigit():   # a phone number sent right away: use the bot's own API
                st.update(step="phone", api_id=0, api_hash="")
                step = "phone"
                text = t
            elif not t.isdigit() or not (1 <= int(t) <= 2147483647) or len(t) > 9:
                hint = (" That looks like a <b>phone number</b> - the phone comes later (step 3)."
                        if t.isdigit() and len(t) >= 10 else "")
                return await event.respond("❌ The APP ID is a number of 5-9 digits (my.telegram.org → API development "
                                           "tools), e.g. <code>1234567</code>." + hint
                                           + "\nSend the APP ID again, or <code>-</code> to use the bot's own API.")
            else:
                st.update(step="api_hash", api_id=int(t))
                await wipe(event)
                arm(uid)
                return await event.respond("2️⃣ Send the <b>APP HASH</b> (the message is deleted right away).")

        if step == "api_hash":
            h = text.strip()
            if not re.fullmatch(r"[0-9a-fA-F]{32}", h):
                return await event.respond("❌ The APP HASH is 32 letters/digits (0-9, a-f). Send it again.")
            st.update(step="phone", api_hash=h)
            await wipe(event)
            arm(uid)
            return await event.respond("3️⃣ Send the <b>phone number</b> with country code, e.g. "
                                       "<code>+8801XXXXXXXXX</code>")

        if step == "phone":
            phone = parse_phone(text)
            if not phone:
                return await event.respond("❌ That does not look like a phone number. "
                                           "Example: <code>+8801XXXXXXXXX</code>")
            await wipe(event)
            status = await event.respond("⏳ Asking Telegram to send a code…")
            client = manager.new_login(api_id=st.get("api_id", 0), api_hash=st.get("api_hash", ""))
            try:
                await client.connect()
                sent = await client.send_code(phone)
            except pl.PhoneInvalid:
                await client.disconnect()
                await drop_state(uid)
                return await status.edit("❌ Telegram says this phone number is invalid. Try /login again.")
            except pl.PhoneBanned:
                await client.disconnect()
                await drop_state(uid)
                return await status.edit("❌ This number is banned by Telegram.")
            except pl.PhoneFlood:
                await client.disconnect()
                await drop_state(uid)
                return await status.edit("⏳ This number asked for too many codes. Telegram blocks new codes for a "
                                         "while (often several hours) - try again later.")
            except pl.FloodWait as exc:
                await client.disconnect()
                await drop_state(uid)
                return await status.edit(f"⏳ Telegram asks to wait {exc.seconds} s before another code. Try later.")
            except pl.ApiInvalid:
                await client.disconnect()
                await drop_state(uid)
                return await status.edit("❌ API_ID / API_HASH are not accepted by Telegram - check the settings.")
            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await drop_state(uid)
                log.warning("send_code_request failed: %s", exc)
                return await status.edit(f"❌ Could not request a code: {esc(short_error(exc))}")
            st.update(step="code", client=client, phone=phone, hash=sent.phone_code_hash, tries=0)
            arm(uid)
            log.info("login code requested for %s: %s", mask_phone(phone), sent.kind)
            return await status.edit(code_prompt(sent, phone), buttons=code_buttons(sent))

        if step == "code":
            code = parse_code(text)
            if not code:
                return await event.respond("❌ Send the digits of the code, e.g. <code>1 2 3 4 5</code>.")
            await wipe(event)
            try:
                await st["client"].sign_in(code=code)
            except pl.PasswordNeeded:
                st["step"] = "password"
                st["tries"] = 0
                arm(uid)
                return await event.respond("🔐 This account has <b>two-step verification</b>. "
                                           "Send the password (the message is deleted right away).")
            except pl.CodeInvalid:
                st["tries"] += 1
                if st["tries"] >= MAX_TRIES:
                    await drop_state(uid)
                    return await event.respond("❌ Wrong code too many times. Start again with /login.")
                return await event.respond(f"❌ Wrong code ({st['tries']}/{MAX_TRIES}). Send it again.")
            except pl.CodeExpired:
                await drop_state(uid)
                return await event.respond("⌛ The code expired. Start again with /login "
                                           "(send the digits with spaces next time).")
            except pl.NoAccount:
                await drop_state(uid)
                return await event.respond("❌ There is no Telegram account on this number. Create it in the "
                                           "Telegram app first.")
            except pl.FloodWait as exc:
                await drop_state(uid)
                return await event.respond(f"⏳ Too many attempts - wait {exc.seconds} s.")
            except Exception as exc:
                await drop_state(uid)
                log.warning("sign_in failed: %s", exc)
                return await event.respond(f"❌ Login failed: {esc(short_error(exc))}")
            return await finish(event, uid)

        if step == "password":
            if not text:
                return
            await wipe(event)
            try:
                await st["client"].sign_in(password=text)
            except pl.PasswordInvalid:
                st["tries"] += 1
                if st["tries"] >= MAX_TRIES:
                    await drop_state(uid)
                    return await event.respond("❌ Wrong password too many times. Start again with /login.")
                return await event.respond(f"❌ Wrong password ({st['tries']}/{MAX_TRIES}). Send it again.")
            except pl.FloodWait as exc:
                await drop_state(uid)
                return await event.respond(f"⏳ Too many attempts - wait {exc.seconds} s.")
            except Exception as exc:
                await drop_state(uid)
                log.warning("2FA sign_in failed: %s", exc)
                return await event.respond(f"❌ Login failed: {esc(short_error(exc))}")
            return await finish(event, uid)

    async def resend(event) -> None:
        uid = event.sender_id
        st = states.get(uid)
        if not st or st["step"] != "code":
            return await event.respond("ℹ️ No login is waiting for a code. Start with /login.")
        try:
            sent = await _resend(st["client"], st["phone"], st["hash"])
        except pl.FloodWait as exc:
            return await event.respond(f"⏳ Telegram asks to wait {exc.seconds} s before sending again.")
        except (pl.CodeExpired, pl.CodeUnavailable):
            await drop_state(uid)
            return await event.respond("❌ Telegram has no other way to send the code right now. "
                                       "Wait a few minutes and start again with /login.")
        except Exception as exc:
            log.warning("resend failed: %s", exc)
            return await event.respond(f"❌ Could not resend: {esc(short_error(exc))}")
        st["hash"] = getattr(sent, "phone_code_hash", st["hash"])
        st["tries"] = 0
        arm(uid)
        log.info("login code re-sent for %s: %s", mask_phone(st["phone"]), sent.kind)
        await event.respond("🔁 " + code_prompt(sent, st["phone"]), buttons=code_buttons(sent))

    async def do_logout(event, uid_target: int, terminate: bool, edit: bool) -> None:
        async def out(text, buttons=None):
            if edit:
                try:
                    return await event.edit(text, buttons=buttons)
                except errors.MessageNotModifiedError:
                    return
            return await event.respond(text, buttons=buttons)

        try:
            key, terminated, err = await manager.logout(uid_target, terminate=terminate)
        except AccountError as exc:
            return await out(f"❌ {esc(str(exc))}", [[Button.inline("👥 Accounts", b"acc:list")]])
        back = [[Button.inline("👥 Accounts", b"acc:list")]]
        if terminated:
            return await out("🗑 <b>Terminated.</b> The Telegram session was ended - its key no longer works.", back)
        text = "🚪 <b>Logged out</b> from this bot. The Telegram session is still valid.\n\n" + key_block(key)
        if terminate and err:
            text = (f"⚠️ Could not end the Telegram session ({esc(err)}). The account was stopped here only.\n\n"
                    + key_block(key))
        text += "\n\nLog in again: <code>/resume &lt;key&gt;</code>"
        await out(text, back)

    # ------------------------------------------------------------------ commands
    @bot.on(events.NewMessage(pattern=r"^/login(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_private))
    async def login_cmd(event):
        if not is_admin(event.sender_id):
            return
        await begin_login(event)
        arg = event.pattern_match.group(1)
        st = states.get(event.sender_id)
        if arg and st and st["step"] == "api_id":
            await handle_step(event, st, text=arg)

    @bot.on(events.NewMessage(pattern=r"^/cancel(?:@\w+)?$", func=lambda e: e.is_private))
    async def cancel_cmd(event):
        if not is_admin(event.sender_id):
            return
        if event.sender_id in states:
            await drop_state(event.sender_id)
            return await event.respond("✅ Login cancelled.")

    @bot.on(events.NewMessage(pattern=r"^/resend(?:@\w+)?$", func=lambda e: e.is_private))
    async def resend_cmd(event):
        if is_admin(event.sender_id):
            await resend(event)

    @bot.on(events.NewMessage(pattern=r"^/accounts(?:@\w+)?$", func=lambda e: e.is_private))
    async def accounts_cmd(event):
        if is_admin(event.sender_id):
            await say_accounts(event, edit=False)

    @bot.on(events.NewMessage(pattern=r"^/(logout|terminate)(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_private))
    async def logout_cmd(event):
        if not is_admin(event.sender_id):
            return
        terminate = event.pattern_match.group(1) == "terminate"
        ref = (event.pattern_match.group(2) or "").strip()
        if not ref:
            if len(manager.instances) == 1:
                ref = str(next(iter(manager.instances)))
            else:
                return await say_accounts(event, edit=False)
        uid = manager.resolve(ref)
        if uid is None:
            if ref == "1" or ref.lstrip("@") == str(manager.primary.me_id):
                return await event.respond("🔒 Account 1 is the primary one - it runs the bot and cannot be "
                                           "logged out from chat.")
            return await event.respond("❌ No such account. See /accounts for the numbers.")
        if terminate:
            return await event.respond(
                "🗑 <b>Terminate</b> ends the Telegram session for good — the key stops working.\nSure?",
                buttons=[[Button.inline("🗑 Yes, terminate", f"acc:termy:{uid}".encode()),
                          Button.inline("Cancel", b"acc:list")]])
        await do_logout(event, uid, terminate=False, edit=False)

    @bot.on(events.NewMessage(pattern=r"^/resume(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_private))
    async def resume_cmd(event):
        if not is_admin(event.sender_id):
            return
        key = event.pattern_match.group(1)
        if not key:
            return await event.respond("ℹ️ Usage: <code>/resume &lt;session key&gt;</code>")
        await wipe(event)  # the key is a secret
        status = await event.respond("⏳ Checking the key…")
        try:
            inst = await manager.resume(key)
        except AccountError as exc:
            return await status.edit(error_text(exc))
        except Exception as exc:
            log.exception("resume failed")
            return await status.edit(f"❌ Could not log in: {esc(short_error(exc))}")
        n = list(manager.instances).index(inst.me_id) + 2
        await status.edit(
            f"✅ <b>Logged in again: {esc(inst.name)}</b>" + (f" (@{esc(inst.username)})" if inst.username else "")
            + f" · no. {n}\n\nLog out: <code>/logout {n}</code> · the same key keeps working.")

    @bot.on(events.NewMessage(func=lambda e: e.is_private and bool(e.raw_text) and not e.raw_text.startswith("/")
                              and e.sender_id in states))
    async def login_text(event):
        st = states.get(event.sender_id)
        if st is not None and is_admin(event.sender_id):
            await handle_step(event, st)

    # ------------------------------------------------------------------ buttons
    async def admin_only(event) -> bool:
        if is_admin(event.sender_id):
            return True
        await event.answer("Unauthorized", alert=True)
        return False

    @bot.on(events.CallbackQuery(data=b"acc:list"))
    async def list_cb(event):
        if await admin_only(event):
            await say_accounts(event, edit=True)

    @bot.on(events.CallbackQuery(data=b"acc:resend"))
    async def resend_cb(event):
        if await admin_only(event):
            await event.answer("Sending…")
            await resend(event)

    @bot.on(events.CallbackQuery(data=b"acc:cancel"))
    async def cancel_cb(event):
        if await admin_only(event):
            await drop_state(event.sender_id)
            await event.answer("Cancelled")
            await event.respond("✅ Login cancelled.")

    @bot.on(events.CallbackQuery(data=b"acc:login"))
    async def login_cb(event):
        if await admin_only(event):
            await event.answer()
            await begin_login(event)

    @bot.on(events.CallbackQuery(data=re.compile(rb"^acc:out:(\d+)$")))
    async def out_cb(event):
        if not await admin_only(event):
            return
        await event.answer("Logging out…")
        await do_logout(event, int(event.data_match.group(1)), terminate=False, edit=True)

    @bot.on(events.CallbackQuery(data=re.compile(rb"^acc:term:(\d+)$")))
    async def term_cb(event):
        if not await admin_only(event):
            return
        uid = int(event.data_match.group(1))
        await event.edit("🗑 <b>Terminate</b> ends the Telegram session for good — the key stops working.\nSure?",
                         buttons=[[Button.inline("🗑 Yes, terminate", f"acc:termy:{uid}".encode()),
                                   Button.inline("Cancel", b"acc:list")]])

    @bot.on(events.CallbackQuery(data=re.compile(rb"^acc:termy:(\d+)$")))
    async def termy_cb(event):
        if not await admin_only(event):
            return
        await event.answer("Terminating…")
        await do_logout(event, int(event.data_match.group(1)), terminate=True, edit=True)
