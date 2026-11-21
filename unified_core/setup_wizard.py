"""First-run setup wizard.

Asks for everything the bot needs (only what is missing), saves each answer to the env file the
moment it is given, logs in to Telegram, picks the channel and sets up X cookies / AI translation.

    python3 run.py            # runs this automatically when something is missing
    python3 run.py --setup    # walk through everything again (current values are the defaults)

Only the standard library is imported at module level, so it can run before `settings` is created.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable

from unified_core.paths import ENV_FILE, data_dir

PLACEHOLDER = re.compile(r"^(your[_\-\s]|<.*>$|changeme|xxx+$)", re.IGNORECASE)
COMPLETE_KEY = "ZEBA_SETUP_COMPLETE"

AI_PRESETS = {
    "1": ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    "2": ("OpenRouter - paid model via OpenAI-compatible API (for FREE models choose 7)", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    "3": ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "4": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash"),
    "5": ("Other OpenAI-compatible", "", "auto"),
    "6": ("Ollama Cloud (free API key, best model chosen automatically)", "ollama", "auto"),
    "7": ("OpenRouter FREE models (free API key, model chosen automatically)", "openrouter", "auto"),
}

# (env key, question, kind, default) - asked only if the user opts into advanced settings.
ADVANCED = [
    ("INCLUDE_ORIGINAL_CAPTION", "Keep the original caption below the translation?", "bool", "true"),
    ("INCLUDE_SOURCE_LINK", "Add a link to the source post?", "bool", "true"),
    ("INCLUDE_COMMENT_COUNT", "Show the comment count?", "bool", "true"),
    ("DELETE_TRIGGER_MESSAGE", "Delete the chat message that contained the link?", "bool", "true"),
    ("NOTIFY_SAVED_MESSAGES", "Send status notifications to Saved Messages?", "bool", "true"),
    ("MAX_COMMENTS", "Max comments/replies to repost under each post (0 = none)", "int", "20"),
    ("MAX_VIDEO_HEIGHT", "Max video height in pixels (720 / 1080 / 1440 / 2160)", "int", "1080"),
    ("MAX_CONCURRENCY", "How many links to process at the same time", "int", "4"),
]


class SetupError(Exception):
    """A problem the user can fix; printed without a traceback."""


class Cancelled(Exception):
    """The user closed the prompt (Ctrl+C / Ctrl+D)."""


# ------------------------------------------------------------------ env file
def _parse_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("#"):  # "KEY=   # just a comment"
        return ""
    if raw[:1] in "\"'" and raw[-1:] == raw[:1] and len(raw) >= 2:
        return raw[1:-1].replace('\\"', '"')
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def read_env_file(path: Path | None = None) -> dict[str, str]:
    path = path or ENV_FILE
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = _parse_value(v)
    return out


def effective(key: str, default: str = "") -> str:
    """Value the bot will actually see (real env wins over the file). Placeholders count as unset."""
    val = os.environ.get(key)
    if val is None:
        val = read_env_file().get(key, "")
    val = (val or "").strip()
    return default if (not val or PLACEHOLDER.match(val)) else val


def _fmt(value: str) -> str:
    return f'"{value.replace(chr(34), chr(92) + chr(34))}"' if re.search(r"[\s#\"']", value) else value


def save(key: str, value: str | int | bool) -> None:
    """Persist one setting (in place, atomically) and make it visible to this process."""
    if isinstance(value, bool):
        value = "true" if value else "false"
    value = str(value)
    path = ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.is_file() else [
        "# Zeba Saver Bot configuration - written by the setup wizard (safe to edit).",
    ]
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    new = f"{key}={_fmt(value)}"
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = new
            break
    else:
        lines.append(new)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    os.environ[key] = value


# ------------------------------------------------------------------ prompts
def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def say(text: str = "") -> None:
    print(text, flush=True)


def heading(text: str) -> None:
    say()
    say(f"=== {text} " + "=" * max(3, 60 - len(text)))


def ask(
    label: str, *, default: str = "", secret: bool = False, optional: bool = False,
    validate: Callable[[str], str] | None = None, hint: str = "",
) -> str:
    """Prompt until valid. `validate` returns the normalised value or raises ValueError(msg)."""
    if hint:
        say(hint)
    shown = f" [{'hidden' if secret and default else default}]" if default else ""
    suffix = " (Enter to skip)" if optional and not default else ""
    while True:
        try:
            raw = (getpass.getpass if secret else input)(f"{label}{shown}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        raw = raw or default
        if not raw:
            if optional:
                return ""
            say("  This value is required.")
            continue
        if validate:
            try:
                return validate(raw)
            except ValueError as exc:
                say(f"  Invalid: {exc}. Try again.")
                continue
        return raw


def yes_no(label: str, default: bool = True) -> bool:
    tail = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            raw = input(f"{label} {tail}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        if not raw:
            return default
        if raw in ("y", "yes", "1", "true"):
            return True
        if raw in ("n", "no", "0", "false"):
            return False


# ---------------------------------------------------------------- validators
def _v_api_id(v: str) -> str:
    if not v.isdigit():
        raise ValueError("API_ID is a number, e.g. 1234567")
    return v


def _v_api_hash(v: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", v):
        raise ValueError("API_HASH is 32 hex characters")
    return v.lower()


def _v_phone(v: str) -> str:
    v = re.sub(r"[\s\-()]", "", v)
    if not re.fullmatch(r"\+?\d{7,15}", v):
        raise ValueError("use international format, e.g. +8801712345678")
    return v if v.startswith("+") else "+" + v


def _v_bot_token(v: str) -> str:
    if not re.fullmatch(r"\d{5,}:[\w\-]{30,}", v):
        raise ValueError("a BotFather token looks like 123456789:AAH...")
    return v


def _v_ids(v: str) -> str:
    ids = [c for c in re.split(r"[,\s]+", v) if c]
    if not ids or not all(re.fullmatch(r"\d+", c) for c in ids):
        raise ValueError("comma-separated numeric user IDs")
    return ",".join(ids)


def _v_cookie(v: str) -> str:
    v = v.strip().strip("\"'")
    if len(v) < 20 or re.search(r"[\s;=]", v):
        raise ValueError("paste just the cookie VALUE (no 'name=' and no spaces)")
    return v


def _v_file(v: str) -> str:
    p = Path(v.strip().strip("\"'")).expanduser()
    if not p.is_file():
        raise ValueError(f"file not found: {p}")
    return str(p)


def _v_choice(*options: str) -> Callable[[str], str]:
    def check(v: str) -> str:
        if v not in options:
            raise ValueError("enter " + " or ".join(options))
        return v

    return check


def _v_number(v: str) -> str:
    if not v.isdigit():
        raise ValueError("a whole number")
    return v


def _v_username(v: str) -> str:
    v = v.strip().lstrip("@")
    if not re.fullmatch(r"\w{1,30}", v):
        raise ValueError("letters, digits and underscore only")
    return v


# --------------------------------------------------------------- telegram
def _client_kwargs() -> dict:
    # Must match unified_main so the session looks like one device.
    return dict(device_model="Desktop", system_version="Windows 10.0", app_version="4.14.8")


def session_file() -> Path:
    p = Path(effective("SESSION_NAME", "mau_saver_session"))
    base = p if p.is_absolute() else data_dir() / p
    return base.with_name(base.name + ".session")


async def pyro_login_prompt(*, desktop: bool = False) -> str:
    """Terminal login with pyrofork: phone -> code -> optional 2FA password.

    Returns the finished login as Telethon StringSession text (the bot itself still runs on Telethon)."""
    from unified_core import pyro_login as pl

    heading("Telegram login")
    say("Log in with the Telegram account that will repost media (it acts as your userbot).")
    api_id, api_hash = effective("API_ID"), effective("API_HASH")
    if not api_id or not api_hash:
        raise SetupError("API_ID / API_HASH are not set (get them at https://my.telegram.org)")
    try:
        phone = ask("Phone number (with country code)", validate=_v_phone)
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None
    login = pl.PyroLogin(int(api_id), api_hash, desktop=desktop)
    try:
        try:
            sent = await login.send_code(phone)
        except pl.FloodWait as exc:
            raise SetupError(f"Telegram asks you to wait {exc.seconds} seconds before another login attempt.") from exc
        except pl.PhoneBanned as exc:
            raise SetupError("Telegram says this phone number is banned.") from exc
        except pl.PhoneInvalid as exc:
            raise SetupError("Telegram says this phone number is invalid.") from exc
        except pl.PhoneFlood as exc:
            raise SetupError("This number asked for too many codes. Try again in a few hours.") from exc
        except pl.ApiInvalid as exc:
            raise SetupError("API_ID / API_HASH are not accepted by Telegram - check them.") from exc
        say(f"  Code sent {pl.describe(sent)}.")

        authorized = needs_password = False
        try:
            for _ in range(5):
                raw = ask("Login code (digits; type 'resend' to get it another way)")
                if raw.strip().lower().startswith("resend"):
                    try:
                        sent = await login.resend()
                    except (pl.CodeUnavailable, pl.CodeExpired) as exc:
                        raise SetupError("Telegram has no other way to send the code right now. Try later.") from exc
                    except pl.FloodWait as exc:
                        raise SetupError(f"Telegram asks you to wait {exc.seconds} seconds.") from exc
                    say(f"  Code sent again {pl.describe(sent)}.")
                    continue
                try:
                    await login.sign_in(code="".join(ch for ch in raw if ch.isdigit()))
                    authorized = True
                    break
                except pl.PasswordNeeded:
                    needs_password = True
                    break
                except pl.CodeInvalid:
                    say("  That code is wrong - try again.")
                except pl.CodeExpired as exc:
                    raise SetupError("The code expired. Run the login again.") from exc
                except pl.NoAccount as exc:
                    raise SetupError("There is no Telegram account on this number. Create it in the Telegram app first.") from exc
                except pl.FloodWait as exc:
                    raise SetupError(f"Too many attempts - wait {exc.seconds} seconds.") from exc
            if needs_password:
                for _ in range(5):
                    pw = ask("Two-step verification password", secret=True)
                    try:
                        await login.sign_in(password=pw)
                        authorized = True
                        break
                    except pl.PasswordInvalid:
                        say("  Wrong password - try again.")
                    except pl.FloodWait as exc:
                        raise SetupError(f"Too many attempts - wait {exc.seconds} seconds.") from exc
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        if not authorized:
            raise SetupError("Too many wrong attempts. Run the login again.")
        text = await login.export_session()
    finally:
        await login.disconnect()
    say("  Logged in.")
    return text


def telethon_session_for(old_client, text: str):
    """A Telethon session object holding the login `text`, of the same kind (string / file) as old_client's."""
    from telethon.sessions import SQLiteSession, StringSession

    fresh = StringSession(text)
    if isinstance(old_client.session, StringSession):
        return fresh
    sess = SQLiteSession(old_client.session.filename)   # the .session file: its old (unauthorized) key is replaced
    sess.set_dc(fresh.dc_id, fresh.server_address, fresh.port)
    sess.auth_key = fresh.auth_key
    sess.save()
    return sess


