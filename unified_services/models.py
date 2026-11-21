from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path


class DownloadError(Exception):
    """Raised when media cannot be fetched from a source."""


@dataclass
class MediaItem:
    path: Path
    kind: str  # "photo" | "video"


@dataclass
class Comment:
    text: str
    author: str = ""


@dataclass
class PostData:
    source_url: str
    platform: str
    caption: str = ""
    total_comments: int = 0
    comments: list[Comment] = field(default_factory=list)
    media: list[MediaItem] = field(default_factory=list)
    # Optional background fetch that fills `comments` while the media is being downloaded/uploaded.
    comments_task: asyncio.Task | None = None
