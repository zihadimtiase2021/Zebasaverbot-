"""Telegram login through pyrofork (a Pyrogram fork) instead of Telethon.

Only the *login* (phone -> code -> optional 2FA password) is done by pyrofork. The rest of the bot still runs on
Telethon, so a finished login is converted into a Telethon ``StringSession`` text (same DC + same auth key, nothing is
re-logged in). That text works everywhere the bot already expects a session: SESSION_STRING, /resume keys,
``accounts.py`` and so on.

Typical use::

    login = PyroLogin(api_id, api_hash)
    sent = await login.send_code("+8801XXXXXXXXX")      # sent.kind / sent.next_type tell where the code went
    try:
        await login.sign_in(code="12345")
    except PasswordNeeded:
        await login.sign_in(password="my 2fa password")
    session_text = await login.export_session()          # Telethon StringSession text
    await login.disconnect()

pyrofork is imported lazily, so importing this module never needs it (tests, other code paths).
"""
from __future__ import annotations

import base64
import ipaddress
import struct
from dataclasses import dataclass

# --------------------------------------------------------------------------- errors (library independent)


class LoginError(Exception):
    """Base class: callers never have to import pyrofork exceptions."""


class FloodWait(LoginError):
    def __init__(self, seconds: int = 0) -> None:
        super().__init__(f"wait {seconds}s")
        self.seconds = int(seconds or 0)


class PhoneInvalid(LoginError): ...
class PhoneBanned(LoginError): ...
class PhoneFlood(LoginError): ...        # this number asked for too many codes
class ApiInvalid(LoginError): ...        # API_ID / API_HASH refused
class CodeInvalid(LoginError): ...
class CodeExpired(LoginError): ...
class CodeUnavailable(LoginError): ...   # Telegram has no other way to deliver the code
class PasswordNeeded(LoginError): ...    # the account has two-step verification
class PasswordInvalid(LoginError): ...
class NoAccount(LoginError): ...         # the number has no Telegram account (sign-up is not supported)


_BY_NAME: dict[str, type[LoginError]] = {
    "SessionPasswordNeeded": PasswordNeeded,
    "PasswordHashInvalid": PasswordInvalid,
    "PhoneCodeInvalid": CodeInvalid,
    "PhoneCodeEmpty": CodeInvalid,
    "PhoneCodeExpired": CodeExpired,
    "PhoneNumberInvalid": PhoneInvalid,
    "PhoneNumberBanned": PhoneBanned,
    "PhoneNumberFlood": PhoneFlood,
    "ApiIdInvalid": ApiInvalid,
    "ApiIdPublishedFlood": ApiInvalid,
    "PhoneNumberUnoccupied": NoAccount,
    "SendCodeUnavailable": CodeUnavailable,
}


def translate(exc: BaseException) -> BaseException:
    """pyrofork exception -> one of the classes above (matched by class name, so it works across versions).
    Anything unknown is returned unchanged."""
    if isinstance(exc, LoginError):
        return exc
    for cls in type(exc).__mro__:
        name = cls.__name__
        if name == "FloodWait":
            return FloodWait(getattr(exc, "value", None) or getattr(exc, "x", 0) or 0)
        mapped = _BY_NAME.get(name)
        if mapped is not None:
            return mapped(str(exc))
    return exc


# --------------------------------------------------------------------------- "code sent" info


@dataclass
class Sent:
    phone_code_hash: str
    kind: str                 # Telethon-style name, e.g. "SentCodeTypeApp" / "SentCodeTypeSms"
    next_type: str | None     # same naming, None = no other way left


_KIND = {
    "APP": "SentCodeTypeApp",
    "SMS": "SentCodeTypeSms",
    "CALL": "SentCodeTypeCall",
    "FLASH_CALL": "SentCodeTypeFlashCall",
    "MISSED_CALL": "SentCodeTypeMissedCall",
    "FRAGMENT_SMS": "SentCodeTypeFragmentSms",
    "FIREBASE_SMS": "SentCodeTypeFirebaseSms",
    "EMAIL_CODE": "SentCodeTypeEmailCode",
}


def _kind(value) -> str | None:
    if value is None:
        return None
    name = str(getattr(value, "name", value)).split(".")[-1].upper()
    return _KIND.get(name, "SentCodeType" + name.title().replace("_", ""))


def _sent(raw) -> Sent:
    return Sent(phone_code_hash=raw.phone_code_hash, kind=_kind(getattr(raw, "type", None)) or "SentCodeType",
                next_type=_kind(getattr(raw, "next_type", None)))


def describe(sent: Sent) -> str:
    """Plain-text 'where did the code go' for the terminal."""
    return {
        "SentCodeTypeApp": "to the Telegram app of that account (chat named 'Telegram'), not by SMS",
        "SentCodeTypeSms": "by SMS",
        "SentCodeTypeCall": "by a phone call that reads the code out",
        "SentCodeTypeFlashCall": "as a missed call (the code is the last digits of the caller number)",
        "SentCodeTypeMissedCall": "as a missed call (the code is the last digits of the caller number)",
        "SentCodeTypeFragmentSms": "as a Fragment message",
        "SentCodeTypeFirebaseSms": "by SMS (Google verification)",
        "SentCodeTypeEmailCode": "to the e-mail address saved on that account",
    }.get(sent.kind, "via Telegram")


# --------------------------------------------------------------------------- session conversion

# Telegram data centers (production / test). The address only matters for the very first connect: Telethon asks
# Telegram for the real list afterwards.
_DC_IPV4 = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
_DC_IPV4_TEST = {1: "149.154.175.10", 2: "149.154.167.40", 3: "149.154.175.117"}

