"""Download media from t.me message links using the userbot session."""
from __future__ import annotations

import re
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import PeerChannel

from unified_core.utils import short_error
from unified_services.models import DownloadError, MediaItem, PostData

_PRIVATE = re.compile(r"t(?:elegram)?\.me/c/(\d+)/(?:\d+/)?(\d+)", re.IGNORECASE)
_PUBLIC = re.compile(r"t(?:elegram)?\.me/([A-Za-z][\w]{3,})/(?:\d+/)?(\d+)", re.IGNORECASE)


async def fetch_telegram(url: str, workdir: Path, client: TelegramClient, max_bytes: int, data: PostData) -> None:
    m = _PRIVATE.search(url)
    if m:
        entity, msg_id = PeerChannel(int(m.group(1))), int(m.group(2))
    else:
        m = _PUBLIC.search(url)
        if not m:
            raise DownloadError("Unsupported Telegram link (need a link to a specific message)")
        entity, msg_id = m.group(1), int(m.group(2))

    try:
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg:
            raise DownloadError("Message not found (are you a member of that chat?)")
        msgs = [msg]
        if msg.grouped_id:
            near = await client.get_messages(entity, ids=list(range(max(1, msg_id - 9), msg_id + 10)))
            msgs = sorted((x for x in near if x and x.grouped_id == msg.grouped_id), key=lambda x: x.id)

        data.caption = next((x.message for x in msgs if x.message), "") or ""
        for x in msgs:
            if not x.media:
                continue
            if x.photo:
                kind = "photo"
            elif x.video or x.gif or x.video_note:
                kind = "video"
            else:
                continue
            size = getattr(x.file, "size", 0) or 0
            if size > max_bytes:
                continue
            path = await client.download_media(x, file=str(workdir / f"tg_{x.id}"))
            if path:
                data.media.append(MediaItem(Path(path), kind))
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"Telegram fetch failed: {short_error(exc)}") from exc
