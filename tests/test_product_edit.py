"""/find -> ✏️ Edit: caption / file / details change the live channel message AND the database record."""
import asyncio
import re
import sys
import types

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

from telethon import errors  # noqa: E402

from unified_core.database import db  # noqa: E402
from unified_services import edit_cmds  # noqa: E402

DST = -1007654321


def run(c):
    return asyncio.run(c)


def err(name, **kw):
    return getattr(errors, name)(request=None, **kw)


class Msg:
    def __init__(self, id, text="", media=True, grouped=None, entities=None):
        self.id, self.message, self.raw_text, self.entities = id, text, text, entities
        self.media = object() if media else None
        self.photo = self.media
        self.document = self.sticker = self.video_note = self.web_preview = None
        self.grouped_id = grouped


class Userbot:
    def __init__(self, chat, refuse=None, flood=None):
        self.chat, self.refuse, self.flood, self.edits = chat, refuse, list(flood or []), []

    async def get_messages(self, chat, ids=None):
        return self.chat.get(ids)

    async def edit_message(self, chat, mid, text=None, **kw):
        if self.refuse:
            raise self.refuse
        if self.flood:
            raise self.flood.pop(0)
        if mid not in self.chat:
            raise err("MessageIdInvalidError")
        self.edits.append((mid, text, kw))


def helpers(*accounts, chat=None):
    """accounts: [(userbot, account id)]"""
    async def album_of(ub, dst, mid):
        m = ub.chat.get(mid)
        if m is None:
            return []
        return sorted([x for x in ub.chat.values() if m.grouped_id and x.grouped_id == m.grouped_id] or [m],
                      key=lambda x: x.id)

    async def find_message(ub, dst, uid):
        return None

    async def get_row(uid):
        return await db.fwd_get(uid)

    fw = types.SimpleNamespace(pipeline=types.SimpleNamespace(caption_limit=1024, max_bytes=10 ** 9))
    return types.SimpleNamespace(clients=lambda: [(ub, me, fw) for ub, me in accounts], album_of=album_of,
                                 find_message=find_message, get_row=get_row)


async def product(media=1, mid=501):
    await db.connect(":memory:")
    assert await db.fwd_reserve("ZF-000001", -1001, 7, DST)
    await db.fwd_set_dst("ZF-000001", mid)
    await db.fwd_set_details("ZF-000001", "Shop", "Red dress", media, 10)
    return await db.fwd_get("ZF-000001")


def test_caption_edit_updates_channel_and_record():
    async def go():
        try:
            row = await product()
            ub = Userbot({501: Msg(501, "Red dress\n\nZF-000001")})
            ed = edit_cmds.ProductEditor(helpers((ub, 10)))
            lines = await ed.edit_caption(row, "Blue dress", None)
            mid, text, kw = ub.edits[0]
            assert mid == 501 and "Blue dress" in text and "<code>ZF-000001</code>" in text and kw["parse_mode"] == "html"
            assert (await db.fwd_get("ZF-000001"))["caption"] == "Blue dress" and "Channel message edited" in lines[1]
            with pytest.raises(edit_cmds.EditError, match="Too long"):
                await ed.edit_caption(row, "x" * 2000, None)
            assert (await db.fwd_get("ZF-000001"))["caption"] == "Blue dress"     # a refused edit changes nothing
        finally:
            await db.close()
    run(go())


def test_deleted_channel_message_is_reported_not_crashed():
    async def go():
        try:
            row = await product()
            ed = edit_cmds.ProductEditor(helpers((Userbot({}), 10)))              # the message is gone
            lines = await ed.edit_caption(row, "New text", None)
            assert "deleted" in lines[1] and (await db.fwd_get("ZF-000001"))["caption"] == "New text"
            with pytest.raises(edit_cmds.EditError, match="deleted"):
                await ed.replace_file(row, 0, "/tmp/x.jpg")                      # a file has nothing to go on
            # the message vanishes between the check and the edit (MESSAGE_ID_INVALID)
            ub = Userbot({501: Msg(501, "t")}, refuse=err("MessageIdInvalidError"))
            lines = await edit_cmds.ProductEditor(helpers((ub, 10))).edit_caption(row, "Again", None)
            assert "deleted" in lines[1]
        finally:
            await db.close()
    run(go())


def test_floodwait_short_is_waited_long_is_reported(monkeypatch):
    async def go():
        try:
            row = await product()
            slept = []

            async def fake_sleep(s):
                slept.append(s)
            monkeypatch.setattr(edit_cmds.asyncio, "sleep", fake_sleep)
            ub = Userbot({501: Msg(501, "x")}, flood=[err("FloodWaitError", capture=3)])
            await edit_cmds.ProductEditor(helpers((ub, 10))).edit_caption(row, "After flood", None)
            assert slept == [4] and len(ub.edits) == 1                            # waited, retried, done
            ub = Userbot({501: Msg(501, "x")}, flood=[err("FloodWaitError", capture=900)])
            with pytest.raises(edit_cmds.EditError, match="900"):
                await edit_cmds.ProductEditor(helpers((ub, 10))).edit_caption(row, "Too soon", None)
            assert (await db.fwd_get("ZF-000001"))["caption"] == "After flood"    # nothing changed on the long wait
        finally:
            await db.close()
    run(go())