async def login_interactive(client, rebuild):
    """Log in with pyrofork, then hand the result to Telethon.

    `client` is the Telethon client that turned out not to be authorized; `rebuild(session)` must return a new
    client (same settings) on the given session. Returns that new client, connected and authorized - use it from
    now on (a Telethon client reads its auth key only when it is created, so it cannot be patched in place)."""
    text = await pyro_login_prompt()
    session = telethon_session_for(client, text)   # needs the old client's session: build before disconnecting
    try:
        await client.disconnect()
    except Exception:
        pass
    new = rebuild(session)
    await new.connect()
    if not await new.is_user_authorized():
        raise SetupError("Login worked, but the bot could not use the session. Run the login again.")
    return new


def _can_post(entity) -> bool:
    if getattr(entity, "creator", False):
        return True
    rights = getattr(entity, "admin_rights", None)
    if getattr(entity, "broadcast", False):
        return bool(rights and getattr(rights, "post_messages", False))
    banned = getattr(entity, "default_banned_rights", None)  # groups: anyone may post unless banned
    return bool(rights or not (banned and getattr(banned, "send_messages", False)))


async def choose_channel(client) -> int:
    """Interactive picker. Saves and returns TARGET_CHANNEL (marked id)."""
    from telethon import utils
    from telethon.tl.functions.channels import CreateChannelRequest
    from telethon.tl.types import Channel

    from unified_core.tg import parse_chat_ref, resolve_peer

    heading("Target channel")
    say("Where should downloaded media be posted? (your account must be able to post there)")
    options: list = []
    async for d in client.iter_dialogs(limit=400):
        e = d.entity
        if isinstance(e, Channel) and _can_post(e):
            options.append(e)
    options.sort(key=lambda e: (not e.broadcast, (e.title or "").lower()))
    options = options[:25]
    for i, e in enumerate(options, 1):
        handle = f"@{e.username}" if getattr(e, "username", None) else "private"
        say(f"  {i:>2}) {e.title}  [{handle}, id {utils.get_peer_id(e)}]")
    say("   n) Create a NEW private channel for me")
    say("   Or paste an @username / t.me link / numeric ID")

    while True:
        choice = ask("Your choice")
        entity = None
        try:
            if choice.lower() == "n":
                title = ask("Name for the new channel", default="Zeba Saver")
                res = await client(CreateChannelRequest(title=title, about="Saved by Zeba Saver Bot",
                                                        broadcast=True, megagroup=False))
                entity = res.chats[0]
                say(f"  Created channel '{title}'.")
            elif choice.isdigit() and 1 <= int(choice) <= len(options):
                entity = options[int(choice) - 1]
            else:
                entity = await resolve_peer(client, parse_chat_ref(choice))
        except (ValueError, RuntimeError) as exc:
            say(f"  Could not use that: {exc}")
            continue
        except Exception as exc:
            say(f"  Telegram error: {exc}")
            continue
        if isinstance(entity, Channel) and not _can_post(entity):
            say("  Warning: your account does not look able to post in that chat. Make it an admin there.")
            if not yes_no("Use it anyway?", default=False):
                continue
        break

    chat_id = utils.get_peer_id(entity)
    save("TARGET_CHANNEL", chat_id)
    say(f"  Target channel: {getattr(entity, 'title', chat_id)} (id {chat_id})")
    await _detect_discussion(client, entity)
    return chat_id


