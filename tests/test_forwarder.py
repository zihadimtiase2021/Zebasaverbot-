"""Chat -> chat copier: routes from .fwdsrc/.fwddst, download + upload, unique ids, loop protection."""
import asyncio
import re
import types

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")


class Ent:
    def __init__(self, id, title, broadcast=False):
        self.id, self.title, self.broadcast = id, title, broadcast


class Msg:
    def __init__(self, id, text="", photo=False, doc=False, sticker=False, grouped=None, out=False,
                 action=None, size=10):
        self.id, self.raw_text, self.entities = id, text, None
        self.grouped_id, self.out, self.action = grouped, out, action
        self.media = object() if (photo or doc or sticker) else None
        self.photo = object() if photo else None
        self.document = types.SimpleNamespace(attributes=[], thumbs=None) if (doc or sticker) else None
        self.sticker = self.document if sticker else None
        self.video = self.video_note = self.voice = self.gif = self.audio = None
        self.file = types.SimpleNamespace(size=size)


class Pipe:
    caption_limit, max_bytes = 1024, 1000

    def __init__(self):
        self.notes = []

    async def _retry(self, factory):
        return await factory()

    async def notify(self, text):
        self.notes.append(text)


class Client:
    def __init__(self):
        self.sent, self.n, self.ents = [], 100, {}

    def add_event_handler(self, *a):
        pass

    async def get_entity(self, ref):
        if ref in self.ents:
            return self.ents[ref]
        raise ValueError("unknown")

    async def get_dialogs(self):
        pass

    async def get_me(self):
        return Ent(1, "me")

    async def get_permissions(self, ent, me):
        return types.SimpleNamespace(is_creator=False, post_messages=True)

    async def send_message(self, dst, text, **kw):
        self.n += 1
        self.sent.append(("msg", dst, text, kw))
        return types.SimpleNamespace(id=self.n)

    async def send_file(self, dst, f, **kw):
        self.n += 1
        self.sent.append(("file", dst, f, kw))
        if isinstance(f, list):
            return [types.SimpleNamespace(id=self.n + i) for i in range(len(f))]
        return types.SimpleNamespace(id=self.n)

    async def download_media(self, m, file=None, thumb=None):
        from pathlib import Path
        p = Path(file)
        if str(file).endswith(("/", "\\")):
            p = p / f"m{m.id}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return str(p)


class Store:
    def __init__(self):
        self.saved, self.saves = {}, 0

    async def save(self):
        self.saves += 1


class Ev:
    def __init__(self, chat):
        self.chat = chat

    async def get_chat(self):
        return self.chat


def test_uid_and_html_fit():
    from unified_core.config import settings
    from unified_services import forwarder as F
    from telethon.tl.types import MessageEntityBold

    assert re.fullmatch(rf"{settings.FWD_ID_PREFIX}-[0-9A-F]{{12}}", F.new_uid())
    assert F.new_uid() != F.new_uid()
    assert F.msg_link(-1001234, 5) == "https://t.me/c/1234/5" and F.msg_link(55, 5) == ""
    out = F.build_html("hello", [MessageEntityBold(0, 5)], "ZF-1", 1024)
    assert re.fullmatch(F.NOTIFY_MARK + r"<(strong|b)>hello</\1>\n\n<code>ZF-1</code>", out)
    long = F.build_html("x" * 5000, [MessageEntityBold(0, 4999)], "ZF-1", 1024)
    assert long.endswith("<code>ZF-1</code>") and "…" in long and not re.search(r"<(strong|b)>", long)  # cut entity dropped
    assert len(re.sub(r"<[^>]+>", "", long)) <= 1024


