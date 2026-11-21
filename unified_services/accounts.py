"""Extra Telegram userbot accounts that are logged in / out from the control bot.

* The *primary* account is the one from SESSION_STRING / the session file - it runs the whole bot and is never
  logged out from chat.
* Every extra account gets its own complete userbot: client + pipeline + queue + forwarder + .dl/.fwd handlers.
  Shared with the primary: the watermark / translation / caption settings and the default TARGET_CHANNEL
  (an extra account may switch to its own channel with ``.target`` in its chats - that part is per account).
* Session key = the Telethon StringSession of the account. Logout only *stops* the account in this bot and forgets
  the key on the server (the Telegram session stays valid), so ``/resume <key>`` logs it in again without a code.
  ``terminate`` really logs the session out on Telegram - the key is dead afterwards.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from telethon import TelegramClient, utils
from telethon.sessions import StringSession

from unified_core.config import settings
from unified_core.database import db
from unified_core.tg import resolve_peer
from unified_core.utils import short_error
from unified_services.context import App

log = logging.getLogger("zeba.accounts")


class AccountError(Exception):
    """A problem that can be shown to the admin as it is. `key` = the session key when the login itself worked."""

    def __init__(self, message: str, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


class AccountCfg:
    """Settings view of an extra account: everything reads/writes the shared global settings, except the
    target channel / comment chat, which are kept per account."""

    _LOCAL = ("TARGET_CHANNEL", "COMMENT_CHAT")

    def __init__(self, base=None) -> None:
        object.__setattr__(self, "_base", base if base is not None else settings)
        object.__setattr__(self, "_local", {})

    def __getattr__(self, name):
        local = object.__getattribute__(self, "_local")
        if name in local:
            return local[name]
        return getattr(object.__getattribute__(self, "_base"), name)

    def __setattr__(self, name, value) -> None:
        if name in self._LOCAL:
            self._local[name] = value
        else:
            setattr(self._base, name, value)


@dataclass
class Instance:
    me_id: int
    name: str
    username: str
    phone: str
    session: str
    client: TelegramClient
    app: App
    runtime: object
    forwarder: object
    source: str = "login"            # "login" (stored in the database) | "env" (EXTRA_SESSIONS)
    started: float = field(default_factory=time.time)
    monitor: asyncio.Task | None = None

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else (self.name or str(self.me_id))

    async def stop(self, terminate: bool = False) -> None:
        """Stop this account's workers, then disconnect (or really log the session out when `terminate`)."""
        if self.monitor is not None and self.monitor is not asyncio.current_task():
            self.monitor.cancel()
        for coro in (self.forwarder.stop(grace=10), self.app.queue.stop(grace=20), self.app.pipeline.drain(timeout=10)):
            try:
                await coro
            except Exception:
                log.exception("stopping account %s", self.label)
        if terminate:
            await self.client.log_out()  # may raise: the caller reports it
        else:
            try:
                await self.client.disconnect()
            except Exception:
                pass


def clean_key(raw: str) -> str:
    return re.sub(r"[\s`'\"<>]", "", raw or "")


def split_keys(raw: str) -> list[str]:
    return [k for k in (clean_key(x) for x in re.split(r"[\s,;]+", raw or "")) if k]


