""".dlauto on/off: links sent in that chat (with or without .dl) are posted back into the same chat."""
import asyncio
import types

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

MARK = "\u2063"


def test_dlauto_toggle_and_same_chat_jobs(monkeypatch):
    from unified_core.database import Database
    from unified_services import userbot_handlers as uh

    jobs = []
    handlers = []

    class Q:
        def enqueue(self, job):
            jobs.append(job)
            return True

    class User:
        def on(self, _event):
            return lambda fn: handlers.append(fn) or fn

    class Ev:
        file = None

        def __init__(self, text, chat_id):
            self.raw_text, self.chat_id = text, chat_id

        async def edit(self, *a, **k): pass
        async def respond(self, *a, **k): return types.SimpleNamespace(id=1)
        async def delete(self): pass

    async def go():
        d = Database()
        await d.connect(":memory:")
        monkeypatch.setattr(uh, "db", d)
        monkeypatch.setattr(uh.settings, "DELETE_TRIGGER_MESSAGE", False)
        try:
            uh.register_userbot_handlers(types.SimpleNamespace(
                userbot=User(), queue=Q(), me_id=1, ignored_chats=set(), pipeline=None))
            send = handlers[0]
            url = "https://x.com/u/status/%d"

            await send(Ev(url % 1, 100))                       # chat is not auto yet -> nothing
            assert jobs == []
            await send(Ev(".dlauto on", 100))
            assert await d.is_dlauto_chat(100)
            await send(Ev(url % 2, 100))                       # plain link works, answer goes to the same chat
            await send(Ev(".dl " + url % 3 + " my caption", 100))
            await send(Ev(MARK + url % 4, 100))                # the bot's own post must never be re-processed
            assert [(j.url[-1], j.dest_chat, j.custom_caption) for j in jobs] == [("2", 100, None), ("3", 100, "my caption")]

            await send(Ev(".dl " + url % 5, 200))              # another chat: old behaviour (channel)
            assert jobs[-1].dest_chat is None

            await send(Ev(".dlauto off", 100))
            assert not await d.is_dlauto_chat(100)
            n = len(jobs)
            await send(Ev(url % 6, 100))
            assert len(jobs) == n
        finally:
            await d.close()

    asyncio.run(go())


def test_pipeline_posts_into_dest_chat(tmp_path, monkeypatch):
    from unified_core.config import settings
    from unified_core.database import Database
    from unified_services import pipeline as pl
    from unified_services.models import Comment, MediaItem, PostData

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "MAX_COMMENTS", 5)
    monkeypatch.setattr(settings, "COMMENT_DELAY", 0)
    sent, replies = [], []

    async def fake_fetch(url, workdir, userbot, max_bytes):
        f = workdir / "a.mp4"
        f.write_bytes(b"x" * 100)
        return PostData(source_url=url, platform="generic", caption="hi", media=[MediaItem(f, "video")],
                        comments=[Comment("nice https://x.com/a/status/9", "bob")])

    class Bot:
        async def send_file(self, target, file, **kw):
            sent.append((target, kw["caption"]))
            return types.SimpleNamespace(id=7)

        async def send_message(self, target, text, **kw):
            replies.append((target, text, kw.get("reply_to")))

    class Tr:
        async def translate(self, text):
            return None

    monkeypatch.setattr(pl, "fetch_post", fake_fetch)
    monkeypatch.setattr(pl, "db", Database())

    async def go():
        await pl.db.connect(":memory:")
        try:
            p = pl.Pipeline(Bot(), "CHANNEL", None, False, Tr())
            res = await p.run(pl.Job(url="https://example.org/v", dest_chat=555, key="k@555"))
            await p.drain(5)
            return res
        finally:
            await pl.db.close()

    res = asyncio.run(go())
    assert res.status == "done"
    assert sent[0][0] == 555 and sent[0][1].startswith(MARK) and len(sent[0][1]) <= 1024
    assert replies == [(555, MARK + "@bob: nice https://x.com/a/status/9", 7)]   # marked -> no loop
