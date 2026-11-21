"""Redeploy survival: encrypted account sessions + resumable Forward all jobs in Saved Messages."""
import asyncio
import types

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

from unified_services import state_backup as sb  # noqa: E402


def run(c):
    return asyncio.run(c)


def test_seal_roundtrip_and_tamper():
    key = "1BVts" + "x" * 300
    t = sb.seal("secret", key)
    assert key not in t and sb.unseal("secret", t) == key
    assert sb.unseal("other", t) is None                       # wrong secret
    assert sb.unseal("secret", t[:-4] + "AAAA") is None        # damaged
    assert sb.seal("secret", key) != t                         # fresh salt / nonce every time


class SavedMessages:
    """Just enough of a Telethon client for 'Saved Messages'."""

    def __init__(self):
        self.msgs, self.n = {}, 0

    async def send_message(self, chat, text, **kw):
        self.n += 1
        self.msgs[self.n] = text
        return types.SimpleNamespace(id=self.n)

    async def edit_message(self, chat, mid, text, **kw):
        self.msgs[mid] = text

    async def iter_messages(self, chat, search=None, limit=None):
        for mid, text in sorted(self.msgs.items(), reverse=True):
            if search in text:
                yield types.SimpleNamespace(id=mid, raw_text=text, out=True)


def test_vault_survives_a_redeploy():
    cl = SavedMessages()
    v1 = sb.StateVault(cl, "s")
    run(v1.set_account(11, "@a", "SESSION-A"))
    run(v1.set_account(12, "@b", "SESSION-B"))
    run(v1.drop_account(12))
    assert len(cl.msgs) == 1 and "SESSION-A" not in cl.msgs[1]       # one message, edited; never in clear text

    v2 = sb.StateVault(cl, "s")                                       # "redeploy": a new process reads it back
    assert run(v2.load()) == 1 and v2.sessions() == [(11, "SESSION-A", "@a")]
    v3 = sb.StateVault(cl, "another secret")                          # secret changed: nothing lost, nothing leaked
    run(v3.load())
    assert v3.sessions() == [] and len(v3.accounts) == 1


class Store:
    def __init__(self):
        self.saved = {}

    async def save(self):
        pass


def test_forward_all_resumes_after_redeploy(monkeypatch, tmp_path):
    from tests.test_forward_all import Client, finish
    from tests.test_forwarder import Ent, Msg, Pipe
    from unified_core.config import settings
    from unified_core.database import db
    from unified_services import forwarder as F

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FWD_DELAY", 0)

    async def go():
        await db.connect(":memory:")
        try:
            hist = {-1001: [Msg(i, f"m{i}") for i in range(1, 6)]}
            store = Store()
            fw1 = F.Forwarder(Client(hist), Pipe(), store=store)
            await fw1._add_route(-1001, "Src", Ent(-1002, "Dst", True))
            fw1.start_all(fw1.routes)                      # started ... and the process dies before it finishes
            await asyncio.sleep(0)
            fw1.jobs[next(iter(fw1.jobs))]["task"].cancel()
            fw1._shutting_down = True
            await asyncio.sleep(0.05)
            assert store.saved[F.JOBS_KEY] == ["-1001>-1002"]  # still listed: it was interrupted, not finished

            cl2, pipe2 = Client(hist), Pipe()
            fw2 = F.Forwarder(cl2, pipe2, store=store)         # new process, same Saved Messages
            await fw2.load()
            fw2._worker = asyncio.create_task(fw2._run())
            assert fw2.resume_keys == ["-1001>-1002"] and await fw2.resume_jobs() == 1
            job = await finish(fw2, fw2.routes[0])
            assert job["state"] == "done" and len([s for s in cl2.sent if s[0] == "msg"]) == 5
            assert any("resumed" in n.lower() for n in pipe2.notes)
            assert fw2.resume_keys == [] and store.saved[F.JOBS_KEY] == []   # finished: never restarts again
            assert await fw2.resume_jobs() == 0
            fw2._worker.cancel()
        finally:
            await db.close()

    asyncio.run(go())