async def _detect_discussion(client, entity) -> None:
    from telethon.tl.functions.channels import GetFullChannelRequest

    if not getattr(entity, "broadcast", False):
        return
    try:
        full = await client(GetFullChannelRequest(entity))
        linked = getattr(full.full_chat, "linked_chat_id", None)
    except Exception:
        linked = None
    if linked:
        save("COMMENT_CHAT", int(f"-100{linked}"))
        say("  Discussion group detected - comments will be posted there automatically.")
    else:
        say("  This channel has no discussion group, so source comments cannot be reposted.")
        say("  (Optional: link one in channel settings -> Discussion, then run  python3 run.py --setup)")


async def telegram_stage(first: bool, force: bool) -> None:
    from telethon import TelegramClient, utils

    from unified_core.tg import resolve_peer

    session = session_file()
    session.parent.mkdir(parents=True, exist_ok=True)
    def make(sess):
        return TelegramClient(sess, int(effective("API_ID")), effective("API_HASH"), **_client_kwargs())

    client = make(str(session.with_suffix("")))
    await client.connect()
    try:
        if not await client.is_user_authorized():
            client = await login_interactive(client, make)
        me = await client.get_me()
        say(f"Telegram account: {me.first_name} (id {me.id})")

        target = None
        raw = effective("TARGET_CHANNEL")
        if raw and not force:
            try:
                target = await resolve_peer(client, int(raw))
                say(f"Target channel OK: {getattr(target, 'title', raw)}")
            except (RuntimeError, ValueError):
                say("The saved TARGET_CHANNEL could not be opened with this account.")
        if target is None:
            await choose_channel(client)

        if not effective("ADMIN_IDS"):
            save("ADMIN_IDS", me.id)
        if (first or force) and not effective("COMMENT_CHAT"):
            ref = ask("Discussion group for comments - @username / ID (optional)", optional=True)
            if ref:
                from unified_core.tg import parse_chat_ref

                try:
                    ent = await resolve_peer(client, parse_chat_ref(ref))
                    save("COMMENT_CHAT", utils.get_peer_id(ent))
                except (ValueError, RuntimeError) as exc:
                    say(f"  Skipped: {exc}")
    finally:
        await client.disconnect()


