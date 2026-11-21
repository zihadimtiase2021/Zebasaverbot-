"""Watch + Forward all: nothing is copied twice, serial numbers count up, links / banned words are removed."""
import asyncio
import re
import types

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

from tests.test_forwarder import Ent, Msg, Pipe  # noqa: E402


class Client:
    """Like the client of test_forwarder, plus a message history that iter_messages walks through."""

    def __init__(self, hist):
        self.hist, self.sent, self.n = hist, [], 1000

    def add_event_handler(self, *a):
        pass

    async def iter_messages(self, chat, reverse=False, min_id=0, **kw):
        for m in sorted(self.hist.get(chat, []), key=lambda m: m.id):
            if m.id > min_id:
                yield m

    async def get_messages(self, chat, limit=0):
        return types.SimpleNamespace(total=len(self.hist.get(chat, [])))

    async def get_me(self):
        return Ent(1, "me")

    async def get_permissions(self, ent, me):
        return types.SimpleNamespace(is_creator=True, post_messages=True)

    async def send_message(self, dst, text, **kw):
        self.n += 1
        if dst != "me":
            self.sent.append(("msg", dst, text))
        return types.SimpleNamespace(id=self.n)

    async def edit_message(self, *a, **kw):
        pass

    async def send_file(self, dst, f, **kw):
        self.n += 1
        self.sent.append(("file", dst, f, kw.get("caption")))
        if isinstance(f, list):
            return [types.SimpleNamespace(id=self.n + i) for i in range(len(f))]
        return types.SimpleNamespace(id=self.n)

    async def download_media(self, m, file=None, thumb=None):
        from pathlib import Path
        p = Path(file) / f"m{m.id}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return str(p)


async def finish(fw, route):
    job = fw._job_for(route)
    if job and job["task"]:
        await job["task"]
    return job


def texts(cl):
    return [s[2] if s[0] == "msg" else s[3] for s in cl.sent]


def test_forward_all_is_incremental_and_never_duplicates(monkeypatch, tmp_path):
    from unified_core.config import settings
    from unified_core.database import db
    from unified_services import forwarder as F

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FWD_DELAY", 0)

    async def go():
        await db.connect(":memory:")
        try:
            hist = {-1001: [Msg(1, "hello https://spam.com/x now"), Msg(2, "https://only.link"),
                            Msg(3, "joined", action=object()), Msg(4, "album", photo=True, grouped=9),
                            Msg(5, photo=True, grouped=9), Msg(6, "plain t.me/foo text @bad")]}
            cl, pipe = Client(hist), Pipe()
            fw = F.Forwarder(cl, pipe)
            fw.bans = ["@bad"]
            src, dst = Ent(-1001, "Src", True), Ent(-1002, "Dst", True)
            await fw._add_route(-1001, "Src", dst)
            route = fw.routes[0]
            fw._worker = asyncio.create_task(fw._run())

            assert "Forward all" in await fw._cmd_fwd("all")
            job = await finish(fw, route)
            assert job["state"] == "done" and job["sent"] == 3 and job["failed"] == 0
            out = texts(cl)
            assert "spam.com" not in " ".join(out) and "t.me" not in " ".join(out) and "@bad" not in " ".join(out)
            assert [re.search(r"ZF-\d{6}", t).group(0) for t in out] == ["ZF-000001", "ZF-000002", "ZF-000003"]
            assert isinstance(cl.sent[1][2], list) and len(cl.sent[1][2]) == 2   # the album stayed one post
            assert fw.ledger.has_all("-1001>-1002", range(1, 7))                 # skipped ones are resolved too

            n = len(cl.sent)                                  # second Forward all: nothing new -> nothing sent
            await fw._cmd_fwd("all")
            await finish(fw, route)
            assert len(cl.sent) == n

            hist[-1001] += [Msg(7, "new one"), Msg(8, "new two")]
            await fw._on_message(types.SimpleNamespace(message=hist[-1001][-2], chat_id=-1001))   # live watcher: 7
            await asyncio.sleep(0.1)
            await fw._cmd_fwd("all")                          # only 8 is missing
            await finish(fw, route)
            out = texts(cl)
            assert sum("new one" in t for t in out) == 1 and sum("new two" in t for t in out) == 1
            assert re.search(r"ZF-000005", out[-1])

            # a failing message stays open and is the only one retried
            hist[-1001] += [Msg(9, "will fail"), Msg(10, "after")]
            orig = cl.send_message

            async def flaky(dst, text, **kw):
                if "will fail" in text:
                    raise RuntimeError("boom")
                return await orig(dst, text, **kw)
            cl.send_message = flaky
            await fw._cmd_fwd("all")
            job = await finish(fw, route)
            assert job["state"] == "done with errors" and job["failed"] == 1
            assert not fw.ledger.has("-1001>-1002", 9) and fw.ledger.has("-1001>-1002", 10)
            cl.send_message = orig
            before = len(cl.sent)
            await fw._cmd_fwd("all")
            await finish(fw, route)
            assert len(cl.sent) == before + 1 and "will fail" in texts(cl)[-1]

            assert "forgot" in (await fw._cmd_fwd("reset 1")).lower()
            fw._worker.cancel()
        finally:
            await db.close()

    asyncio.run(go())


def test_watch_pause_links_switch_and_stop(monkeypatch, tmp_path):
    from unified_core.config import settings
    from unified_core.database import db
    from unified_services import forwarder as F

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FWD_DELAY", 0)

    async def go():
        await db.connect(":memory:")
        try:
            cl, pipe = Client({-1001: []}), Pipe()
            fw = F.Forwarder(cl, pipe)
            await fw._add_route(-1001, "Src", Ent(-1002, "Dst", True))
            assert fw._wanted(-1001, Msg(1, "hi"))                      # watching by default
            assert "Paused" in await fw._cmd_fwd("unwatch 1")
            assert not fw._wanted(-1001, Msg(1, "hi"))
            assert "Watching" in await fw._cmd_fwd("watch")
            assert fw._wanted(-1001, Msg(1, "hi"))
            assert "removed" in await fw._cmd_fwd("links on") and fw.strip_links
            assert "kept" in await fw._cmd_fwd("links off") and not fw.strip_links
            with pytest.raises(ValueError):
                await fw._cmd_fwd("reset")
            assert "No Forward all" in await fw._cmd_fwd("stop")
            assert "Nothing was started" in await fw._cmd_fwd("progress")
            assert (await fw._cmd_fwd("id 7")).startswith("❌")           # unknown serial
        finally:
            await db.close()

    asyncio.run(go())
