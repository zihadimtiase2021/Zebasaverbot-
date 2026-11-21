"""Caption builder (HTML), respecting Telegram's caption length limit."""
from __future__ import annotations

import html

from unified_core.config import settings
from unified_core.utils import clip_escaped


def build_caption(
    *, translated: str | None, original: str, source_url: str,
    comment_count: int, tag: str, limit: int,
) -> str:
    footer: list[str] = []
    if settings.INCLUDE_COMMENT_COUNT and comment_count:
        footer.append(f"💬 <b>Comments:</b> {comment_count}")
    if settings.INCLUDE_SOURCE_LINK:
        footer.append(f'🔗 <a href="{html.escape(source_url, quote=True)}">Source</a>')
    footer.append(f"🆔 <code>{tag}</code>")
    footer_text = "\n".join(footer)

    blocks: list[tuple[str, str]] = []
    if translated:
        blocks.append(("✨ <b>Bangla Translation:</b>\n", translated))
    if original and (not translated or settings.INCLUDE_ORIGINAL_CAPTION):
        blocks.append(("📝 <b>Original:</b>\n" if translated else "", original))

    remaining = limit - len(footer_text) - 2 - sum(len(h) for h, _ in blocks) - 2 * max(0, len(blocks) - 1)
    parts: list[str] = []
    for i, (header, text) in enumerate(blocks):
        share = max(0, remaining // (len(blocks) - i))
        body = clip_escaped(text, share) if share > 10 else ""
        if not body:
            continue
        parts.append(header + body)
        remaining -= len(body)
    parts.append(footer_text)
    return "\n\n".join(parts)
