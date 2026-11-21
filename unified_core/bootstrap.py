"""Apply settings that arrive as environment variables on hosts without a terminal (Render, Railway, ...)."""
from __future__ import annotations

import base64
import binascii
import logging

from unified_core import x_accounts
from unified_core.config import settings

log = logging.getLogger("zeba.bootstrap")


def apply_cookies_from_env() -> None:
    """COOKIES_B64 (base64 of cookies.txt) -> <data>/cookies.txt, so yt-dlp can use your browser login."""
    raw = (settings.COOKIES_B64 or "").strip()
    if not raw:
        return
    try:
        data = base64.b64decode(raw, validate=False)
        text = data.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        log.error("COOKIES_B64 is not valid base64 of a text file - ignored")
        return
    target = settings.DATA_DIR / "cookies.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != text:
        target.write_text(text, encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
        log.info("cookies.txt written from COOKIES_B64")


async def import_x_accounts_from_env() -> int:
    """X_ACCOUNTS="user:auth_token:ct0,user2:auth_token:ct0" -> twscrape pool (safe to run on every start)."""
    done = 0
    for entry in (settings.X_ACCOUNTS or "").replace("\n", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3 or not all(parts):
            log.error("X_ACCOUNTS entry ignored (expected user:auth_token:ct0)")
            continue
        user, auth, ct0 = (p.strip() for p in parts)
        try:
            await x_accounts.add_account(settings.TWSCRAPE_DB, user.lstrip("@"), x_accounts.cookie_header(auth, ct0))
            done += 1
        except Exception as exc:
            log.error("could not import X account @%s: %s", user, exc.__class__.__name__)
    if done:
        log.info("%d X account(s) imported from X_ACCOUNTS", done)
    return done
