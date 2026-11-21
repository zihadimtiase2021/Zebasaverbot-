"""Optional GPT4Free (g4f) translation backend.

g4f uses unofficial, reverse-engineered public endpoints: quality and uptime vary and captions are sent to
third-party providers. It is therefore never the default - you switch to it with `.ai g4f` (or use it only as a
last-resort fallback with TRANSLATE_FALLBACK_G4F=true). A broken provider can never delay a post: every call has
a hard timeout and repeated failures pause the backend for a few minutes.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time

from unified_core.utils import short_error
from unified_services.ollama_cloud import looks_translated

log = logging.getLogger("zeba.g4f")

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
PAUSE_AFTER_FAILURES = 3
PAUSE_SECONDS = 300


class G4FChat:
    def __init__(self, system: str = "", model: str = "", timeout: float = 30.0) -> None:
        self.system = system
        self.model = model
        self.timeout = timeout
        self._client = None
        self._fails = 0
        self._blocked_until = 0.0
        self.last_error = ""

    @staticmethod
    def available() -> bool:
        try:
            importlib.import_module("g4f.client")
            return True
        except Exception:
            return False

    def _get_client(self):
        if self._client is None:
            from g4f.client import AsyncClient

            self._client = AsyncClient()
        return self._client

    def _fail(self, why: str) -> None:
        self._fails += 1
        self.last_error = why
        if self._fails >= PAUSE_AFTER_FAILURES:
            self._blocked_until = time.monotonic() + PAUSE_SECONDS
            self._fails = 0
            log.warning("g4f failed %d times in a row - paused for %ds (%s)", PAUSE_AFTER_FAILURES, PAUSE_SECONDS, why)

    async def translate(self, text: str) -> str | None:
        """Bangla translation or None. Never raises, never takes longer than `timeout`."""
        if time.monotonic() < self._blocked_until:
            return None
        if not self.available():
            self.last_error = "g4f is not installed"
            return None
        try:
            resp = await asyncio.wait_for(
                self._get_client().chat.completions.create(
                    model=self.model,
                    messages=([{"role": "system", "content": self.system}] if self.system else [])
                             + [{"role": "user", "content": text}],
                ),
                self.timeout,
            )
            out = _THINK.sub("", str(resp.choices[0].message.content or "")).strip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(short_error(exc, 120) or "error")
            log.info("g4f failed: %s", self.last_error)
            return None
        if not looks_translated(text, out):
            self._fail("answer was not a Bangla translation")
            return None
        self._fails = 0
        self.last_error = ""
        return out

    def describe(self) -> str:
        if not self.available():
            return "GPT4Free (g4f): NOT installed - run  .venv/bin/pip install -U g4f  (Windows: .venv\\Scripts\\pip)"
        paused = max(0, int(self._blocked_until - time.monotonic()))
        return f"GPT4Free (g4f) · model {self.model or 'default'}" + (f" · paused ~{paused // 60 + 1} min" if paused else "")