def test_routes_copy_and_loop_protection(monkeypatch, tmp_path):
    from unified_core.config import settings
    from unified_core.database import db
    from unified_services import forwarder as F

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FWD_DELAY", 0)

    async def go():
        await db.connect(":memory:")
        try:
            cl, st, pipe = Client(), Store(), Pipe()
            src, dst = Ent(-1001, "Src", True), Ent(-1002, "Dst", True)
            fw = F.Forwarder(cl, pipe, me_id=1, store=st)

            assert "Source" in await fw.handle_command(Ev(src), ".fwdsrc")
            assert "Route #1" in await fw.handle_command(Ev(dst), ".fwddst")
            assert st.saved["FWD_ROUTES"][0]["dst"] == -1002
            await fw.handle_command(Ev(src), ".fwdsrc")
            assert "already exists" in await fw.handle_command(Ev(dst), ".fwddst")
            await fw.handle_command(Ev(dst), ".fwdsrc")
            assert "reverse" in await fw.handle_command(Ev(src), ".fwddst")

            # what is (not) copied
            assert fw._wanted(-1001, Msg(1, "hi")) and not fw._wanted(-999, Msg(1, "hi"))
            assert not fw._wanted(-1001, Msg(1, ".fwdsrc", out=True))      # my own command
            assert not fw._wanted(-1001, Msg(1, F.NOTIFY_MARK + "copy"))   # a copy made by the bot
            assert not fw._wanted(-1001, Msg(1, "joined", action=object()))

            routes = fw._routes_for(-1001)
            await fw._copy(F.Item(-1001, routes, [Msg(7, "hello")]))
            kind, to, text, _ = cl.sent[-1]
            uid = re.search(r"ZF-\d{6}", text).group(0)
            assert uid == "ZF-000001"  # serial numbers count up
            assert kind == "msg" and to == -1002 and "hello" in text
            row = await db.fwd_get(uid)
            assert row["src_msg"] == 7 and row["dst_msg"]

            n = len(cl.sent)  # same source message again (re-delivered update): nothing is sent twice
            await fw._copy(F.Item(-1001, routes, [Msg(7, "hello")]))
            assert len(cl.sent) == n

            await fw._copy(F.Item(-1001, routes, [Msg(8, "cap", photo=True)]))
            assert cl.sent[-1][0] == "file" and "ZF-" in cl.sent[-1][3]["caption"]

            await fw._copy(F.Item(-1001, routes, [Msg(11, "album", photo=True, grouped=5),
                                                  Msg(12, photo=True, grouped=5)]))
            assert isinstance(cl.sent[-1][2], list) and len(cl.sent[-1][2]) == 2

            await fw._copy(F.Item(-1001, routes, [Msg(20, sticker=True)]))  # no caption possible: id as reply
            assert cl.sent[-2][0] == "file" and cl.sent[-1][0] == "msg" and "ZF-" in cl.sent[-1][2]

            await fw._copy(F.Item(-1001, routes, [Msg(30, doc=True, size=10**6)]))  # over the upload limit
            assert pipe.notes
            assert not list((settings.tmp_dir).glob("fwd_*"))               # nothing left on disk

            assert "source:" in await fw.handle_command(Ev(src), f".fwd id {uid.lower()}")
            assert "Paused" in await fw.handle_command(Ev(src), ".fwd unwatch 1")
            assert not fw._wanted(-1001, Msg(1, "hi"))
            fw2 = F.Forwarder(cl, pipe, store=st)
            await fw2.load()
            assert len(fw2.routes) == 1 and fw2.routes[0]["active"] is False
        finally:
            await db.close()

    asyncio.run(go())


def test_can_route_tells_which_account_may_make_a_route(monkeypatch, tmp_path):
    """Used by the account choice in the Forward menu: None = this account can do it, else the reason."""
    from unified_core.config import settings
    from unified_core.database import db
    from unified_services import forwarder as F

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)

    class NoPost(Client):
        async def get_permissions(self, ent, me):
            return types.SimpleNamespace(is_creator=False, post_messages=False)

    async def go():
        await db.connect(":memory:")
        try:
            src, dst = Ent(-1001, "Src", True), Ent(-1002, "Dst", True)
            both, only_src, no_post = Client(), Client(), NoPost()
            for c in (both, no_post):
                c.ents.update({"srcchat": src, "dstchat": dst})
            only_src.ents["srcchat"] = src               # this account is not a member of the destination
            a = F.Forwarder(both, Pipe(), me_id=1, store=Store())
            b = F.Forwarder(only_src, Pipe(), me_id=2, store=Store())
            c = F.Forwarder(no_post, Pipe(), me_id=3, store=Store())

            assert await a.can_route("@srcchat", "@dstchat") is None
            assert "must be a member" in await b.can_route("@srcchat", "@dstchat")
            assert "Post messages" in await c.can_route("@srcchat", "@dstchat")
            assert "different" in await a.can_route("@srcchat", "@srcchat")

            assert "Route #1" in await a.handle_command(Ev(src), ".fwd @srcchat @dstchat")
            assert "already exists" in await a.can_route("@srcchat", "@dstchat")
            assert "reverse" in await a.can_route("@dstchat", "@srcchat")
            assert not b.routes and not c.routes                   # asking never creates a route
        finally:
            await db.close()

    asyncio.run(go())
