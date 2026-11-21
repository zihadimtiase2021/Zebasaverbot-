"""Handlers for the userbot (your own account): .dl / .dlauto / .autodl / JSON batches."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
import uuid

from telethon import events

from unified_core import x_accounts
from unified_core.cleanup import safe_rmtree
from unified_core.config import settings
from unified_core.database import db
from unified_core.utils import NOTIFY_MARK, esc, extract_urls, items_from_file, short_error, split_url_and_caption
from unified_services.context import App
from unified_services.pipeline import BatchTracker, Job, JobResult

log = logging.getLogger("zeba.userbot")

MAX_URLS_PER_MESSAGE = 10


def _render_dashboard(app: App, tracker: BatchTracker, final: bool = False) -> str:
    total, done = tracker.total, tracker.done
    pct = (done / total * 100) if total else 100
    filled = int(15 * done // total) if total else 15
    bar = "█" * filled + "░" * (15 - filled)
    active = [j for j in app.queue.active.values() if j.batch is tracker]
    pending = max(0, total - done - len(active))

    lines = [
        NOTIFY_MARK + "✨ <b>ZEBA BATCH PROCESSOR</b> ✨",
        "",
        f"📊 <b>Progress:</b> <code>{done} / {total}</code> ({pct:.1f}%)",
        f"<code>[{bar}]</code>",
        f"✅ {tracker.ok}   ⏭ {tracker.skipped}   ❌ {tracker.failed}",
    ]
    if not final:
        lines += ["", f"⚡ <b>Active:</b> {len(active)}/{settings.MAX_CONCURRENCY}   ⏳ <b>Queued:</b> {pending}", ""]
        for j in active[:8]:
            name = j.url.rstrip("/").split("/")[-1].split("?")[0][:25] or "link"
            lines.append(f"▪️ <code>{esc(name)}</code> — <i>{esc(j.step)}</i>")
    else:
        lines += ["", "🎉 <b>Batch finished.</b>"]
        for url, err in tracker.failures[:10]:
            lines.append(f"❌ <code>{esc(url[:60])}</code>\n   ↳ {esc(err[:120])}")
        if len(tracker.failures) > 10:
            lines.append(f"…and {len(tracker.failures) - 10} more failures (see logs).")
    return "\n".join(lines)


_X_USER = re.compile(r"[A-Za-z0-9_]{1,15}")
_X_SECRET = re.compile(r"[^\s;=]{10,}")


def _fmt_status(a: dict) -> str:
    if not a["active"]:
        return f"⚪ <code>@{esc(a['username'])}</code> — inactive"
    if a["locked_until"]:
        return (f"⏳ <code>@{esc(a['username'])}</code> — rate-limited until "
                f"{a['locked_until'].strftime('%H:%M')} UTC")
    return f"✅ <code>@{esc(a['username'])}</code> — ready"


async def x_command(event, cmd: str) -> str:
    """.xacc | .xadd <user> <auth_token> <ct0> | .xdel <user> | .xunlock  ->  reply text (HTML)."""
    parts = cmd.split()
    name, db_path = parts[0], settings.TWSCRAPE_DB
    if name == ".xacc":
        accounts = await x_accounts.account_status(db_path)
        if not accounts:
            return ("🐦 <b>No X accounts yet.</b>\nAdd one: <code>.xadd username auth_token ct0</code>\n"
                    "(the message is deleted right away)")
        ready = sum(1 for a in accounts if a["active"] and not a["locked_until"])
        return f"🐦 <b>X accounts</b> ({ready}/{len(accounts)} ready)\n" + "\n".join(_fmt_status(a) for a in accounts)
    if name == ".xunlock":
        await x_accounts.unlock_all(db_path)
        return "🔓 <b>Rate-limit locks cleared.</b>"
    if name == ".xdel":
        if len(parts) != 2:
            return "ℹ️ Usage: <code>.xdel username</code>"
        ok = await x_accounts.remove_account(db_path, parts[1].lstrip("@"))
        return "🗑 <b>Removed.</b>" if ok else "❌ <b>No such account.</b>"
    # .xadd - the message holds secrets: delete it before doing anything else
    try:
        await event.delete()
    except Exception:
        pass
    if len(parts) != 4 or not _X_USER.fullmatch(parts[1].lstrip("@")) \
            or not _X_SECRET.fullmatch(parts[2]) or not _X_SECRET.fullmatch(parts[3]):
        return "ℹ️ Usage: <code>.xadd username auth_token ct0</code>"
    settings.ensure_dirs()
    accounts = await x_accounts.add_account(
        db_path, parts[1].lstrip("@"), x_accounts.cookie_header(parts[2], parts[3]))
    return ("✅ <b>X account saved.</b> Accounts: "
            + ", ".join(f"@{esc(n)}" for n, _ in accounts))


_TASKS: set[asyncio.Task] = set()  # keep references so running batches are never garbage-collected


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)


async def _show(app: App, state: dict, text: str) -> None:
    """Edit the dashboard message (state = {'chat_id', 'msg_id', 'batch_id'}); send a new one if it is gone."""
    try:
        if state.get("msg_id"):
            await app.userbot.edit_message(state["chat_id"], state["msg_id"], text, parse_mode="html")
            return
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return
        log.debug("dashboard edit: %s", exc)
    try:
        msg = await app.userbot.send_message(state["chat_id"], text, parse_mode="html")
        state["msg_id"] = msg.id
        await db.batch_set_message(state["batch_id"], msg.id)
    except Exception as exc:
        log.debug("dashboard send: %s", exc)


async def _drive_batch(app: App, state: dict, tracker: BatchTracker, todo: list[tuple[int, str, str | None]],
                       force: bool, dest: int | None = None) -> None:
    """Queue `todo` (idx, url, caption) and keep the dashboard fresh until the batch is complete."""
    batch_id = state["batch_id"]
    for idx, url, custom in todo:
        job = Job(url=url, force=force, notify=False, batch=tracker, custom_caption=custom,
                  batch_id=batch_id, item_idx=idx, dest_chat=dest)
        if not app.queue.enqueue(job):  # same URL already queued/running elsewhere
            await db.batch_item_set(batch_id, idx, "skipped")
            tracker.record(url, JobResult("skipped"))

    last = ""
    while not tracker.done_event.is_set():
        text = _render_dashboard(app, tracker)
        if text != last:
            await _show(app, state, text)
            last = text
        try:
            await asyncio.wait_for(tracker.done_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass
    await db.batch_finish(batch_id)
    await _show(app, state, _render_dashboard(app, tracker, final=True))


async def run_batch(app: App, event, items: list[tuple[str, str | None]], force: bool = False,
                    dest: int | None = None) -> None:
    items = items[: settings.MAX_BATCH_URLS]
    batch_id = await db.batch_create(event.chat_id, force, items, dest, owner=getattr(app, "me_id", None))  # stored first: survives a restart
    msg = await event.respond(NOTIFY_MARK + "🔄 <b>Initializing batch…</b>", parse_mode="html")
    await db.batch_set_message(batch_id, msg.id)
    state = {"chat_id": event.chat_id, "msg_id": msg.id, "batch_id": batch_id}
    tracker = BatchTracker(len(items))
    await _drive_batch(app, state, tracker, [(i, u, c) for i, (u, c) in enumerate(items)], force, dest)


async def resume_batches(app: App, primary: bool = True) -> int:
    """After a restart: continue every unfinished batch with only the links that are not finished yet.
    Only the batches started by this account (app.me_id) are taken; old rows without an owner are the primary's."""
    if not settings.RESUME_BATCHES:
        return 0
    resumed = 0
    for b in await db.batches_unfinished(owner=getattr(app, "me_id", None), legacy=primary):
        items = await db.batch_items(b["id"])
        counts = {"done": 0, "skipped": 0, "failed": 0}
        failures: list[tuple[str, str]] = []
        todo: list[tuple[int, str, str | None]] = []
        for it in items:
            if it["status"] == "pending":
                todo.append((it["idx"], it["url"], it["custom_caption"]))
            else:
                counts[it["status"]] = counts.get(it["status"], 0) + 1
                if it["status"] == "failed":
                    failures.append((it["url"], it["error"] or ""))
        tracker = BatchTracker(len(items), ok=counts["done"], skipped=counts["skipped"],
                               failed=counts["failed"], failures=failures)
        state = {"chat_id": b["chat_id"], "msg_id": b["msg_id"], "batch_id": b["id"]}
        log.warning("Resuming batch %s: %d of %d links left", b["id"], len(todo), len(items))
        await app.pipeline.notify(
            f"♻️ <b>Resuming batch after restart</b>\n"
            f"Already finished: {tracker.done} / {tracker.total} · remaining: {len(todo)}"
        )
        _spawn(_drive_batch(app, state, tracker, todo, bool(b["force"]), b.get("dest")))
        resumed += 1
    return resumed


