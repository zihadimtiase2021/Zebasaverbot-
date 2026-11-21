"""Control-bot side of the multi-account feature: /login, /accounts, /logout, /terminate, /resume <key>."""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import time

from telethon import Button, errors, events

from unified_core import pyro_login as pl
from unified_core.config import settings
from unified_core.utils import esc, flood_seconds, short_error
from unified_services import ui
from unified_services.accounts import AccountError, AccountManager, clean_key

log = logging.getLogger("zeba.accounts.bot")

LOGIN_TTL = 300          # seconds the admin has to finish a login
MAX_PENDING_LOGINS = 3   # half-finished logins at the same time
MAX_TRIES = 3            # wrong code / password attempts
NET_TIMEOUT = 60         # seconds one Telegram call of the login may take before it counts as failed
RESEND_COOLDOWN = 30     # seconds between two "send the code another way" requests (Telegram blocks numbers that spam it)
FLOOD_MAX_WAIT = 30      # a FloodWait on a bot reply up to this long is waited out; longer ones are given up
# our own commands - a 2FA password that merely starts with "/" is still a password
_OWN_COMMAND = re.compile(r"^/(?:login|cancel|resend|accounts|logout|terminate|resume|start)(?:@\w+)?(?:\s|$)", re.IGNORECASE)

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
    if re.fullmatch(r"\+?[0-9]{7,15}", t):
        return t if t.startswith("+") else "+" + t
    return None


def parse_code(text: str) -> str | None:
    digits = re.sub(r"[^0-9]", "", text or "")
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
    rows.append(ui.cancel_row(b"acc:cancel"))
    return rows


def code_prompt(sent, phone: str) -> str:
    return (delivery_text(sent, phone) + "\n\n"
            "Send the code <b>with spaces between the digits</b>, e.g. <code>1 2 3 4 5</code> — "
            "Telegram cancels a code that is sent as one plain number.\n"
            "<i>Nothing arrived? Wait a minute, then use the button below (or /resend). /cancel to stop.</i>")


def fmt_wait(seconds: int) -> str:
    seconds = max(1, int(seconds or 0))
    return f"{seconds} s" if seconds < 120 else f"{seconds // 60} min"


async def _flood_retry(factory, retries: int = 2):
    """Run `factory()` (a coroutine factory). A FloodWait of up to FLOOD_MAX_WAIT s is waited out and retried;
    anything else - and longer waits - is raised to the caller."""
    for attempt in range(retries + 1):
        try:
            return await factory()
        except Exception as exc:
            wait = flood_seconds(exc)
            if wait is None or attempt >= retries or wait > FLOOD_MAX_WAIT:
                raise
            log.warning("FloodWait %ss on a bot message - waiting", wait)
            await asyncio.sleep(wait + 1)


async def _net(coro):
    """One call to Telegram during a login: it may not hang forever (a hung call would block the admin's login)."""
    return await asyncio.wait_for(coro, NET_TIMEOUT)


