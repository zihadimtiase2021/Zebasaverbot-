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


def parse_chat_topic(text: str) -> tuple[int | str, int]:
    """Chat reference with an optional forum topic -> (chat ref, topic id); topic 0 = the whole chat.

    ``3617219724/119`` · ``-1003617219724/119`` · ``@group/119`` · ``https://t.me/c/3617219724/119`` (topic link)
    ``3617219724`` / ``@group`` (no topic) -> topic 0.  Topic 1 is the "General" topic.
    """
    t = (text or "").strip().strip("\"'")
    if not t:
        raise ValueError("empty value")
    m = _TME.match(t)
    if m:
        parts = [x for x in m.group(1).split("?")[0].split("#")[0].split("/") if x]
        if parts and parts[0] == "c" and len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            return int(f"-100{parts[1]}"), int(parts[2])
        if len(parts) >= 2 and _USERNAME.match(parts[0]) and parts[1].isdigit():
            return parts[0], int(parts[1])
        return parse_chat_ref(t), 0
    if "/" in t:
        ref, _, top = t.rpartition("/")
        if not top.strip().isdigit() or int(top) < 1:
            raise ValueError("the topic must be a number: chat/topic, e.g. -1003617219724/119")
        return parse_chat_ref(ref), int(top)
    return parse_chat_ref(t), 0


def forum_topic_of(msg) -> int:
    """Topic id of a message sent inside a forum topic, 0 when it carries no topic (plain chat, or the General topic)."""
    rt = getattr(msg, "reply_to", None)
    if rt is not None and getattr(rt, "forum_topic", False):
        return int(getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None) or 0)
    return 0


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