def register_userbot_handlers(app: App) -> None:
    userbot = app.userbot

    async def auto_enabled(chat_id: int) -> bool:
        return (
            chat_id == app.me_id
            or await db.is_auto_chat(chat_id)
            or await db.is_dlauto_chat(chat_id)
            or await db.get_auto_global()
        )

    async def reply(event, text: str) -> None:
        text = NOTIFY_MARK + text
        try:
            await event.edit(text, parse_mode="html")
        except Exception:
            await event.respond(text, parse_mode="html")

    @userbot.on(events.NewMessage(outgoing=True))
    async def on_outgoing(event):
        try:
            await _handle(event)
        except Exception:
            log.exception("outgoing handler error")

    @userbot.on(events.Album(func=lambda e: e.chat_id == app.me_id))
    async def on_saved_album(event):  # albums sent to Saved Messages come back watermarked as one album
        try:
            from unified_services import wm_handlers

            msgs = list(event.messages)
            if (not settings.WM_ENABLED or not settings.WM_TEXT or not msgs[0].out
                    or any(NOTIFY_MARK in (m.raw_text or "") for m in msgs)
                    or not any(wm_handlers.media_kind(m) for m in msgs)):
                return
            err = await wm_handlers.process_media(userbot, msgs, app.me_id, mark=NOTIFY_MARK)
            if err:
                await userbot.send_message("me", NOTIFY_MARK + f"❌ Watermark: {esc(err)}", parse_mode="html")
        except Exception:
            log.exception("album watermark error")

    async def _handle(event) -> None:
        text = event.raw_text or ""
        chat_id = event.chat_id
        if NOTIFY_MARK in text or chat_id in app.ignored_chats:
            return
        cmd = text.strip()

        # ---- chat toggles ----
        if cmd.split(" ")[0] == ".dlauto":
            arg = cmd[len(".dlauto"):].strip().lower()
            if arg == "on":
                await db.add_dlauto_chat(chat_id)
                return await reply(
                    event,
                    "✅ <b>.dlauto ON</b> — send a link here (with or without <code>.dl</code>) and the media "
                    "is posted in <b>this chat</b>. Turn off: <code>.dlauto off</code>")
            if arg == "off":
                await db.remove_dlauto_chat(chat_id)
                return await reply(event, "❌ <b>.dlauto OFF</b> for this chat.")
            on = await db.is_dlauto_chat(chat_id)
            return await reply(
                event, f"ℹ️ .dlauto is <b>{'ON' if on else 'OFF'}</b> here. Use <code>.dlauto on</code> / "
                       "<code>.dlauto off</code>")
        if cmd == ".autodl":
            await db.add_auto_chat(chat_id)
            return await reply(event, "✅ <b>Auto-scrape enabled for this chat.</b>")
        if cmd == ".autodl off":
            await db.remove_auto_chat(chat_id)
            return await reply(event, "❌ <b>Auto-scrape disabled for this chat.</b>")
        if cmd.split(" ")[0] == ".ai":
            tr = app.pipeline.translator
            arg = cmd[len(".ai"):].strip()
            if arg == "test":
                await reply(event, "🤖 Testing every configured translation provider…")
                return await event.respond(NOTIFY_MARK + "🤖 <b>Translation test</b>\n" + esc(await tr.diagnose()),
                                           parse_mode="html")
            if arg:
                return await reply(event, "🤖 " + esc(await tr.set_mode(arg)))
            return await reply(event, "🤖 <b>Translation</b>\n" + esc(await tr.describe()))
        if cmd.split(" ")[0] in (".settings", ".target", ".source", ".comments") and app.runtime is not None:
            from unified_services.runtime_config import handle_command

            try:
                answer = await handle_command(app.runtime, cmd)
            except Exception as exc:
                answer = f"❌ <b>Failed:</b> {esc(short_error(exc))}"
            return await reply(event, answer)
        if cmd.split(" ")[0] == ".wm" and app.runtime is not None:
            from unified_services import wm_handlers
            from unified_services.runtime_config import wm_command

            answer = await wm_command(app.runtime, cmd[len(".wm"):])
            if answer == "__PREVIEW__":
                await wm_handlers.send_preview(userbot, chat_id)
                answer = "\U0001f4a7 Preview sent."
            return await reply(event, answer)
        if cmd == ".wmfont" or cmd.startswith(".wmfont "):
            from unified_services import wm_handlers

            try:
                name = await wm_handlers.save_font(userbot, event.message)
                return await reply(event, f"\u2705 Font added: <b>{esc(name)}</b> \u2014 use <code>.wm font {esc(name)}</code>")
            except ValueError as exc:
                return await reply(event, f"\u274c {esc(str(exc))}")
        if cmd == ".zstat":
            s = app.queue.snapshot()
            return await reply(
                event,
                f"📊 <b>Zeba</b>\nQueue: {s['pending']} · Active: {s['active']}\n"
                f"Done: {s['ok']} · Skipped: {s['skipped']} · Failed: {s['failed']}",
            )

        # ---- message forwarding: .fwdsrc / .fwddst / .fwd ... (copy chat -> chat, download + upload) ----
        if cmd.split(" ")[0] in (".fwd", ".fwdsrc", ".fwddst") and getattr(app, "forwarder", None) is not None:
            try:
                answer = await app.forwarder.handle_command(event, cmd)
            except Exception as exc:
                answer = f"❌ <b>Failed:</b> {esc(short_error(exc))}"
            await reply(event, answer)
            if cmd.split(" ")[0] in (".fwdsrc", ".fwddst") and chat_id != app.me_id:
                app.forwarder.delete_later(chat_id, event.id)  # typed inside a real chat: clean up after 20 s
            return

        # ---- X (Twitter) accounts: several accounts = automatic fallback when one hits its limit ----
        if cmd.split(" ")[0] in (".xacc", ".xadd", ".xdel", ".xunlock"):
            try:
                answer = await x_command(event, cmd)
            except Exception as exc:
                answer = f"❌ <b>Failed:</b> {esc(short_error(exc))}"
            if cmd.startswith(".xadd"):  # original message is already deleted
                await event.respond(NOTIFY_MARK + answer, parse_mode="html")
                return
            return await reply(event, answer)

        # ---- photo / video sent to Saved Messages: comes back watermarked ----
        if chat_id == app.me_id and settings.WM_ENABLED and settings.WM_TEXT and not event.message.grouped_id:
            from unified_services import wm_handlers

            if wm_handlers.media_kind(event.message):
                err = await wm_handlers.process_media(userbot, event.message, chat_id, mark=NOTIFY_MARK)
                if err:
                    await event.respond(NOTIFY_MARK + f"\u274c Watermark: {esc(err)}", parse_mode="html")
                return

        # ---- batch file (.json / .txt) ----
        f = event.file
        if f and f.name and f.name.lower().endswith((".json", ".txt")):
            if (f.size or 0) <= 20 * 1024 * 1024 and (
                text.startswith(".batch") or await auto_enabled(chat_id)
            ):
                tmp = settings.tmp_dir / f"batch_{uuid.uuid4().hex[:8]}"
                tmp.mkdir(parents=True, exist_ok=True)
                try:
                    path = await event.download_media(file=str(tmp / f.name))
                    items = items_from_file(Path(path))
                finally:
                    safe_rmtree(tmp)
                if not items:
                    return await reply(event, "❌ <b>No supported URLs found in the file.</b>")
                dest = chat_id if await db.is_dlauto_chat(chat_id) else None
                _spawn(run_batch(app, event, items, force=text.startswith(".batch -f"), dest=dest))
            return

        # ---- single / multiple links ----
        force, caption = False, None
        if re.match(r"\.dl(\s|$)", text):
            rest = text[3:].strip()
            if rest.startswith("-f"):
                force, rest = True, rest[2:].strip()
            urls, caption = split_url_and_caption(rest, MAX_URLS_PER_MESSAGE)
            if not urls:
                return await reply(event, "ℹ️ Usage: <code>.dl [-f] &lt;url&gt; [your caption]</code>")
        elif await auto_enabled(chat_id):
            urls = extract_urls(text, MAX_URLS_PER_MESSAGE)
        else:
            return
        if not urls:
            return

        log.info("Queueing %d link(s) from chat %s", len(urls), chat_id)
        if settings.DELETE_TRIGGER_MESSAGE:
            try:
                await event.delete()
            except Exception:
                pass
        dest = chat_id if await db.is_dlauto_chat(chat_id) else None  # .dlauto chat: answer in the same chat
        for url in urls:
            if not app.queue.enqueue(Job(url=url, force=force, custom_caption=caption, dest_chat=dest)):
                log.info("already queued: %s", url)


async def on_job_result(app: App, job: Job, result: JobResult) -> None:
    """Queue callback: batch bookkeeping or a Saved-Messages notification."""
    if job.batch is not None:
        if job.batch_id is not None and job.item_idx is not None:
            await db.batch_item_set(job.batch_id, job.item_idx, result.status, result.error or None)
        job.batch.record(job.url, result)
        return
    if not job.notify:
        return
    if job.dest_chat is not None and result.status == "done":
        return  # the post itself is the confirmation
    u = esc(job.url)
    if result.status == "done":
        extra = f" (+{result.comments} comments)" if result.comments else ""
        link = f"\n{esc(result.link)}" if result.link else ""
        await app.pipeline.notify(f"✅ <b>Posted</b>{extra}{link}\n<code>{u}</code>")
    elif result.status == "skipped":
        link = f"\n{esc(result.link)}" if result.link else ""
        await app.pipeline.notify(f"⏭ <b>Already posted</b> (use <code>.dl -f</code> to force){link}\n<code>{u}</code>")
    else:
        await app.pipeline.notify(f"❌ <b>Failed</b>: {esc(result.error)}\n<code>{u}</code>")
