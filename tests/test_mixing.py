"""Mixing mode: Forward all over several routes takes turns, 5 messages per route."""
import asyncio
import re

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

from tests.test_forward_all import Client, finish  # noqa: E402
from tests.test_forwarder import Ent, Msg, Pipe  # noqa: E402


def order(cl):
    """[(dst, text)] in the order the copies were posted."""
    return [(s[1], re.sub(r"\s*ZF-\d+.*", "", s[2]).strip()) for s in cl.sent if s[0] == "msg"]


def test_mixing_round_robin_5_per_route(monkeypatch, tmp_path):
    from unified_core.config import settings
    from unified_core.database import db
    from unified_services import forwarder as F

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FWD_DELAY", 0)

    async def go():
        await db.connect(":memory:")
        try:
            hist = {-1001: [Msg(i, f"A{i}") for i in range(1, 13)],     # 12 messages
                    -1003: [Msg(i, f"B{i}") for i in range(1, 8)]}      # 7 messages (shorter route)
            cl, pipe = Client(hist), Pipe()
            fw = F.Forwarder(cl, pipe)
            await fw._add_route(-1001, "A", Ent(-1002, "DA", True))
            await fw._add_route(-1003, "B", Ent(-1004, "DB", True))
            fw._worker = asyncio.create_task(fw._run())

            # OFF (default): one route after the other
            assert fw.mixing is False
            await fw._cmd_fwd("all")
            for r in fw.routes:
                await finish(fw, r)
            seq = [t for _d, t in order(cl)]
            assert seq == [f"A{i}" for i in range(1, 13)] + [f"B{i}" for i in range(1, 8)]

            # ON: 5 + 5 + 5 ... ; route B runs dry first and drops out cleanly
            await fw._cmd_fwd("reset 1")
            await fw._cmd_fwd("reset 2")
            cl.sent.clear()
            assert "ON" in await fw._cmd_fwd("mix on") and fw.mixing
            assert "Mixing ON" in await fw._cmd_fwd("all")
            for r in fw.routes:
                await finish(fw, r)
            seq = [t for _d, t in order(cl)]
            assert seq == ([f"A{i}" for i in range(1, 6)] + [f"B{i}" for i in range(1, 6)]
                           + [f"A{i}" for i in range(6, 11)] + ["B6", "B7"]
                           + ["A11", "A12"])
            for r in fw.routes:
                job = fw._job_for(r)
                assert job["state"] == "done" and job["failed"] == 0
            assert fw._job_for(fw.routes[0])["last_id"] == 12 and fw._job_for(fw.routes[1])["last_id"] == 7
            assert fw.ledger.has_all("-1001>-1002", range(1, 13)) and fw.ledger.has_all("-1003>-1004", range(1, 8))

            # a single route is never "mixed", and OFF restores the default
            assert "OFF" in await fw._cmd_fwd("mix off") and fw.mixing is False
            fw._worker.cancel()
        finally:
            await db.close()

    asyncio.run(go())
