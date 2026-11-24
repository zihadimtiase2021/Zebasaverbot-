"""Per-route settings (Watch, ban words, serial numbers) and running ONE route on its own."""
import asyncio

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

from tests.test_forward_all import Client, finish, texts  # noqa: E402
from tests.test_forwarder import Ent, Msg, Pipe  # noqa: E402


class Store:                                     # stands in for RuntimeConfig (Saved Messages)
    def __init__(self, saved=None):
        self.saved = saved or {}

    async def save(self):
        pass


def setup(monkeypatch, tmp_path):
    from unified_core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FWD_DELAY", 0)


def test_old_saved_routes_get_defaults_and_new_settings_are_saved(monkeypatch, tmp_path):
    from unified_core.database import db
    from unified_services import forwarder as F

    setup(monkeypatch, tmp_path)

    async def go():
        await db.connect(":memory:")
        try:
            old = {"src": -1001, "dst": -1002, "src_name": "A", "dst_name": "B", "active": False}  # saved by an old version
            store = Store({F.ROUTES_KEY: [old]})
            fw = F.Forwarder(Client({}), Pipe(), store=store)
            await fw.load()
            r = fw.routes[0]
            assert (r["active"], r["bans"], r["ban_mode"], r["serial_on"], r["serial_start"]) == (False, [], "add", True, 0)
            await fw.route_ban_add(r, "spam, @bad")
            await fw.set_ban_mode(r, "only")
            await fw.set_serial_on(r, False)
            await fw.set_serial_start(r, 42)
            saved = store.saved[F.ROUTES_KEY][0]                         # written to the Saved Messages store at once
            assert saved["bans"] == ["spam", "@bad"] and saved["ban_mode"] == "only"
            assert saved["serial_on"] is False and saved["serial_start"] == 42
            fw2 = F.Forwarder(Client({}), Pipe(), store=store)           # "restart": everything is read back
            await fw2.load()
            assert fw2.routes[0] == F.route_defaults(saved) and fw2.routes[0]["serial_start"] == 42
        finally:
            await db.close()

    asyncio.run(go())


def test_ban_words_per_route_combine_or_override(monkeypatch, tmp_path):
    from unified_core.database import db
    from unified_services import forwarder as F

    setup(monkeypatch, tmp_path)

    async def go():
        await db.connect(":memory:")
        try:
            cl = Client({})
            fw = F.Forwarder(cl, Pipe())
            await fw._add_route(-1001, "Src", Ent(-1002, "A", True))
            await fw._add_route(-1001, "Src", Ent(-1003, "B", True))
            ra, rb = fw.routes
            fw.bans = ["global"]
            await fw.route_ban_add(ra, "onlya")
            await fw.route_ban_add(rb, "onlyb")
            await fw.set_ban_mode(rb, "only")                            # B ignores the global list
            assert fw.effective_bans(ra) == ["global", "onlya"] and fw.effective_bans(rb) == ["onlyb"]
            assert await fw._copy(F.Item(-1001, [ra, rb], [Msg(5, "hello global onlya onlyb")])) == "sent"
            to_a = next(s for s in cl.sent if s[1] == -1002)[2]
            to_b = next(s for s in cl.sent if s[1] == -1003)[2]
            assert "global" not in to_a and "onlya" not in to_a and "onlyb" in to_a
            assert "global" in to_b and "onlyb" not in to_b and "onlya" in to_b
            with pytest.raises(ValueError):
                await fw.route_ban_add(ra, "ONLYA")                      # nothing new to add
            await fw.route_ban_remove(ra, 0)
            assert ra["bans"] == [] and "cleared" in await fw.route_ban_clear(rb) and rb["bans"] == []
        finally:
            await db.close()

    asyncio.run(go())