# ---------------------------------------------------------- optional stages
def control_bot_stage(force: bool) -> None:
    if effective("CONTROL_BOT_TOKEN") and not force:
        return
    heading("Control bot (optional)")
    say("A private admin panel bot (stats, auto-mode toggle, restart). Create one with @BotFather.")
    tok = ask("Bot token", default=effective("CONTROL_BOT_TOKEN"), optional=True, validate=_v_bot_token)
    save("CONTROL_BOT_TOKEN", tok)
    if tok:
        ids = ask("Extra admin user IDs (comma-separated, Enter = only you)",
                  optional=True, validate=_v_ids)
        if ids:
            mine = effective("ADMIN_IDS")
            save("ADMIN_IDS", ",".join(dict.fromkeys([*(mine.split(",") if mine else []), *ids.split(",")])))


def _test_ai(key: str, base: str, model: str) -> str:
    """Returns '' when the key works, otherwise the error text."""
    async def go() -> str:
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=key, base_url=base, timeout=25, max_retries=0)
            await client.chat.completions.create(
                model=model, max_tokens=8, messages=[{"role": "user", "content": "Reply with OK"}])
            return ""
        except Exception as exc:
            return str(exc).replace("\n", " ")[:200]

    return asyncio.run(go())


def ai_stage(force: bool) -> None:
    if (effective("OPENAI_API_KEY") or effective("OLLAMA_API_KEY") or effective("OPENROUTER_API_KEY")
            or effective("TRANSLATE_ENABLED").lower() == "false") and not force:
        return
    heading("AI Bangla caption translation (optional)")
    if not yes_no("Translate captions to Bangla with an AI model?", default=True):
        save("TRANSLATE_ENABLED", False)
        return
    say("Provider (any OpenAI-compatible API):")
    for k, (name, _, _) in AI_PRESETS.items():
        say(f"  {k}) {name}")
    choice = ask("Choose", default="1", validate=_v_choice(*AI_PRESETS))
    _, base, model = AI_PRESETS[choice]
    if base == "ollama":
        return _ollama_stage()
    if base == "openrouter":
        return _openrouter_stage()
    if not base:
        base = ask("Base URL", default=effective("OPENAI_BASE_URL"))
    model = ask("Model name", default=effective("OPENAI_MODEL") if effective("OPENAI_MODEL") not in ("", "auto")
                else model)
    while True:
        key = ask("API key (input hidden)", secret=True, default=effective("OPENAI_API_KEY"))
        say("  Testing the key...")
        err = _test_ai(key, base, model)
        if not err:
            say("  Works.")
            break
        say(f"  That did not work: {err}")
        if yes_no("Save these settings anyway?", default=False):
            break
    save("OPENAI_BASE_URL", base)
    save("OPENAI_MODEL", model)
    save("OPENAI_API_KEY", key)
    save("TRANSLATE_PROVIDER", "auto")
    save("TRANSLATE_ENABLED", True)


