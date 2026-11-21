"""Bulk delete: link parsing, same-chat check, 100-id chunks, missing ids, FloodWait, errors."""
import asyncio
import types

import pytest

pytest.importorskip("telethon")

from telethon import errors  # noqa: E402

from unified_core.tg import parse_message_link  # noqa: E402
from unified_services import bulk_delete as bd  # noqa: E402


def test_parse_links():
    assert parse_message_link("https://t.me/c/2940625991/2236") == (-1002940625991, 2236)
    assert parse_message_link("t.me/c/2940625991/12/2236?single") == (-1002940625991, 2236)   # topic link
    assert parse_message_link("https://t.me/SomeChannel/55") == ("somechannel", 55)
    for bad in ("hello", "https://t.me/c/123", "https://t.me/somechannel", "https://t.me/+abcdef/5"):
        with pytest.raises(ValueError):
            parse_message_link(bad)


class Ent:
    def __init__(self, id, title="Chat"):
        self.id, self.title = id, title


class Client:
    def __init__(self, existing, flood_once=False, forbid=False):
        self.existing, self.deleted_calls = set(existing), []
        self.flood_once, self.forbid = flood_once, forbid
        self.ents = {-1002940625991: Ent(2940625991), "pub": Ent(2940625991)}

    async def get_entity(self, ref):
        if ref in self.ents:
            return self.ents[ref]
        raise ValueError(ref)

    async def get_dialogs(self):
        pass

    async def get_messages(self, chat, ids=None):
        return [types.SimpleNamespace(id=i) if i in self.existing else None for i in ids]

    async def delete_messages(self, chat, ids, revoke=True):
        if self.forbid:
            raise errors.ChatAdminRequiredError(None)
        if self.flood_once:
            self.flood_once = False
            raise errors.FloodWaitError(request=None, capture=0)
        assert len(ids) <= 100
        self.deleted_calls.append(list(ids))
        return [types.SimpleNamespace(pts_count=len(ids))]


def run(c):
    return asyncio.run(c)


def test_plan_validation():
    cl = Client([])
    plan = run(bd.make_plan(cl, "https://t.me/c/2940625991/2236", "https://t.me/c/2940625991/2255"))
    assert (plan.start, plan.end, plan.total) == (2236, 2255, 20) and "2236" in plan.summary()
    cl.ents[-1009999999999] = Ent(9999999999)
    with pytest.raises(ValueError, match="different chats"):
        run(bd.make_plan(cl, "https://t.me/c/2940625991/1", "https://t.me/c/9999999999/5"))
    with pytest.raises(ValueError, match="OLDER"):
        run(bd.make_plan(cl, "https://t.me/c/2940625991/50", "https://t.me/c/2940625991/10"))
    with pytest.raises(ValueError, match="limit"):
        run(bd.make_plan(cl, "https://t.me/c/2940625991/1", "https://t.me/c/2940625991/99999999"))
    with pytest.raises(ValueError, match="not found"):
        run(bd.make_plan(cl, "https://t.me/c/1111111111/1", "https://t.me/c/1111111111/5"))


def test_delete_in_chunks_skips_missing():
    cl = Client(set(range(1, 251)) - {7, 150})
    plan = bd.BulkPlan(Ent(1), -1001, "Chat", 1, 250)
    res = run(bd.delete_range(cl, plan, pause=0))
    assert [len(c) for c in cl.deleted_calls] == [99, 99, 50]      # 100/100/50 ids asked, 2 of them missing
    assert (res.deleted, res.missing, res.failed, res.done_to) == (248, 2, 0, 250)


def test_flood_wait_is_retried(monkeypatch):
    async def fast(_s):
        pass
    monkeypatch.setattr(bd.asyncio, "sleep", fast)
    cl = Client(range(1, 11), flood_once=True)
    res = run(bd.delete_range(cl, bd.BulkPlan(Ent(1), -1001, "Chat", 1, 10), pause=0))
    assert res.deleted == 10 and res.flood_waits == 1 and not res.note


def test_no_admin_right_stops_cleanly():
    cl = Client(range(1, 301), forbid=True)
    res = run(bd.delete_range(cl, bd.BulkPlan(Ent(1), -1001, "Chat", 1, 300), pause=0))
    assert res.deleted == 0 and res.failed == 300 and "admin" in res.note


def test_stop_between_chunks_keeps_what_was_deleted():
    cl = Client(range(1, 301))
    calls = {"n": 0}

    def should_stop():          # let the first chunk run, then ask to stop
        calls["n"] += 1
        return calls["n"] > 1

    res = run(bd.delete_range(cl, bd.BulkPlan(Ent(1), -1001, "Chat", 1, 300), pause=0, should_stop=should_stop))
    assert res.deleted == 100 and res.done_to == 100 and "stopped by you" in res.note
    assert [len(c) for c in cl.deleted_calls] == [100]


def test_flood_seconds_helper_knows_every_variant():
    from unified_core.utils import flood_seconds
    assert flood_seconds(errors.FloodWaitError(request=None, capture=7)) == 7
    assert flood_seconds(ValueError("x")) is None
