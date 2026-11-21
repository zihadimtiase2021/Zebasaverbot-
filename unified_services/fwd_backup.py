"""Backup of the hidden product details in your Telegram **Saved Messages**.

Why: a free Render instance has no disk, so the SQLite database (serial numbers -> source / copy ids, original captions,
your notes, default notes) is empty after every restart.  The same data is therefore kept as ONE small file in the Saved
Messages of the main account (``zeba_fwd_backup.json.gz``, caption starts with ``#zeba_fwd_backup``):

* written a few seconds after a note / default note changes, and at most every few minutes while copies are being made
  (each write replaces the previous file - only the newest one stays in Saved Messages);
* read at start-up: whatever is MISSING in the database is put back (nothing that exists is changed);
* never written while the database holds nothing at all, so an empty database can not overwrite a good backup.

Do not delete that message from Saved Messages.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import time

log = logging.getLogger("zeba.fwd.backup")

TAG = "#zeba_fwd_backup"
FILE_NAME = "zeba_fwd_backup.json.gz"
MIN_GAP = 20.0   # seconds between two uploads


class FwdBackup:
    def __init__(self, userbot, db) -> None:
        self.userbot = userbot
        self.db = db
        self.msg_ids: list[int] = []        # backup message(s) currently in Saved Messages
        self._due: float | None = None      # monotonic time of the next planned write
        self._last = 0.0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stopping = False

    # ------------------------------------------------------------------ restore
    async def restore(self) -> int:
        """Read the newest backup and add what the database is missing. Returns the number of copies added."""
        try:
            found = []
            async for m in self.userbot.iter_messages("me", search=TAG, limit=10):
                if TAG in (getattr(m, "raw_text", "") or "") and getattr(m, "document", None) is not None:
                    found.append(m)
            self.msg_ids = [m.id for m in found]
            if not found:
                return 0
            raw = await self.userbot.download_media(found[0], file=bytes)
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
            if not isinstance(data, dict) or data.get("v") != 1:
                raise ValueError("unknown backup format")
            added = await self.db.fwd_import(data)
            log.info("Product backup read from Saved Messages: %d copy(ies) restored, %d in the file",
                     added, len(data.get("rows", [])))
            return added
        except Exception as exc:
            log.warning("could not read the product backup from Saved Messages: %s", exc)
            return 0

    # ------------------------------------------------------------------ write
    def touch(self, delay: float = 20.0) -> None:
        """Something changed: write the backup in `delay` seconds (an earlier plan is kept)."""
        if self._stopping:
            return
        due = time.monotonic() + max(delay, 0.0)
        self._due = due if self._due is None else min(self._due, due)
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._later(), name="zeba-fwd-backup")
            except RuntimeError:  # no running loop (plain unit tests): flush() is called explicitly
                self._task = None

    async def _later(self) -> None:
        while self._due is not None:
            wait = max(self._due - time.monotonic(), self._last + MIN_GAP - time.monotonic(), 0.0)
            await asyncio.sleep(wait)
            if self._due is None or time.monotonic() < self._due:
                continue
            self._due = None
            await self.flush()

    async def flush(self) -> bool:
        """Upload a fresh snapshot, then remove the older backup message(s). False when nothing was written."""
        async with self._lock:
            try:
                snap = await self.db.fwd_export()
                if not snap["rows"] and not snap["defaults"]:
                    return False                      # nothing to protect - never replace a backup by an empty one
                data = gzip.compress(json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 6)
                buf = io.BytesIO(data)
                buf.name = FILE_NAME
                caption = (f"{TAG}\n{len(snap['rows'])} product(s), {len(snap['defaults'])} default note(s) · "
                           f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"
                           "Holds your product ids, details and notes - do not delete.")
                sent = await self.userbot.send_file("me", buf, caption=caption, force_document=True)
                old, self.msg_ids = self.msg_ids, [sent.id]
                self._last = time.monotonic()
                if old:
                    try:
                        await self.userbot.delete_messages("me", old)
                    except Exception as exc:
                        log.debug("could not remove the previous backup: %s", exc)
                log.info("Product backup written to Saved Messages (%d KB, %d copies)",
                         len(data) // 1024, len(snap["rows"]))
                return True
            except Exception as exc:
                log.warning("could not write the product backup to Saved Messages: %s", exc)
                self._due = time.monotonic() + 120.0   # try again in two minutes
                return False

    async def stop(self, timeout: float = 20.0) -> None:
        """Shutdown: write what is still waiting (the connection must still be open)."""
        self._stopping = True
        pending = self._due is not None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._due = None
        if pending:
            try:
                await asyncio.wait_for(self.flush(), timeout)
            except Exception as exc:
                log.warning("final product backup failed: %s", exc)