def _ollama_stage() -> None:
    from unified_services.ollama_cloud import OllamaChat

    say("  Create a free key at https://ollama.com/settings/keys (free plan = light usage; when the limit is")
    say("  reached, posts simply continue without translation until it resets).")
    while True:
        key = ask("Ollama API key (input hidden)", secret=True, default=effective("OLLAMA_API_KEY"))
        say("  Testing the key and picking the best model...")
        err, model, sample = asyncio.run(OllamaChat("https://ollama.com", key, "auto", timeout=60).check())
        if not err:
            say(f"  Works. Model picked automatically: {model}  ->  {sample[:50]}")
            break
        say(f"  That did not work: {err}")
        if yes_no("Save this key anyway?", default=False):
            break
    save("OLLAMA_API_KEY", key)
    save("OLLAMA_MODEL", "auto")
    save("TRANSLATE_PROVIDER", "auto")
    save("TRANSLATE_ENABLED", True)


def _openrouter_stage() -> None:
    from unified_services.openrouter import OpenRouterChat

    say("  Create a key at https://openrouter.ai/keys . Only FREE models (id ends in :free) are ever used.")
    say("  Free limit: 20 requests/minute, 50/day (1000/day after buying $10 of credits once).")
    while True:
        key = ask("OpenRouter API key (input hidden)", secret=True, default=effective("OPENROUTER_API_KEY"))
        say("  Testing the key and picking a free model...")
        err, model, sample = asyncio.run(OpenRouterChat(key, "auto", timeout=60).check())
        if not err:
            say(f"  Works. Free model: {model}  ->  {sample[:50]}")
            break
        say(f"  That did not work: {err}")
        if yes_no("Save this key anyway?", default=False):
            break
    save("OPENROUTER_API_KEY", key)
    save("OPENROUTER_MODEL", "auto")
    save("TRANSLATE_PROVIDER", "auto")
    save("TRANSLATE_ENABLED", True)


