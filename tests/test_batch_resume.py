"""A batch survives a restart: finished links are not redone, the rest continues without re-sending the file."""
import asyncio
import types

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")


def _items(n):
    return [(f"https://x.com/u/status/{i}", f"cap{i}" if i % 2 else None) for i in range(n)]


def test_batch_rows_roundtrip_and_unfinished():
    from unified_core.database import Database

    async def go():
        d = Database()
        await d.connect(":memory:")
        try:
            bid = await d.batch_create(42, True, _items(5))
            assert [b["id"] for b in await d.batches_unfinished()] == [bid]
            await d.batch_item_set(bid, 0, "done")
            await d.batch_item_set(bid, 1, "failed", "boom")
            rows = await d.batch_items(bid)
            assert [r["status"] for r in rows] == ["done", "failed", "pending", "pending", "pending"]
            assert rows[1]["error"] == "boom" and rows[1]["custom_caption"] == "cap1"
            await d.batch_finish(bid)
            assert await d.batches_unfinished() == []
        finally:
            await d.close()

    asyncio.run(go())


def test_batch_resumes_where_it_stopped(monkeypatch):
    from unified_core.config import settings
    from unified_core.database import Database
    from unified_services import userbot_handlers as uh
    from unified_services.pipeline import JobResult
    from unified_services.queue import JobQueue

    monkeypatch.setattr(settings, "RESUME_BATCHES", True)
    monkeypatch.setattr(settings, "MAX_BATCH_URLS", 1000)
    monkeypatch.setattr(settings, "MAX_CONCURRENCY", 1)
    N, CRASH_AFTER = 50, 10
    processed: list[str] = []   # every URL a "pipeline" actually handled, across both runs
    edits: list[str] = []

    class User:
        async def edit_message(self, chat, msg_id, text, **kw):
            edits.append(text)

        async def send_message(self, chat, text, **kw):
            edits.append(text)
            return types.SimpleNamespace(id=99)

    class Pipe:
        async def notify(self, text):
            edits.append(text)

    class Event:
        chat_id = 7

        async def respond(self, text, **kw):
            return types.SimpleNamespace(id=5)

    def make_app(runner, db_obj):
        holder = {}

        async def on_result(job, result):
            await uh.on_job_result(holder["app"], job, result)

        q = JobQueue(runner, on_result)
        app = types.SimpleNamespace(userbot=User(), queue=q, pipeline=Pipe())
        holder["app"] = app
        q.start(1)
        return app

    async def go():
        d = Database()
        await d.connect(":memory:")
        monkeypatch.setattr(uh, "db", d)
        try:
            # ---- first run: "crashes" after 10 links
            gate = asyncio.Event()

            async def runner1(job):
                if len(processed) >= CRASH_AFTER:
                    await gate.wait()           # hangs like a job that is killed mid-way
                processed.append(job.url)
                return JobResult("done", link="l")

            app1 = make_app(runner1, d)
            t = asyncio.create_task(uh.run_batch(app1, Event(), _items(N)))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(processed) >= CRASH_AFTER:
                    break
            await asyncio.sleep(0.1)
            # power cut: workers die, nothing else is recorded
            for w in app1.queue._workers:
                w.cancel()
            t.cancel()
            await asyncio.gather(*app1.queue._workers, t, return_exceptions=True)

            left = [i for i in await d.batch_items(1) if i["status"] == "pending"]
            assert len(left) == N - CRASH_AFTER
            assert len(await d.batches_unfinished()) == 1

            # ---- second run: new process, only the file's remaining links are processed
            before = list(processed)

            async def runner2(job):
                processed.append(job.url)
                return JobResult("done", link="l")

            app2 = make_app(runner2, d)
            assert await uh.resume_batches(app2) == 1
            for _ in range(500):
                await asyncio.sleep(0.01)
                if not await d.batches_unfinished():
                    break
            assert not await d.batches_unfinished()
            assert len(processed) == N and len(set(processed)) == N          # nothing twice
            assert processed[len(before):] == [i["url"] for i in left]       # only the remainder
            assert all(i["status"] == "done" for i in await d.batch_items(1))
            assert "50 / 50" in edits[-1]                                    # dashboard knows earlier progress
            for w in app2.queue._workers:
                w.cancel()
            await asyncio.gather(*app2.queue._workers, return_exceptions=True)
        finally:
            await d.close()

    asyncio.run(go())


def test_comments_run_in_background_and_do_not_block_the_job(tmp_path, monkeypatch):
    from unified_core.config import settings
    from unified_core.database import Database
    from unified_services import pipeline as pl
    from unified_services.models import Comment, MediaItem, PostData

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "MAX_COMMENTS", 5)
    order: list[str] = []

    async def fake_fetch(url, workdir, userbot, max_bytes):
        f = workdir / "a.mp4"
        f.write_bytes(b"x" * 100)

        async def slow_replies():
            await asyncio.sleep(0.2)   # replies being fetched while the media is uploaded
            data.comments.append(Comment("nice", "bob"))

        data = PostData(source_url=url, platform="twitter", media=[MediaItem(f, "video")])
        data.comments_task = asyncio.create_task(slow_replies())
        return data

    class Bot:
        async def send_file(self, *a, **kw):
            assert kw.get("part_size_kb") == 512
            return types.SimpleNamespace(id=1)

    async def fake_comments(self, msg, tag, comments):
        order.append(f"comments:{len(comments)}")
        return len(comments)

    class Tr:
        async def translate(self, text):
            return None

    monkeypatch.setattr(pl, "fetch_post", fake_fetch)
    monkeypatch.setattr(pl.Pipeline, "_post_comments", fake_comments)
    monkeypatch.setattr(pl, "db", Database())

    async def go():
        await pl.db.connect(":memory:")
        try:
            p = pl.Pipeline(Bot(), None, None, False, Tr())
            res = await p.run(pl.Job(url="https://x.com/u/status/1"))
            order.append("job returned")
            await p.drain(5)
            return res
        finally:
            await pl.db.close()

    res = asyncio.run(go())
    assert res.status == "done"
    assert order == ["job returned", "comments:1"]
