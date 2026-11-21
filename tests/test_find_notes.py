"""/find, /addnote, /removenote, /defnote and the Saved Messages backup of the hidden product details."""
import asyncio
import gzip
import json
import re
import sys
import types

try:
    import telethon  # noqa: F401
except ImportError:
    t = types.ModuleType("telethon")
    t.Button = types.SimpleNamespace(inline=lambda text, data=None: (text, data))
    t.errors = types.SimpleNamespace(MessageNotModifiedError=type("MessageNotModifiedError", (Exception,), {}))
    t.events = types.SimpleNamespace(CallbackQuery=lambda **kw: ("cb", kw), NewMessage=lambda **kw: ("nm", kw))
    sys.modules["telethon"] = t

from unified_core.database import db  # noqa: E402
from unified_services import find_cmds  # noqa: E402
from unified_services.fwd_backup import FwdBackup, TAG  # noqa: E402

SRC, DST = -1001234567, -1007654321


class Bot:
    def __init__(self):
        self.h = []

    def on(self, spec):
        def deco(fn):
            self.h.append((spec[1]["pattern"], fn))
            return fn
        return deco


class Ev:
    def __init__(self, text, uid=1):
        self.text, self.sender_id, self.out, self.pattern_match = text, uid, [], None

    async def respond(self, text, **kw):
        self.out.append(text)


def run(coro):
    return asyncio.run(coro)


async def fill():
    await db.connect(":memory:")
    for n, (src, cap) in enumerate([("Shop A", "Red dress 50% off"), ("Shop B", "Blue shirt_xl")], 1):
        uid = f"ZF-{n:06d}"
        assert await db.fwd_reserve(uid, SRC, 100 + n, DST)
        await db.fwd_set_dst(uid, 500 + n)
        await db.fwd_set_details(uid, src, cap, 3, 10)


def make_bot(routes=()):
    bot = Bot()
    accounts = types.SimpleNamespace(instances={}, primary_username="boss", primary_name="Boss")
    app = types.SimpleNamespace(me_id=10, forwarder=types.SimpleNamespace(routes=list(routes)))
    find_cmds.register_find_commands(bot, lambda uid: uid == 1, accounts, app)

    async def call(text, uid=1):
        ev = Ev(text, uid)
        for pat, fn in bot.h:
            m = re.match(pat, text)
            if m:
                ev.pattern_match = m
                await fn(ev)
                return ev.out
        raise AssertionError("no handler for " + text)
    return call


def test_details_note_search_default_and_prune():
    async def go():
        await fill()
        try:
            row = await db.fwd_get("ZF-000001")
            assert row["src_name"] == "Shop A" and row["caption"] == "Red dress 50% off" and row["media"] == 3
            assert row["account"] == 10 and row["note"] is None
            assert await db.fwd_set_note("ZF-000001", "price 15$ supplier Rahim")
            assert not await db.fwd_set_note("ZF-999999", "x")
            assert [r["uid"] for r in await db.fwd_search("rahim")] == ["ZF-000001"]
            assert [r["uid"] for r in await db.fwd_search("50%")] == ["ZF-000001"]          # % is no wildcard
            assert [r["uid"] for r in await db.fwd_search("shirt_xl")] == ["ZF-000002"]
            assert await db.fwd_search("shirt%xl") == []
            # a default note lands on a NEW copy, never over an existing note
            await db.fwd_default_set(SRC, "Shop A", "Supplier Rahim, COD")
            assert await db.fwd_reserve("ZF-000003", SRC, 103, DST)
            await db.fwd_set_dst("ZF-000003", 503)
            await db.fwd_set_details("ZF-000003", "Shop A", "x", 1, 10, await db.fwd_default_get(SRC))
            assert (await db.fwd_get("ZF-000003"))["note"] == "Supplier Rahim, COD"
            await db.fwd_set_details("ZF-000001", "Shop A", "x", 1, 10, "default")
            assert (await db.fwd_get("ZF-000001"))["note"] == "price 15$ supplier Rahim"
            # pruning: old copies go, except the one with a note the admin wrote (a default note does not protect)
            await db.conn.execute("UPDATE fwd_log SET created_at = 1")
            await db.conn.commit()
            await db.fwd_prune(90)
            assert await db.fwd_get("ZF-000002") is None and await db.fwd_get("ZF-000003") is None
            assert await db.fwd_get("ZF-000001") is not None
            await db.fwd_default_set(SRC, "Shop A", None)
            assert await db.fwd_default_get(SRC) is None
        finally:
            await db.close()
    run(go())


def test_serial_to_uid():
    assert find_cmds.serial_to_uid("42") == "ZF-000042"
    assert find_cmds.serial_to_uid("zf-000042") == "ZF-000042"
    assert find_cmds.serial_to_uid("red dress") is None


