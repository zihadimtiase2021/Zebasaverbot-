"""Async SQLite layer (single persistent connection, WAL mode)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import aiosqlite

from unified_core.config import settings

NOTE_MAX = 1500  # characters of one admin note (a Telegram message holds 4096)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    auto_dl INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS auto_chats (
    chat_id INTEGER PRIMARY KEY
);
-- chats where `.dlauto on` was used: links are downloaded and posted back into the same chat
CREATE TABLE IF NOT EXISTS dlauto_chats (
    chat_id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    url_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    posted_link TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at);

-- Batch persistence: every URL of a .json/.txt batch is stored, so a restart continues where it stopped.
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    msg_id INTEGER,
    force INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    dest INTEGER,
    total INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
-- Message forwarding: one row per copied message, uid = the unique id printed under the copy.
CREATE TABLE IF NOT EXISTS fwd_log (
    uid TEXT PRIMARY KEY,
    src_chat INTEGER NOT NULL,
    src_msg INTEGER NOT NULL,
    dst_chat INTEGER NOT NULL,
    dst_msg INTEGER,
    created_at INTEGER NOT NULL,
    src_name TEXT,
    caption TEXT,
    media INTEGER,
    account INTEGER,
    note TEXT,
    note_auto INTEGER NOT NULL DEFAULT 0
);
-- Default note (admin only) put on every NEW copy that comes from this source group / channel
CREATE TABLE IF NOT EXISTS fwd_defaults (
    src_chat INTEGER PRIMARY KEY,
    src_name TEXT,
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fwd_src ON fwd_log(src_chat, src_msg, dst_chat);
-- Extra userbot accounts logged in from the control bot (the session is the "key"; removed again on logout)
CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    url TEXT NOT NULL,
    custom_caption TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    PRIMARY KEY (batch_id, idx)
);
"""


