"""OpenRouter backend for caption translation - FREE models only.

Only models whose id ends in ":free" (pricing 0) and the free-only router "openrouter/free" are ever called, so this
can never spend credits. Free models share an account-wide quota (20 requests/min, 50/day until $10 of credits have
ever been bought, then 1000/day); when it is used up translation pauses and posts continue untranslated.
This module must not import `settings` (the setup wizard uses it before the configuration is loaded).
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from unified_core.utils import short_error
from unified_services.ollama_cloud import MAX_TRIES, MODEL_MEMORY_SECONDS, _THINK, _retry_after, looks_translated, rank_models

log = logging.getLogger("zeba.openrouter")

BASE_URL = "https://openrouter.ai/api/v1"
FREE_ROUTER = "openrouter/free"


def is_free_id(model_id: str) -> bool:
    return model_id == FREE_ROUTER or model_id.endswith(":free")


def _zero(v) -> bool:
    try:
        return v is not None and float(v) == 0.0
    except (TypeError, ValueError):
        return False


def free_text_models(data: list[dict]) -> list[str]:
    """ids from GET /models that cost nothing and produce text."""
    out = []
    for m in data or []:
        mid = str((m or {}).get("id") or "")
        pricing = (m or {}).get("pricing") or {}
        outputs = ((m or {}).get("architecture") or {}).get("output_modalities")
        if mid and mid.endswith(":free") and _zero(pricing.get("prompt")) and _zero(pricing.get("completion")) \
                and (not outputs or "text" in outputs):
            out.append(mid)
    return out


class OpenRouterChat:
    def __init__(self, api_key: str, model: str = "auto", *, timeout: float = 60.0, system: str = "",
                 base_url: str = BASE_URL, prefer_uncensored: bool = True) -> None:
        self.key = api_key
        self.model = (model or "auto").strip() or "auto"
        self.base = (base_url or BASE_URL).rstrip("/")
        self.timeout = timeout
        self.system = system
        self.prefer_uncensored = prefer_uncensored
        self._catalog: list[str] = []
        self._catalog_until = 0.0
        self._model: str | None = None
        self._model_until = 0.0
        self._bad_until: dict[str, float] = {}
        self._blocked_until = 0.0
        self.last_error = ""

    async def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object, dict]:
        headers = {"Authorization": f"Bearer {self.key}", "X-Title": "Zeba Saver Bot"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.request(method, self.base + path, json=payload, headers=headers)
        except Exception as exc:
            return 0, short_error(exc), {}
        try:
            body: object = r.json()
        except Exception:
            body = r.text
        return r.status_code, body, {k.lower(): v for k, v in r.headers.items()}

    async def catalog(self) -> list[str]:
        now = time.monotonic()
        if self._catalog and now < self._catalog_until:
            return self._catalog
        status, body, _ = await self._request("GET", "/models")
        ids = free_text_models(body.get("data") if status == 200 and isinstance(body, dict) else [])
        if ids:
            self._catalog, self._catalog_until = ids, now + MODEL_MEMORY_SECONDS
            return ids
        log.info("OpenRouter model list unavailable (HTTP %s) - using the free router only", status)
        return list(self._catalog)

    async def candidates(self) -> list[str]:
        if self.model != "auto":
            if not is_free_id(self.model):  # safety: never call a paid model
                self.last_error = f"{self.model} is not a free model (must end in :free)"
                return []
            return [self.model]
        now = time.monotonic()
        ranked = [m for m in rank_models(await self.catalog(), self.prefer_uncensored)
                  if self._bad_until.get(m, 0) <= now]
        if self._model and now < self._model_until and self._model in ranked:
            ranked.remove(self._model)
            ranked.insert(0, self._model)
        # the free-only router is the safety net: OpenRouter itself picks any free model that is up
        return ranked[: MAX_TRIES - 1] + [FREE_ROUTER]

    async def _chat(self, model: str, text: str) -> tuple[int, str, dict, object]:
        payload = {
            "model": model, "temperature": 0.2,
            "messages": ([{"role": "system", "content": self.system}] if self.system else [])
                        + [{"role": "user", "content": text}],
        }
        status, body, headers = await self._request("POST", "/chat/completions", payload)
        content = ""
        if status == 200 and isinstance(body, dict):
            if body.get("error"):  # OpenRouter sometimes answers 200 with an error object
                status = int((body["error"] or {}).get("code") or 502) if str((body["error"] or {}).get("code") or "").isdigit() else 502
            else:
                try:
                    content = str(body["choices"][0]["message"]["content"] or "")
                except (KeyError, IndexError, TypeError):
                    status = 502
        return status, _THINK.sub("", content).strip(), headers, body

    def _block(self, seconds: int, why: str) -> None:
        self._blocked_until = time.monotonic() + seconds
        self.last_error = why
        log.warning("OpenRouter: %s - translation via OpenRouter paused for %d min", why, seconds // 60 + 1)

    async def translate(self, text: str) -> str | None:
        """Bangla translation or None (paused / nothing worked). Never raises."""
        if time.monotonic() < self._blocked_until:
            return None
        try:
            for model in (await self.candidates())[:MAX_TRIES]:
                status, out, headers, body = await self._chat(model, text)
                if status == 200:
                    if looks_translated(text, out):
                        if model != FREE_ROUTER:
                            self._model, self._model_until = model, time.monotonic() + MODEL_MEMORY_SECONDS
                        self.last_error = ""
                        return out
                    log.info("%s did not return a Bangla translation (refusal?) - trying the next one", model)
                    continue
                if status == 401:
                    self._block(3600, "API key rejected (HTTP 401)")
                    return None
                if status == 402:
                    self._block(3600, "negative credit balance (HTTP 402) - even free models are refused until it is positive")
                    return None
                if status == 429:
                    daily = bool(re.search(r"per.?day|daily|free-models-per-day", str(body), re.IGNORECASE))
                    self._block(3600 if daily else (_retry_after(headers) or 65),
                                "daily free-model limit reached (50/day without credits)" if daily else "rate limit (20 requests/min)")
                    return None
                self._bad_until[model] = time.monotonic() + 3600
                self.last_error = f"{model}: HTTP {status} {short_error(str(body), 120)}"
                log.warning("OpenRouter model %s unusable (%s) - trying the next one", model, self.last_error)
        except Exception as exc:
            log.warning("openrouter translate failed: %s", short_error(exc))
        return None

    async def describe(self) -> str:
        now = time.monotonic()
        lines = ["OpenRouter (free models only)"]
        if self.model != "auto":
            lines.append(f"Model (pinned): {self.model}")
        else:
            current = self._model if self._model and now < self._model_until else None
            lines.append(f"Model (auto): {current or 'chosen on first translation'}")
            lines.append("Next candidates: " + ", ".join((await self.candidates())[:3]))
        if now < self._blocked_until:
            lines.append(f"Paused: {self.last_error or 'limit'} (~{int((self._blocked_until - now) / 60) + 1} min left)")
        return "\n".join(lines)

    async def check(self) -> tuple[str, str, str]:
        """Diagnostic: ('', model, sample_translation) when it works, else (exact error, '', '')."""
        last = ""
        for model in await self.candidates():
            status, out, headers, body = await self._chat(model, "Hello, how are you today my friend?")
            if status == 200 and out:
                return "", model, out
            last = f"{model}: HTTP {status} {short_error(str(body), 200)}"
            if status == 401:
                return "API key rejected (HTTP 401) - " + short_error(str(body), 160), "", ""
            if status == 402:
                return "HTTP 402 - " + short_error(str(body), 160), "", ""
            if status == 429:
                return "HTTP 429 - limit reached right now (the key itself is fine): " + short_error(str(body), 160), "", ""
        return last or self.last_error or "no free model answered", "", ""
