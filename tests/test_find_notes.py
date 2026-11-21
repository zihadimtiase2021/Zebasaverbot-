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
    def __init__(self, text, uid=1, client=None):
        self.text, self.sender_id, self.out, self.pattern_match = text, uid, [], None
        self.kw, self.client, self.chat_id, self.deleted = [], client, 42, 0

    async def respond(self, text, **kw):
        self.out.append(text)
        self.kw.append(kw)
        ev = self

        async def delete():
            ev.deleted += 1
        return types.SimpleNamespace(id=len(self.out), delete=delete)


def run(coro):
    return asyncio.run(coro)


async def fill():
    await db.connect(":memory:")
    for n, (src, cap) in enumerate([("Shop A", "Red dress 50% off"), ("Shop B", "Blue shirt_xl")], 1):
        uid = f"ZF-{n:06d}"
        assert await db.fwd_reserve(uid, SRC, 100 + n, DST)
        await db.fwd_set_dst(uid, 500 + n)
        await db.fwd_set_details(uid, src, cap, 3, 10)


def make_bot(routes=(), userbot=None, client=None):
    bot = Bot()
    accounts = types.SimpleNamespace(instances={}, primary_username="boss", primary_name="Boss")
    fw = types.SimpleNamespace(routes=list(routes), userbot=userbot, me_id=10)
    app = types.SimpleNamespace(me_id=10, forwarder=fw)
    find_cmds.register_find_commands(bot, lambda uid: uid == 1, accounts, app)

    async def call(text, uid=1, full=False):
        ev = Ev(text, uid, client)
        for pat, fn in bot.h:
            if isinstance(pat, bytes):          # callback-query handlers are called with call_cb()
                continue
            m = re.match(pat, text)
            if m:
                ev.pattern_match = m
                await fn(ev)
                return ev if full else ev.out
        raise AssertionError("no handler for " + text)

    async def call_cb(data: bytes, uid=1):
        ev = Ev("", uid, client)
        ev.answer = lambda *a, **k: asyncio.sleep(0)
        for pat, fn in bot.h:
            m = re.match(pat, data) if isinstance(pat, bytes) else None
            if m:
                ev.pattern_match = m
                await fn(ev)
                return ev
        raise AssertionError("no callback handler for %r" % data)
    call.cb = call_cb
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
            assert "https://t.me/c/1234567/101" in card and "https://t.me/c/7654321/501" in card and "no note" in card
            assert "Files" not in card and "Original caption" not in card          # minimal card: no "Files: 3" line
            assert "added" in (await call("/addnote 1 price 15$"))[0]
            assert "added" in (await call("/addnote ZF-000001 supplier Rahim"))[0]      # second note = new line
            assert "price 15$\nsupplier Rahim" in (await call("/find 1"))[0]
            out = await call("/find rahim")                                              # search = full details
            assert len(out) == 1 and "ZF-000001" in out[0] and "supplier Rahim" in out[0]      # one match = the card
            assert "No copy" in (await call("/find 77"))[0] and "No copy" in (await call("/addnote 77 x"))[0]
            assert "Nothing found" in (await call("/find zzz"))[0]
            assert "Too long" in (await call("/addnote 1 " + "x" * 1600))[0]
            assert "removed" in (await call("/removenote 1"))[0]
            assert "no note" in (await call("/find 1"))[0] and "no note" in (await call("/removenote 1"))[0]
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


# ---------------------------------------------------------------------------------------------------------------
# the database forgot the serial (Render restart / 90 day clean-up): /find searches the destination chat itself
class FakeMsg(types.SimpleNamespace):
    pass


def tg_msg(mid, text="", gid=None, media=False, reply=None):
    return FakeMsg(id=mid, raw_text=text, grouped_id=gid, media=object() if media else None,
                   photo=object() if media else None, document=None, reply_to_msg_id=reply,
                   date=types.SimpleNamespace(timestamp=lambda: 1_700_000_000), file=types.SimpleNamespace(size=10),
                   video=None, voice=None, video_note=None, gif=None, audio=None)


class FakeUserbot:
    """A destination chat with one 7 file album whose first file carries the caption + serial, and a text post."""

    def __init__(self, tmp_path=None):
        album = [tg_msg(1870, "\u2063Roza Non professional. 1 hr 08k\n\nZF-000020", gid=77, media=True)]
        album += [tg_msg(1870 + i, "", gid=77, media=True) for i in range(1, 7)]
        self.chat = {m.id: m for m in album}
        self.chat[1900] = tg_msg(1900, "Just text ZF-000200")
        self.searches, self.downloads = [], []

    async def iter_messages(self, chat, search=None, limit=None):
        assert chat == DST
        self.searches.append(search)
        for m in sorted(self.chat.values(), key=lambda x: -x.id):
            if search is None or search.lower() in m.raw_text.lower():
                yield m

    async def get_messages(self, chat, ids=None):
        if isinstance(ids, list):
            return [self.chat.get(i) for i in ids]
        return self.chat.get(ids)

    async def download_media(self, m, file=None):
        path = f"{file}{m.id}.jpg"
        open(path, "wb").write(b"x")
        self.downloads.append(m.id)
        return path


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_file(self, chat, file, **kw):
        self.sent.append((chat, file, kw))
        return [types.SimpleNamespace(id=5000 + i) for i in range(len(file) if isinstance(file, list) else 1)]


