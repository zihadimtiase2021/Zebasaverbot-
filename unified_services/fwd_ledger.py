"""Persistent "already copied" ledger + serial counter of the message forwarder.

Why not only the SQLite ``fwd_log``?  A free Render instance has no disk, so the database is empty after every
restart / redeploy - ``Forward all`` would then copy everything a second time.  The ledger is therefore also kept in
your Telegram **Saved Messages** (one small JSON message tagged ``#zeba_fwd_ledger``) and in the database.

Message ids of a chat grow one by one, so the copied ones are stored as *ranges* (``[[1, 5400], [5402, 5410]]``),
not as a list of single ids: even 100 000 copied messages need only a few bytes.

Layout of the stored JSON::

    {"n": 128, "r": {"-1001>-1002": [[1, 5400], [5402, 5410]]}}      n = last serial number handed out
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
from typing import Iterable

log = logging.getLogger("zeba.fwd.ledger")

TAG = "#zeba_fwd_ledger"
DB_KEY = "fwd_ledger"
MAX_CHARS = 3600          # a Telegram message holds 4096 characters (non-Premium)
FLUSH_DELAY = 2.0         # seconds: several copies in a row are written with one edit


def route_key(src: int, dst: int) -> str:
    return f"{src}>{dst}"


class Ledger:
    def __init__(self, userbot=None, db=None) -> None:
        self.userbot = userbot
        self.db = db
        self.ranges: dict[str, list[list[int]]] = {}
        self.serial = 0
        self.msg_id: int | None = None
        self._dirty = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ queries
    def has(self, key: str, mid: int) -> bool:
        rs = self.ranges.get(key)
        if not rs:
            return False
        i = bisect.bisect_right([r[0] for r in rs], mid) - 1
        return i >= 0 and rs[i][0] <= mid <= rs[i][1]

    def has_all(self, key: str, ids: Iterable[int]) -> bool:
        return all(self.has(key, i) for i in ids)

    def count(self, key: str) -> int:
        return sum(b - a + 1 for a, b in self.ranges.get(key, []))

    def resume_from(self, key: str) -> int:
        """Everything up to this id is already done (messages after it still have to be looked at)."""
        rs = self.ranges.get(key)
        return rs[0][1] if rs and rs[0][0] <= 1 else 0

    # ------------------------------------------------------------------ changes
    def add(self, key: str, first: int, last: int | None = None) -> None:
        """Mark ids first..last as done (``last`` defaults to ``first``). Neighbouring ranges are merged."""
        last = first if last is None else last
        if last < first:
            return
        rs = self.ranges.setdefault(key, [])
        rs.append([first, last])
        rs.sort()
        merged: list[list[int]] = []
        for a, b in rs:
            if merged and a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.ranges[key] = merged
        self.touch()

    def next_serial(self) -> int:
        self.serial += 1
        self.touch()
        return self.serial

    def release(self, serial: int) -> None:
        """Give the newest serial number back (its copy failed, nothing was posted)."""
        if serial == self.serial and serial > 0:
            self.serial -= 1
            self.touch()

    def forget(self, key: str) -> None:
        if self.ranges.pop(key, None) is not None:
            self.touch()

    # ------------------------------------------------------------------ (de)serialise
    def dump(self) -> str:
        """Compact JSON. When it would not fit into one message, the smallest gaps are closed (logged)."""
        data = {"n": self.serial, "r": self.ranges}
        text = json.dumps(data, separators=(",", ":"), sort_keys=True)
        closed = 0
        while len(text) > MAX_CHARS:
            best = None  # (gap size, key, index)
            for k, rs in self.ranges.items():
                for i in range(len(rs) - 1):
                    gap = rs[i + 1][0] - rs[i][1] - 1
                    if best is None or gap < best[0]:
                        best = (gap, k, i)
            if best is None:
                break
            _, k, i = best
            rs = self.ranges[k]
            rs[i] = [rs[i][0], rs[i + 1][1]]
            del rs[i + 1]
            closed += best[0]
            text = json.dumps({"n": self.serial, "r": self.ranges}, separators=(",", ":"), sort_keys=True)
        if closed:
            log.warning("forward ledger too big for one message: closed gaps covering %d id(s)", closed)
        return text

    def merge(self, text: str) -> bool:
        """Add what a stored JSON text knows (union of ranges, highest serial). False when it is not valid."""
        try:
            data = json.loads(text)
            ranges = data.get("r", {})
            n = int(data.get("n", 0))
            if not isinstance(ranges, dict):
                return False
            clean: dict[str, list[list[int]]] = {}
            for k, rs in ranges.items():
                clean[str(k)] = [[int(a), int(b)] for a, b in rs]
        except (ValueError, TypeError, AttributeError):
            return False
        self.serial = max(self.serial, n)
        for k, rs in clean.items():
            for a, b in rs:
                self.add(k, a, b)
        self._dirty = False
        return True

    # ------------------------------------------------------------------ persistence
    async def load(self) -> None:
        if self.userbot is not None:
            try:
                async for m in self.userbot.iter_messages("me", search=TAG, limit=10):
                    text = getattr(m, "raw_text", None) or ""
                    if getattr(m, "out", True) and TAG in text:
                        if self.merge(text.split(TAG, 1)[1].strip()):
                            self.msg_id = m.id
                            break
            except Exception as exc:
                log.warning("could not read the forward ledger from Saved Messages: %s", exc)
        if self.db is not None:
            try:
                raw = await self.db.get_setting(DB_KEY)
                if raw:
                    self.merge(raw)
            except Exception as exc:
                log.debug("could not read the forward ledger from the database: %s", exc)
        self._dirty = False
        if self.ranges:
            log.info("Forward ledger: %d route(s), last serial %d", len(self.ranges), self.serial)

    def touch(self) -> None:
        """Something changed: write it soon (debounced)."""
        self._dirty = True
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._later(), name="zeba-fwd-ledger")
            except RuntimeError:  # no running loop (plain unit tests): flush() is called explicitly
                self._task = None

    async def _later(self) -> None:
        await asyncio.sleep(FLUSH_DELAY)
        await self.flush()

    async def flush(self) -> None:
        if not self._dirty:
            return
        async with self._lock:
            self._dirty = False
            text = self.dump()
            if self.db is not None:
                try:
                    await self.db.set_setting(DB_KEY, text)
                except Exception as exc:
                    log.debug("could not save the forward ledger to the database: %s", exc)
            if self.userbot is None:
                return
            from unified_core.utils import NOTIFY_MARK  # lazy: keeps this module importable on its own

            body = f"{NOTIFY_MARK}\U0001f9fe Zeba forward ledger (please do not delete)\n{TAG}\n{text}"
            try:
                if self.msg_id:
                    try:
                        await self.userbot.edit_message("me", self.msg_id, body, parse_mode=None)
                        return
                    except Exception as exc:
                        if "not modified" in str(exc).lower():
                            return
                        log.info("ledger message could not be edited (%s) - sending a new one", exc)
                        self.msg_id = None
                m = await self.userbot.send_message("me", body, parse_mode=None)
                self.msg_id = m.id
            except Exception as exc:
                self._dirty = True  # try again with the next change / at shutdown
                log.warning("could not save the forward ledger to Saved Messages: %s", exc)

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.flush()
