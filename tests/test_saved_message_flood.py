"""Saved Messages writers: a FloodWait must be waited out and the SAME message edited again (no duplicates), and
settings that cannot fit into one message are refused instead of being lost silently."""
import asyncio
import types

import pytest

pytest.importorskip("telethon")

from telethon import errors  # noqa: E402

from unified_services.fwd_ledger import Ledger  # noqa: E402
from unified_services.runtime_config import MAX_CHARS, RuntimeConfig  # noqa: E402


class Client:
    def __init__(self, flood_times=1):
        self.flood_times, self.edits, self.sent = flood_times, [], []

    async def edit_message(self, chat, mid, text, **kw):
        if self.flood_times:
            self.flood_times -= 1
            raise errors.FloodWaitError(request=None, capture=1)
        self.edits.append((mid, text))

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)
        return types.SimpleNamespace(id=100 + len(self.sent))


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def fast(_s):
        pass
    monkeypatch.setattr(asyncio, "sleep", fast)


def test_runtime_settings_flood_edits_same_message():
    async def go():
        cl = Client()
        rc = RuntimeConfig(cl, cfg=types.SimpleNamespace())
        rc.msg_id, rc.saved = 55, {"A": 1}
        await rc.save()
        assert cl.sent == [] and [m for m, _ in cl.edits] == [55] and rc.msg_id == 55
    asyncio.run(go())


def test_runtime_settings_too_big_is_refused():
    async def go():
        cl = Client(flood_times=0)
        rc = RuntimeConfig(cl, cfg=types.SimpleNamespace())
        rc.saved = {"BIG": "x" * (MAX_CHARS + 10)}
        with pytest.raises(ValueError):
            await rc.save()
        assert cl.sent == [] and cl.edits == []
    asyncio.run(go())


def test_ledger_flood_edits_same_message():
    async def go():
        cl = Client()
        led = Ledger(cl, None)
        led.msg_id = 9
        led.add("1>2", 1, 5)
        await led.flush()
        assert cl.sent == [] and [m for m, _ in cl.edits] == [9]
    asyncio.run(go())
