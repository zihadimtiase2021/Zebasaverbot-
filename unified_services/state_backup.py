"""Account sessions that survive a redeploy: an encrypted JSON message in the PRIMARY account's Saved Messages.

Problem: on a host without a disk (Render free) the SQLite database is empty after every redeploy, so the extra
accounts (``/login``) were gone and had to be brought back by hand with ``/resume <key>``.

Fix: every extra account's session key is also kept in ONE small message in the Saved Messages of the primary
account (tag ``#zeba_state``).  It is updated automatically when an account is added / removed, and read at
start-up, before the accounts are restored.  Session keys are full logins, so they are never stored in clear text:
each one is encrypted + authenticated (see :func:`seal`) with a key derived from ``STATE_KEY`` (or, when that is not
set, from ``API_HASH`` + ``CONTROL_BOT_TOKEN``, which are in the environment anyway and survive redeploys).

What can NOT be stored here: the *primary* session itself.  Saved Messages can only be read with a working login,
so the primary account must come from ``SESSION_STRING`` (or a persistent disk) - see README.

Layout of the message::

    #zeba_state
    {"v": 1, "t": 1760000000, "accounts": {"123456": {"n": "@name", "s": "v1.<sealed session key>"}}}
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time

from unified_core.utils import NOTIFY_MARK

log = logging.getLogger("zeba.state")

TAG = "#zeba_state"
VERSION = 1
_ITER = 120_000


# ------------------------------------------------------------------ encryption (standard library only)
def _keys(secret: str, salt: bytes) -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, _ITER, 64)
    return material[:32], material[32:]


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 in counter mode: a keystream of n bytes."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def seal(secret: str, text: str) -> str:
    """Encrypt-then-MAC: ``v1.`` + base64url(salt | nonce | ciphertext | tag)."""
    salt, nonce = os.urandom(16), os.urandom(16)
    enc, mac = _keys(secret, salt)
    data = text.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _stream(enc, nonce, len(data))))
    tag = hmac.new(mac, salt + nonce + ct, hashlib.sha256).digest()
    return "v1." + base64.urlsafe_b64encode(salt + nonce + ct + tag).decode("ascii")


def unseal(secret: str, token: str) -> str | None:
    """The original text, or None when the token is damaged or was sealed with another secret."""
    try:
        if not token.startswith("v1."):
            return None
        raw = base64.urlsafe_b64decode(token[3:].encode("ascii"))
        if len(raw) < 16 + 16 + 32:
            return None
        salt, nonce, ct, tag = raw[:16], raw[16:32], raw[32:-32], raw[-32:]
        enc, mac = _keys(secret, salt)
        if not hmac.compare_digest(tag, hmac.new(mac, salt + nonce + ct, hashlib.sha256).digest()):
            return None
        return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct)))).decode("utf-8")
    except Exception:
        return None


def default_secret(settings) -> str:
    """STATE_KEY, else something that is in the environment on every start."""
    explicit = (getattr(settings, "STATE_KEY", "") or "").strip()
    if explicit:
        return explicit
    return f"zeba-state|{getattr(settings, 'API_HASH', '')}|{getattr(settings, 'CONTROL_BOT_TOKEN', '')}"


# ------------------------------------------------------------------ the vault
class StateVault:
    def __init__(self, userbot, secret: str) -> None:
        self.userbot = userbot
        self.secret = secret
        self.accounts: dict[str, dict] = {}     # str(user id) -> {"n": label, "s": sealed session key}
        self.msg_id: int | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- read
    async def load(self) -> int:
        """Read the saved message. Returns the number of accounts found in it."""
        try:
            async for m in self.userbot.iter_messages("me", search=TAG, limit=10):
                text = getattr(m, "raw_text", None) or ""
                if not (getattr(m, "out", True) and TAG in text):
                    continue
                a, b = text.find("{"), text.rfind("}")
                if a < 0 or b <= a:
                    continue
                try:
                    data = json.loads(text[a:b + 1])
                except ValueError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                    self.msg_id = m.id
                    self.accounts = {str(k): v for k, v in data["accounts"].items()
                                     if isinstance(v, dict) and isinstance(v.get("s"), str)}
                    return len(self.accounts)
        except Exception as exc:
            log.warning("could not read the saved account state: %s", exc)
        return 0

    def sessions(self) -> list[tuple[int, str, str]]:
        """[(user id, session key, label)] of every account that could be decrypted."""
        out = []
        for uid, row in self.accounts.items():
            key = unseal(self.secret, row["s"])
            if key is None:
                log.warning("saved session of account %s cannot be decrypted (STATE_KEY / API_HASH changed?) - kept", uid)
                continue
            if uid.lstrip("-").isdigit():
                out.append((int(uid), key, str(row.get("n", ""))))
        return out

    # ---------------------------------------------------------------- write
    async def set_account(self, user_id: int, label: str, session: str) -> None:
        row = self.accounts.get(str(user_id))
        if row is not None and unseal(self.secret, row["s"]) == session and row.get("n") == label:
            return                                   # nothing changed: no message edit
        self.accounts[str(user_id)] = {"n": label[:60], "s": seal(self.secret, session)}
        await self.save()

    async def drop_account(self, user_id: int) -> None:
        if self.accounts.pop(str(user_id), None) is not None:
            await self.save()

    def _text(self) -> str:
        body = json.dumps({"v": VERSION, "t": int(time.time()), "accounts": self.accounts},
                          separators=(",", ":"), sort_keys=True)
        return (f"{NOTIFY_MARK}🔐 Zeba account state (encrypted, please do not delete)\n{TAG}\n{body}")

    async def save(self) -> bool:
        """Write the message (edit the old one, or send a new one). FloodWait is waited out and retried."""
        from telethon import errors

        async with self._lock:
            text = self._text()
            if len(text) > 4000:
                log.warning("account state too big for one Saved Messages message (%d chars) - lower MAX_ACCOUNTS", len(text))
                return False
            for attempt in range(3):
                try:
                    if self.msg_id:
                        try:
                            await self.userbot.edit_message("me", self.msg_id, text, parse_mode=None)
                            return True
                        except Exception as exc:
                            if "not modified" in str(exc).lower():
                                return True
                            if isinstance(exc, errors.FloodWaitError):
                                raise
                            log.info("state message could not be edited (%s) - sending a new one", exc)
                            self.msg_id = None
                    sent = await self.userbot.send_message("me", text, parse_mode=None)
                    self.msg_id = sent.id
                    return True
                except errors.FloodWaitError as exc:
                    wait = int(getattr(exc, "seconds", 0) or 1)
                    if wait > 300 or attempt == 2:
                        log.warning("could not save the account state: FloodWait %ss", wait)
                        return False
                    await asyncio.sleep(wait + 1)
                except Exception as exc:
                    log.warning("could not save the account state to Saved Messages: %s", exc)
                    return False
            return False
