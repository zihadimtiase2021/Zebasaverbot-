"""Route based message copier: every new message of a SOURCE chat is re-posted into a DESTINATION chat.

* Works with channels, groups (also private ones) - anything your own account is a member of.
* It never uses Telegram's "forward" (so "restricted / forwarding disabled" chats work too): the media is
  **downloaded first, then uploaded again** as a brand new message, text formatting is kept.
* Every copy ends with a serial number (``ZF-000123``, counting up) so you can find / reference it later
  (``.fwd id <serial>``).  What was copied is remembered per route (message-id ranges, kept in Saved Messages too),
  so a message is never copied twice - not by the live watcher, not by ``Forward all``, not after a restart.
* Links are removed from the copied text (switchable) and so are the words of the ban list.
* Source and destination are set from the chats themselves:

      .fwdsrc            (send it inside the SOURCE chat)
      .fwddst            (send it inside the DESTINATION chat)      -> route created
      .fwd @source @destination                                      -> same thing in one line
      .fwd list | watch|unwatch [n] | del <n> | clear | id <uid>
      .fwd all [n]       (Forward all: copy everything of the source that was not copied yet)
      .fwd stop | .fwd progress | .fwd links on|off | .fwd ban ...

Routes are stored in Saved Messages together with the other runtime settings (a free Render instance has no
disk), so they survive restarts and redeploys.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from telethon import errors, events, utils
from telethon.tl.types import MessageMediaWebPage

from unified_core.cleanup import safe_rmtree
from unified_core.config import settings
from unified_core.database import db
from unified_core.tg import parse_chat_ref, resolve_peer
from unified_core.utils import NOTIFY_MARK, esc, short_error
from unified_services import banwords, watermark
from unified_services.fwd_ledger import Ledger, route_key

log = logging.getLogger("zeba.forward")

ROUTES_KEY = "FWD_ROUTES"
BAN_KEY = "FWD_BAN"
LINKS_KEY = "FWD_LINKS"
MIX_KEY = "FWD_MIX"
JOBS_KEY = "FWD_JOBS"                           # routes whose "Forward all" is running -> resumed after a redeploy
MIX_BATCH = 5                                  # messages per route and turn in Mixing mode
TEXT_LIMIT = 4096
NO_CAPTION = {"sticker", "video_note"}        # Telegram cannot attach a caption to these
_COMMAND = re.compile(r"^\.[A-Za-z]+(\s|$)")  # my own ".dl", ".fwdsrc" ... messages are never copied


class TooLarge(Exception):
    """A file is bigger than the upload limit of this account."""


# ------------------------------------------------------------------ pure helpers
def new_uid(prefix: str | None = None, serial: int | None = None) -> str:
    """Serial number: prefix + counter, e.g. ZF-000123 (counter given), else prefix + time + random (legacy form)."""
    prefix = prefix or settings.FWD_ID_PREFIX
    if serial is not None:
        return f"{prefix}-{serial:06d}"
    return f"{prefix}-{int(time.time()):X}{secrets.token_hex(2).upper()}"


def msg_link(chat_id: int, msg_id: int | None) -> str:
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{msg_id}" if s.startswith("-100") and msg_id else ""


def build_html(raw: str, entities, uid: str, limit: int) -> str:
    """Original text (formatting kept, cut to fit `limit`) + blank line + the id in <code> (tap to copy).

    The invisible NOTIFY_MARK in front makes the userbot ignore its own copy (no loops, no ``.dlauto`` triggers).
    """
    from telethon.extensions import html as tg_html
    from telethon.helpers import add_surrogate, del_surrogate

    raw = raw or ""
    tail = (f"\n\n{uid}" if raw else uid)
    budget = max(0, limit - len(tail) - 1)  # -1: NOTIFY_MARK
    s = add_surrogate(raw)                  # entity offsets are UTF-16 units
    ents = list(entities or [])
    if len(s) > budget:
        s = s[: max(0, budget - 1)]
        if s and 0xD800 <= ord(s[-1]) <= 0xDBFF:  # never cut a surrogate pair in half
            s = s[:-1]
        keep = len(s)
        ents = [e for e in ents if e.offset + e.length <= keep]
        cut = del_surrogate(s) + "…"
    else:
        cut = raw
    try:
        body = tg_html.unparse(cut, ents)
    except Exception as exc:  # exotic entity: plain text is better than losing the message
        log.debug("html unparse failed (%s) - sending plain text", exc)
        body = _html.escape(cut)
    sep = "\n\n" if raw else ""
    return f"{NOTIFY_MARK}{body}{sep}<code>{uid}</code>"


def _media_kind(m) -> str | None:
    if m.media is None or isinstance(m.media, MessageMediaWebPage):  # link previews are not files
        return None
    if m.photo:
        return "photo"
    if not m.document:
        return None  # poll, location, contact, dice ... : not copied
    if m.sticker:
        return "sticker"
    if m.video_note:
        return "video_note"
    if m.voice:
        return "voice"
    if m.gif:
        return "gif"
    if m.video:
        return "video"
    if m.audio:
        return "audio"
    return "document"


@dataclass
class FileSpec:
    path: str | None
    kind: str
    document: object | None = None   # stickers are re-sent by reference (nothing to download)
    attributes: list | None = None   # filename, duration, size, "voice", "round video" ... of the original
    thumb: str | None = None
    force_document: bool = False


@dataclass
class Item:
    src: int
    routes: list
    msgs: list = field(default_factory=list)
    job: dict | None = None   # "Forward all" progress record this item belongs to (None = live watcher)


# ------------------------------------------------------------------ the forwarder
class Forwarder:
    def __init__(self, userbot, pipeline, me_id: int = 0, store=None) -> None:
        self.userbot = userbot
        self.pipeline = pipeline
        self.me_id = me_id
        self.store = store                 # RuntimeConfig (Saved Messages) or None -> database settings
        self.routes: list[dict] = []       # {"src","dst","src_name","dst_name","active"}
        self.bans: list[str] = []          # words / urls cut out of copied captions
        self.strip_links = bool(settings.FWD_STRIP_LINKS)   # remove every link from copied text
        self.resume_keys: list[str] = []   # route keys with an unfinished Forward all (saved in Saved Messages)
        self._shutting_down = False        # True while the process stops: interrupted jobs must stay on the list
        self._bg_tasks: set = set()
        self.mixing = False                # Forward all over several routes: 5 messages per route, round-robin
        self.ledger = Ledger(userbot, db)  # what was copied already (per route) + serial counter
        self.jobs: dict[str, dict] = {}    # route key -> progress of a running / finished "Forward all"
        self.pending: dict | None = None   # source chosen with .fwdsrc, waiting for .fwddst
        self._q: asyncio.Queue[Item] = asyncio.Queue(maxsize=1000)
        self._worker: asyncio.Task | None = None
        self._sent: deque = deque(maxlen=4000)   # (chat_id, msg_id) of our own copies
        self._busy: dict[int, int] = {}          # destination chats we are sending to right now
        self._registered = False

    # ---------------------------------------------------------------- lifecycle
    async def load(self) -> None:
        data = None
        if self.store is not None and isinstance(self.store.saved.get(ROUTES_KEY), list):
            data = self.store.saved[ROUTES_KEY]
        else:
            try:
                raw = await db.get_setting("fwd_routes")
                data = json.loads(raw) if raw else None
            except Exception as exc:
                log.warning("could not read saved forwarding routes: %s", exc)
        self.routes = [
            {"src": int(r["src"]), "dst": int(r["dst"]), "src_name": str(r.get("src_name", "")),
             "dst_name": str(r.get("dst_name", "")), "active": bool(r.get("active", True))}
            for r in (data or []) if isinstance(r, dict) and isinstance(r.get("src"), int)
            and isinstance(r.get("dst"), int)
        ]
        saved = self.store.saved.get(BAN_KEY) if self.store is not None else None
        self.bans = ([str(b) for b in saved if str(b).strip()] if isinstance(saved, list)
                     else banwords.split_bans(getattr(settings, "FWD_BAN", "")))
        if self.store is not None and isinstance(self.store.saved.get(LINKS_KEY), bool):
            self.strip_links = self.store.saved[LINKS_KEY]
        if self.store is not None and isinstance(self.store.saved.get(MIX_KEY), bool):
            self.mixing = self.store.saved[MIX_KEY]
        if self.store is not None and isinstance(self.store.saved.get(JOBS_KEY), list):
            self.resume_keys = [k for k in self.store.saved[JOBS_KEY] if isinstance(k, str)]
        await self.ledger.load()
        if self.routes:
            log.info("Forwarding: %d route(s) loaded", len(self.routes))

    def register(self) -> None:
        if self._registered:
            return
        self._registered = True
        self.userbot.add_event_handler(self._on_message, events.NewMessage())
        self.userbot.add_event_handler(self._on_album, events.Album())
        self._worker = asyncio.create_task(self._run(), name="zeba-forwarder")

    async def stop(self, grace: float = 20.0) -> None:
        self._shutting_down = True                   # the jobs below are interrupted, not finished: keep them saved
        for job in self.jobs.values():
            job["stop"] = True                       # Forward all: stop reading, drop what is not copied yet
        tasks = [j["task"] for j in self.jobs.values() if j.get("task") is not None]
        if tasks:
            _, still = await asyncio.wait(tasks, timeout=5)
            for t in still:
                t.cancel()
        try:
            await self._stop_worker(grace)
        finally:
            await self.ledger.close()                # write the last copies into Saved Messages

    async def _stop_worker(self, grace: float) -> None:
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._q.join(), grace)
        except asyncio.TimeoutError:
            log.warning("forwarder: %d queued item(s) dropped at shutdown", self._q.qsize())
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None

    async def _persist(self) -> None:
        if self.store is not None:
            self.store.saved[ROUTES_KEY] = self.routes
            self.store.saved[BAN_KEY] = self.bans
            self.store.saved[LINKS_KEY] = self.strip_links
            self.store.saved[MIX_KEY] = self.mixing
            self.store.saved[JOBS_KEY] = list(self.resume_keys)
            try:
                await self.store.save()
            except Exception as exc:
                log.warning("could not save routes to Saved Messages: %s", exc)
        try:
            await db.set_setting("fwd_routes", json.dumps(self.routes))
        except Exception as exc:
            log.debug("could not save routes to the database: %s", exc)

    # ---------------------------------------------------------------- incoming events
    def _routes_for(self, chat_id: int) -> list[dict]:
        return [r for r in self.routes if r["active"] and r["src"] == chat_id]

    def _is_ours(self, chat_id: int, msg) -> bool:
        if (chat_id, msg.id) in self._sent:
            return True
        raw = getattr(msg, "raw_text", "") or ""
        if NOTIFY_MARK in raw:
            return True
        if getattr(msg, "out", False):
            if _COMMAND.match(raw):          # a command typed by me (".fwdsrc", ".dl ...")
                return True
            if self._busy.get(chat_id):      # echo of a copy we are uploading right now
                return True
        return False

    def _copyable(self, chat_id: int, msg) -> bool:
        """Service messages, our own copies / commands and messages without text or a copyable file are skipped."""
        if getattr(msg, "action", None) is not None:
            return False  # service messages (joined, pinned, ...) are not copied
        if self._is_ours(chat_id, msg):
            return False
        return bool(getattr(msg, "raw_text", "") or _media_kind(msg))

    def _wanted(self, chat_id: int, msg) -> list[dict]:
        if not settings.FWD_ENABLED:
            return []
        routes = self._routes_for(chat_id)
        if not routes or not self._copyable(chat_id, msg):
            return []
        return routes

    async def _on_message(self, event) -> None:
        try:
            msg = event.message
            if msg.grouped_id:               # albums arrive through _on_album as one unit
                return
            routes = self._wanted(event.chat_id, msg)
            if routes:
                self._enqueue(Item(event.chat_id, routes, [msg]))
        except Exception:
            log.exception("forwarder: message handler error")

    async def _on_album(self, event) -> None:
        try:
            msgs = list(event.messages)
            if not msgs:
                return
            routes = self._wanted(event.chat_id, msgs[0])
            if routes:
                self._enqueue(Item(event.chat_id, routes, msgs))
        except Exception:
            log.exception("forwarder: album handler error")

    def _enqueue(self, item: Item) -> None:
        try:
            self._q.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("forwarder queue is full - message %s dropped", item.msgs[0].id)

    # ---------------------------------------------------------------- worker
    async def _run(self) -> None:
        while True:
            item = await self._q.get()
            job = item.job
            try:
                status = await self._copy(item)
                if job is not None:
                    job[status] = job.get(status, 0) + 1
                    if status == "sent":           # last message of this route that was really forwarded
                        job["last_id"] = max(job.get("last_id", 0), max(m.id for m in item.msgs))
                    job["streak"] = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("forwarder: copy failed")
                if job is not None:
                    job["failed"] += 1
                    job["failed_ids"].append(item.msgs[0].id)
                    job["streak"] = job.get("streak", 0) + 1
                    if job["streak"] >= 5 and not job["stop"]:
                        job["stop"] = True
                        job["note"] = "stopped after 5 failures in a row"
                if job is None or job["failed"] <= 3:  # a broken route must not flood the chat with errors
                    await self.pipeline.notify(
                        f"❌ <b>Forward failed</b>\n<code>{esc(short_error(exc))}</code>\n"
                        f"{esc(msg_link(item.src, item.msgs[0].id) or str(item.src))}")
            finally:
                if job is not None:
                    job["pending"] -= 1
                self._q.task_done()

    def _mark(self, src: int, dst: int, msgs) -> None:
        key = route_key(src, dst)
        for m in msgs:
            self.ledger.add(key, m.id)

    async def _copy(self, item: Item) -> str:
        """Copy one message / album to every route of the item. Returns sent | seen | skipped | empty | stopped."""
        if item.job is not None and item.job["stop"]:
            return "stopped"
        msgs = sorted(item.msgs, key=lambda m: m.id)
        ids = [m.id for m in msgs]
        todo = []
        for r in item.routes:
            key = route_key(item.src, r["dst"])
            if self.ledger.has_all(key, ids):
                continue
            if await db.fwd_seen(item.src, msgs[0].id, r["dst"]):  # copied before the ledger existed
                self._mark(item.src, r["dst"], msgs)
                continue
            todo.append(r)
        if not todo:
            return "seen"
        workdir = settings.tmp_dir / f"fwd_{uuid.uuid4().hex[:8]}"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            raw, ents, specs, skipped = await self._prepare(msgs, workdir)
            where = esc(msg_link(item.src, msgs[0].id) or str(item.src))
            if not specs and not raw:
                for r in todo:                       # nothing left to send: do not look at it again
                    self._mark(item.src, r["dst"], msgs)
                if skipped:
                    await self.pipeline.notify(
                        f"⚠️ <b>Not copied</b> (file bigger than the upload limit)\n{where}")
                    return "skipped"
                return "empty"                       # e.g. a message that only held a link
            preview = isinstance(msgs[0].media, MessageMediaWebPage) and not self.strip_links
            for route in todo:
                await self._deliver(route, msgs[0], raw, ents, specs, preview)
                self._mark(item.src, route["dst"], msgs)
                await asyncio.sleep(settings.FWD_DELAY)
            if skipped:
                await self.pipeline.notify(f"⚠️ {skipped} file(s) skipped: bigger than the upload limit ({where})")
            return "sent"
        finally:
            safe_rmtree(workdir)

    async def _prepare(self, msgs, workdir):
        holder = next((m for m in msgs if m.raw_text), None)
        raw = holder.raw_text if holder else ""
        ents = holder.entities if holder else None
        if self.strip_links:
            raw, ents = banwords.strip_links(raw, ents)   # 1) every link goes
        raw, ents = banwords.strip_banned(raw, ents, self.bans)  # 2) then the banned words / @names
        specs: list[FileSpec] = []
        skipped = 0
        for m in msgs:
            try:
                spec = await self._download(m, workdir)
            except TooLarge:
                skipped += 1
                continue
            if spec:
                specs.append(spec)
        return raw, ents, specs, skipped

    async def _download(self, m, workdir) -> FileSpec | None:
        kind = _media_kind(m)
        if kind is None:
            return None
        if kind == "sticker":
            return FileSpec(None, kind, document=m.document)
        if (getattr(m.file, "size", 0) or 0) > self.pipeline.max_bytes:
            raise TooLarge()
        d = workdir / str(m.id)
        d.mkdir(parents=True, exist_ok=True)
        path = await self.pipeline._retry(lambda: self.userbot.download_media(m, file=str(d) + os.sep))
        if not path:
            raise RuntimeError(f"download of message {m.id} returned nothing")
        if kind in ("photo", "video", "gif") and watermark.is_active(settings, "fwd"):
            new = await watermark.apply(path, settings, kind)
            if new != str(path):
                path = new
        spec = FileSpec(str(path), kind)
        if m.document:
            spec.attributes = list(m.document.attributes or [])
            for a in spec.attributes:  # the watermark can change the extension (e.g. .gif/.mov -> .mp4)
                if hasattr(a, "file_name") and a.file_name:
                    a.file_name = os.path.splitext(a.file_name)[0] + os.path.splitext(str(path))[1]
            spec.force_document = kind == "document"
            if kind in ("video", "gif", "video_note") and getattr(m.document, "thumbs", None):
                try:
                    spec.thumb = await self.userbot.download_media(m, file=str(d / "thumb.jpg"), thumb=-1)
                except Exception as exc:
                    log.debug("thumbnail skipped: %s", exc)
        return spec

    # ---------------------------------------------------------------- Forward all (history of the source)
    def _job_for(self, route: dict) -> dict | None:
        return self.jobs.get(route_key(route["src"], route["dst"]))

    def start_all(self, routes: list[dict]) -> str:
        """Copy everything of each source that is not in the ledger yet (oldest first). Returns the answer text."""
        if not settings.FWD_ENABLED:
            raise ValueError("FWD_ENABLED=false: copying is switched off in the environment")
        if not routes:
            raise ValueError("no routes yet - create one with .fwdsrc / .fwddst")
        started, running = [], []
        new_jobs: list[tuple[dict, dict]] = []
        for r in routes:
            old = self._job_for(r)
            if old is not None and old["state"] == "running":
                running.append(r)
                continue
            job = {"state": "running", "src": r["src"], "dst": r["dst"], "src_name": r["src_name"],
                   "dst_name": r["dst_name"], "stop": False, "pending": 0, "scanned": 0, "sent": 0, "seen": 0,
                   "skipped": 0, "empty": 0, "stopped": 0, "failed": 0, "failed_ids": [], "streak": 0,
                   "total": 0, "note": "", "started": time.time(), "task": None, "last_id": 0, "mixed": False}
            self.jobs[route_key(r["src"], r["dst"])] = job
            new_jobs.append((r, job))
            started.append(r)
        if new_jobs:                              # saved first: a redeploy right now still resumes these routes
            for r, _job in new_jobs:
                k = route_key(r["src"], r["dst"])
                if k not in self.resume_keys:
                    self.resume_keys.append(k)
            self._spawn(self._persist())
        if self.mixing and len(new_jobs) > 1:     # Mixing: ONE task takes turns between the routes
            for _r, job in new_jobs:
                job["mixed"] = True
            task = asyncio.create_task(self._mix_backfill(new_jobs), name="zeba-fwd-mix")
            for _r, job in new_jobs:
                job["task"] = task
        else:
            for r, job in new_jobs:
                job["task"] = asyncio.create_task(self._backfill(r, job), name="zeba-fwd-all")
        if not started:
            return "⏳ Forward all is already running for " + (
                "that route" if len(routes) == 1 else "these routes") + ". <code>.fwd progress</code>"
        names = ", ".join(f"{esc(r['src_name'])} ➜ {esc(r['dst_name'])}" for r in started)
        extra = f"\n(already running: {len(running)})" if running else ""
        mode = (f"\n🔀 <b>Mixing ON</b>: {MIX_BATCH} messages per route, taking turns"
                if len(new_jobs) > 1 and self.mixing else "")
        return (f"📥 <b>Forward all</b> started: {names}{extra}{mode}\n"
                "Only messages that were <b>not copied before</b> are sent, oldest first, each with its serial number.\n"
                "<code>.fwd progress</code> · <code>.fwd stop</code>")

    def stop_all(self, routes: list[dict]) -> int:
        n = 0
        for r in routes:
            job = self._job_for(r)
            if job is not None and job["state"] == "running" and not job["stop"]:
                job["stop"] = True
                job["note"] = "stopped by you"
                n += 1
        return n

    async def _queue_group(self, job: dict, route: dict, msgs: list) -> bool:
        key = route_key(route["src"], route["dst"])
        ids = [m.id for m in msgs]
        if self.ledger.has_all(key, ids):
            job["seen"] += 1
            return False
        if not any(self._copyable(route["src"], m) for m in msgs):
            for i in ids:                      # service message / empty: resolved, never looked at again
                self.ledger.add(key, i)
            job["skipped"] += 1
            return False
        while job["pending"] >= 20 and not job["stop"]:   # leave room in the queue for the live watcher
            await asyncio.sleep(0.2)
        if job["stop"]:
            return False
        job["pending"] += 1
        await self._q.put(Item(route["src"], [route], msgs, job))
        return True                            # queued (Mixing counts only these toward its 5 per turn)

    async def _backfill(self, route: dict, job: dict) -> None:
        key = route_key(route["src"], route["dst"])
        floor = self.ledger.resume_from(key)
        last = floor
        group: list = []
        try:
            try:
                job["total"] = int((await self.userbot.get_messages(route["src"], limit=0)).total or 0)
            except Exception as exc:
                log.debug("could not count the messages of %s: %s", route["src"], exc)
            async for m in self.userbot.iter_messages(route["src"], reverse=True, min_id=floor):
                if job["stop"] or route not in self.routes:
                    job["stop"] = True
                    break
                job["scanned"] += 1
                last = max(last, m.id)
                gid = getattr(m, "grouped_id", None)
                if group and (not gid or gid != getattr(group[0], "grouped_id", None)):
                    await self._queue_group(job, route, group)
                    group = []
                if gid:
                    group.append(m)
                else:
                    await self._queue_group(job, route, [m])
            if group and not job["stop"]:
                await self._queue_group(job, route, group)
            while job["pending"] > 0:          # wait until the worker has finished this route's items
                await asyncio.sleep(0.5)
            if job["failed_ids"]:              # everything below the first failure is resolved, retry the rest
                limit = min(job["failed_ids"]) - 1
            else:
                limit = 0 if job["stop"] else last
            if limit >= 1:
                self.ledger.add(key, 1, limit)  # also covers deleted ids, so the next run starts at the end
            job["state"] = "stopped" if job["stop"] else ("done with errors" if job["failed"] else "done")
        except asyncio.CancelledError:
            job["state"] = "stopped"
            raise
        except Exception as exc:
            log.exception("forward all failed")
            job["state"] = "failed"
            job["note"] = short_error(exc)
        finally:
            job["task"] = None
            await self.ledger.flush()
            await self._job_ended(job)
            try:
                await self.pipeline.notify(self._fmt_job(job, final=True))
            except Exception as exc:
                log.debug("could not send the Forward all summary: %s", exc)

    # ---------------------------------------------------------------- Mixing (several routes, taken in turns)
    async def _iter_groups(self, route: dict, job: dict, floor: int):
        """Yield the not yet resolved messages of a route's source, oldest first; an album is ONE group."""
        group: list = []
        async for m in self.userbot.iter_messages(route["src"], reverse=True, min_id=floor):
            if job["stop"] or route not in self.routes:
                job["stop"] = True
                return
            job["scanned"] += 1
            gid = getattr(m, "grouped_id", None)
            if group and (not gid or gid != getattr(group[0], "grouped_id", None)):
                yield group
                group = []
            if gid:
                group.append(m)
            else:
                yield [m]
        if group and not job["stop"]:
            yield group

    async def _mix_backfill(self, pairs: list[tuple[dict, dict]]) -> None:
        """Forward all for several routes in turns: MIX_BATCH messages of route 1, then of route 2 ... then the next
        MIX_BATCH of route 1 ... A route that runs out of messages simply drops out of the rotation. Every route
        keeps its own job / ledger, so the result is the same as separate runs - only the ORDER is mixed."""
        states = []
        for route, job in pairs:
            key = route_key(route["src"], route["dst"])
            floor = self.ledger.resume_from(key)
            states.append({"route": route, "job": job, "key": key, "last": floor, "done": False,
                           "gen": self._iter_groups(route, job, floor)})
        try:
            for st in states:
                try:
                    st["job"]["total"] = int((await self.userbot.get_messages(st["route"]["src"], limit=0)).total or 0)
                except Exception as exc:
                    log.debug("could not count the messages of %s: %s", st["route"]["src"], exc)
            while any(not st["done"] for st in states):
                for st in states:
                    if st["done"]:
                        continue
                    job, route = st["job"], st["route"]
                    taken = 0
                    while taken < MIX_BATCH:           # this route's turn: up to 5 messages that really need sending
                        if job["stop"] or route not in self.routes:
                            job["stop"] = True
                            st["done"] = True
                            break
                        try:
                            group = await st["gen"].__anext__()
                        except StopAsyncIteration:
                            st["done"] = True          # fewer than 5 left: the turn just ends early
                            break
                        st["last"] = max(st["last"], max(m.id for m in group))
                        if await self._queue_group(job, route, group):
                            taken += len(group)        # an album is never cut in the middle
                    if job["stop"]:
                        st["done"] = True
        except asyncio.CancelledError:
            for st in states:
                st["job"]["state"] = "stopped"
            raise
        except Exception as exc:
            log.exception("mixed forward all failed")
            for st in states:
                st["job"]["state"] = "failed"
                st["job"]["note"] = short_error(exc)
                st["job"]["stop"] = True
        finally:
            for st in states:                          # an early stop must not leave the readers half-open
                try:
                    await st["gen"].aclose()
                except Exception as exc:
                    log.debug("could not close the reader of %s: %s", st["route"].get("src_name"), exc)
            for st in states:
                await self._finish_mixed(st)

    async def _finish_mixed(self, st: dict) -> None:
        job = st["job"]
        try:
            while job["pending"] > 0:
                await asyncio.sleep(0.5)
            if job["state"] == "running":
                if job["failed_ids"]:
                    limit = min(job["failed_ids"]) - 1
                else:
                    limit = 0 if job["stop"] else st["last"]
                if limit >= 1:
                    self.ledger.add(st["key"], 1, limit)
                job["state"] = "stopped" if job["stop"] else ("done with errors" if job["failed"] else "done")
        except Exception as exc:
            log.exception("could not finish a mixed job")
            job["state"] = "failed"
            job["note"] = short_error(exc)
        finally:
            job["task"] = None
            await self.ledger.flush()
            await self._job_ended(job)
            try:
                await self.pipeline.notify(self._fmt_job(job, final=True))
            except Exception as exc:
                log.debug("could not send the Forward all summary: %s", exc)

    # ---------------------------------------------------------------- resume after a redeploy
    def _spawn(self, coro) -> None:
        """Run a coroutine in the background (start_all is a plain function, the save is async)."""
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:                       # no running loop (plain unit tests)
            coro.close()
            return
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    async def _job_ended(self, job: dict) -> None:
        """A Forward all ended by itself (finished / stopped by the person / failed): it must not restart later.
        When the process is shutting down the job was only interrupted, so it stays on the list."""
        if self._shutting_down:
            return
        key = route_key(job["src"], job["dst"])
        if key in self.resume_keys:
            self.resume_keys.remove(key)
            try:
                await self._persist()
            except Exception as exc:
                log.warning("could not update the saved Forward all list: %s", exc)

    async def clear_resume(self) -> None:
        if self.resume_keys:
            self.resume_keys.clear()
            await self._persist()

    async def resume_jobs(self) -> int:
        """At start-up: continue every Forward all that was running when the bot stopped. The ledger knows what was
        already copied, so only the rest is sent (and nothing twice). Returns the number of routes resumed."""
        if not self.resume_keys:
            return 0
        by_key = {route_key(r["src"], r["dst"]): r for r in self.routes}
        gone = [k for k in self.resume_keys if k not in by_key]
        if gone:                                    # route deleted meanwhile
            self.resume_keys = [k for k in self.resume_keys if k in by_key]
            await self._persist()
        routes = [by_key[k] for k in self.resume_keys]
        if not routes:
            return 0
        try:
            self.start_all(routes)
        except ValueError as exc:                   # e.g. FWD_ENABLED=false: stay saved, try again next start
            log.warning("Forward all not resumed: %s", exc)
            return 0
        names = ", ".join(f"{esc(r['src_name'])} ➜ {esc(r['dst_name'])}" for r in routes)
        try:
            await self.pipeline.notify(f"🔄 <b>Forward all resumed</b> after the restart: {names}\n"
                                       "Only what was not copied yet is sent.")
        except Exception as exc:
            log.debug("could not send the resume notice: %s", exc)
        return len(routes)

    async def set_mixing(self, on: bool) -> str:
        self.mixing = on
        await self._persist()
        if on:
            return (f"🔀 Mixing <b>ON</b>: Forward all over several routes sends {MIX_BATCH} messages of route 1, "
                    f"then {MIX_BATCH} of route 2 … and starts again until every route is finished.")
        return "🔀 Mixing <b>OFF</b>: routes are forwarded one after the other (default)."

    def _fmt_job(self, job: dict, final: bool = False) -> str:
        icon = {"running": "⏳", "done": "✅", "done with errors": "⚠️", "stopped": "⏹", "failed": "❌"}.get(
            job["state"], "ℹ️")
        scanned = f"{job['scanned']}/{job['total']}" if job["total"] else str(job["scanned"])
        head = f"{icon} <b>Forward all</b> {esc(job['src_name'])} ➜ {esc(job['dst_name'])} — {esc(job['state'])}"
        body = (f"\n📤 copied: <b>{job['sent']}</b> · ⏭ already copied: {job['seen']} · "
                f"🚫 skipped: {job['skipped'] + job['empty']} · ❌ failed: {job['failed']}\n🔎 looked at: {scanned}")
        if job.get("last_id"):
            body += f"\n🏁 last forwarded message ID: <b>{job['last_id']}</b>" + (" · 🔀 mixed" if job.get("mixed") else "")
        if job["failed"] and final:
            body += "\nFailed ones stay open: run <code>.fwd all</code> again to retry only those."
        if job["note"]:
            body += f"\n<i>{esc(job['note'])}</i>"
        return head + body

    def _fmt_progress(self) -> str:
        if not self.jobs:
            return "ℹ️ Nothing was started yet. <code>.fwd all</code> copies everything not copied before."
        return "\n\n".join(self._fmt_job(j) for j in self.jobs.values())

    # ---------------------------------------------------------------- upload
    async def _uid_for(self, route: dict, src_msg) -> tuple[str, int]:
        """Next serial number (ZF-000123). The database row doubles as a lock against a duplicate serial."""
        for _ in range(100):
            n = self.ledger.next_serial()
            uid = new_uid(serial=n)
            if await db.fwd_reserve(uid, route["src"], src_msg.id, route["dst"]):
                return uid, n
        raise RuntimeError("could not allocate a serial number")

    async def _deliver(self, route: dict, first_msg, raw, ents, specs, preview: bool) -> None:
        dst = route["dst"]
        uid, serial = await self._uid_for(route, first_msg)
        self._busy[dst] = self._busy.get(dst, 0) + 1
        try:
            sent = await self._send(dst, uid, raw, ents, specs, preview)
        except BaseException:
            await db.fwd_drop(uid)
            self.ledger.release(serial)              # nothing was posted: the number is handed out again
            raise
        finally:
            self._busy[dst] -= 1
            if not self._busy[dst]:
                self._busy.pop(dst, None)
        for s in sent:
            self._sent.append((dst, s.id))
        await db.fwd_set_dst(uid, sent[0].id)
        try:  # hidden details for /find (admin only, never posted to the channel)
            default = await db.fwd_default_get(route["src"])   # /defnote of this source group / channel
            await db.fwd_set_details(uid, route.get("src_name", ""), raw or "", len(specs), self.me_id, default)
        except Exception as exc:
            log.debug("could not save the details of %s: %s", uid, exc)
        log.info("copied %s:%s -> %s as %s", route["src"], first_msg.id, dst, uid)

    async def _send(self, dst, uid: str, raw, ents, specs: list[FileSpec], preview: bool) -> list:
        retry = self.pipeline._retry
        if not specs:
            text = build_html(raw, ents, uid, TEXT_LIMIT)
            return [await retry(lambda: self.userbot.send_message(
                dst, text, parse_mode="html", link_preview=preview))]
        if len(specs) == 1 and specs[0].kind in NO_CAPTION:  # sticker / round video: id goes in a reply
            media = await retry(lambda: self._send_one(dst, specs[0], None))
            text = build_html(raw, ents, uid, TEXT_LIMIT)
            note = await retry(lambda: self.userbot.send_message(
                dst, text, parse_mode="html", reply_to=media.id, link_preview=False))
            return [media, note]
        caption = build_html(raw, ents, uid, self.pipeline.caption_limit)
        if len(specs) == 1:
            return [await retry(lambda: self._send_one(dst, specs[0], caption))]
        sent: list = []
        for i in range(0, len(specs), 10):           # Telegram albums hold at most 10 items
            chunk = specs[i:i + 10]
            cap = caption if i == 0 else None
            if len({s.force_document for s in chunk}) > 1:  # documents cannot share an album with photos
                sent += await self._send_each(dst, chunk, cap)
                continue
            try:
                res = await retry(lambda: self.userbot.send_file(
                    dst, [s.path for s in chunk], caption=cap, parse_mode="html",
                    force_document=chunk[0].force_document, supports_streaming=True, part_size_kb=512))
                sent += list(res) if isinstance(res, (list, tuple)) else [res]
            except errors.RPCError as exc:
                log.warning("album send failed (%s) - sending the files one by one", short_error(exc))
                sent += await self._send_each(dst, chunk, cap)
        return sent

    async def _send_each(self, dst, specs, caption) -> list:
        out = []
        for i, spec in enumerate(specs):
            out.append(await self.pipeline._retry(
                lambda spec=spec, i=i: self._send_one(dst, spec, caption if i == 0 else None)))
        return out

    async def _send_one(self, dst, spec: FileSpec, caption: str | None):
        if spec.kind == "sticker":
            return await self.userbot.send_file(dst, spec.document)
        kw: dict = {"caption": caption, "parse_mode": "html", "force_document": spec.force_document,
                    "part_size_kb": 512}
        if spec.attributes:
            kw["attributes"] = spec.attributes
        if spec.thumb:
            kw["thumb"] = spec.thumb
        if spec.kind in ("video", "gif"):
            kw["supports_streaming"] = True
        if spec.kind == "voice":
            kw["voice_note"] = True
        if spec.kind == "video_note":
            kw["video_note"] = True
        return await self.userbot.send_file(dst, spec.path, **kw)

    # ---------------------------------------------------------------- commands
    HELP = (
        "🔁 <b>Message forwarding</b> (copy = download + upload, works with private / restricted chats)\n"
        "1️⃣ In the <b>source</b> chat send <code>.fwdsrc</code>\n"
        "2️⃣ In the <b>destination</b> chat send <code>.fwddst</code>\n"
        "Or in one line: <code>.fwd @source @destination</code>\n"
        "👁 <b>Watch</b>: every new message of the source is copied automatically — "
        "<code>.fwd watch [n]</code> / <code>.fwd unwatch [n]</code>\n"
        "🔀 <b>Mixing</b>: <code>.fwd mix on|off</code> — with several routes, <code>.fwd all</code> sends 5 messages "
        "per route in turns\n"
        "📥 <b>Forward all</b>: <code>.fwd all [n]</code> copies the whole source, but only what was not copied yet "
        "· <code>.fwd progress</code> · <code>.fwd stop [n]</code>\n"
        "<code>.fwd list</code> · <code>.fwd del n</code> · <code>.fwd clear</code> · <code>.fwd id 123</code> · "
        "<code>.fwd reset n</code> (forget what was copied)\n"
        "🔗 <code>.fwd links on|off</code> — links are removed from copied text\n"
        "🚫 <code>.fwd ban word, @name</code> · <code>.fwd bans</code> · <code>.fwd unban n</code> — removed too\n"
        "Every copy ends with a serial number (ZF-000123); a message is never copied twice."
    )

    async def handle_command(self, event, cmd: str) -> str:
        name, _, arg = cmd.strip().partition(" ")
        arg = arg.strip()
        try:
            if name == ".fwdsrc":
                return await self._cmd_src(event, arg)
            if name == ".fwddst":
                return await self._cmd_dst(event, arg)
            return await self._cmd_fwd(arg)
        except ValueError as exc:
            return f"❌ {esc(str(exc))}"

    async def _entity(self, ref: str):
        try:
            return await resolve_peer(self.userbot, parse_chat_ref(ref))
        except RuntimeError as exc:
            raise ValueError("that chat was not found - this account must be a member of it") from exc

    async def _chat_of(self, event, arg: str):
        return await self._entity(arg) if arg else await event.get_chat()

    @staticmethod
    def _name(ent) -> str:
        try:
            return utils.get_display_name(ent) or "chat"
        except Exception:
            return "chat"

    async def _cmd_src(self, event, arg: str) -> str:
        ent = await self._chat_of(event, arg)
        self.pending = {"src": utils.get_peer_id(ent), "name": self._name(ent)}
        return (f"📤 Source: <b>{esc(self.pending['name'])}</b>\n"
                "Now open the <b>destination</b> chat and send <code>.fwddst</code>")

    async def _cmd_dst(self, event, arg: str) -> str:
        if not self.pending:
            raise ValueError("first send .fwdsrc in the source chat (or use .fwd @source @destination)")
        ent = await self._chat_of(event, arg)
        src = self.pending
        self.pending = None
        return await self._add_route(src["src"], src["name"], ent)

    async def _check_route(self, src_id: int, dst_ent) -> int:
        """Raise ValueError unless THIS account may create the route src -> dst. Returns the destination id."""
        dst_id = utils.get_peer_id(dst_ent)
        if dst_id == src_id:
            raise ValueError("source and destination must be different chats")
        if any(r["src"] == src_id and r["dst"] == dst_id for r in self.routes):
            raise ValueError("this route already exists (.fwd list)")
        if any(r["src"] == dst_id and r["dst"] == src_id for r in self.routes):
            raise ValueError("the reverse route exists - both directions would copy each other forever")
        if getattr(dst_ent, "broadcast", False):
            try:
                me = await self.userbot.get_me()
                perms = await self.userbot.get_permissions(dst_ent, me)
                if not (getattr(perms, "is_creator", False) or getattr(perms, "post_messages", False)):
                    raise ValueError("this account cannot post there - it needs the 'Post messages' admin right")
            except ValueError:
                raise
            except Exception as exc:  # could not verify: the first copy will tell
                log.warning("permission check skipped: %s", exc)
        return dst_id

    async def can_route(self, src_ref: str, dst_ref: str) -> str | None:
        """Used by the account choice: None = this account can make the route src -> dst (it is a member of both
        chats and may post in the destination), otherwise a short reason why it cannot."""
        try:
            src = await self._entity(src_ref)
            dst = await self._entity(dst_ref)
            await self._check_route(utils.get_peer_id(src), dst)
        except ValueError as exc:
            return str(exc)
        return None

    async def _add_route(self, src_id: int, src_name: str, dst_ent) -> str:
        dst_id = await self._check_route(src_id, dst_ent)
        dst_name = self._name(dst_ent)
        self.routes.append({"src": src_id, "dst": dst_id, "src_name": src_name, "dst_name": dst_name,
                            "active": True})
        await self._persist()
        return (f"✅ <b>Route #{len(self.routes)}</b>: {esc(src_name)} ➜ {esc(dst_name)}\n"
                "👁 Every new message there is now copied (download + upload) with a serial number.\n"
                "Old messages: <code>.fwd all</code> copies them (only what was not copied before).")

    def _fmt_routes(self) -> str:
        if not self.routes:
            return "ℹ️ No routes yet.\n\n" + self.HELP
        lines = ["🔁 <b>Forward routes</b>"]
        for i, r in enumerate(self.routes, 1):
            done = self.ledger.count(route_key(r["src"], r["dst"]))
            lines.append(f"{i}. {'👁' if r['active'] else '⏸'} {esc(r['src_name'])} <code>{r['src']}</code> ➜ "
                         f"{esc(r['dst_name'])} <code>{r['dst']}</code> · {done} copied")
        lines.append(f"\n👁 = watching new messages · ⏸ = paused · 🔗 links removed: {'yes' if self.strip_links else 'no'}")
        if len(self.routes) > 1:
            lines.append(f"🔀 Mixing: {'ON' if self.mixing else 'OFF'} (Forward all: {MIX_BATCH} messages per route in turns)")
        if not settings.FWD_ENABLED:
            lines.append("⚠️ FWD_ENABLED=false: copying is switched off in the environment.")
        return "\n".join(lines)

    def _fmt_bans(self) -> str:
        if not self.bans:
            return "🚫 <b>Ban list</b>: empty\nAdd: <code>.fwd ban word, https://spam.com, @name</code>"
        return ("🚫 <b>Ban list</b> (removed from every copied caption)\n"
                + "\n".join(f"{i}. <code>{esc(b)}</code>" for i, b in enumerate(self.bans, 1))
                + "\n<code>.fwd unban n|word</code> · <code>.fwd unban all</code>")

    async def _cmd_ban(self, sub: str, rest: str) -> str:
        if sub == "bans" or (sub == "ban" and not rest):
            return self._fmt_bans()
        if sub == "ban":
            added = [b for b in banwords.split_bans(rest) if b.lower() not in (x.lower() for x in self.bans)]
            self.bans += added
            await self._persist()
            return f"✅ Banned: {esc(', '.join(added)) or '(already in the list)'}\n" + self._fmt_bans()
        if rest.lower() == "all":
            self.bans.clear()
        elif rest.isdigit() and 1 <= int(rest) <= len(self.bans):
            self.bans.pop(int(rest) - 1)
        else:
            keep = [b for b in self.bans if b.lower() != rest.lower()]
            if len(keep) == len(self.bans):
                raise ValueError("not in the ban list (.fwd bans)")
            self.bans[:] = keep
        await self._persist()
        return "✅ Removed.\n" + self._fmt_bans()

    def _pick(self, arg: str) -> list[dict]:
        if not arg:
            return list(self.routes)
        if not arg.isdigit() or not 1 <= int(arg) <= len(self.routes):
            raise ValueError("no such route number (see .fwd list)")
        return [self.routes[int(arg) - 1]]

    async def _cmd_fwd(self, arg: str) -> str:
        parts = arg.split()
        sub = parts[0].lower() if parts else "list"
        rest = " ".join(parts[1:])
        if sub in ("list", "status"):
            return self._fmt_routes()
        if sub in ("help", "?"):
            return self.HELP
        if sub in ("on", "off", "watch", "unwatch"):
            targets = self._pick(rest)
            on = sub in ("on", "watch")
            for r in targets:
                r["active"] = on
            await self._persist()
            return (f"{'👁 Watching' if on else '⏸ Paused'}: {len(targets)} route(s).\n"
                    + ("New messages of the source are copied automatically.\n" if on else "") + self._fmt_routes())
        if sub in ("mix", "mixing"):
            if rest.lower() not in ("on", "off"):
                return (f"🔀 Mixing is <b>{'ON ✅' if self.mixing else 'OFF ❌'}</b>. "
                        "Use <code>.fwd mix on</code> / <code>.fwd mix off</code>")
            return await self.set_mixing(rest.lower() == "on")
        if sub == "all":
            return self.start_all(self._pick(rest))
        if sub in ("stop", "cancel"):
            n = self.stop_all(self._pick(rest))
            return f"⏹ Stopping {n} Forward all job(s)…" if n else "ℹ️ No Forward all is running."
        if sub in ("progress", "stats"):
            return self._fmt_progress()
        if sub == "links":
            if rest.lower() not in ("on", "off"):
                return (f"🔗 Link removal is <b>{'ON ✅' if self.strip_links else 'OFF ❌'}</b>. "
                        "Use <code>.fwd links on</code> / <code>.fwd links off</code>")
            self.strip_links = rest.lower() == "on"
            await self._persist()
            return f"🔗 Links in copied text: <b>{'removed' if self.strip_links else 'kept'}</b> (saved)."
        if sub == "reset":
            if not rest:
                raise ValueError("usage: .fwd reset <n>  (forget what was copied for that route)")
            (r,) = self._pick(rest)
            job = self._job_for(r)
            if job is not None and job["state"] == "running":
                raise ValueError("Forward all is running for that route - .fwd stop first")
            self.ledger.forget(route_key(r["src"], r["dst"]))
            return (f"♻️ Forgot what was copied for {esc(r['src_name'])} ➜ {esc(r['dst_name'])}. "
                    "The next <code>.fwd all</code> copies everything again.")
        if sub in ("ban", "bans", "unban"):
            return await self._cmd_ban(sub, rest)
        if sub == "del":
            if not rest:
                raise ValueError("usage: .fwd del <n>")
            (gone,) = self._pick(rest)
            self.routes.remove(gone)
            await self._persist()
            return f"🗑 Removed: {esc(gone['src_name'])} ➜ {esc(gone['dst_name'])}"
        if sub == "clear":
            n = len(self.routes)
            self.routes.clear()
            await self._persist()
            return f"🗑 {n} route(s) removed."
        if sub == "id":
            if not rest:
                raise ValueError("usage: .fwd id 123  (or ZF-000123)")
            uid = rest.strip().upper()
            if uid.isdigit():
                uid = new_uid(serial=int(uid))
            row = await db.fwd_get(uid)
            if not row:
                return "❌ Unknown id (the log only keeps the last 90 days and a free host may reset it)."
            a, b = msg_link(row["src_chat"], row["src_msg"]), msg_link(row["dst_chat"], row["dst_msg"])
            return (f"🆔 <code>{esc(row['uid'])}</code>\nsource: <code>{row['src_chat']}</code> msg {row['src_msg']} "
                    f"{esc(a)}\ncopy: <code>{row['dst_chat']}</code> msg {row['dst_msg']} {esc(b)}")
        if len(parts) == 2:  # .fwd <source> <destination>
            src = await self._entity(parts[0])
            dst = await self._entity(parts[1])
            return await self._add_route(utils.get_peer_id(src), self._name(src), dst)
        raise ValueError("unknown usage - send .fwd help")

    # ---------------------------------------------------------------- housekeeping
    def delete_later(self, chat_id: int, msg_id: int, delay: float = 20.0) -> None:
        """The answer to .fwdsrc / .fwddst is typed inside a real chat: remove it again after a few seconds."""
        async def go():
            await asyncio.sleep(delay)
            try:
                await self.userbot.delete_messages(chat_id, msg_id)
            except Exception as exc:
                log.debug("could not delete the command answer: %s", exc)

        t = asyncio.create_task(go())
        self._bg = getattr(self, "_bg", set())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
