"""Bulk delete: remove every message between two message links (inclusive) of ONE chat.

    start  https://t.me/c/2940625991/2236
    end    https://t.me/c/2940625991/2255      -> messages 2236 ... 2255 are deleted

* Private (``/c/<id>/<msg>``) and public (``/<username>/<msg>``) links are understood; both links must point to
  the same chat (a ``/c/`` link and a public link of the same chat are fine too).
* IDs are checked 100 at a time: ids that no longer exist are skipped, only real messages are deleted.
* ``FloodWait`` is waited out and the same chunk is retried; a missing right (not admin) stops cleanly.
* Nothing is deleted before the caller has shown :meth:`BulkPlan.summary` and the person confirmed.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
from dataclasses import dataclass, field

from telethon import errors, utils

from unified_core.tg import parse_message_link, resolve_peer
from unified_core.utils import short_error

log = logging.getLogger("zeba.bulkdel")

CHUNK = 100                 # Telegram deletes at most 100 ids per request
MAX_RANGE = 20000           # safety net against a typo like 1 -> 9999999
MAX_FLOOD_WAIT = 900        # seconds; a longer wait aborts the run (it can be started again)
MAX_FLOOD_RETRIES = 5


@dataclass
class BulkPlan:
    chat: object            # resolved entity
    chat_id: int
    name: str
    start: int
    end: int

    @property
    def total(self) -> int:
        return self.end - self.start + 1

    def summary(self) -> str:
        return (f"🗑 <b>Bulk delete</b>\nChat: <b>{_html.escape(self.name)}</b> <code>{self.chat_id}</code>\n"
                f"Messages <b>{self.start}</b> → <b>{self.end}</b> (inclusive) = up to <b>{self.total}</b> "
                f"message(s)\nIDs that do not exist are skipped.\n\n⚠️ <b>This cannot be undone.</b> Delete them?")


@dataclass
class BulkResult:
    total: int
    deleted: int = 0
    missing: int = 0
    failed: int = 0
    done_to: int = 0        # highest id that was fully handled
    flood_waits: int = 0
    note: str = ""
    errors: list = field(default_factory=list)

    def text(self) -> str:
        icon = "⚠️" if (self.failed or self.note) else "✅"
        out = (f"{icon} <b>Bulk delete finished</b>\n🗑 deleted: <b>{self.deleted}</b> · ⏭ not found: {self.missing}"
               f" · ❌ failed: {self.failed}\n🔢 range size: {self.total}")
        if self.flood_waits:
            out += f" · ⏳ flood waits: {self.flood_waits}"
        if self.note:
            out += f"\n<i>{_html.escape(self.note)}</i>"
        return out


async def make_plan(client, start_link: str, end_link: str) -> BulkPlan:
    """Parse + validate both links. Raises ValueError with a message that can be shown to the person."""
    c1, start = parse_message_link(start_link)
    c2, end = parse_message_link(end_link)
    try:
        ent1 = await resolve_peer(client, c1)
        ent2 = ent1 if c1 == c2 else await resolve_peer(client, c2)
    except RuntimeError as exc:
        raise ValueError("that chat was not found - this account must be a member of it") from exc
    id1, id2 = utils.get_peer_id(ent1), utils.get_peer_id(ent2)
    if id1 != id2:
        raise ValueError("the two links belong to different chats - start and end must be in the same chat")
    if start > end:
        raise ValueError(f"the start ({start}) is after the end ({end}) - send the OLDER message first")
    if end - start + 1 > MAX_RANGE:
        raise ValueError(f"that range has {end - start + 1} ids; the limit per run is {MAX_RANGE}")
    try:
        name = utils.get_display_name(ent1) or "chat"
    except Exception:
        name = "chat"
    return BulkPlan(ent1, id1, name, start, end)


async def _with_flood_wait(factory, result: BulkResult):
    """Run one request; on FloodWait sleep and retry the same request."""
    for attempt in range(MAX_FLOOD_RETRIES + 1):
        try:
            return await factory()
        except errors.FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 0) or 1)
            if wait > MAX_FLOOD_WAIT or attempt == MAX_FLOOD_RETRIES:
                raise
            result.flood_waits += 1
            log.warning("bulk delete: FloodWait %ss", wait)
            await asyncio.sleep(wait + 1)


def _deleted_count(res, fallback: int) -> int:
    items = res if isinstance(res, (list, tuple)) else [res]
    total, known = 0, False
    for r in items:
        n = getattr(r, "pts_count", None)
        if isinstance(n, int):
            total += n
            known = True
    return total if known else fallback


async def delete_range(client, plan: BulkPlan, progress=None, pause: float = 0.5, should_stop=None) -> BulkResult:
    """Delete plan.start..plan.end in chunks of 100. ``progress(result)`` is awaited after every chunk;
    ``should_stop()`` is asked before every chunk - True ends the run cleanly (what was deleted stays deleted)."""
    result = BulkResult(total=plan.total)
    for lo in range(plan.start, plan.end + 1, CHUNK):
        if should_stop is not None and should_stop():
            result.note = f"stopped by you after message {result.done_to or plan.start - 1} - the rest was not touched"
            break
        ids = list(range(lo, min(lo + CHUNK, plan.end + 1)))
        try:
            found = await _with_flood_wait(lambda: client.get_messages(plan.chat, ids=ids), result)
            if not isinstance(found, (list, tuple)):
                found = [found]
            real = [m.id for m in found if m is not None and getattr(m, "id", None) in ids
                    and type(m).__name__ != "MessageEmpty"]
            result.missing += len(ids) - len(real)
            if real:
                res = await _with_flood_wait(lambda: client.delete_messages(plan.chat, real, revoke=True), result)
                n = min(_deleted_count(res, len(real)), len(real))
                result.deleted += n
                result.failed += len(real) - n
            result.done_to = ids[-1]
        except (errors.ChatAdminRequiredError, errors.MessageDeleteForbiddenError, errors.ChatWriteForbiddenError):
            result.failed += plan.end - lo + 1
            result.note = "stopped: this account may not delete these messages (it needs the 'Delete messages' admin right)"
            break
        except errors.FloodWaitError as exc:
            result.failed += plan.end - lo + 1
            result.note = f"stopped: Telegram asks to wait {getattr(exc, 'seconds', '?')}s - run it again later"
            break
        except Exception as exc:
            log.exception("bulk delete chunk failed")
            result.failed += len(ids)
            result.errors.append(short_error(exc))
            result.note = f"chunk {ids[0]}-{ids[-1]} failed: {short_error(exc)}"
            result.done_to = ids[-1]
        if progress is not None:
            try:
                await progress(result)
            except Exception as exc:
                log.debug("bulk delete progress callback failed: %s", exc)
        if pause:
            await asyncio.sleep(pause)
    return result