class Database:
    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None

    def _changed(self, delay: float) -> None:
        """Tell the Saved Messages backup that product details changed (it writes them soon, debounced)."""
        cb = getattr(self, "on_change", None)
        if cb is not None:
            try:
                cb(delay)
            except Exception:  # a backup problem must never break forwarding
                pass

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        return self._conn

    async def connect(self, path: str | None = None) -> None:
        path = path or settings.DATABASE_PATH
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        async with self._conn.execute("PRAGMA table_info(batches)") as cur:  # upgrade older databases
            cols = {r[1] for r in await cur.fetchall()}
        if "dest" not in cols:
            await self._conn.execute("ALTER TABLE batches ADD COLUMN dest INTEGER")
        if "owner" not in cols:  # which Telegram account started the batch (NULL = the primary account)
            await self._conn.execute("ALTER TABLE batches ADD COLUMN owner INTEGER")
        async with self._conn.execute("PRAGMA table_info(fwd_log)") as cur:  # hidden product details (admin only)
            fcols = {r[1] for r in await cur.fetchall()}
        for col, typ in (("src_name", "TEXT"), ("caption", "TEXT"), ("media", "INTEGER"),
                         ("account", "INTEGER"), ("note", "TEXT"),
                         ("note_auto", "INTEGER NOT NULL DEFAULT 0")):
            if col not in fcols:
                await self._conn.execute(f"ALTER TABLE fwd_log ADD COLUMN {col} {typ}")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---------- users (control bot audience) ----------
    async def register_user(self, user_id: int) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO users (user_id, auto_dl) VALUES (?, 1)", (user_id,))
        await self.conn.commit()

    async def user_count(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM users") as cur:
            return (await cur.fetchone())[0]

    # ---------- generic settings ----------
    async def get_setting(self, key: str) -> str | None:
        async with self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def delete_setting(self, key: str) -> None:
        await self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self.conn.commit()

    async def get_auto_global(self) -> bool:
        v = await self.get_setting("auto_dl")
        return settings.AUTO_DEFAULT if v is None else v == "1"

    async def set_auto_global(self, enabled: bool) -> None:
        await self.set_setting("auto_dl", "1" if enabled else "0")

    # ---------- auto chats ----------
    async def add_auto_chat(self, chat_id: int) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO auto_chats (chat_id) VALUES (?)", (chat_id,))
        await self.conn.commit()

    async def remove_auto_chat(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM auto_chats WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    async def is_auto_chat(self, chat_id: int) -> bool:
        async with self.conn.execute("SELECT 1 FROM auto_chats WHERE chat_id = ?", (chat_id,)) as cur:
            return await cur.fetchone() is not None

    # ---------- .dlauto chats (post into the same chat) ----------
    async def add_dlauto_chat(self, chat_id: int) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO dlauto_chats (chat_id) VALUES (?)", (chat_id,))
        await self.conn.commit()

    async def remove_dlauto_chat(self, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM dlauto_chats WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()

    async def is_dlauto_chat(self, chat_id: int) -> bool:
        async with self.conn.execute("SELECT 1 FROM dlauto_chats WHERE chat_id = ?", (chat_id,)) as cur:
            return await cur.fetchone() is not None

    # ---------- jobs (dedupe + history) ----------
    async def job_get(self, url_key: str) -> dict | None:
        async with self.conn.execute("SELECT * FROM jobs WHERE url_key = ?", (url_key,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def job_start(self, url_key: str, url: str) -> None:
        now = int(time.time())
        await self.conn.execute(
            "INSERT INTO jobs (url_key, url, status, attempts, created_at, updated_at) "
            "VALUES (?, ?, 'running', 1, ?, ?) "
            "ON CONFLICT(url_key) DO UPDATE SET status='running', url=excluded.url, error=NULL, "
            "attempts=attempts+1, updated_at=excluded.updated_at",
            (url_key, url, now, now),
        )
        await self.conn.commit()

    async def job_finish(self, url_key: str, status: str, error: str | None = None, link: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = ?, error = ?, posted_link = COALESCE(?, posted_link), updated_at = ? "
            "WHERE url_key = ?",
            (status, error, link, int(time.time()), url_key),
        )
        await self.conn.commit()

    async def recover_stale(self) -> int:
        """Jobs left 'running' by a crash are marked failed so they can be retried."""
        cur = await self.conn.execute(
            "UPDATE jobs SET status='failed', error='interrupted by restart', updated_at=? WHERE status='running'",
            (int(time.time()),),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    # ---------- batches (resume after restart) ----------
    # item status: pending (not finished yet, includes "was running when the bot stopped")
    #              | done | skipped | failed
    async def batch_create(self, chat_id: int, force: bool, items: list[tuple[str, str | None]],
                           dest: int | None = None, owner: int | None = None) -> int:
        now = int(time.time())
        cur = await self.conn.execute(
            "INSERT INTO batches (chat_id, force, status, dest, owner, total, created_at, updated_at) "
            "VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
            (chat_id, 1 if force else 0, dest, owner, len(items), now, now),
        )
        batch_id = cur.lastrowid
        await self.conn.executemany(
            "INSERT INTO batch_items (batch_id, idx, url, custom_caption) VALUES (?, ?, ?, ?)",
            [(batch_id, i, url, cap) for i, (url, cap) in enumerate(items)],
        )
        await self.conn.commit()
        return int(batch_id)

    async def batch_set_message(self, batch_id: int, msg_id: int) -> None:
        await self.conn.execute(
            "UPDATE batches SET msg_id = ?, updated_at = ? WHERE id = ?", (msg_id, int(time.time()), batch_id)
        )
        await self.conn.commit()

    async def batch_item_set(self, batch_id: int, idx: int, status: str, error: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE batch_items SET status = ?, error = ? WHERE batch_id = ? AND idx = ?",
            (status, error, batch_id, idx),
        )
        await self.conn.execute("UPDATE batches SET updated_at = ? WHERE id = ?", (int(time.time()), batch_id))
        await self.conn.commit()

    async def batch_items(self, batch_id: int) -> list[dict]:
        async with self.conn.execute(
            "SELECT idx, url, custom_caption, status, error FROM batch_items WHERE batch_id = ? ORDER BY idx",
            (batch_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def batch_finish(self, batch_id: int, status: str = "finished") -> None:
        await self.conn.execute(
            "UPDATE batches SET status = ?, updated_at = ? WHERE id = ?", (status, int(time.time()), batch_id)
        )
        await self.conn.commit()

    async def batches_unfinished(self, owner: int | None = None, legacy: bool = False) -> list[dict]:
        """Running batches. `owner` = only those started by that Telegram account (`legacy` also takes the old
        rows that have no owner - they belong to the primary account). owner=None -> every running batch."""
        if owner is None:
            sql, args = "SELECT * FROM batches WHERE status = 'running' ORDER BY id", ()
        else:
            sql = ("SELECT * FROM batches WHERE status = 'running' AND (owner = ?"
                   + (" OR owner IS NULL" if legacy else "") + ") ORDER BY id")
            args = (owner,)
        async with self.conn.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---------- extra userbot accounts ----------
    async def account_save(self, user_id: int, name: str, phone: str, session: str) -> None:
        await self.conn.execute(
            "INSERT INTO accounts (user_id, name, phone, session, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, phone = excluded.phone, "
            "session = excluded.session",
            (user_id, name, phone, session, int(time.time())),
        )
        await self.conn.commit()

    async def account_delete(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def accounts_all(self) -> list[dict]:
        async with self.conn.execute("SELECT * FROM accounts ORDER BY created_at, user_id") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def batch_prune(self, days: int = 14) -> None:
        """Forget finished batches older than `days` (keeps the database small)."""
        cutoff = int(time.time()) - days * 86400
        await self.conn.execute(
            "DELETE FROM batch_items WHERE batch_id IN (SELECT id FROM batches WHERE status != 'running' AND updated_at < ?)",
            (cutoff,),
        )
        await self.conn.execute("DELETE FROM batches WHERE status != 'running' AND updated_at < ?", (cutoff,))
        await self.conn.commit()

    # ---------- message forwarding log ----------
    async def fwd_reserve(self, uid: str, src_chat: int, src_msg: int, dst_chat: int) -> bool:
        """Claim a unique id. False when the id already exists (caller generates another one)."""
        try:
            await self.conn.execute(
                "INSERT INTO fwd_log (uid, src_chat, src_msg, dst_chat, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, src_chat, src_msg, dst_chat, int(time.time())),
            )
            await self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    async def fwd_set_dst(self, uid: str, dst_msg: int) -> None:
        await self.conn.execute("UPDATE fwd_log SET dst_msg = ? WHERE uid = ?", (dst_msg, uid))
        await self.conn.commit()

    async def fwd_set_details(self, uid: str, src_name: str, caption: str, media: int, account: int,
                              note: str | None = None) -> None:
        """Hidden details of a copy (never posted to the channel): where it came from, the ORIGINAL caption, ...
        `note` = the default note of the source (kept only when the copy has no note yet)."""
        await self.conn.execute(
            "UPDATE fwd_log SET src_name = :src, caption = :cap, media = :media, account = :acc, "
            "note_auto = CASE WHEN (note IS NULL OR note = '') AND :note IS NOT NULL THEN 1 ELSE note_auto END, "
            "note = COALESCE(NULLIF(note, ''), :note) WHERE uid = :uid",
            {"src": src_name, "cap": (caption or "")[:3500], "media": media, "acc": account,
             "note": (note or "").strip()[:NOTE_MAX] or None, "uid": uid})
        await self.conn.commit()
        self._changed(300.0)

    async def fwd_set_caption(self, uid: str, caption: str) -> bool:
        """The ORIGINAL caption of a copy after the admin edited it (/find -> Edit). False when the serial is unknown."""
        cur = await self.conn.execute("UPDATE fwd_log SET caption = ? WHERE uid = ? AND dst_msg IS NOT NULL",
                                      ((caption or "")[:3500], uid))
        await self.conn.commit()
        self._changed(20.0)
        return cur.rowcount > 0

    async def fwd_set_note(self, uid: str, note: str | None) -> bool:
        """Admin note of a copy ('' / None clears it). False when the serial number is unknown."""
        cur = await self.conn.execute("UPDATE fwd_log SET note = ?, note_auto = 0 WHERE uid = ? AND dst_msg IS NOT NULL",
                                      ((note or "").strip()[:NOTE_MAX] or None, uid))
        await self.conn.commit()
        self._changed(20.0)
        return cur.rowcount > 0

    # ---------- default note per source + export / import (Saved Messages backup) ----------
    async def fwd_default_get(self, src_chat: int) -> str | None:
        async with self.conn.execute("SELECT note FROM fwd_defaults WHERE src_chat = ?", (src_chat,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def fwd_default_set(self, src_chat: int, src_name: str, note: str | None) -> None:
        """Default note of a source group / channel ('' / None deletes it)."""
        note = (note or "").strip()[:NOTE_MAX]
        if note:
            await self.conn.execute(
                "INSERT INTO fwd_defaults (src_chat, src_name, note) VALUES (?, ?, ?) "
                "ON CONFLICT(src_chat) DO UPDATE SET src_name = excluded.src_name, note = excluded.note",
                (src_chat, src_name, note))
        else:
            await self.conn.execute("DELETE FROM fwd_defaults WHERE src_chat = ?", (src_chat,))
        await self.conn.commit()
        self._changed(20.0)

    async def fwd_defaults_all(self) -> list[dict]:
        async with self.conn.execute("SELECT * FROM fwd_defaults ORDER BY src_chat") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fwd_export(self) -> dict:
        """Everything worth keeping when the database is lost: finished copies (serial, ids, details, note) + defaults."""
        async with self.conn.execute("SELECT * FROM fwd_log WHERE dst_msg IS NOT NULL ORDER BY created_at, uid") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"v": 1, "rows": rows, "defaults": await self.fwd_defaults_all()}

    async def fwd_import(self, data: dict, keep_days: int = 90) -> int:
        """Put an export back. Only what is MISSING is added (nothing that exists is changed); copies older than
        the prune limit are skipped unless they carry a note. Returns the number of rows added."""
        cols = ("uid", "src_chat", "src_msg", "dst_chat", "dst_msg", "created_at",
                "src_name", "caption", "media", "account", "note", "note_auto")
        cutoff = int(time.time()) - keep_days * 86400
        added = 0
        for r in data.get("rows", []):
            try:
                own_note = bool((r.get("note") or "").strip()) and not r.get("note_auto")
                if int(r["created_at"]) < cutoff and not own_note:
                    continue
                cur = await self.conn.execute(
                    f"INSERT OR IGNORE INTO fwd_log ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    tuple((r.get(c) or 0) if c == "note_auto" else r.get(c) for c in cols))
                added += max(cur.rowcount, 0)
            except (KeyError, TypeError, ValueError):
                continue
        for d in data.get("defaults", []):
            try:
                await self.conn.execute(
                    "INSERT OR IGNORE INTO fwd_defaults (src_chat, src_name, note) VALUES (?, ?, ?)",
                    (int(d["src_chat"]), d.get("src_name"), str(d["note"])[:NOTE_MAX]))
            except (KeyError, TypeError, ValueError):
                continue
        await self.conn.commit()
        return added

    async def fwd_search(self, text: str, limit: int = 10) -> list[dict]:
        """Copies whose serial number, source name, original caption or note contain `text` (newest first)."""
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self.conn.execute(
            "SELECT * FROM fwd_log WHERE dst_msg IS NOT NULL AND (uid LIKE ? ESCAPE '\\' OR src_name LIKE ? ESCAPE '\\' "
            "OR caption LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\') ORDER BY created_at DESC, uid DESC LIMIT ?",
            (like, like, like, like, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fwd_adopt(self, uid: str, dst_chat: int, dst_msg: int, created_at: int, caption: str,
                        media: int | None, account: int = 0) -> bool:
        """Put back a copy that was found in its destination chat (the database had forgotten it).
        The source is unknown then (src_chat = 0). Never changes an existing row."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO fwd_log (uid, src_chat, src_msg, dst_chat, dst_msg, created_at, src_name, caption, "
            "media, account) VALUES (?, 0, 0, ?, ?, ?, '', ?, ?, ?)",
            (uid, dst_chat, dst_msg, int(created_at), (caption or "")[:3500], media, account))
        await self.conn.commit()
        self._changed(300.0)
        return cur.rowcount > 0

    async def fwd_dst_chats(self) -> list[int]:
        """Every destination chat that ever received a copy (newest first)."""
        async with self.conn.execute(
                "SELECT dst_chat FROM fwd_log GROUP BY dst_chat ORDER BY MAX(created_at) DESC") as cur:
            return [int(r[0]) for r in await cur.fetchall()]

    async def fwd_drop(self, uid: str) -> None:
        """Forget a reserved id whose copy failed (the message may be copied again later)."""
        await self.conn.execute("DELETE FROM fwd_log WHERE uid = ? AND dst_msg IS NULL", (uid,))
        await self.conn.commit()

    async def fwd_get(self, uid: str) -> dict | None:
        async with self.conn.execute("SELECT * FROM fwd_log WHERE uid = ?", (uid,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fwd_seen(self, src_chat: int, src_msg: int, dst_chat: int) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM fwd_log WHERE src_chat = ? AND src_msg = ? AND dst_chat = ? AND dst_msg IS NOT NULL",
            (src_chat, src_msg, dst_chat),
        ) as cur:
            return await cur.fetchone() is not None

    async def fwd_prune(self, days: int = 90) -> None:
        await self.conn.execute(  # a copy with a note the admin wrote is never pruned (a default note does not count)
            "DELETE FROM fwd_log WHERE created_at < ? AND (note IS NULL OR note = '' OR note_auto = 1)",
            (int(time.time()) - days * 86400,))
        await self.conn.commit()

    async def job_stats(self) -> dict:
        out = {"done": 0, "failed": 0, "running": 0, "last_24h": 0}
        async with self.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status") as cur:
            for status, count in await cur.fetchall():
                out[status] = count
        async with self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='done' AND updated_at > ?", (int(time.time()) - 86400,)
        ) as cur:
            out["last_24h"] = (await cur.fetchone())[0]
        return out


db = Database()