class AccountManager:
    def __init__(self, primary: App, primary_name: str = "", primary_username: str = "") -> None:
        self.primary = primary
        self.primary_name = primary_name
        self.primary_username = primary_username
        self.instances: dict[int, Instance] = {}
        self.vault = None   # StateVault: the extra accounts' sessions, encrypted, in the primary's Saved Messages
        self._lock = asyncio.Lock()
        self._stopping = 0  # logouts in progress: a key must not be used while its old connection is still closing

    # ------------------------------------------------------------------ helpers
    def new_client(self, session: str = "", plain: bool = False, api_id: int = 0, api_hash: str = "") -> TelegramClient:
        api_id, api_hash = api_id or settings.API_ID, api_hash or settings.API_HASH
        if plain:
            # Exactly what the web setup panel uses for its login (default device info). Telegram delivers the
            # login code to this reliably; the spoofed "Telegram Desktop" identity below can make it stay silent.
            return TelegramClient(StringSession(session), api_id, api_hash)
        return TelegramClient(
            StringSession(session), api_id, api_hash,
            device_model="Desktop", system_version="Windows 10.0", app_version="4.14.8",
            connection_retries=-1, retry_delay=2, auto_reconnect=True, flood_sleep_threshold=120,
        )

    def new_login(self, api_id: int = 0, api_hash: str = ""):
        """Login object for /login and the web panel: the phone/code/2FA steps run on pyrofork and the result is
        exported as a Telethon session key (see unified_core.pyro_login)."""
        from unified_core.pyro_login import PyroLogin
        return PyroLogin(api_id or settings.API_ID, api_hash or settings.API_HASH)

    @property
    def total(self) -> int:
        return 1 + len(self.instances)

    def listing(self) -> list[dict]:
        """[{n, id, name, username, phone, primary, since}] - n = number used by /logout n (1 = primary)."""
        rows = [{"n": 1, "id": self.primary.me_id, "name": self.primary_name, "username": self.primary_username,
                 "phone": "", "primary": True, "since": None, "source": "primary"}]
        for i, inst in enumerate(self.instances.values(), start=2):
            rows.append({"n": i, "id": inst.me_id, "name": inst.name, "username": inst.username,
                         "phone": inst.phone, "primary": False, "since": inst.started, "source": inst.source})
        return rows

    def resolve(self, ref: str) -> int | None:
        """'2' (position in /accounts), a user id or @username -> the user id of an *extra* account."""
        ref = (ref or "").strip().lstrip("@")
        if not ref:
            return None
        if ref.isdigit():
            n = int(ref)
            if n in self.instances:
                return n
            for row in self.listing():
                if row["n"] == n and not row["primary"]:
                    return row["id"]
            return None
        for inst in self.instances.values():
            if inst.username.lower() == ref.lower():
                return inst.me_id
        return None

    def _clients(self) -> list:
        return [self.primary.userbot] + [i.client for i in self.instances.values()]

    def _auth_key_of(self, client) -> bytes | None:
        try:
            ak = client.session.auth_key
            return getattr(ak, "key", None) if ak else None
        except Exception:
            return None

    def _check_key_free(self, session: StringSession) -> None:
        """Using one auth key from two connections at once makes Telegram kill it - never connect twice."""
        if self._stopping:
            raise AccountError("A logout is still finishing - try again in a few seconds.")
        key = getattr(getattr(session, "auth_key", None), "key", None)
        if not key:
            raise AccountError("That is not a valid session key.")
        for client in self._clients():
            if self._auth_key_of(client) == key:
                raise AccountError("That account is already running in this bot.")

    # ------------------------------------------------------------------ login / resume
    async def resume(self, raw_key: str, source: str = "login") -> Instance:
        key = clean_key(raw_key)
        try:
            session = StringSession(key)
        except Exception:
            raise AccountError("That is not a valid session key.") from None
        self._check_key_free(session)
        client = self.new_client(key)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise AccountError("This key no longer works - the session was terminated or has expired. "
                                   "Log in again with /login.")
        except AccountError:
            await _quiet_disconnect(client)
            raise
        except Exception as exc:
            await _quiet_disconnect(client)
            raise AccountError(f"Could not connect to Telegram: {short_error(exc)}") from exc
        return await self.add_client(client, source=source)

    async def add_client(self, client: TelegramClient, phone: str = "", source: str = "login") -> Instance:
        """Take a connected + authorized client and run it as an account. On failure the client is closed again
        (not logged out) and the AccountError carries the key so the admin can /resume later."""
        key = client.session.save()
        try:
            return await self._attach(client, key, phone, source)
        except AccountError as exc:
            exc.key = exc.key or key
            await _quiet_disconnect(client)
            raise
        except Exception as exc:
            await _quiet_disconnect(client)
            raise AccountError(f"Could not start the account: {short_error(exc)}", key=key) from exc

    async def _attach(self, client, key: str, phone: str, source: str) -> Instance:
        async with self._lock:
            me = await client.get_me()
            if me.id == self.primary.me_id or me.id in self.instances:
                raise AccountError("That account is already running in this bot.")
            if self.total >= settings.MAX_ACCOUNTS:
                raise AccountError(f"Account limit reached ({settings.MAX_ACCOUNTS}). Log one out first "
                                   "(or raise MAX_ACCOUNTS).")
            inst = await self._build(client, me, key, phone or getattr(me, "phone", "") or "", source)
            self.instances[me.id] = inst
        if source == "login":
            await db.account_save(me.id, inst.name, inst.phone, key)
            await self._vault_set(inst, key)
        log.info("Account %s (id=%s) is running (%d total)", inst.label, me.id, self.total)
        return inst

    async def _build(self, client, me, key: str, phone: str, source: str) -> Instance:
        """The same wiring as unified_main, for one more account."""
        from unified_services.forwarder import Forwarder
        from unified_services.pipeline import Pipeline
        from unified_services.queue import JobQueue
        from unified_services.runtime_config import RuntimeConfig
        from unified_services.userbot_handlers import on_job_result, register_userbot_handlers, resume_batches

        cfg = AccountCfg()
        runtime = RuntimeConfig(client, cfg=cfg)
        runtime.apply_toggles = False  # only this account's own target / forwarding routes are read from its Saved Messages
        await runtime.load()

        target = None
        if runtime.saved_target:
            try:
                target = await resolve_peer(client, runtime.saved_target)
                cfg.TARGET_CHANNEL = runtime.saved_target
                cfg.COMMENT_CHAT = runtime.saved_comment_chat
            except RuntimeError:
                target = None
        if target is None:
            try:
                target = await resolve_peer(client, settings.TARGET_CHANNEL)
            except RuntimeError as exc:
                raise AccountError(
                    f"This account cannot reach the target channel ({exc}). Add it to the channel as an admin "
                    "(Post messages), then /resume the key again.") from exc
            cfg.TARGET_CHANNEL = utils.get_peer_id(target)
        comment_chat = None
        if cfg.COMMENT_CHAT:
            try:
                comment_chat = await resolve_peer(client, cfg.COMMENT_CHAT)
            except RuntimeError as exc:
                log.warning("COMMENT_CHAT unavailable for %s: %s", me.id, exc)
        ignored = {utils.get_peer_id(target)}
        if comment_chat is not None:
            ignored.add(utils.get_peer_id(comment_chat))

        name = " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x) or str(me.id)
        username = getattr(me, "username", "") or ""
        pipeline = Pipeline(client, target, comment_chat, bool(getattr(me, "premium", False)),
                            self.primary.pipeline.translator)
        pipeline.control_bot = self.primary.control_bot
        pipeline.label = f"@{username}" if username else name
        holder: dict[str, App] = {}

        async def _on_result(job, result):
            await on_job_result(holder["app"], job, result)

        queue = JobQueue(pipeline.run, _on_result)
        app = App(userbot=client, control_bot=self.primary.control_bot, queue=queue, pipeline=pipeline,
                  me_id=me.id, ignored_chats=ignored)
        holder["app"] = app
        runtime.bind(app)
        app.runtime = runtime
        forwarder = Forwarder(client, pipeline, me_id=me.id, store=runtime)
        inst = Instance(me_id=me.id, name=name, username=username, phone=phone, session=key, client=client,
                        app=app, runtime=runtime, forwarder=forwarder, source=source)
        try:
            register_userbot_handlers(app)
            await forwarder.load()
            forwarder.register()
            app.forwarder = forwarder
            queue.start(settings.EXTRA_ACCOUNT_WORKERS)
            await resume_batches(app, primary=False)
            await forwarder.resume_jobs()       # a Forward all that was running before the redeploy continues
            inst.monitor = asyncio.create_task(self._watch(inst), name=f"zeba-watch-{me.id}")
        except Exception:
            await inst.stop()
            raise
        return inst

    # ------------------------------------------------------------------ logout
    async def logout(self, user_id: int, terminate: bool = False) -> tuple[str, bool, str | None]:
        """Stop one extra account. Returns (key, terminated, error). The server forgets the key; the admin keeps it."""
        async with self._lock:
            inst = self.instances.pop(user_id, None)
            if inst is None:
                raise AccountError("That account is not logged in.")
            self._stopping += 1
        error = None
        terminated = False
        try:
            await db.account_delete(user_id)
            await self._vault_drop(user_id)
            try:
                await inst.forwarder.clear_resume()   # a logged-out account must not restart its Forward all later
            except Exception:
                log.debug("could not clear the saved jobs of %s", inst.label)
            try:
                await inst.stop(terminate=terminate)
                terminated = terminate
            except Exception as exc:  # log_out failed: the session is still alive, the key still works
                error = short_error(exc)
                await _quiet_disconnect(inst.client)
        finally:
            self._stopping -= 1
        log.info("Account %s logged out (terminate=%s, ok=%s)", inst.label, terminate, terminated)
        return inst.session, terminated, error

    async def _watch(self, inst: Instance) -> None:
        """If the connection dies for good (session revoked from another device...), drop the account."""
        try:
            await inst.client.disconnected
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if self.instances.get(inst.me_id) is not inst:
            return  # deliberate logout
        try:
            await inst.client.connect()
            if await inst.client.is_user_authorized():
                inst.monitor = asyncio.create_task(self._watch(inst), name=f"zeba-watch-{inst.me_id}")
                return
        except Exception:
            pass
        log.warning("Account %s lost its session", inst.label)
        self.instances.pop(inst.me_id, None)
        await db.account_delete(inst.me_id)
        await self._vault_drop(inst.me_id)
        await inst.stop()
        await self.primary.pipeline.notify(
            f"⚠️ <b>{_h(inst.label)}</b> was logged out by Telegram (session ended elsewhere). "
            "Log in again with /login.")

    # ------------------------------------------------------------------ start / stop of the whole set
    async def restore(self) -> int:
        """At start-up: bring back the accounts stored in the database and those in EXTRA_SESSIONS."""
        started = 0
        rows = await db.accounts_all()
        todo = [(r["session"], "login", r["user_id"]) for r in rows]
        if self.vault is not None:      # accounts kept in Saved Messages: they come back after a redeploy wiped the DB
            known = {r["user_id"] for r in rows}
            for uid, key, _label in self.vault.sessions():
                if uid not in known:
                    todo.append((key, "login", uid))
                    log.info("Account %s restored from the encrypted state in Saved Messages", uid)
        todo += [(k, "env", None) for k in split_keys(settings.EXTRA_SESSIONS)]
        for key, source, uid in todo:
            try:
                await self.resume(key, source=source)
                started += 1
            except AccountError as exc:
                if "already running" in str(exc) and source == "env":
                    continue
                log.warning("Could not restore an account: %s", exc)
                if uid is not None:  # permanent problem: forget it here, the admin still has the key
                    await db.account_delete(uid)
                    if not str(exc).startswith("Could not connect"):   # a network hiccup must not lose the saved login
                        await self._vault_drop(uid)
                await self.primary.pipeline.notify(
                    f"⚠️ <b>An extra account was not started:</b> {_h(str(exc))}"
                    + (f"\n🔑 <code>{_h(key)}</code>\nFix it, then <code>/resume</code> this key." if uid is not None else ""))
            except Exception:
                log.exception("restoring an account failed (kept for the next start)")
        return started

    async def _vault_set(self, inst: "Instance", key: str) -> None:
        if self.vault is not None:
            try:
                await self.vault.set_account(inst.me_id, inst.label, key)
            except Exception:
                log.exception("could not save the account to the Saved Messages state")

    async def _vault_drop(self, user_id: int) -> None:
        if self.vault is not None:
            try:
                await self.vault.drop_account(user_id)
            except Exception:
                log.exception("could not remove the account from the Saved Messages state")

    async def stop_all(self) -> None:
        for uid in list(self.instances):
            inst = self.instances.pop(uid, None)
            if inst is not None:
                try:
                    await inst.stop()
                except Exception:
                    log.exception("stopping %s", inst.label)


async def _quiet_disconnect(client) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass


def _h(text: str) -> str:
    from unified_core.utils import esc
    return esc(text)
