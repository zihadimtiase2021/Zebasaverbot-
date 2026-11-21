from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from telethon import TelegramClient

from unified_services.pipeline import Pipeline
from unified_services.queue import JobQueue


@dataclass
class App:
    userbot: TelegramClient
    control_bot: TelegramClient | None
    queue: JobQueue
    pipeline: Pipeline
    me_id: int
    ignored_chats: set[int]
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int = 0
    runtime: object | None = None  # RuntimeConfig: settings changed live (target channel, source link, comments...)
    forwarder: object | None = None  # Forwarder: .fwdsrc / .fwddst chat-to-chat copier

    def request_stop(self, code: int = 0) -> None:
        self.exit_code = code
        self.stop_event.set()
