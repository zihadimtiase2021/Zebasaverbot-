"""Extra Telegram accounts: login key, logout (stop, key kept), /resume <key>, limits, restore, batch owners."""
import asyncio
import os
import types

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

from telethon.crypto import AuthKey  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from unified_core.database import db  # noqa: E402
from unified_services import account_bot as ab  # noqa: E402
from unified_services import accounts as acc  # noqa: E402


def make_key() -> str:
    s = StringSession()
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(os.urandom(256))
    return s.save()


class World:
    """Pretend Telegram: which key belongs to which user, and which keys are still valid."""

    def __init__(self):
        self.users = {}      # key -> (id, first_name, username)
        self.revoked = set()
        self.clients = []

    def add_user(self, uid, name, username):
        key = make_key()
        self.users[key] = (uid, name, username)
        return key

    def client(self, key=""):
        c = FakeClient(self, key)
        self.clients.append(c)
        return c


class FakeClient:
    def __init__(self, world, key):
        self.world, self.key = world, key
        self.session = StringSession(key)
        self.connected = self.logged_out = False
        self.fail_logout = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.key in self.world.users and self.key not in self.world.revoked

    async def get_me(self):
        uid, name, username = self.world.users[self.key]
        return types.SimpleNamespace(id=uid, first_name=name, last_name="", username=username,
                                     phone=f"88017{uid:08d}", premium=False)

    async def log_out(self):
        if self.fail_logout:
            raise RuntimeError("network down")
        self.logged_out = True
        self.world.revoked.add(self.key)
        self.connected = False


class FakeInstance(acc.Instance):
    async def stop(self, terminate=False):
        self.stopped = True
        if terminate:
            await self.client.log_out()
        else:
            await self.client.disconnect()


class Manager(acc.AccountManager):
    def __init__(self, primary, world, fail_build=None):
        super().__init__(primary, "Main", "main")
        self.world, self.fail_build = world, fail_build

    def new_client(self, session="", plain=False):
        return self.world.client(session)

    async def _build(self, client, me, key, phone, source):
        if self.fail_build:
            raise acc.AccountError(self.fail_build)
        return FakeInstance(me_id=me.id, name=me.first_name, username=me.username or "", phone=phone, session=key,
                            client=client, app=types.SimpleNamespace(), runtime=None, forwarder=None, source=source)


def setup():
    world = World()
    pkey = world.add_user(1, "Main", "main")
    primary_client = world.client(pkey)

    async def notify(text):
        notes.append(text)

    notes = []
    primary = types.SimpleNamespace(me_id=1, userbot=primary_client, control_bot=None,
                                    pipeline=types.SimpleNamespace(notify=notify))
    return world, pkey, Manager(primary, world), notes


def run(coro_fn):
    async def wrapper():
        await db.connect(":memory:")
        try:
            return await coro_fn()
        finally:
            await db.close()
    return asyncio.run(wrapper())


def test_login_logout_resume_cycle():
    world, _, mgr, _ = setup()
    key = world.add_user(2, "Rahim", "rahim")

    async def go():
        inst = await mgr.resume(key)                       # same path as a fresh login
        assert inst.me_id == 2 and mgr.total == 2
        assert [r["user_id"] for r in await db.accounts_all()] == [2]   # remembered for the next start
        assert mgr.resolve("2") == 2 and mgr.resolve("@rahim") == 2 and mgr.resolve("1") is None

        got, terminated, err = await mgr.logout(2)
        assert got == key and terminated is False and err is None  # the key comes back to the admin
        assert mgr.total == 1 and inst.stopped
        assert await db.accounts_all() == []                       # the server forgets it
        assert world.users[key] and key not in world.revoked       # ... but the Telegram session is alive

        again = await mgr.resume(key)                               # /resume <key>
        assert again.me_id == 2 and mgr.total == 2
    run(go)


def test_same_account_is_never_connected_twice():
    world, pkey, mgr, _ = setup()
    key = world.add_user(2, "Rahim", "rahim")

    async def go():
        await mgr.resume(key)
        for dup in (key, pkey, key + "\n"):
            before = len(world.clients)
            with pytest.raises(acc.AccountError, match="already running"):
                await mgr.resume(dup)
            assert len(world.clients) == before                    # no second connection was even opened
        with pytest.raises(acc.AccountError, match="not a valid"):
            await mgr.resume("not-a-key")
    run(go)


