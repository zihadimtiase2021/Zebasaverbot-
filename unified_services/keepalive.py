"""Keep a free Render instance awake: ping our own public /health URL now and then.

Render's free web services sleep after ~15 minutes without inbound HTTP traffic (and a sleeping service means an offline
bot). A request to the service's public URL counts as traffic. This only helps while the process is running - an external
monitor (UptimeRobot / cron-job.org) is the backup that can also wake a service that is already asleep.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

log = logging.getLogger("zeba.keepalive")


class KeepAlive:
    def __init__(self, url: str, interval: int = 600, getter=None) -> None:
        self.url = url.rstrip("/") + "/health"
        self.interval = max(60, interval)
        self._get = getter or self._http_get
        self._task: asyncio.Task | None = None
        self.pings = 0

    @staticmethod
    async def _http_get(url: str) -> int:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            return (await client.get(url)).status_code

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="zeba-keepalive")
        log.info("Keep-alive ping every %ds -> %s", self.interval, self.url)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        await asyncio.sleep(min(30, self.interval))  # let the HTTP server come up first
        while True:
            try:
                status = await self._get(self.url)
                self.pings += 1
                if status != 200:
                    log.warning("keep-alive got HTTP %s", status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("keep-alive ping failed: %s", exc.__class__.__name__)
            await asyncio.sleep(self.interval + random.uniform(-20, 20))
