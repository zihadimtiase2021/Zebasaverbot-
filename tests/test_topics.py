"""Forum topics: chat/topic parsing, topic routes (source and destination), ledger keys, sending into a topic."""
import asyncio
import types

import pytest

from unified_core.tg import forum_topic_of, parse_chat_topic
from unified_services.fwd_ledger import route_key


def test_parse_chat_topic():
    assert parse_chat_topic("3617219724/119") == (3617219724, 119)
    assert parse_chat_topic("-1003617219724/119") == (-1003617219724, 119)
    assert parse_chat_topic("@mygroup/7") == ("mygroup", 7)
    assert parse_chat_topic("https://t.me/c/3617219724/119") == (-1003617219724, 119)
    assert parse_chat_topic("https://t.me/c/3617219724/119/5000") == (-1003617219724, 119)   # message in a topic
    assert parse_chat_topic("https://t.me/mygroup/7") == ("mygroup", 7)
    assert parse_chat_topic("3617219724") == (3617219724, 0)
    assert parse_chat_topic("@mygroup") == ("mygroup", 0)
    for bad in ("", "3617219724/abc", "3617219724/0", "3617219724/"):
        with pytest.raises(ValueError):
            parse_chat_topic(bad)


def test_forum_topic_of():
    rt = types.SimpleNamespace
    assert forum_topic_of(types.SimpleNamespace()) == 0
    assert forum_topic_of(types.SimpleNamespace(reply_to=rt(forum_topic=True, reply_to_msg_id=119, reply_to_top_id=None))) == 119
    assert forum_topic_of(types.SimpleNamespace(reply_to=rt(forum_topic=True, reply_to_msg_id=900, reply_to_top_id=119))) == 119
    assert forum_topic_of(types.SimpleNamespace(reply_to=rt(forum_topic=False, reply_to_msg_id=900, reply_to_top_id=None))) == 0


def test_route_key_is_backward_compatible():
    assert route_key(-1001, -1002) == "-1001>-1002"
    assert route_key(-1001, -1002, 0, 119) == "-1001>-1002#119"
    assert route_key(-1001, -1002, 5, 119) == "-1001#5>-1002#119"


# ---- the forwarder itself (needs telethon + the project's dependencies, like the other forwarder tests)
class Ev0:
    async def get_chat(self):
        raise AssertionError("not needed")


def test_topic_routes(monkeypatch, tmp_path):
    pytest.importorskip("telethon")
    pytest.importorskip("pydantic_settings")
    pytest.importorskip("aiosqlite")
    from tests.test_forwarder import Client, Ent, Msg, Pipe, Store

    class ForumEnt(Ent):
        forum = True

    def topic_msg(id, topic, text="hi"):
        m = Msg(id, text)
        m.reply_to = types.SimpleNamespace(forum_topic=True, reply_to_msg_id=topic, reply_to_top_id=None)
        return m

    from unified_core.config import settings
    from unified_core.database import db
    from unified_services import forwarder as F

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FWD_DELAY", 0)

    async def go():
        await db.connect(":memory:")
        try:
            cl, st, pipe = Client(), Store(), Pipe()
            src, dst = ForumEnt(-1001, "Src", True), ForumEnt(-1002, "Dst", True)
            cl.ents.update({-1001: src, -1002: dst, 1001: src, 1002: dst})
            fw = F.Forwarder(cl, pipe, me_id=1, store=st)

            # topic 5 of the source -> topic 119 of the destination, written as chat/topic
            ans = await fw._cmd_fwd("-1001/5 -1002/119")
            assert "Route #1" in ans
            r = st.saved["FWD_ROUTES"][0]
            assert (r["src_topic"], r["dst_topic"]) == (5, 119)
            assert "already exists" in await fw.handle_command(Ev0(), ".fwd -1001/5 -1002/119")
            assert "reverse" in await fw.handle_command(Ev0(), ".fwd -1002/119 -1001/5")
            assert "Route #2" in await fw._cmd_fwd("-1001/6 -1002/119")        # another topic: its own route

            # only messages of topic 5 are wanted by route 1; route 2 wants topic 6
            assert [x["src_topic"] for x in fw._wanted(-1001, topic_msg(1, 5))] == [5]
            assert [x["src_topic"] for x in fw._wanted(-1001, topic_msg(2, 6))] == [6]
            assert not fw._wanted(-1001, Msg(3, "General / no topic"))

            # the copy lands in topic 119 of the destination
            await fw._deliver(fw.routes[0], topic_msg(1, 5), "hello", None, [], False)
            kind, chat, _text, kw = cl.sent[-1]
            assert chat == -1002 and kw.get("reply_to") == 119

            # each topic route has its own "already copied" history
            assert F.rkey(fw.routes[0]) != F.rkey(fw.routes[1])

            # a route without topics sends without reply_to
            fw2 = F.Forwarder(cl, pipe, me_id=1, store=Store())
            await fw2._cmd_fwd("-1001 -1002")
            await fw2._deliver(fw2.routes[0], Msg(1, "x"), "plain", None, [], False)
            assert "reply_to" not in cl.sent[-1][3]

            # one forum group: topic -> topic is fine, but both topics must be named and differ
            fw3 = F.Forwarder(cl, pipe, me_id=1, store=Store())
            assert "Route #1" in await fw3._cmd_fwd("-1001/5 -1001/6")
            assert "different" in await fw3.handle_command(Ev0(), ".fwd -1001 -1001/6")
            assert "different topics" in await fw3.handle_command(Ev0(), ".fwd -1001/5 -1001/5")
        finally:
            await db.close()

    asyncio.run(go())


def test_old_routes_load_without_topics():
    pytest.importorskip("telethon")
    pytest.importorskip("pydantic_settings")
    pytest.importorskip("aiosqlite")
    from unified_services import forwarder as F
    r = F.route_defaults({"src": -1, "dst": -2, "src_name": "A", "dst_name": "B"})
    assert r["src_topic"] == 0 and r["dst_topic"] == 0 and F.rkey(r) == "-1>-2"
