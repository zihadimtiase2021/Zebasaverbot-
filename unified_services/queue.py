"""Bounded worker pool consuming Jobs."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Awaitable, Callable

from unified_core.utils import normalize_url, short_error
from unified_services.pipeline import Job, JobResult

log = logging.getLogger("zeba.queue")


class JobQueue:
    def __init__(
        self,
        runner: Callable[[Job], Awaitable[JobResult]],
        on_result: Callable[[Job, JobResult], Awaitable[None]],
    ) -> None:
        self._q: asyncio.Queue[Job] = asyncio.Queue()
        self._runner = runner
        self._on_result = on_result
        self._workers: list[asyncio.Task] = []
        self._pending: set[str] = set()
        self._closing = False
        self.active: dict[str, Job] = {}
        self.counters: Counter = Counter()

    def start(self, workers: int) -> None:
        for i in range(workers):
            self._workers.append(asyncio.create_task(self._worker(), name=f"zeba-worker-{i}"))

    def enqueue(self, job: Job) -> bool:
        """Queue a job. Returns False if the same URL is already queued/running."""
        job.key = normalize_url(job.url) + (f"@{job.dest_chat}" if job.dest_chat is not None else "")
        if job.key in self._pending:
            return False
        self._pending.add(job.key)
        self._q.put_nowait(job)
        return True

    def snapshot(self) -> dict:
        return {
            "pending": self._q.qsize(),
            "active": len(self.active),
            "ok": self.counters["done"],
            "skipped": self.counters["skipped"],
            "failed": self.counters["failed"],
        }

    async def _worker(self) -> None:
        while True:
            job = await self._q.get()
            self.active[job.id] = job
            result = JobResult("failed", error="unknown")
            try:
                result = await self._runner(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # runner must never kill the worker
                log.exception("runner crashed")
                result = JobResult("failed", error=short_error(exc))
            finally:
                self.active.pop(job.id, None)
                self._pending.discard(job.key)
                self._q.task_done()
            self.counters[result.status] += 1
            try:
                await self._on_result(job, result)
            except Exception:
                log.exception("result handler crashed")

    async def stop(self, grace: float = 30.0) -> None:
        self._closing = True
        while not self._q.empty():  # drop not-yet-started jobs
            try:
                self._q.get_nowait()
                self._q.task_done()
            except asyncio.QueueEmpty:
                break
        deadline = asyncio.get_event_loop().time() + grace
        while self.active and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