async def _quiet(client) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), 10)
    except Exception:
        pass


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
    buttons.append(ui.nav_row(None, close=True))
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
    states: dict[int, dict] = {}   # admin id -> {"step": api_id|api_hash|phone|code|password, "client", "phone", "hash", "tries", "timer"}
    locks: dict[int, asyncio.Lock] = {}   # admin id -> serialises that admin's login steps (messages are handled concurrently)
    starting: set[str] = set()     # session keys being logged in right now: one key must never be connected twice
    leaving: set[int] = set()      # accounts being logged out right now (a double tap must not run it twice)

    def lock_of(uid: int) -> asyncio.Lock:
        lk = locks.get(uid)
        if lk is None:
            lk = locks[uid] = asyncio.Lock()
        return lk

    def live(uid: int, st: dict) -> bool:
        """False once the login was cancelled / timed out / restarted while a step was still waiting on Telegram."""
        return states.get(uid) is st

    # ------------------------------------------------------------------ Telegram I/O that never raises
    async def reply(event, text: str, **kw):
        """event.respond that survives FloodWait and any other send error (returns None when it really failed)."""
        try:
            return await _flood_retry(lambda: event.respond(text, **kw))
        except Exception as exc:
            log.warning("could not send a reply: %s", short_error(exc))
            return None

    async def edit(msg, event, text: str, **kw):
        """Edit `msg` (a Message, or a CallbackQuery event); if that is impossible send a NEW message instead - the text
        may carry a session key that would otherwise be lost."""
        if msg is None:
            return await reply(event, text, **kw)
        try:
            return await _flood_retry(lambda: msg.edit(text, **kw))
        except errors.MessageNotModifiedError:
            return msg
        except Exception as exc:
            log.info("edit failed (%s) - sending a new message", short_error(exc))
            return await reply(event, text, **kw)

    async def answer(event, *args, **kw) -> None:
        """Answer a button press. An expired query ('too old') must not abort the action behind the button."""
        try:
            await event.answer(*args, **kw)
        except Exception as exc:
            log.debug("callback answer failed: %s", exc)

    async def wipe(event) -> None:
        """Codes, passwords and keys must not stay in the chat."""
        try:
            await asyncio.wait_for(event.delete(), 10)
        except Exception:
            pass

    def guard(fn):
        """A crashing handler must tell the admin instead of dying silently inside Telethon's dispatcher."""
        @functools.wraps(fn)
        async def wrapper(event):
            try:
                await fn(event)
            except Exception as exc:
                log.exception("account handler %s crashed", fn.__name__)
                await reply(event, f"⚠️ Something went wrong ({esc(type(exc).__name__)}). Try again, or /cancel.")
        return wrapper

    # ------------------------------------------------------------------ login state
    async def drop_state(uid: int, st: dict | None = None, disconnect: bool = True) -> None:
        """Forget a login (`st`, default: the current one). Only removes the entry if it is still THAT login, so a late
        error handler can never wipe a login the admin has started again in the meantime."""
        cur = states.get(uid)
        st = st if st is not None else cur
        if st is None:
            return
        if cur is st:
            states.pop(uid, None)
        timer = st.get("timer")
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        client = st.get("client")
        if disconnect and client is not None:
            st["client"] = None
            await _quiet(client)   # never log_out: nothing was signed in, or the key is kept

    async def expire(uid: int, st: dict) -> None:
        await asyncio.sleep(LOGIN_TTL)
        if states.get(uid) is st and st.get("timer") is asyncio.current_task():
            await drop_state(uid, st)
            try:
                await _flood_retry(lambda: bot.send_message(uid, "⌛ Login timed out. Start again with /login.", parse_mode="html"))
            except Exception:
                pass

    def arm(uid: int) -> None:
        st = states.get(uid)
        if st is None:   # cancelled while a step was waiting on Telegram
            return
        old = st.get("timer")
        if old is not None and old is not asyncio.current_task():
            old.cancel()
        st["timer"] = asyncio.create_task(expire(uid, st))

    def number_of(inst) -> str | None:
        try:
            return str(list(manager.instances).index(inst.me_id) + 2)
        except ValueError:   # the account was dropped again between starting and this message
            return None

    async def say_accounts(event, edit_it: bool) -> None:
        text, buttons = render_accounts(manager)
        if edit_it:
            await edit(event, event, text, buttons=buttons)
        else:
            await reply(event, text, buttons=buttons)

    async def begin_login(event) -> None:
        uid = event.sender_id
        if manager.total >= settings.MAX_ACCOUNTS:
            return await reply(event, f"❌ Account limit reached ({settings.MAX_ACCOUNTS}). Log one out first.")
        if uid not in states and len(states) >= MAX_PENDING_LOGINS:
            return await reply(event, "⏳ Too many logins in progress - try again in a few minutes.")
        # swap synchronously (no await between check and set), then clean the old one up
        old = states.pop(uid, None)
        st = {"step": "api_id", "client": None, "phone": "", "hash": "", "tries": 0, "api_id": 0, "api_hash": "",
              "resent": 0.0, "timer": None}
        states[uid] = st
        arm(uid)
        if old is not None:
            await drop_state(uid, old)
        await reply(event,
                    "1️⃣ <b>Add an account</b>\n\nSend your <b>APP ID</b> (my.telegram.org → API development tools).\n"
                    "Send <code>-</code> to use this bot's own API instead.\n<i>/cancel to stop.</i>")

    async def finish(event, uid: int, st: dict) -> None:
        login_client, phone = st["client"], st["phone"]
        api_id, api_hash = st.get("api_id", 0), st.get("api_hash", "")
        await drop_state(uid, st, disconnect=False)   # the client now belongs to the manager (or is closed below)
        if login_client is None:
            return
        status = await reply(event, "⏳ Finishing the login…")
        # Login done with the plain client; run the account on a normal long-running client with the same key.
        try:
            key = await _net(login_client.export_session())
        except Exception as exc:
            await _quiet(login_client)
            return await edit(status, event, f"❌ Logged in, but the session could not be exported: {esc(short_error(exc))}")
        await _quiet(login_client)
        if key in starting:
            return await edit(status, event, "⏳ That account is already being started.")
        starting.add(key)
        try:
            client = manager.new_client(key, api_id=api_id, api_hash=api_hash)
            try:
                await _net(client.connect())
            except Exception as exc:
                await _quiet(client)
                return await edit(status, event, f"❌ Logged in, but could not reconnect: {esc(short_error(exc))}\n\n"
                                  + key_block(key) + "\nUse <code>/resume</code> with this key.")
            await edit(status, event, "⏳ Starting the account…")
            try:
                inst = await manager.add_client(client, phone=phone)
            except AccountError as exc:
                return await edit(status, event, error_text(exc))
            except Exception as exc:   # add_client wraps everything, but the key must reach the admin whatever happens
                log.exception("add_client failed")
                await _quiet(client)
                return await edit(status, event, f"❌ Could not start the account: {esc(short_error(exc))}\n\n"
                                  + key_block(key) + "\nFix it, then <code>/resume</code> this key.")
        finally:
            starting.discard(key)
        n = number_of(inst)
        await edit(
            status, event,
            f"✅ <b>Logged in: {esc(inst.name)}</b>" + (f" (@{esc(inst.username)})" if inst.username else "")
            + (f" · no. {n}" if n else "") + "\n\n" + key_block(inst.session)
            + f"\n\n• Log out from here: <code>/logout {n or inst.me_id}</code>\n"
              "• Log in again any time: <code>/resume &lt;key&gt;</code>\n"
              "<i>Save the key somewhere safe (e.g. Saved Messages). Anyone who has it can use the account.</i>")

    async def handle_step(event, st: dict, text: str | None = None) -> None:
        """One message of a login. Steps of one admin run strictly one after the other: Telethon handles messages
        concurrently, so two quick messages (code sent twice, code + button...) used to race on the same client."""
        uid = event.sender_id
        arrived = st["step"]
        async with lock_of(uid):
            if not live(uid, st) or st["step"] != arrived:
                # cancelled / restarted / already answered by the message before this one
                if arrived != "api_id":
                    await wipe(event)
                return
            try:
                await _step(event, uid, st, text)
            except Exception as exc:
                log.exception("login step %s crashed", arrived)
                await drop_state(uid, st)
                await reply(event, f"⚠️ The login stopped unexpectedly ({esc(type(exc).__name__)}). Start again with /login.")

    async def _step(event, uid: int, st: dict, text: str | None) -> None:
        text = (event.raw_text or "") if text is None else text
        step = st["step"]
        if step in ("api_hash", "phone", "code", "password"):
            await wipe(event)   # secrets - also when the input turns out to be invalid (it may be the wrong secret)

        if step == "api_id":
            t = text.strip()
            if t in ("-", "0", "default"):
                st.update(step="phone", api_id=0, api_hash="")
                arm(uid)
                return await reply(event, "3️⃣ Send the <b>phone number</b> with country code, e.g. "
                                          "<code>+8801XXXXXXXXX</code>")
            plain_digits = re.fullmatch(r"[0-9]+", t) is not None   # str.isdigit() also accepts "²" and then int() crashes
            if parse_phone(t) and not plain_digits:   # a phone number sent right away: use the bot's own API
                st.update(step="phone", api_id=0, api_hash="")
                step = "phone"
                text = t
                await wipe(event)
            elif not plain_digits or len(t) > 9 or not (1 <= int(t) <= 2147483647):
                if re.fullmatch(r"[0-9a-fA-F]{32}", t):   # the APP HASH sent too early: do not leave it in the chat
                    await wipe(event)
                hint = (" That looks like a <b>phone number</b> - the phone comes later (step 3)."
                        if plain_digits and len(t) >= 10 else "")
                return await reply(event, "❌ The APP ID is a number of 5-9 digits (my.telegram.org → API development "
                                          "tools), e.g. <code>1234567</code>." + hint
                                          + "\nSend the APP ID again, or <code>-</code> to use the bot's own API.")
            else:
                st.update(step="api_hash", api_id=int(t))
                await wipe(event)
                arm(uid)
                return await reply(event, "2️⃣ Send the <b>APP HASH</b> (the message is deleted right away).")

        if step == "api_hash":
            h = text.strip()
            if not re.fullmatch(r"[0-9a-fA-F]{32}", h):
                return await reply(event, "❌ The APP HASH is 32 letters/digits (0-9, a-f). Send it again.")
            st.update(step="phone", api_hash=h)
            arm(uid)
            return await reply(event, "3️⃣ Send the <b>phone number</b> with country code, e.g. "
                                      "<code>+8801XXXXXXXXX</code>")

        if step == "phone":
            phone = parse_phone(text)
            if not phone:
                return await reply(event, "❌ That does not look like a phone number. "
                                          "Example: <code>+8801XXXXXXXXX</code>")
            status = await reply(event, "⏳ Asking Telegram to send a code…")
            client = manager.new_login(api_id=st.get("api_id", 0), api_hash=st.get("api_hash", ""))
            try:
                await _net(client.connect())
                sent = await _net(client.send_code(phone))
            except Exception as exc:
                await _quiet(client)
                if not live(uid, st):
                    return
                return await phone_failed(event, uid, st, status, exc)
            if not live(uid, st):   # cancelled while Telegram was answering: do not leave a connected login behind
                return await _quiet(client)
            st.update(step="code", client=client, phone=phone, hash=sent.phone_code_hash, tries=0, resent=time.time())
            arm(uid)
            log.info("login code requested for %s: %s", mask_phone(phone), sent.kind)
            return await edit(status, event, code_prompt(sent, phone), buttons=code_buttons(sent))

        if step in ("code", "password"):
            client = st.get("client")
            if client is None:
                return
            if step == "code":
                code = parse_code(text)
                if not code:
                    return await reply(event, "❌ Send the digits of the code, e.g. <code>1 2 3 4 5</code>.")
                call = client.sign_in(code=code)
            else:
                if not text:
                    return
                call = client.sign_in(password=text)
            try:
                await _net(call)
            except Exception as exc:
                if not live(uid, st):   # the login was cancelled / timed out while Telegram was working: stay quiet
                    return
                return await sign_in_failed(event, uid, st, exc, step)
            if not live(uid, st):
                return await _quiet(client)
            return await finish(event, uid, st)

    async def phone_failed(event, uid: int, st: dict, status, exc: Exception) -> None:
        if isinstance(exc, pl.PhoneInvalid):   # typo: let the admin simply send the number again
            arm(uid)
            return await edit(status, event, "❌ Telegram says this phone number is invalid. "
                                             "Send the number again (with country code), or /cancel.")
        if isinstance(exc, pl.FloodWait):
            if exc.seconds <= LOGIN_TTL:        # short wait: keep the typed API settings, just ask for the phone again
                arm(uid)
                return await edit(status, event, f"⏳ Telegram asks to wait {fmt_wait(exc.seconds)} before another code. "
                                                 "Then send the phone number again, or /cancel.")
            await drop_state(uid, st)
            return await edit(status, event, f"⏳ Telegram asks to wait {fmt_wait(exc.seconds)} before another code. "
                                             "Try /login later.")
        await drop_state(uid, st)
        if isinstance(exc, pl.PhoneBanned):
            return await edit(status, event, "❌ This number is banned by Telegram.")
        if isinstance(exc, pl.PhoneFlood):
            return await edit(status, event, "⏳ This number asked for too many codes. Telegram blocks new codes for a "
                                             "while (often several hours) - try again later.")
        if isinstance(exc, pl.ApiInvalid):
            return await edit(status, event, "❌ API_ID / API_HASH are not accepted by Telegram - check the settings.")
        if isinstance(exc, asyncio.TimeoutError):
            return await edit(status, event, "⌛ Telegram did not answer in time. Try /login again.")
        log.warning("send_code failed: %s", exc)
        await edit(status, event, f"❌ Could not request a code: {esc(short_error(exc))}")

    async def sign_in_failed(event, uid: int, st: dict, exc: Exception, step: str) -> None:
        if step == "code" and isinstance(exc, pl.PasswordNeeded):
            st["step"] = "password"
            st["tries"] = 0
            arm(uid)
            return await reply(event, "🔐 This account has <b>two-step verification</b>. "
                                      "Send the password (the message is deleted right away).")
        wrong = pl.CodeInvalid if step == "code" else pl.PasswordInvalid
        if isinstance(exc, wrong):
            st["tries"] += 1
            what = "code" if step == "code" else "password"
            if st["tries"] >= MAX_TRIES:
                await drop_state(uid, st)
                return await reply(event, f"❌ Wrong {what} too many times. Start again with /login.")
            return await reply(event, f"❌ Wrong {what} ({st['tries']}/{MAX_TRIES}). Send it again.")
        if isinstance(exc, pl.FloodWait):
            # Do not throw the login away: the code stays valid, the admin only has to wait (not a wrong attempt).
            if exc.seconds <= LOGIN_TTL:
                arm(uid)
                return await reply(event, f"⏳ Telegram asks to wait {fmt_wait(exc.seconds)} before another attempt. "
                                          "Send it again after that, or /cancel.")
            await drop_state(uid, st)
            return await reply(event, f"⏳ Too many attempts - Telegram asks to wait {fmt_wait(exc.seconds)}. Try /login later.")
        await drop_state(uid, st)
        if step == "code" and isinstance(exc, pl.CodeExpired):
            return await reply(event, "⌛ The code expired. Start again with /login (send the digits with spaces next time).")
        if step == "code" and isinstance(exc, pl.NoAccount):
            return await reply(event, "❌ There is no Telegram account on this number. Create it in the Telegram app first.")
        if isinstance(exc, asyncio.TimeoutError):
            return await reply(event, "⌛ Telegram did not answer in time. Start again with /login.")
        log.warning("%s failed: %s", "sign_in" if step == "code" else "2FA sign_in", exc)
        await reply(event, f"❌ Login failed: {esc(short_error(exc))}")

    async def resend(event) -> None:
        uid = event.sender_id
        st = states.get(uid)
        if not st or st["step"] != "code":
            return await reply(event, "ℹ️ No login is waiting for a code. Start with /login.")
        async with lock_of(uid):
            if not live(uid, st) or st["step"] != "code" or st.get("client") is None:
                return
            wait = RESEND_COOLDOWN - (time.time() - st.get("resent", 0.0))
            if wait > 0:   # hammering "send another way" is what gets a number blocked for hours
                return await reply(event, f"⏳ Wait {int(wait) + 1} s before asking for the code again.")
            try:
                sent = await _net(_resend(st["client"], st["phone"], st["hash"]))
            except Exception as exc:
                if not live(uid, st):
                    return
                if isinstance(exc, pl.FloodWait):
                    st["resent"] = time.time() + min(exc.seconds, LOGIN_TTL) - RESEND_COOLDOWN
                    return await reply(event, f"⏳ Telegram asks to wait {fmt_wait(exc.seconds)} before sending again.")
                if isinstance(exc, (pl.CodeExpired, pl.CodeUnavailable)):
                    await drop_state(uid, st)
                    return await reply(event, "❌ Telegram has no other way to send the code right now. "
                                              "Wait a few minutes and start again with /login.")
                log.warning("resend failed: %s", exc)
                return await reply(event, f"❌ Could not resend: {esc(short_error(exc))}")
            if not live(uid, st):
                return
            st["hash"] = getattr(sent, "phone_code_hash", st["hash"])
            st["tries"] = 0
            st["resent"] = time.time()
            arm(uid)
            log.info("login code re-sent for %s: %s", mask_phone(st["phone"]), sent.kind)
            await reply(event, "🔁 " + code_prompt(sent, st["phone"]), buttons=code_buttons(sent))

    async def do_logout(event, uid_target: int, terminate: bool, edit_it: bool) -> None:
        back = [ui.nav_row(b"acc:list", back_label="👥 Accounts")]

        async def out(text, buttons=None):
            if edit_it:
                return await edit(event, event, text, buttons=buttons)
            return await reply(event, text, buttons=buttons)

        if uid_target in leaving:   # double tap: the first one is on it and will show the key
            return await reply(event, "⏳ That account is already being logged out.")
        leaving.add(uid_target)
        try:
            key, terminated, err = await manager.logout(uid_target, terminate=terminate)
        except AccountError as exc:
            # A NEW message, never an edit: a late second tap must not overwrite the message that holds the session key.
            return await reply(event, f"❌ {esc(str(exc))}", buttons=back)
        except Exception as exc:
            log.exception("logout failed")
            return await reply(event, f"❌ Logout failed: {esc(short_error(exc))}\nCheck /accounts before trying again.",
                               buttons=back)
        finally:
            leaving.discard(uid_target)
        if terminated:
            return await out("🗑 <b>Terminated.</b> The Telegram session was ended - its key no longer works.", back)
        text = "🚪 <b>Logged out</b> from this bot. The Telegram session is still valid.\n\n" + key_block(key)
        if terminate and err:
            text = (f"⚠️ Could not end the Telegram session ({esc(err)}). The account was stopped here only.\n\n"
                    + key_block(key))
        text += "\n\nLog in again: <code>/resume &lt;key&gt;</code>"
        if await out(text, back) is None:
            log.error("the session key of account %s could not be delivered to the admin", uid_target)

    # ------------------------------------------------------------------ commands
    @bot.on(events.NewMessage(pattern=r"^/login(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_private))
    @guard
    async def login_cmd(event):
        if not is_admin(event.sender_id):
            return
        await begin_login(event)
        arg = event.pattern_match.group(1)
        st = states.get(event.sender_id)
        if arg and st and st["step"] == "api_id":
            await handle_step(event, st, text=arg)

    @bot.on(events.NewMessage(pattern=r"^/cancel(?:@\w+)?$", func=lambda e: e.is_private))
    @guard
    async def cancel_cmd(event):
        if not is_admin(event.sender_id):
            return
        if event.sender_id in states:
            await drop_state(event.sender_id)
            return await reply(event, "✅ Login cancelled.")

    @bot.on(events.NewMessage(pattern=r"^/resend(?:@\w+)?$", func=lambda e: e.is_private))
    @guard
    async def resend_cmd(event):
        if is_admin(event.sender_id):
            await resend(event)

    @bot.on(events.NewMessage(pattern=r"^/accounts(?:@\w+)?$", func=lambda e: e.is_private))
    @guard
    async def accounts_cmd(event):
        if is_admin(event.sender_id):
            await say_accounts(event, edit_it=False)

    @bot.on(events.NewMessage(pattern=r"^/(logout|terminate)(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_private))
    @guard
    async def logout_cmd(event):
        if not is_admin(event.sender_id):
            return
        terminate = event.pattern_match.group(1) == "terminate"
        ref = (event.pattern_match.group(2) or "").strip()
        if not ref:
            if len(manager.instances) == 1:
                ref = str(next(iter(manager.instances)))
            else:
                return await say_accounts(event, edit_it=False)
        uid = manager.resolve(ref)
        if uid is None:
            if ref == "1" or ref.lstrip("@") == str(manager.primary.me_id):
                return await reply(event, "🔒 Account 1 is the primary one - it runs the bot and cannot be "
                                          "logged out from chat.")
            return await reply(event, "❌ No such account. See /accounts for the numbers.")
        if terminate:
            return await reply(
                event, ui.head("🗑", "Terminate this account?", "This ends the Telegram session for good — the key stops working."),
                buttons=[ui.confirm_row("🗑 Yes, terminate", f"acc:termy:{uid}", b"acc:list")])
        await do_logout(event, uid, terminate=False, edit_it=False)

    @bot.on(events.NewMessage(pattern=r"(?s)^/resume(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_private))
    @guard
    async def resume_cmd(event):
        if not is_admin(event.sender_id):
            return
        key = (event.pattern_match.group(1) or "").strip()
        if not key:
            return await reply(event, "ℹ️ Usage: <code>/resume &lt;session key&gt;</code>")
        await wipe(event)  # the key is a secret
        ck = clean_key(key)
        if ck in starting:   # the same key twice at once = two connections on one auth key: Telegram kills that key
            return await reply(event, "⏳ That key is already being logged in.")
        status = await reply(event, "⏳ Checking the key…")
        starting.add(ck)
        try:
            inst = await manager.resume(key)
        except AccountError as exc:
            return await edit(status, event, error_text(exc))
        except Exception as exc:
            log.exception("resume failed")
            return await edit(status, event, f"❌ Could not log in: {esc(short_error(exc))}")
        finally:
            starting.discard(ck)
        n = number_of(inst)
        await edit(
            status, event,
            f"✅ <b>Logged in again: {esc(inst.name)}</b>" + (f" (@{esc(inst.username)})" if inst.username else "")
            + (f" · no. {n}" if n else "") + f"\n\nLog out: <code>/logout {n or inst.me_id}</code> · the same key keeps working.")

    def is_login_text(e) -> bool:
        if not (e.is_private and e.raw_text and e.sender_id in states):
            return False
        if not e.raw_text.startswith("/"):
            return True
        # a 2FA password may legitimately start with "/" - only our own commands are excluded
        st = states.get(e.sender_id)
        return bool(st and st["step"] == "password" and not _OWN_COMMAND.match(e.raw_text))

    @bot.on(events.NewMessage(func=is_login_text))
    @guard
    async def login_text(event):
        st = states.get(event.sender_id)
        if st is not None and is_admin(event.sender_id):
            await handle_step(event, st)

    # ------------------------------------------------------------------ buttons
    async def admin_only(event) -> bool:
        if is_admin(event.sender_id):
            return True
        await answer(event, "Unauthorized", alert=True)
        return False

    @bot.on(events.CallbackQuery(data=b"acc:list"))
    @guard
    async def list_cb(event):
        if await admin_only(event):
            await answer(event)
            await say_accounts(event, edit_it=True)

    @bot.on(events.CallbackQuery(data=b"acc:resend"))
    @guard
    async def resend_cb(event):
        if await admin_only(event):
            await answer(event, "Sending…")
            await resend(event)

    @bot.on(events.CallbackQuery(data=b"acc:cancel"))
    @guard
    async def cancel_cb(event):
        if await admin_only(event):
            await answer(event, "Cancelled")   # first: a slow disconnect must not let the query expire
            await drop_state(event.sender_id)
            await reply(event, "✅ Login cancelled.")

    @bot.on(events.CallbackQuery(data=b"acc:login"))
    @guard
    async def login_cb(event):
        if await admin_only(event):
            await answer(event)
            await begin_login(event)

    @bot.on(events.CallbackQuery(data=re.compile(rb"^acc:out:(\d+)$")))
    @guard
    async def out_cb(event):
        if not await admin_only(event):
            return
        await answer(event, "Logging out…")
        await do_logout(event, int(event.data_match.group(1)), terminate=False, edit_it=True)

    @bot.on(events.CallbackQuery(data=re.compile(rb"^acc:term:(\d+)$")))
    @guard
    async def term_cb(event):
        if not await admin_only(event):
            return
        await answer(event)
        uid = int(event.data_match.group(1))
        await edit(event, event, ui.head("🗑", "Terminate this account?", "This ends the Telegram session for good — the key stops working."),
                   buttons=[ui.confirm_row("🗑 Yes, terminate", f"acc:termy:{uid}", b"acc:list")])

    @bot.on(events.CallbackQuery(data=re.compile(rb"^acc:termy:(\d+)$")))
    @guard
    async def termy_cb(event):
        if not await admin_only(event):
            return
        await answer(event, "Terminating…")
        await do_logout(event, int(event.data_match.group(1)), terminate=True, edit_it=True)
