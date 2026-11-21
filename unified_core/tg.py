"""Small Telegram helpers shared by the bot and the setup wizard."""
from __future__ import annotations

import re

_TME = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(.+)", re.IGNORECASE)
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,}$")


def parse_chat_ref(text: str) -> int | str:
    """Turn what a human types (@name, t.me link, numeric id) into something get_entity accepts."""
    t = (text or "").strip().strip("\"'")
    if not t:
        raise ValueError("empty value")
    m = _TME.match(t)
    if m:
        path = m.group(1).split("?")[0].strip("/")
        parts = path.split("/")
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return int(f"-100{parts[1]}")
        if parts[0].startswith("+") or parts[0] == "joinchat":
            raise ValueError("invite links are not supported - join the chat first, then pick it from the list")
        t = parts[0]
    t = t.lstrip("@")
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if _USERNAME.match(t):
        return t
    raise ValueError("expected an @username, a t.me link or a numeric chat ID")


async def resolve_peer(client, raw: int | str):
    """Resolve a chat. Accepts marked (-100...) ids, bare channel ids and usernames."""
    candidates: list[int | str] = [raw]
    if isinstance(raw, int) and raw > 0:
        candidates.append(int(f"-100{raw}"))
    last: Exception | None = None
    for attempt in (1, 2):
        for cand in candidates:
            try:
                return await client.get_entity(cand)
            except Exception as exc:
                last = exc
        if attempt == 1:
            await client.get_dialogs()  # warm the entity cache, then retry once
    raise RuntimeError(f"Cannot resolve chat {raw}: {last}")


_MSG_LINK = re.compile(r"^(?:https?://)?(?:t|telegram)\.me/(.+)$", re.IGNORECASE)


def parse_message_link(text: str) -> tuple[int | str, int]:
    """``https://t.me/c/2940625991/2236`` or ``https://t.me/username/12`` -> (chat ref, message id).

    The chat ref is ``-1002940625991`` for private (``/c/``) links and the lower-cased username for public ones.
    Topic links (``/c/<id>/<topic>/<msg>``, ``/<name>/<topic>/<msg>``) use the last number as the message id.
    """
    t = (text or "").strip().strip("\"'<>")
    m = _MSG_LINK.match(t)
    if not m:
        raise ValueError("send a message link like https://t.me/c/1234567890/55 or https://t.me/username/55")
    parts = [x for x in m.group(1).split("?")[0].split("#")[0].split("/") if x]
    if parts and parts[0] == "c":
        if len(parts) < 3 or not parts[1].isdigit() or not all(x.isdigit() for x in parts[2:]):
            raise ValueError("that private link has no message number (expected t.me/c/<chat>/<message>)")
        chat: int | str = int(f"-100{parts[1]}")
    else:
        if len(parts) < 2 or not _USERNAME.match(parts[0]) or not all(x.isdigit() for x in parts[1:]):
            raise ValueError("that is not a message link (expected t.me/<username>/<message>)")
        chat = parts[0].lower()
    msg_id = int(parts[-1])
    if msg_id < 1:
        raise ValueError("the message number must be 1 or more")
    return chat, msg_id
