"""pyrofork login helper: session conversion, error mapping and the PyroLogin flow (pyrofork itself is faked)."""
import asyncio
import base64
import struct
import sys
import types

import pytest

from unified_core import pyro_login as pl

KEY = bytes(range(256))


def run(coro):
    return asyncio.run(coro)


def decode_telethon(text: str):
    """Reference decoder = what telethon.sessions.StringSession does."""
    assert text[0] == "1"
    raw = base64.urlsafe_b64decode(text[1:])
    ip_len = 4 if len(raw) == 1 + 4 + 2 + 256 else 16
    dc, ip, port, key = struct.unpack(f">B{ip_len}sH256s", raw)
    return dc, ".".join(map(str, ip)), port, key


def test_telethon_string_layout():
    dc, ip, port, key = decode_telethon(pl.telethon_string(4, KEY))
    assert (dc, ip, port, key) == (4, "149.154.167.91", 443, KEY)
    assert decode_telethon(pl.telethon_string(2, KEY, test_mode=True))[1] == "149.154.167.40"


def test_telethon_string_rejects_bad_input():
    with pytest.raises(ValueError):
        pl.telethon_string(2, b"short")
    with pytest.raises(ValueError):
        pl.telethon_string(9, KEY)


def _pyro_text(fmt, *vals):
    return base64.urlsafe_b64encode(struct.pack(fmt, *vals)).decode().rstrip("=")


def test_parse_pyrogram_string_both_formats():
    new = _pyro_text(">BI?256sQ?", 5, 12345, False, KEY, 777, False)
    old = _pyro_text(">B?256sQ?", 5, False, KEY, 777, False)
    assert pl.parse_pyrogram_string(new) == (5, KEY, False)
    assert pl.parse_pyrogram_string(old) == (5, KEY, False)
    assert decode_telethon(pl.pyrogram_to_telethon(new))[0] == 5
    with pytest.raises(ValueError):
        pl.parse_pyrogram_string("AAAA")


def test_translate_by_class_name():
    def make(name, **attrs):
        exc = type(name, (Exception,), {})("x")
        for k, v in attrs.items():
            setattr(exc, k, v)
        return exc

    assert isinstance(pl.translate(make("SessionPasswordNeeded")), pl.PasswordNeeded)
    assert isinstance(pl.translate(make("PhoneCodeInvalid")), pl.CodeInvalid)
    assert isinstance(pl.translate(make("PhoneCodeExpired")), pl.CodeExpired)
    assert isinstance(pl.translate(make("PasswordHashInvalid")), pl.PasswordInvalid)
    flood = pl.translate(make("FloodWait", value=42))
    assert isinstance(flood, pl.FloodWait) and flood.seconds == 42
    other = ValueError("boom")
    assert pl.translate(other) is other


class _Storage:
    async def dc_id(self): return 2
    async def auth_key(self): return KEY
    async def test_mode(self): return False


class _Sent:
    def __init__(self, kind="APP", nxt="SMS"):
        self.phone_code_hash = "H1"
        self.type = types.SimpleNamespace(name=kind)
        self.next_type = types.SimpleNamespace(name=nxt) if nxt else None


class _FakeClient:
    log: list = []

    def __init__(self, name, **kw):
        self.kw = kw
        self.storage = _Storage()
        _FakeClient.last = self

    async def connect(self): self.log.append("connect")
    async def disconnect(self): self.log.append("disconnect")
    async def send_code(self, phone): return _Sent()
    async def resend_code(self, phone, h):
        assert (phone, h) == ("+8801700000000", "H1")
        return _Sent("SMS", None)

    async def sign_in(self, phone, h, code):
        if code == "00000":
            raise type("PhoneCodeInvalid", (Exception,), {})()
        if code == "2fa":
            raise type("SessionPasswordNeeded", (Exception,), {})()
        return types.SimpleNamespace(id=99)

    async def check_password(self, pw):
        if pw != "good":
            raise type("PasswordHashInvalid", (Exception,), {})()
        return types.SimpleNamespace(id=99)


@pytest.fixture
def fake_pyrogram(monkeypatch):
    mod = types.ModuleType("pyrogram")
    mod.Client = _FakeClient
    monkeypatch.setitem(sys.modules, "pyrogram", mod)
    _FakeClient.log = []


def test_full_flow(fake_pyrogram):
    async def go():
        login = pl.PyroLogin(1, "h" * 32)
        sent = await login.send_code("+8801700000000")
        assert (sent.kind, sent.next_type) == ("SentCodeTypeApp", "SentCodeTypeSms")
        sent2 = await login.resend()
        assert sent2.kind == "SentCodeTypeSms" and sent2.next_type is None
        with pytest.raises(pl.CodeInvalid):
            await login.sign_in(code="00000")
        with pytest.raises(pl.PasswordNeeded):
            await login.sign_in(code="2fa")
        with pytest.raises(pl.PasswordInvalid):
            await login.sign_in(password="bad")
        await login.sign_in(password="good")
        text = await login.export_session()
        await login.disconnect()
        return text

    text = run(go())
    assert decode_telethon(text)[0::3] == (2, KEY)
    assert _FakeClient.log == ["connect", "disconnect"]


def test_desktop_identity_only_when_asked(fake_pyrogram):
    run(pl.PyroLogin(1, "h", desktop=True).connect())
    assert _FakeClient.last.kw["device_model"] == "Desktop" and _FakeClient.last.kw["in_memory"] is True
    run(pl.PyroLogin(1, "h").connect())
    assert "device_model" not in _FakeClient.last.kw


def test_sign_up_required_is_no_account(fake_pyrogram, monkeypatch):
    async def no_user(self, *a): return False
    monkeypatch.setattr(_FakeClient, "sign_in", no_user)

    async def go():
        login = pl.PyroLogin(1, "h")
        await login.send_code("+8801700000000")
        await login.sign_in(code="12345")

    with pytest.raises(pl.NoAccount):
        run(go())
