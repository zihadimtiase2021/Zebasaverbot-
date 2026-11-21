"""Job model + the download -> translate -> upload -> comments pipeline."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from dataclasses import dataclass, field

from telethon import TelegramClient, errors, types

from unified_core.cleanup import dir_size, safe_rmtree
from unified_core.config import settings
from unified_core.database import db
from unified_core.utils import NOTIFY_MARK, esc, human_size, normalize_url, short_error
from unified_services import watermark
from unified_services.captions import build_caption
from unified_services.downloader import fetch_post
from unified_services.models import Comment, DownloadError, PostData
from unified_services.translator import Translator

log = logging.getLogger("zeba.pipeline")


# ------------------------------------------------------------------ models
@dataclass
class JobResult:
    status: str  # "done" | "skipped" | "failed"
    link: str = ""
    error: str = ""
    comments: int = 0


class BatchTracker:
    def __init__(self, total: int, ok: int = 0, skipped: int = 0, failed: int = 0,
                 failures: list[tuple[str, str]] | None = None) -> None:
        """`ok/skipped/failed/failures` start non-zero when a batch is resumed after a restart."""
        self.total = total
        self.ok = ok
        self.skipped = skipped
        self.failed = failed
        self.failures: list[tuple[str, str]] = list(failures or [])
        self.done_event = asyncio.Event()
        if self.done >= total:
            self.done_event.set()

    @property
    def done(self) -> int:
        return self.ok + self.skipped + self.failed

    def record(self, url: str, result: JobResult) -> None:
        if result.status == "done":
            self.ok += 1
        elif result.status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
            self.failures.append((url, result.error))
        if self.done >= self.total:
            self.done_event.set()


@dataclass
class Job:
    url: str
    force: bool = False
    notify: bool = True
    batch: BatchTracker | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    key: str = ""
    step: str = "Queued"
    custom_caption: str | None = None  # from a JSON batch: replaces the scraped caption
    batch_id: int | None = None        # persisted batch this job belongs to (resume after restart)
    item_idx: int | None = None
    dest_chat: int | None = None      # .dlauto chat: post the result into this chat instead of the channel


# ---------------------------------------------------------------- pipeline
class Pipeline:
    def __init__(self, userbot: TelegramClient, target, comment_chat, premium: bool, translator: Translator) -> None:
        self.userbot = userbot
        self.target = target
        self.comment_chat = comment_chat
        self.translator = translator
        self.caption_limit = 2048 if premium else 1024
        mib = settings.MAX_FILE_MB or (4000 if premium else 2000)
        self.max_bytes = int(mib * 1024 * 1024 * 0.98)
        self._upload_sem = asyncio.Semaphore(settings.UPLOAD_CONCURRENCY)
        self._comment_sem = asyncio.Semaphore(settings.COMMENT_CONCURRENCY)
        self._bg: set[asyncio.Task] = set()  # comment posting runs in the background, off the worker slots
        self.control_bot: TelegramClient | None = None  # set by main once the bot is running
        self.label = ""  # extra accounts: shown above every status message so you know which account it is about
        self._warned_bot = False

    # ---- public ----
    async def run(self, job: Job) -> JobResult:
        key = job.key or normalize_url(job.url) + (f"@{job.dest_chat}" if job.dest_chat is not None else "")
        if not job.force:
            prev = await db.job_get(key)
            if prev and prev["status"] == "done":
                return JobResult("skipped", link=prev.get("posted_link") or "")

        await db.job_start(key, job.url)
        workdir = settings.tmp_dir / uuid.uuid4().hex
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            return await asyncio.wait_for(self._execute(job, workdir, key), settings.JOB_TIMEOUT)
        except asyncio.CancelledError:
            await db.job_finish(key, "failed", error="cancelled")
            raise
        except asyncio.TimeoutError:
            await db.job_finish(key, "failed", error="timeout")
            return JobResult("failed", error="Timed out")
        except Exception as exc:
            err = short_error(exc)
            if isinstance(exc, DownloadError):
                log.warning("download failed for %s: %s", job.url, err)
            else:
                log.exception("job failed for %s", job.url)
            await db.job_finish(key, "failed", error=err)
            return JobResult("failed", error=err)
        finally:
            # Whatever happened (success, failure, timeout, cancel): nothing stays on disk.
            safe_rmtree(workdir)  # synchronous on purpose: must also run while being cancelled

    async def notify(self, text: str) -> None:
        """Status message: to the admins in the control bot chat, else (fallback) to Saved Messages."""
        if not settings.NOTIFY_SAVED_MESSAGES:
            return
        if self.label:
            text = f"👤 <b>{esc(self.label)}</b>\n{text}"
        if self.control_bot is not None:
            delivered = False
            for uid in settings.admin_ids:
                try:
                    await self.control_bot.send_message(uid, text, parse_mode="html", link_preview=False)
                    delivered = True
                except Exception as exc:
                    log.debug("bot notify to %s failed: %s", uid, exc)
            if delivered:
                return
            if not self._warned_bot:
                self._warned_bot = True
                log.warning("Control bot could not message the admin(s) - open your bot in Telegram and press "
                            "/start once. Using Saved Messages until then.")
        try:
            await self.userbot.send_message("me", NOTIFY_MARK + text, parse_mode="html", link_preview=False)
        except Exception as exc:
            log.debug("notify failed: %s", exc)

    async def drain(self, timeout: float | None = None) -> None:
        """Wait for background comment posting; whatever is still running afterwards is cancelled."""
        if self._bg:
            await asyncio.wait(set(self._bg), timeout=timeout)
        for t in list(self._bg):
            t.cancel()
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ---- internals ----
    async def _execute(self, job: Job, workdir, key: str) -> JobResult:
        tag = f"#zeba_{uuid.uuid4().hex[:8]}"
        data: PostData | None = None
        handed_off = False
        try:
            job.step = "Downloading media"
            data = await fetch_post(job.url, workdir, self.userbot, self.max_bytes)
            if watermark.is_active(settings, "url"):
                job.step = "Adding watermark"
                for item in data.media:
                    if item.path.exists() and item.kind in ("photo", "video"):
                        item.path = Path(await watermark.apply(item.path, settings, item.kind))
            files = self._select_files(data)

            if job.custom_caption:  # used exactly as given: no scraped caption, no translation
                data.caption = job.custom_caption
                translated = None
            else:
                job.step = "Translating caption"
                translated = await self.translator.translate(data.caption)
            dest = job.dest_chat
            caption = build_caption(
                translated=translated, original=data.caption, source_url=job.url,
                comment_count=data.total_comments, tag=tag,
                limit=self.caption_limit - (1 if dest is not None else 0),
            )
            if dest is not None:  # invisible mark: the userbot must not treat its own post as a new link
                caption = NOTIFY_MARK + caption

            job.step = "Uploading to channel"
            route = (self.target, self.comment_chat)  # the channel can be switched at runtime: pin it for this post
            async with self._upload_sem:
                sent = await self._upload(files, caption, dest if dest is not None else route[0])
            link = self._message_link(sent[0], route[0]) if dest is None else ""

            # Upload succeeded -> delete the downloaded media right now (comments need no files).
            job.step = "Cleaning up"
            freed = await asyncio.to_thread(dir_size, workdir)
            if await asyncio.to_thread(safe_rmtree, workdir):
                log.info("Uploaded %d file(s); deleted local copies (%s freed)", len(files), human_size(freed))

            # Comments are posted in the background: the job (and its worker slot) is finished as soon
            # as the post is live, so the next link starts immediately.
            queued = 0
            if settings.comments_on and (data.comments or data.comments_task is not None):
                queued = len(data.comments[: settings.MAX_COMMENTS])
                self._spawn_comments(sent[0], tag, data, dest, route)
                handed_off = True

            await db.job_finish(key, "done", link=link or None)
            return JobResult("done", link=link, comments=queued)
        finally:
            if not handed_off and data is not None and data.comments_task is not None:
                data.comments_task.cancel()

    def _spawn_comments(self, post_msg, tag: str, data: PostData, dest: int | None = None, route=None) -> None:
        task = asyncio.create_task(self._comments_worker(post_msg, tag, data, dest, route), name=f"zeba-comments-{tag}")
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _comments_worker(self, post_msg, tag: str, data: PostData, dest: int | None = None, route=None) -> None:
        try:  # the post is already live: a comment problem must never turn into a failure
            if data.comments_task is not None:
                await data.comments_task  # replies were being fetched while the media uploaded
            if not data.comments:
                return
            async with self._comment_sem:
                kw: dict = {}
                if dest is not None:
                    kw["dest"] = dest
                if route is not None and (route[0] is not self.target or route[1] is not self.comment_chat):
                    kw["route"] = route  # channel was switched after this post: comment where the post lives
                await self._post_comments(post_msg, tag, data.comments, **kw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("comments skipped: %s", short_error(exc))

    def _select_files(self, data: PostData) -> list[tuple[str, str]]:
        files = []
        for item in data.media:
            if not item.path.exists():
                continue
            if item.path.stat().st_size > self.max_bytes:
                log.warning("skipping %s: larger than upload limit", item.path.name)
                continue
            files.append((str(item.path), item.kind))
        if not files:
            raise DownloadError("All media files exceed the Telegram upload limit")
        return files

    async def _retry(self, factory):
        delay = 2.0
        for attempt in range(settings.MAX_RETRIES + 1):
            try:
                return await factory()
            except errors.FloodWaitError as exc:
                if exc.seconds > 600 or attempt == settings.MAX_RETRIES:
                    raise
                log.warning("FloodWait %ss", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
            except (ConnectionError, OSError, asyncio.TimeoutError, errors.ServerError) as exc:
                if attempt == settings.MAX_RETRIES:
                    raise
                log.warning("transient error (%s), retrying", short_error(exc))
                await asyncio.sleep(delay)
                delay *= 2

    async def _send_chunk(self, chunk: list[tuple[str, str]], caption: str | None, dest) -> list:
        paths = [p for p, _ in chunk]
        target = paths if len(paths) > 1 else paths[0]

        async def go():
            return await self.userbot.send_file(
                dest, target, caption=caption, parse_mode="html",
                supports_streaming=True, force_document=False, part_size_kb=512,
            )

        try:
            res = await self._retry(go)
            return list(res) if isinstance(res, (list, tuple)) else [res]
        except errors.RPCError as exc:
            if len(paths) == 1:
                raise
            log.warning("album send failed (%s); sending files individually", short_error(exc))
            out = []
            for i, p in enumerate(paths):
                single = await self._retry(
                    lambda p=p, i=i: self.userbot.send_file(
                        dest, p, caption=caption if i == 0 else None,
                        parse_mode="html", supports_streaming=True, part_size_kb=512,
                    )
                )
                out.append(single)
            return out

    async def _upload(self, files: list[tuple[str, str]], caption: str, dest) -> list:
        sent: list = []
        for i in range(0, len(files), 10):  # Telegram albums hold max 10 items
            sent += await self._send_chunk(files[i:i + 10], caption if i == 0 else None, dest)
        return sent

    def _message_link(self, msg, target=None) -> str:
        ent = target if target is not None else self.target
        if isinstance(ent, types.Channel):
            if getattr(ent, "username", None):
                return f"https://t.me/{ent.username}/{msg.id}"
            return f"https://t.me/c/{ent.id}/{msg.id}"
        return ""

    # ---- comments ----
    def _fmt_comment(self, c: Comment) -> str:
        text = c.text.strip()
        if settings.COMMENT_INCLUDE_AUTHOR and c.author:
            text = f"@{c.author}: {text}"
        return text[:4000]

    async def _comment_sender(self, post_msg, tag: str, route=None):
        """Return an async callable(text) that posts into the post's comment thread."""
        target, comment_chat = route if route is not None else (self.target, self.comment_chat)
        # Preferred: the channel's linked discussion thread (Telegram "comments").
        from telethon.tl.functions.messages import GetDiscussionMessageRequest

        for attempt in range(5):
            try:
                await self.userbot(GetDiscussionMessageRequest(peer=target, msg_id=post_msg.id))
                return lambda t: self.userbot.send_message(
                    target, t, comment_to=post_msg, parse_mode=None, link_preview=False
                )
            except Exception as exc:
                log.debug("discussion not ready (%d): %s", attempt + 1, short_error(exc))
                await asyncio.sleep(0.5 * (attempt + 1))

        # Fallback: locate the forwarded post in COMMENT_CHAT by its unique tag.
        if comment_chat is not None:
            msg_id = None
            for _ in range(4):
                async for m in self.userbot.iter_messages(comment_chat, search=tag, limit=5):
                    msg_id = m.id
                    break
                if not msg_id:
                    async for m in self.userbot.iter_messages(comment_chat, limit=30):
                        if m.raw_text and tag in m.raw_text:
                            msg_id = m.id
                            break
                if msg_id:
                    break
                await asyncio.sleep(2)
            if msg_id:
                return lambda t: self.userbot.send_message(
                    comment_chat, t, reply_to=msg_id, parse_mode=None, link_preview=False
                )
        return None

    async def _post_comments(self, post_msg, tag: str, comments: list[Comment], dest: int | None = None, route=None) -> int:
        texts = [t for t in (self._fmt_comment(c) for c in comments[: settings.MAX_COMMENTS]) if t]
        if not texts:
            return 0
        if dest is not None:  # .dlauto chat: replies in the same chat (marked so the userbot ignores them)
            sender = lambda t: self.userbot.send_message(  # noqa: E731
                dest, NOTIFY_MARK + t, reply_to=post_msg.id, parse_mode=None, link_preview=False)
        else:
            sender = await self._comment_sender(post_msg, tag, route)
        if sender is None:
            log.warning("No comment thread available for %s; comments skipped", tag)
            return 0
        posted = 0
        for text in texts:
            try:
                await self._retry(lambda t=text: sender(t))
                posted += 1
            except Exception as exc:
                log.warning("comment failed: %s", short_error(exc))
            await asyncio.sleep(settings.COMMENT_DELAY)
        return posted