def x_stage(force: bool) -> None:
    from unified_core import x_accounts

    db_path = data_dir() / "accounts.db"
    try:
        existing = asyncio.run(x_accounts.list_accounts(db_path))
    except Exception:
        existing = []
    if existing and not force:
        return
    heading("X (Twitter) accounts (optional)")
    say("Gives access to tweet text + replies. Media from X works without it, but replies are not reposted.")
    say("Add several accounts: when one hits its rate limit the next one takes over automatically.")
    if existing:
        say("  Already configured: " + ", ".join(n for n, _ in existing))
    if not yes_no("Set up an X account now?", default=not existing):
        return
    while True:
        saved = _add_x_account(db_path)
        if not yes_no("Add another X account (fallback)?" if saved else "Try again?", default=False):
            return


def _add_x_account(db_path: Path) -> bool:
    """Ask for one X account and store it. Returns True when it was saved."""
    from unified_core import x_accounts

    say("  1) Paste auth_token and ct0   (browser: x.com -> F12 -> Application -> Cookies -> https://x.com)")
    say("  2) Use a cookies.txt file exported from your browser (Netscape format, e.g. Cookie-Editor)")
    mode = ask("Choose", default="1", validate=_v_choice("1", "2"))

    cookies, auth, ct0 = "", "", ""
    if mode == "1":
        auth = ask("auth_token", secret=True, validate=_v_cookie)
        ct0 = ask("ct0", secret=True, validate=_v_cookie)
        cookies = x_accounts.cookie_header(auth, ct0)
    else:
        src = Path(ask("Path to cookies.txt", validate=_v_file))
        cookies = x_accounts.cookies_from_netscape(src)
        if not x_accounts.has_x_session(cookies):
            say("  That file has no x.com auth_token/ct0 cookies (log in to x.com first, then export again).")
            return False
        _install_cookies_file(src)
    username = ask("This X account's username (without @)", validate=_v_username)
    try:
        accounts = asyncio.run(x_accounts.add_account(db_path, username, cookies))
    except Exception as exc:
        say(f"  Could not save the X account: {exc}")
        return False
    say("  X accounts: " + ", ".join(f"{n} ({'active' if a else 'inactive'})" for n, a in accounts))
    if mode == "1":  # let yt-dlp reuse the same login (first account only; rotation uses the stored accounts)
        target = data_dir() / "cookies.txt"
        if not target.exists():
            target.write_text(x_accounts.netscape_for_x(auth, ct0), encoding="utf-8")
            _private(target)
    return True


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _install_cookies_file(src: Path) -> None:
    dest = data_dir() / "cookies.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    _private(dest)


