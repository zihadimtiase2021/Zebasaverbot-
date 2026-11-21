"""Zeba Saver Bot - production entry point."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import sys

if __name__ == "__main__":
    # First run / missing values: ask for them *before* the settings are loaded.
    from unified_core.setup_wizard import ensure_ready

    ensure_ready(force=os.environ.get("ZEBA_FORCE_SETUP") == "1")

from telethon import TelegramClient, utils  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from unified_core.cleanup import purge_tmp  # noqa: E402
from unified_core.config import settings  # noqa: E402
from unified_core.database import db  # noqa: E402
from unified_core.logging_setup import setup_logging  # noqa: E402
from unified_core.tg import resolve_peer  # noqa: E402
from unified_services.context import App  # noqa: E402
from unified_services.control_bot import register_control_bot  # noqa: E402
from unified_services.pipeline import Pipeline  # noqa: E402
from unified_services.queue import JobQueue  # noqa: E402
from unified_services.translator import Translator  # noqa: E402
from unified_core.bootstrap import apply_cookies_from_env, import_x_accounts_from_env  # noqa: E402
from unified_services.keepalive import KeepAlive  # noqa: E402
from unified_services.webhook import WebhookServer  # noqa: E402
from unified_services.runtime_config import RuntimeConfig  # noqa: E402
from unified_services.forwarder import Forwarder  # noqa: E402
from unified_services.fwd_backup import FwdBackup  # noqa: E402
from unified_services.admin_panel import AdminPanel  # noqa: E402
from unified_services.accounts import AccountManager  # noqa: E402
from unified_services.userbot_handlers import on_job_result, register_userbot_handlers, resume_batches  # noqa: E402

log = logging.getLogger("zeba")


async def tmp_janitor() -> None:
    """Safety net: remove temp dirs that outlived any possible job (e.g. a crashed worker thread)."""
    max_age = settings.JOB_TIMEOUT * 2 + 300
    while True:
        await asyncio.sleep(600)
        try:
            n = await asyncio.to_thread(purge_tmp, settings.tmp_dir, max_age)
            if n:
                log.info("Janitor removed %d stale temp item(s)", n)
        except Exception:
            log.exception("janitor failed")


async def main() -> int:
    setup_logging()
    problems = settings.validate_required()
    if problems:
        for p in problems:
            log.error("Config error: %s", p)
        log.error("Run  python3 run.py --setup  to fill them in.")
        return 2

    # Anything left in the temp folder is debris from a crash: wipe it before starting.
    leftovers = purge_tmp(settings.tmp_dir)
    if leftovers:
        log.info("Removed %d leftover temp item(s) from a previous run", leftovers)

    apply_cookies_from_env()
    await db.connect()
    await db.batch_prune()
    await db.fwd_prune()
    recovered = await db.recover_stale()
    if recovered:
        log.warning("Marked %d interrupted job(s) as failed", recovered)

    # SESSION_STRING = login kept in an environment variable (Render & co have no terminal to log in with)
    session = StringSession(settings.SESSION_STRING.strip()) if settings.SESSION_STRING.strip() else str(settings.session_path)
    def make_userbot(sess) -> TelegramClient:
        return TelegramClient(
            sess, settings.API_ID, settings.API_HASH,
            device_model="Desktop", system_version="Windows 10.0", app_version="4.14.8",
            connection_retries=-1, retry_delay=2, auto_reconnect=True, flood_sleep_threshold=120,
        )

    userbot = make_userbot(session)
    control_bot: TelegramClient | None = None
    queue: JobQueue | None = None
    pipeline: Pipeline | None = None
    app: App | None = None
    janitor: asyncio.Task | None = None
    web: WebhookServer | None = None
    keepalive: KeepAlive | None = None
    forwarder: Forwarder | None = None
    fwd_backup: FwdBackup | None = None
    accounts: AccountManager | None = None

    try:
        await userbot.connect()
        if not await userbot.is_user_authorized():
            if not sys.stdin.isatty():
                log.error("Userbot session is not authorized. Run  python3 run.py --setup  in a terminal.")
                return 2
            from unified_core.setup_wizard import login_interactive

            userbot = await login_interactive(userbot, make_userbot)   # login on pyrofork, then a fresh Telethon client

        me = await userbot.get_me()
        premium = bool(getattr(me, "premium", False))
        log.info("Logged in as %s (id=%s, premium=%s)", me.first_name, me.id, premium)

        if not settings.admin_ids:
            settings.ADMIN_IDS = str(me.id)  # nobody configured -> you are the admin
        # Settings changed from /admin or .target/.source/.comments live in Saved Messages and beat the environment
        runtime = RuntimeConfig(userbot)
        await runtime.load()
        if runtime.saved_target:
            try:
                await resolve_peer(userbot, runtime.saved_target)
                settings.TARGET_CHANNEL = runtime.saved_target
                settings.COMMENT_CHAT = runtime.saved_comment_chat
                log.info("Target channel taken from the saved runtime settings (%s)", runtime.saved_target)
            except RuntimeError as exc:
                log.warning("Saved target channel unavailable (%s) - using TARGET_CHANNEL from the environment", exc)
        try:
            target = await resolve_peer(userbot, settings.TARGET_CHANNEL)
        except RuntimeError as exc:
            if not sys.stdin.isatty():
                log.error("%s  (the account must be a member/admin of TARGET_CHANNEL)", exc)
                return 2
            from unified_core.setup_wizard import choose_channel

            log.warning("%s", exc)
            settings.TARGET_CHANNEL = await choose_channel(userbot)
            target = await resolve_peer(userbot, settings.TARGET_CHANNEL)
        comment_chat = None
        if settings.COMMENT_CHAT:
            try:
                comment_chat = await resolve_peer(userbot, settings.COMMENT_CHAT)
            except RuntimeError as exc:
                log.warning("COMMENT_CHAT unavailable, tag-based fallback disabled: %s", exc)

        ignored = {utils.get_peer_id(target)}
        if comment_chat is not None:
            ignored.add(utils.get_peer_id(comment_chat))

        translator = Translator()
        await translator.load_mode()  # provider chosen with .ai before the last restart
        pipeline = Pipeline(userbot, target, comment_chat, premium, translator)
        holder: dict[str, App] = {}

        async def _on_result(job, result):
            await on_job_result(holder["app"], job, result)

        queue = JobQueue(pipeline.run, _on_result)
        app = App(userbot=userbot, control_bot=None, queue=queue, pipeline=pipeline,
                  me_id=me.id, ignored_chats=ignored)
        holder["app"] = app
        runtime.bind(app)
        app.runtime = runtime

        register_userbot_handlers(app)

        # product details / notes survive a wiped database (Render free): kept as a file in Saved Messages
        fwd_backup = FwdBackup(userbot, db)
        await fwd_backup.restore()
        db.on_change = fwd_backup.touch
        forwarder = Forwarder(userbot, pipeline, me_id=me.id, store=runtime)
        await forwarder.load()      # routes saved in Saved Messages (survive Render restarts)
        forwarder.register()
        app.forwarder = forwarder
        # more Telegram accounts: /login, /accounts, /logout, /resume <key> in the control bot
        accounts = AccountManager(app, primary_name=me.first_name or "", primary_username=me.username or "")

        if settings.CONTROL_BOT_TOKEN:
            try:
                # File session (named by token) so the bot remembers its users across restarts
                # and can message the admin first.
                tag = hashlib.sha1(settings.CONTROL_BOT_TOKEN.encode()).hexdigest()[:8]
                control_bot = TelegramClient(str(settings.DATA_DIR / f"control_bot_{tag}"),
                                             settings.API_ID, settings.API_HASH)
                await control_bot.start(bot_token=settings.CONTROL_BOT_TOKEN)
                app.control_bot = control_bot
                pipeline.control_bot = control_bot
                register_control_bot(app, accounts)
                log.info("Control bot started")
            except Exception as exc:
                log.warning("Control bot disabled: %s", exc)
                control_bot = None
        else:
            log.info("CONTROL_BOT_TOKEN not set - control bot disabled")

        queue.start(settings.MAX_CONCURRENCY)
        await import_x_accounts_from_env()
        port = settings.PORT or (settings.WEBHOOK_PORT if settings.WEBHOOK_SECRET else 0)
        if settings.WEBHOOK_ENABLED and port:  # Render needs an open port; the webhook lives on the same server
            panel = AdminPanel(runtime, queue, settings.WEBHOOK_SECRET)
            panel.accounts = accounts if control_bot is not None else None
            web = WebhookServer(queue, secret=settings.WEBHOOK_SECRET, max_queue=settings.WEBHOOK_MAX_QUEUE,
                                cors_origins=settings.WEBHOOK_CORS_ORIGINS, rate_per_min=settings.WEBHOOK_RATE_PER_MIN,
                                admin=panel)
            await web.start(settings.WEBHOOK_HOST, port)
            public = settings.KEEPALIVE_URL or settings.RENDER_EXTERNAL_URL
            if settings.KEEPALIVE_ENABLED and public:
                keepalive = KeepAlive(public, settings.KEEPALIVE_INTERVAL)
                keepalive.start()
        resumed = await resume_batches(app)  # unfinished .json/.txt batches continue where they stopped
        if resumed:
            log.info("Resumed %d unfinished batch(es)", resumed)
        restored = await accounts.restore()  # extra accounts logged in earlier (database / EXTRA_SESSIONS)
        if restored:
            log.info("Started %d extra account(s)", restored)
        janitor = asyncio.create_task(tmp_janitor(), name="zeba-janitor")
        log.info("Zeba is running (workers=%d, upload_limit=%s MiB)",
                 settings.MAX_CONCURRENCY, int(pipeline.max_bytes / 1048576))

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, app.request_stop, 0)
            except NotImplementedError:  # Windows
                pass

        disconnected = asyncio.create_task(userbot.run_until_disconnected())
        stop_wait = asyncio.create_task(app.stop_event.wait())
        done, pending = await asyncio.wait({disconnected, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if disconnected in done and not app.stop_event.is_set():
            log.error("Userbot disconnected unexpectedly")
            app.exit_code = 1
        return app.exit_code

    finally:
        log.info("Shutting down...")
        if janitor is not None:
            janitor.cancel()
        if keepalive is not None:
            await keepalive.stop()
        if web is not None:
            await web.stop()
        if accounts is not None:
            await accounts.stop_all()
        if forwarder is not None:
            await forwarder.stop(grace=20)
        if fwd_backup is not None:
            await fwd_backup.stop()   # the copies are finished: write the last details while the userbot is connected
        if queue is not None:
            await queue.stop(grace=30)
        if pipeline is not None:
            await pipeline.drain(timeout=20)  # let background comment posting finish, then cancel the rest
        purge_tmp(settings.tmp_dir)  # workers are stopped: leave nothing behind
        for client in (control_bot, userbot):
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        await db.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