_PYRO_FORMAT = ">BI?256sQ?"      # dc_id, api_id, test_mode, auth_key, user_id, is_bot   (pyrofork export_session_string)
_PYRO_FORMAT_OLD = ">B?256sQ?"   # same without api_id (older Pyrogram)


def telethon_string(dc_id: int, auth_key: bytes, test_mode: bool = False) -> str:
    """Build a Telethon StringSession text ('1' + urlsafe base64 of  dc_id | ip | port | auth_key)."""
    if len(auth_key) != 256:
        raise ValueError("auth key must be 256 bytes")
    ip = (_DC_IPV4_TEST if test_mode else _DC_IPV4).get(int(dc_id)) or _DC_IPV4.get(int(dc_id))
    if ip is None:
        raise ValueError(f"unknown data center {dc_id}")
    packed = ipaddress.ip_address(ip).packed
    raw = struct.pack(f">B{len(packed)}sH256s", int(dc_id), packed, 443, bytes(auth_key))
    return "1" + base64.urlsafe_b64encode(raw).decode("ascii")


def parse_pyrogram_string(text: str) -> tuple[int, bytes, bool]:
    """pyrofork/Pyrogram session text -> (dc_id, auth_key, test_mode)."""
    text = (text or "").strip()
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) == struct.calcsize(_PYRO_FORMAT):
        dc_id, _api_id, test_mode, auth_key, _uid, _bot = struct.unpack(_PYRO_FORMAT, raw)
    elif len(raw) == struct.calcsize(_PYRO_FORMAT_OLD):
        dc_id, test_mode, auth_key, _uid, _bot = struct.unpack(_PYRO_FORMAT_OLD, raw)
    else:
        raise ValueError("not a pyrofork session string")
    return dc_id, auth_key, bool(test_mode)


def pyrogram_to_telethon(text: str) -> str:
    dc_id, auth_key, test_mode = parse_pyrogram_string(text)
    return telethon_string(dc_id, auth_key, test_mode)


# --------------------------------------------------------------------------- the login object


class PyroLogin:
    """One login attempt (in memory, nothing is written to disk).

    desktop=True makes the session look like Telegram Desktop (same identity as the running bot); the default plain
    identity is what Telegram delivers login codes to most reliably, so the chat/web account flows use that.
    """

    def __init__(self, api_id: int, api_hash: str, *, desktop: bool = False) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.phone = ""
        self.code_hash = ""
        self.user = None
        self._desktop = desktop
        self._app = None

    # ------------------------------------------------------------------ connection
    def _make(self):
        from pyrogram import Client  # pyrofork installs the "pyrogram" package

        kw: dict = dict(api_id=self.api_id, api_hash=self.api_hash, in_memory=True, no_updates=True)
        if self._desktop:
            kw.update(device_model="Desktop", system_version="Windows 10.0", app_version="4.14.8")
        return Client(f"zeba_login_{id(self):x}", **kw)

    async def connect(self) -> None:
        if self._app is not None:
            return
        app = self._make()
        try:
            await app.connect()
        except Exception as exc:
            raise translate(exc) from exc
        self._app = app

    async def disconnect(self) -> None:
        app, self._app = self._app, None
        if app is not None:
            try:
                await app.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------ steps
    async def send_code(self, phone: str) -> Sent:
        await self.connect()
        try:
            raw = await self._app.send_code(phone)
        except Exception as exc:
            raise translate(exc) from exc
        self.phone, self.code_hash = phone, raw.phone_code_hash
        return _sent(raw)

    async def resend(self) -> Sent:
        """Ask Telegram to deliver the code another way (SMS, call, ...)."""
        if self._app is None or not self.code_hash:
            raise CodeExpired("no login in progress")
        try:
            raw = await self._app.resend_code(self.phone, self.code_hash)
        except Exception as exc:
            raise translate(exc) from exc
        self.code_hash = raw.phone_code_hash or self.code_hash
        return _sent(raw)

    async def sign_in(self, code: str | None = None, password: str | None = None):
        """Give the code first; if PasswordNeeded is raised, call again with password=... (code stays valid)."""
        if self._app is None:
            raise CodeExpired("no login in progress")
        try:
            if password is not None and not code:
                user = await self._app.check_password(password)
            else:
                user = await self._app.sign_in(self.phone, self.code_hash, code or "")
        except Exception as exc:
            raise translate(exc) from exc
        if not isinstance(getattr(user, "id", None), int):   # False / TermsOfService = the number has no account
            raise NoAccount("no Telegram account on this number")
        self.user = user
        return user

    async def export_session(self) -> str:
        """The finished login as Telethon StringSession text."""
        if self._app is None:
            raise CodeExpired("no login in progress")
        storage = self._app.storage
        try:
            dc_id, auth_key, test_mode = await storage.dc_id(), await storage.auth_key(), await storage.test_mode()
            return telethon_string(dc_id, auth_key, bool(test_mode))
        except Exception:
            return pyrogram_to_telethon(await self._app.export_session_string())


# --------------------------------------------------------------------------- check with Telethon


async def verify_telethon_string(text: str, api_id: int, api_hash: str, **client_kwargs) -> bool:
    """True when Telethon can use the converted text (connects once and asks if it is authorized)."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(text), int(api_id), api_hash, **client_kwargs)
    try:
        await client.connect()
        return bool(await client.is_user_authorized())
    except Exception:
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