def test_serial_numbers_per_route(monkeypatch, tmp_path):
    from unified_core.database import db
    from unified_services import forwarder as F

    setup(monkeypatch, tmp_path)

    async def go():
        await db.connect(":memory:")
        try:
            cl = Client({})
            fw = F.Forwarder(cl, Pipe())
            await fw._add_route(-1001, "Src", Ent(-1002, "G", True))     # global counter
            await fw._add_route(-1001, "Src", Ent(-1003, "O", True))     # own counter from 5
            await fw._add_route(-1001, "Src", Ent(-1004, "N", True))     # serial numbers OFF
            g, o, off = fw.routes
            await fw.set_serial_start(o, 5)
            await fw.set_serial_on(off, False)
            m = Msg(1, "x")
            assert [(await fw._uid_for(g, m))[1] for _ in range(2)] == ["ZF-000001", "ZF-000002"]
            u5, s5, _ = await fw._uid_for(o, m)
            u6, s6, give_back = await fw._uid_for(o, m)
            assert (s5, s6) == ("ZF-000005", "ZF-000006")
            give_back()                                                  # that copy failed: the number comes back
            assert (await fw._uid_for(o, m))[1] == "ZF-000006"
            serial = fw.ledger.serial
            uid, shown, _ = await fw._uid_for(off, m)
            assert shown == "" and uid and fw.ledger.serial == serial    # OFF: hidden id, no number used
            o2 = F.route_defaults({"src": -1001, "dst": -1005})          # another route may repeat a shown number
            await fw.set_serial_start(o2, 5)
            u, shown, _ = await fw._uid_for(o2, m)
            assert shown == "ZF-000005" and u != shown and u.startswith("ZF-000005~")
            await fw.reset_serial(o)                                     # back to the global counter
            assert o["serial_start"] == 0 and "-1001>-1003" not in fw.ledger.route_serial
            # what is posted: with ID / without ID
            await fw._copy(F.Item(-1001, [off], [Msg(9, "plain")]))
            await fw._copy(F.Item(-1001, [g], [Msg(10, "numbered")]))
            posted = [s[2] for s in cl.sent if s[0] == "msg"]
            assert "<code>" not in posted[0] and "<code>ZF-" in posted[1]
        finally:
            await db.close()

    asyncio.run(go())


def test_one_route_runs_and_stops_without_touching_the_others(monkeypatch, tmp_path):
    from unified_core.database import db
    from unified_services import forwarder as F

    setup(monkeypatch, tmp_path)

    async def go():
        await db.connect(":memory:")
        try:
            cl = Client({-1001: [Msg(1, "one"), Msg(2, "two")], -1003: [Msg(1, "three")]})
            fw = F.Forwarder(cl, Pipe())
            await fw._add_route(-1001, "S1", Ent(-1002, "D1", True))
            await fw._add_route(-1003, "S2", Ent(-1004, "D2", True))
            r1, r2 = fw.routes
            fw.start_all([r1])                                           # ONLY route 1
            job = await finish(fw, r1)
            assert job["sent"] == 2 and fw._job_for(r2) is None
            assert fw.ledger.count("-1001>-1002") == 2 and fw.ledger.count("-1003>-1004") == 0
            assert all(s[1] == -1002 for s in cl.sent if s[0] == "msg")  # nothing went to route 2's destination
            fw.start_all([r2])                                           # route 2 on its own afterwards
            await finish(fw, r2)
            assert fw.ledger.count("-1003>-1004") == 1 and "three" in " ".join(texts(cl))
            assert fw.stop_all([r1]) == 0                                # not running: nothing to stop
            # a per-route Watch OFF stops live copies of that route only; the other route still watches
            await fw._cmd_fwd("unwatch 1")
            await fw._add_route(-1001, "S1", Ent(-1005, "D3", True))
            routes = fw._wanted(-1001, Msg(7, "live"))
            assert [r["dst"] for r in routes] == [-1005]
            fw._worker.cancel()
        finally:
            await db.close()

    asyncio.run(go())