def test_dead_key_and_limit(monkeypatch):
    world, _, mgr, _ = setup()
    a, b = world.add_user(2, "A", "a"), world.add_user(3, "B", "b")
    monkeypatch.setattr(acc.settings, "MAX_ACCOUNTS", 2)

    async def go():
        world.revoked.add(a)
        with pytest.raises(acc.AccountError, match="no longer works"):
            await mgr.resume(a)
        world.revoked.discard(a)
        await mgr.resume(a)
        with pytest.raises(acc.AccountError, match="limit") as ei:
            await mgr.resume(b)
        assert ei.value.key == b                                    # the admin still gets the key
        assert not world.clients[-1].connected                      # ... and the client is closed again
        assert mgr.total == 2
    run(go)


def test_terminate_kills_the_key_and_reports_failures():
    world, _, mgr, _ = setup()
    a, b = world.add_user(2, "A", "a"), world.add_user(3, "B", "b")

    async def go():
        await mgr.resume(a)
        got, terminated, err = await mgr.logout(2, terminate=True)
        assert terminated and err is None and a in world.revoked
        with pytest.raises(acc.AccountError, match="no longer works"):
            await mgr.resume(a)

        await mgr.resume(b)
        next(c for c in world.clients if c.key == b and c.connected).fail_logout = True
        got, terminated, err = await mgr.logout(3, terminate=True)
        assert got == b and terminated is False and "network down" in err
        assert b not in world.revoked                               # session untouched: the key still works
        with pytest.raises(acc.AccountError, match="not logged in"):
            await mgr.logout(3)
    run(go)


def test_restore_from_database_and_env(monkeypatch):
    world, _, mgr, notes = setup()
    a, b, c = (world.add_user(2, "A", "a"), world.add_user(3, "B", "b"), world.add_user(4, "C", "c"))
    monkeypatch.setattr(acc.settings, "EXTRA_SESSIONS", f"{c}")

    async def go():
        await db.account_save(2, "A", "", a)
        await db.account_save(3, "B", "", "garbage")                # broken row: dropped, admin told
        assert await mgr.restore() == 2
        assert sorted(mgr.instances) == [2, 4]
        assert mgr.instances[4].source == "env"
        assert [r["user_id"] for r in await db.accounts_all()] == [2]
        assert any("not started" in n for n in notes)
        mgr.fail_build = None
        await mgr.stop_all()
        assert mgr.total == 1
    run(go)


def test_target_problem_is_reported_with_the_key():
    world, _, mgr, _ = setup()
    key = world.add_user(2, "A", "a")
    mgr.fail_build = "This account cannot reach the target channel"

    async def go():
        with pytest.raises(acc.AccountError) as ei:
            await mgr.resume(key)
        assert ei.value.key == key and mgr.total == 1
        assert await db.accounts_all() == []
        assert key in ab.error_text(ei.value)
    run(go)


def test_batches_belong_to_the_account_that_started_them():
    async def go():
        a = await db.batch_create(10, False, [("u1", None)], owner=2)
        b = await db.batch_create(11, False, [("u2", None)], owner=3)
        legacy = await db.batch_create(12, False, [("u3", None)])   # old row without an owner
        ids = lambda rows: sorted(r["id"] for r in rows)            # noqa: E731
        assert ids(await db.batches_unfinished(owner=2)) == [a]
        assert ids(await db.batches_unfinished(owner=3)) == [b]
        assert ids(await db.batches_unfinished(owner=1, legacy=True)) == [legacy]
        assert ids(await db.batches_unfinished()) == [a, b, legacy]
    run(go)


def test_account_cfg_shares_everything_but_the_target():
    base = types.SimpleNamespace(TARGET_CHANNEL=-100, COMMENT_CHAT=0, WM_TEXT="x")
    cfg = acc.AccountCfg(base)
    assert cfg.TARGET_CHANNEL == -100
    cfg.TARGET_CHANNEL = -200
    cfg.WM_TEXT = "shared"
    assert cfg.TARGET_CHANNEL == -200 and base.TARGET_CHANNEL == -100
    assert base.WM_TEXT == "shared"


def test_input_helpers():
    assert ab.parse_phone("+880 1712-345678") == "+8801712345678"
    assert ab.parse_phone("8801712345678") == "+8801712345678"
    assert ab.parse_phone("hello") is None
    assert ab.parse_code("1 2 3 4 5") == "12345" and ab.parse_code("12-345") == "12345"
    assert ab.parse_code("12") is None
    assert ab.mask_phone("+8801712345678") == "+88•••5678"
    assert acc.split_keys("a, b\nc;d") == ["a", "b", "c", "d"]
    assert acc.clean_key(" `abc` \n") == "abc"