def test_find_addnote_removenote_admin_only():
    async def go():
        await fill()
        try:
            call = make_bot()
            assert await call("/find 1", uid=2) == [] and await call("/addnote 1 x", uid=2) == []   # not an admin
            card = (await call("/find 1"))[0]
            assert "ZF-000001" in card and "Shop A" in card and "Red dress 50% off" in card and "@boss" in card
            assert "https://t.me/c/1234567/101" in card and "https://t.me/c/7654321/501" in card and "(none)" in card
            assert "added" in (await call("/addnote 1 price 15$"))[0]
            assert "added" in (await call("/addnote ZF-000001 supplier Rahim"))[0]      # second note = new line
            assert "price 15$\nsupplier Rahim" in (await call("/find 1"))[0]
            out = await call("/find rahim")                                              # search = full details
            assert out[0].startswith("🔎") and "Original caption" in out[1] and "supplier Rahim" in out[1]
            assert "No copy" in (await call("/find 77"))[0] and "No copy" in (await call("/addnote 77 x"))[0]
            assert "Nothing found" in (await call("/find zzz"))[0]
            assert "Too long" in (await call("/addnote 1 " + "x" * 1600))[0]
            assert "removed" in (await call("/removenote 1"))[0]
            assert "(none)" in (await call("/find 1"))[0] and "no note" in (await call("/removenote 1"))[0]
            assert "Usage" in (await call("/addnote"))[0] and "Usage" in (await call("/removenote"))[0]
        finally:
            await db.close()
    run(go())


def test_defnote_lists_sources_and_sets_default():
    async def go():
        await fill()
        try:
            routes = [{"src": SRC, "dst": DST, "src_name": "Shop A", "dst_name": "Main"},
                      {"src": -1009999, "dst": DST, "src_name": "Shop B", "dst_name": "Main"}]
            call = make_bot(routes)
            listing = (await call("/defnote"))[0]
            assert "1. <b>Shop A" in listing and "2. <b>Shop B" in listing and "no default note" in listing
            assert "saved" in (await call("/defnote 2 Supplier Karim"))[0]
            assert await db.fwd_default_get(-1009999) == "Supplier Karim" and await db.fwd_default_get(SRC) is None
            assert "Supplier Karim" in (await call("/defnote"))[0]
            assert "Unknown" in (await call("/defnote 9 x"))[0]
            assert "deleted" in (await call("/defnote 2 -"))[0] and await db.fwd_default_get(-1009999) is None
        finally:
            await db.close()
    run(go())


class SavedMessages:
    """Just enough of a Telegram client: a Saved Messages folder that holds files."""

    def __init__(self):
        self.msgs, self.next_id = [], 1

    async def send_file(self, chat, buf, caption="", force_document=False):
        assert chat == "me" and buf.name.endswith(".json.gz")
        m = types.SimpleNamespace(id=self.next_id, raw_text=caption, document=True, data=buf.getvalue(), out=True)
        self.next_id += 1
        self.msgs.append(m)
        return m

    async def iter_messages(self, chat, search=None, limit=None):
        for m in reversed(self.msgs):
            if search in m.raw_text:
                yield m

    async def download_media(self, m, file=None):
        return m.data

    async def delete_messages(self, chat, ids):
        self.msgs = [m for m in self.msgs if m.id not in ids]


def test_backup_survives_a_wiped_database():
    async def go():
        tg = SavedMessages()
        await fill()
        await db.fwd_set_note("ZF-000001", "price 15$ supplier Rahim")
        await db.fwd_default_set(SRC, "Shop A", "Supplier Rahim")
        try:
            b = FwdBackup(tg, db)
            assert await b.flush()
            assert len(tg.msgs) == 1 and TAG in tg.msgs[0].raw_text
            assert await b.flush() and len(tg.msgs) == 1 and tg.msgs[0].id == 2     # newest replaces the older one
            data = json.loads(gzip.decompress(tg.msgs[0].data))
            assert {r["uid"] for r in data["rows"]} == {"ZF-000001", "ZF-000002"}
        finally:
            await db.close()
        # a new, EMPTY database (Render restart): the backup puts everything back
        await db.connect(":memory:")
        try:
            empty = FwdBackup(tg, db)
            assert not await empty.flush() and len(tg.msgs) == 1                      # an empty db never overwrites it
            assert await empty.restore() == 2
            row = await db.fwd_get("ZF-000001")
            assert row["note"] == "price 15$ supplier Rahim" and row["caption"] == "Red dress 50% off"
            assert (row["src_chat"], row["src_msg"], row["dst_chat"], row["dst_msg"]) == (SRC, 101, DST, 501)
            assert await db.fwd_default_get(SRC) == "Supplier Rahim"
            # restoring again changes nothing that exists (a note removed meanwhile stays removed)
            await db.fwd_set_note("ZF-000001", None)
            assert await empty.restore() == 0 and (await db.fwd_get("ZF-000001"))["note"] is None
            # the next write replaces the old file, it does not pile up
            assert await empty.flush() and len(tg.msgs) == 1
        finally:
            await db.close()
    run(go())


def test_backup_is_written_after_a_change():
    async def go():
        tg = SavedMessages()
        await fill()
        try:
            b = FwdBackup(tg, db)
            db.on_change = b.touch
            await db.fwd_set_note("ZF-000001", "hello")
            assert b._due is not None                    # planned; the timer writes it a few seconds later
            await b.stop()                               # shutdown writes what is waiting
            assert len(tg.msgs) == 1 and b"hello" in gzip.decompress(tg.msgs[0].data)
        finally:
            db.on_change = None
            await db.close()
    run(go())