def test_find_restores_a_forgotten_serial_from_the_destination_and_sends_the_album():
    async def go():
        await db.connect(":memory:")                                   # EMPTY database: it knows nothing
        try:
            ub, client = FakeUserbot(), FakeClient()
            call = make_bot([{"src": SRC, "dst": DST, "src_name": "Shop", "dst_name": "Main"}], ub, client)
            ev = await call("/find 20", full=True)
            assert len(client.sent) == 1                               # ONE message: the album, card as its caption
            chat, files, kw = client.sent[0]
            assert chat == 42 and isinstance(files, list) and len(files) == 7
            assert "ZF-000020" in kw["caption"] and "Roza Non professional" in kw["caption"] and "Files" not in kw["caption"]
            assert "https://t.me/c/7654321/1870" in kw["caption"] and len(kw["caption"]) <= 1024
            assert sorted(ub.downloads) == list(range(1870, 1877)) and ev.deleted >= 1   # status message removed
            assert ub.searches[0] == "ZF-000020"                       # the serial itself was searched
            row = await db.fwd_get("ZF-000020")                        # and the product is back in the database
            assert row["dst_chat"] == DST and row["dst_msg"] == 1870 and row["media"] == 7
            assert "Roza" in row["caption"] and "ZF-000020" not in row["caption"]
            # now a note works on it, and a second /find does not search the chat again
            assert "added" in (await call("/addnote 20 price 12k"))[0]
            before = len(ub.searches)
            await call("/find 20")
            assert len(ub.searches) == before and "price 12k" in client.sent[-1][2]["caption"]
            # ZF-000200 is a different serial: ZF-000020 must not match it
            assert "No copy" in (await call("/find 21"))[-1]       # (the first message is the "Searching…" status)
            # a serial that is nowhere: a clear message, nothing stored
            assert await db.fwd_get("ZF-000021") is None
        finally:
            await db.close()
    run(go())


def test_addnote_on_a_forgotten_serial_and_text_search_in_the_destination():
    async def go():
        await db.connect(":memory:")
        try:
            ub, client = FakeUserbot(), FakeClient()
            call = make_bot([{"src": SRC, "dst": DST}], ub, client)
            assert "added" in (await call("/addnote ZF-000020 supplier Rahim"))[0]
            assert (await db.fwd_get("ZF-000020"))["note"] == "supplier Rahim"
            await db.close()
            await db.connect(":memory:")
            out = await call("/find roza")                              # words: not in db -> destination search
            assert len(client.sent) == 1 and "ZF-000020" in client.sent[0][2]["caption"]
        finally:
            await db.close()
    run(go())


def test_text_search_with_several_matches_is_a_list_with_buttons():
    async def go():
        await fill()
        try:
            call = make_bot()
            ev = await call("/find e", full=True)                        # both products contain an "e"
            assert len(ev.out) == 1 and "ZF-000001" in ev.out[0] and "ZF-000002" in ev.out[0]
            rows = ev.kw[0]["buttons"]
            assert [b[0] for r in rows for b in r] == ["ZF-000002", "ZF-000001"]           # newest first
            assert [b[1] for r in rows for b in r] == [b"fd:2", b"fd:1"]
            card = (await call.cb(b"fd:2")).out[0]                       # tapping a button opens the product
            assert "ZF-000002" in card and "Blue shirt_xl" in card
            assert (await call.cb(b"fd:2", uid=9)).out == []             # not an admin: nothing
        finally:
            await db.close()
    run(go())


def test_card_is_minimal_and_keeps_a_caption_within_telegram_limits():
    row = {"uid": "ZF-000246", "created_at": 1_700_000_000, "src_chat": -1002869880872, "src_msg": 1280,
           "src_name": "Realservicedhakabd", "dst_chat": -1002940625991, "dst_msg": 1876, "media": 7,
           "caption": "Name: Roza " * 200, "note": "n" * 1500}
    full, short = find_cmds.make_card(row, "@lovebirds25"), find_cmds.make_card(row, "@lovebirds25", compact=True)
    assert "Files" not in full and "7" not in full.split("\n")[0] and "@lovebirds25" in full
    assert "Source" in full and "Copy" in full and "<blockquote>" in full
    plain = re.sub(r"<[^>]+>", "", short)
    assert len(plain) <= 1024
