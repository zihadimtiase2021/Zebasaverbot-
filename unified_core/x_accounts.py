"""X / Twitter cookie handling for twscrape and yt-dlp (no Telegram dependencies)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

X_DOMAINS = ("x.com", "twitter.com")


def cookies_from_netscape(path: Path) -> str:
    """Build a 'name=value; name=value' header from a Netscape cookies.txt (x.com / twitter.com only)."""
    pairs: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and any(d in parts[0] for d in X_DOMAINS):
            pairs[parts[5]] = parts[6]
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def cookie_header(auth_token: str, ct0: str) -> str:
    return f"auth_token={auth_token.strip()}; ct0={ct0.strip()}"


def has_x_session(cookies: str) -> bool:
    return "auth_token=" in cookies and "ct0=" in cookies


def netscape_for_x(auth_token: str, ct0: str) -> str:
    """A minimal cookies.txt that lets yt-dlp use the same X login."""
    far = 2147483647
    rows = ["# Netscape HTTP Cookie File"]
    for name, val in (("auth_token", auth_token), ("ct0", ct0)):
        rows.append("\t".join((".x.com", "TRUE", "/", "TRUE", str(far), name, val.strip())))
    return "\n".join(rows) + "\n"


def _api(db_path: str | Path):
    from twscrape import API  # lazy: heavy import

    return API(str(db_path))


async def list_accounts(db_path: str | Path) -> list[tuple[str, bool]]:
    if not Path(db_path).is_file():
        return []
    api = _api(db_path)
    return [(a.username, bool(a.active)) for a in await api.pool.get_all()]


async def add_account(db_path: str | Path, username: str, cookies: str) -> list[tuple[str, bool]]:
    """Register (or replace) a cookie-based X account and return all accounts."""
    if not has_x_session(cookies):
        raise ValueError("cookies must include auth_token and ct0")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    api = _api(db_path)
    try:  # replace an old entry with the same name instead of silently keeping stale cookies
        await api.pool.delete_accounts(username)
    except Exception:
        pass
    await api.pool.add_account(username, "-", "-", "-", cookies=cookies)
    return [(a.username, bool(a.active)) for a in await api.pool.get_all()]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _info(acc) -> dict:
    """Plain-dict view of a twscrape account (tolerant to attribute differences between versions)."""
    now = datetime.now(timezone.utc)
    locks = getattr(acc, "locks", None) or {}
    until = None
    for v in locks.values():
        try:
            v = _aware(v)
        except Exception:
            continue
        if v > now and (until is None or v > until):
            until = v
    return {
        "username": acc.username,
        "active": bool(getattr(acc, "active", False)),
        "locked_until": until,
        "cookies": dict(getattr(acc, "cookies", None) or {}),
    }


async def account_status(db_path: str | Path) -> list[dict]:
    """[{username, active, locked_until (aware datetime | None), cookies}] for every stored X account."""
    if not Path(db_path).is_file():
        return []
    api = _api(db_path)
    return [_info(a) for a in await api.pool.get_all()]


async def remove_account(db_path: str | Path, username: str) -> bool:
    if not Path(db_path).is_file():
        return False
    api = _api(db_path)
    before = {a.username for a in await api.pool.get_all()}
    if username not in before:
        return False
    await api.pool.delete_accounts(username)
    return True


async def unlock_all(db_path: str | Path) -> None:
    """Clear rate-limit locks on every account (use when you know the limits have reset)."""
    if Path(db_path).is_file():
        await _api(db_path).pool.reset_locks()


def netscape_from_cookie_dict(cookies: dict[str, str]) -> str:
    """cookies.txt content (x.com) built from a twscrape account's cookie dict."""
    far = 2147483647
    rows = ["# Netscape HTTP Cookie File"]
    for name, val in cookies.items():
        rows.append("\t".join((".x.com", "TRUE", "/", "TRUE", str(far), str(name), str(val).strip())))
    return "\n".join(rows) + "\n"
