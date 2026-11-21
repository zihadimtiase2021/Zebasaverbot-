"""Telegram-side helpers for the watermark: media sent straight to the bot, font upload, preview."""
from __future__ import annotations

import io
import logging
import os
import uuid
from pathlib import Path

from unified_core.cleanup import safe_rmtree
from unified_core.config import settings
from unified_core.utils import short_error
from unified_services import watermark

log = logging.getLogger("zeba.wm")


def media_kind(msg) -> str | None:
    """'photo' | 'video' | 'gif' for something we can watermark, else None."""
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "gif", None):
        return "gif"
    if getattr(msg, "video", None) or getattr(msg, "video_note", None):
        return "video"
    doc = getattr(msg, "document", None)
    mime = (getattr(doc, "mime_type", "") or "") if doc else ""
    if mime.startswith("image/") and mime != "image/gif" and not getattr(msg, "sticker", None):
        return "photo"
    if mime.startswith("video/") or mime == "image/gif":
        return "video"
    return None


async def process_media(client, msgs, dest, mark: str = "") -> str:
    """Download one message or a whole album, watermark every photo/video, send them to `dest` (as an album when
    there are several). Items that cannot be watermarked are sent unchanged. Returns '' on success, else an error."""
    msgs = [msgs] if not isinstance(msgs, (list, tuple)) else sorted(msgs, key=lambda m: m.id)
    kinds = [media_kind(m) for m in msgs]
    if not any(kinds):
        return "only photos and videos can be watermarked"
    if not watermark.Style.from_cfg(settings).text.strip():
        return "no watermark text set - use the Watermark menu"
    workdir = settings.tmp_dir / f"wm_{uuid.uuid4().hex[:8]}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        files, done = [], 0
        for i, (m, kind) in enumerate(zip(msgs, kinds)):
            if getattr(m, "media", None) is None:
                continue
            d = workdir / str(i)
            d.mkdir()
            path = await client.download_media(m, file=str(d) + os.sep)
            if not path:
                return "download failed"
            if kind:
                out = await watermark.apply(path, settings, kind)
                done += Path(out) != Path(path)
                path = out
            files.append(path)
        if not done:
            return "watermark could not be applied (see logs)"
        text = next((m.raw_text for m in msgs if m.raw_text), "")
        cap = mark + (text or "")
        for k in range(0, len(files), 10):  # an album holds at most 10 items
            part = files[k:k + 10]
            await client.send_file(dest, part if len(part) > 1 else part[0], caption=(cap or None) if k == 0 else None,
                                   parse_mode=None, supports_streaming=True, reply_to=msgs[0].id)
        return ""
    except Exception as exc:
        log.warning("watermark job failed: %s", short_error(exc))
        return short_error(exc)
    finally:
        safe_rmtree(workdir)


async def save_font(client, msg) -> str:
    """Store a .ttf/.otf sent with the caption .wmfont in DATA_DIR/fonts. Returns the font name (raises ValueError)."""
    name = (getattr(msg.file, "name", "") or "").strip()
    if Path(name).suffix.lower() not in (".ttf", ".otf", ".ttc"):
        raise ValueError("send a .ttf / .otf font file with the caption .wmfont")
    if (msg.file.size or 0) > 25 * 1024 * 1024:
        raise ValueError("font file is too large (max 25 MB)")
    d = Path(settings.DATA_DIR) / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    path = d / Path(name).name
    await client.download_media(msg, file=str(path))
    watermark.font_index(settings.DATA_DIR, refresh=True)
    return path.stem


async def send_preview(client, dest) -> None:
    data = watermark.sample_preview(settings)
    bio = io.BytesIO(data)
    bio.name = "preview.jpg"
    await client.send_file(dest, bio, caption="\U0001f4a7 Watermark preview")