def test_another_account_is_tried_when_the_author_may_not_edit():
    async def go():
        try:
            row = await product()
            chat = {501: Msg(501, "x")}
            author = Userbot(chat, refuse=err("MessageAuthorRequiredError"))
            admin = Userbot(chat)
            ed = edit_cmds.ProductEditor(helpers((admin, 99), (author, 10)))      # author (id 10) is asked first
            await ed.edit_caption(row, "By the admin account", None)
            assert not author.edits and len(admin.edits) == 1
            nobody = edit_cmds.ProductEditor(helpers((Userbot(chat, refuse=err("ChatAdminRequiredError")), 10)))
            with pytest.raises(edit_cmds.EditError, match="No account may edit"):
                await nobody.edit_caption(row, "no", None)
        finally:
            await db.close()
    run(go())


def test_replace_file_keeps_the_caption():
    async def go():
        try:
            row = await product(media=2)
            chat = {501: Msg(501, "Red dress\n\nZF-000001", grouped=9, entities=["E"]), 502: Msg(502, "", grouped=9)}
            ub = Userbot(chat)
            ed = edit_cmds.ProductEditor(helpers((ub, 10)))
            await ed.replace_file(row, 0, "/tmp/new.jpg")                           # the file WITH the caption
            mid, text, kw = ub.edits[0]
            assert mid == 501 and text == "Red dress\n\nZF-000001" and kw["file"] == "/tmp/new.jpg"
            assert kw["formatting_entities"] == ["E"] and kw["parse_mode"] is None
            await ed.replace_file(row, 1, "/tmp/second.jpg", force_document=True)  # a file without caption
            assert ub.edits[1][0] == 502 and ub.edits[1][1] is None and ub.edits[1][2]["force_document"] is True
            with pytest.raises(edit_cmds.EditError, match="no file 3"):
                await ed.replace_file(row, 2, "/tmp/x.jpg")
        finally:
            await db.close()
    run(go())


# ------------------------------------------------------------------ the bot UI
class Bot:
    def __init__(self):
        self.cb, self.nm = [], []

    def on(self, spec):
        def deco(fn):
            (self.cb if spec[0] == "cb" else self.nm).append((spec[1], fn))
            return fn
        return deco


class Ev:
    def __init__(self, data=b"", text="", uid=1, media=None):
        self.data, self.raw_text, self.sender_id, self.is_private, self.out = data, text, uid, True, []
        self.media = media
        self.message = types.SimpleNamespace(raw_text=text, entities=None, file=None)

    async def edit(self, text, buttons=None):
        self.out.append(("edit", text, buttons))

    async def respond(self, text, buttons=None, **kw):
        self.out.append(("respond", text, buttons))
        return types.SimpleNamespace(delete=_noop, edit=_noop)

    async def answer(self, text="", alert=False):
        self.out.append(("answer", text, alert))


async def _noop(*a, **k):
    return None


def test_ui_flow_caption_back_cancel_and_details():
    async def go():
        try:
            row = await product()
            ub = Userbot({501: Msg(501, "Red dress\n\nZF-000001")})
            bot, app = Bot(), types.SimpleNamespace()
            edit_cmds.register_edit_commands(bot, lambda u: u == 1, app, helpers((ub, 10)))
            (spec, cb), (nspec, on_text) = bot.cb[0], bot.nm[0]

            async def press(data: bytes):
                ev = Ev(data)
                ev.pattern_match = re.match(spec["pattern"], data)
                await cb(ev)
                return ev

            ev = await press(b"fe:ZF-000001")                                       # Edit under the product
            assert ev.out[-1][0] == "respond" and "Edit ZF-000001" in ev.out[-1][1]
            ev = await press(b"fe:ZF-000001:c")                                     # 📝 Caption
            assert app.editing[1]["mode"] == "caption"
            ev = await press(b"fe:ZF-000001:p")                                     # ⬅️ Back
            assert 1 not in app.editing and "Edit ZF-000001" in ev.out[-1][1]
            await press(b"fe:ZF-000001:c")
            ev = await press(b"fe:ZF-000001:x")                                     # ❌ Cancel
            assert 1 not in app.editing and "closed" in ev.out[-1][1]

            await press(b"fe:ZF-000001:c")
            msg = Ev(text="Blue dress")
            assert nspec["func"](msg)
            with pytest.raises(BaseException):                                       # StopPropagation: no watermark handler
                await on_text(msg)
            assert ub.edits and "Blue dress" in ub.edits[0][1]
            assert (await db.fwd_get("ZF-000001"))["caption"] == "Blue dress" and 1 not in app.editing

            await press(b"fe:ZF-000001:d")
            with pytest.raises(BaseException):
                await on_text(Ev(text="supplier Rahim, COD"))
            assert (await db.fwd_get("ZF-000001"))["note"] == "supplier Rahim, COD"
            assert not nspec["func"](Ev(text="hello"))                               # nothing pending: ignored
            assert (await press(b"fe:ZF-000009")) is not None                        # unknown product: alert, no crash
        finally:
            await db.close()
    run(go())