def cookies_stage(force: bool) -> None:
    dest = data_dir() / "cookies.txt"
    if dest.exists() and not force:
        return
    heading("Browser cookies for yt-dlp (optional)")
    say("Helps with private / age-restricted / rate-limited posts (Instagram, YouTube, Facebook...).")
    path = ask("Path to a cookies.txt file (Netscape format)", optional=True, validate=_v_file)
    if path:
        _install_cookies_file(Path(path))
        say(f"  Saved to {dest}")


def advanced_stage() -> None:
    heading("Behaviour")
    if not yes_no("Review advanced settings (comments, video quality, notifications)?", default=False):
        return
    for key, question, kind, default in ADVANCED:
        cur = effective(key, default)
        if kind == "bool":
            save(key, yes_no(question, default=cur.lower() == "true"))
        else:
            save(key, ask(question, default=cur, validate=_v_number))


# -------------------------------------------------------------- entry point
def missing_required() -> list[str]:
    out = []
    for key, why in (("API_ID", "from https://my.telegram.org"), ("API_HASH", "from https://my.telegram.org"),
                     ("TARGET_CHANNEL", "channel id/@username where media is posted")):
        v = effective(key)
        if not v or (key != "API_HASH" and not re.fullmatch(r"-?\d+", v)):
            out.append(f"{key}  ({why})")
    if not session_file().is_file() and not effective("SESSION_STRING"):
        out.append(f"Telegram login session ({session_file()}) or SESSION_STRING")
    return out


def ensure_ready(force: bool = False) -> None:
    """Make sure the bot can start; ask for whatever is missing. Exits with code 2 if impossible."""
    complete = effective(COMPLETE_KEY) == "1"
    missing = missing_required()
    if complete and not missing and not force:
        return

    if not interactive():
        if missing:
            sys.stderr.write(
                "Configuration incomplete and no terminal is available to ask for it.\n"
                "Missing:\n  - " + "\n  - ".join(missing) + "\n"
                "Run  python3 run.py  once in an interactive terminal (or set these as environment variables).\n")
            sys.exit(2)
        return

    first = not complete
    try:
        say("\nZeba Saver Bot - setup" + (" (first run)" if first else ""))
        say("Answers are saved as you go, so you can safely stop (Ctrl+C) and continue later.")
        for attempt in range(3):
            heading("Telegram API")
            if force or not effective("API_ID") or not re.fullmatch(r"\d+", effective("API_ID")):
                say("Create an app at https://my.telegram.org -> 'API development tools' to get these.")
                save("API_ID", ask("API_ID", default=effective("API_ID"), validate=_v_api_id))
            if force or not effective("API_HASH") or not re.fullmatch(r"[0-9a-fA-F]{32}", effective("API_HASH")):
                save("API_HASH", ask("API_HASH", default=effective("API_HASH"), validate=_v_api_hash))
            try:
                asyncio.run(telegram_stage(first, force))
                break
            except Exception as exc:
                if exc.__class__.__name__ == "ApiIdInvalidError":
                    say("Telegram rejected API_ID / API_HASH. Please enter them again.")
                    save("API_ID", "")
                    save("API_HASH", "")
                    force = True
                    continue
                raise
        else:
            raise SetupError("Telegram keeps rejecting the API credentials. Check them at https://my.telegram.org.")

        if first or force:
            control_bot_stage(force)
            ai_stage(force)
            x_stage(force)
            cookies_stage(force)
            advanced_stage()
        save(COMPLETE_KEY, "1")
        heading("All set")
        say(f"Configuration saved to {ENV_FILE}")
        say("Re-run the whole wizard any time with:  python3 run.py --setup\n")
    except Cancelled:
        say("\nSetup interrupted - answers so far are saved. Run again to continue.")
        sys.exit(130)
    except SetupError as exc:
        sys.stderr.write(f"\nSetup failed: {exc}\n")
        sys.exit(2)
    except Exception as exc:  # network down, Telegram error... keep it readable
        if os.environ.get("ZEBA_DEBUG"):
            raise
        sys.stderr.write(f"\nSetup failed: {exc.__class__.__name__}: {exc}\n"
                         "Check your internet connection and run the command again - saved answers are kept.\n")
        sys.exit(2)
