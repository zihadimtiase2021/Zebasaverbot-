"""Ollama Cloud (or a local Ollama) chat backend for caption translation, with automatic model selection.

* Talks to Ollama's native API:  POST {base}/api/chat   GET {base}/api/tags   (Bearer key for https://ollama.com)
* Model "auto": the live catalogue (/api/tags) is ranked and the best candidates are tried in order.
  Hosted models are retired from time to time, so nothing is hard-coded except a last-resort fallback list.
* Free plan friendly: a 429 (session/weekly limit) pauses translation instead of hammering the API; the post
  itself is never delayed or failed because of it.
This module must not import `settings` (the setup wizard uses it before the configuration is loaded).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from unified_core.utils import short_error

log = logging.getLogger("zeba.ollama")

CLOUD_URL = "https://ollama.com"

# Models whose names say they are uncensored / abliterated (usually community models, i.e. local Ollama).
UNCENSORED_HINTS = ("abliterat", "uncensor", "dolphin", "huihui", "josiefied", "unfilter", "lexi")
# Strongest/most permissive general models for multilingual translation first. gpt-oss is ranked last because
# it is the most refusal-prone family. This is a heuristic: pin a model with OLLAMA_MODEL if you prefer another.
FAMILIES = ("glm", "kimi", "minimax", "deepseek", "qwen", "gemma", "mistral", "mixtral", "nemotron", "llama")
RESTRICTIVE = ("gpt-oss",)
EXCLUDE = re.compile(r"(embed|coder|[-:_.]code\b|rerank|guard|[-_]vl\b|ocr)", re.IGNORECASE)
HEAVY = re.compile(r"(?<![a-z])(pro|ultra|max)(?![a-z])", re.IGNORECASE)
FALLBACK_MODELS = ("glm-5.2", "kimi-k2.6", "minimax-m2.7", "gemma4", "gpt-oss:120b", "gpt-oss:20b")

_SIZE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)([bt])\b", re.IGNORECASE)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_STRIP_FOR_COUNT = re.compile(r"https?://\S+|[#@]\w+")

MODEL_MEMORY_SECONDS = 6 * 3600
MAX_TRIES = 3


def _size_b(name: str) -> float:
    m = _SIZE.search(name)
    if not m:
        return 0.0
    n = float(m.group(1))
    return n * 1000 if m.group(2).lower() == "t" else n


def _version(name: str) -> tuple[int, ...]:
    base = _SIZE.sub("", name.split(":")[0])
    nums = tuple(int(n) for n in re.findall(r"\d+", base))
    return (nums + (0,) * 4)[:4]


def rank_models(names: list[str], prefer_uncensored: bool = True) -> list[str]:
    """Best candidate first. Pure function (unit-tested)."""
    scored = []
    for name in dict.fromkeys(names):
        low = name.lower()
        if EXCLUDE.search(low):
            continue
        if prefer_uncensored and any(h in low for h in UNCENSORED_HINTS):
            tier = 0
        elif any(r in low for r in RESTRICTIVE):
            tier = 2
        else:
            tier = 1
        heavy = 1 if (HEAVY.search(low) or _size_b(low) >= 400) else 0  # burns the free quota fastest
        fam = next((i for i, f in enumerate(FAMILIES) if f in low), len(FAMILIES))
        scored.append(((tier, heavy, fam, tuple(-n for n in _version(low)), _size_b(low)), name))
    return [n for _, n in sorted(scored, key=lambda x: x[0])]


def looks_translated(src: str, out: str) -> bool:
    """False when the answer contains (almost) no Bangla although the caption clearly had text: refusal / chatter."""
    if not out:
        return False
    letters = sum(c.isalpha() for c in _STRIP_FOR_COUNT.sub("", src))
    if letters < 20:
        return True  # too little text to judge
    bn = sum("\u0980" <= c <= "\u09ff" for c in out)
    return bn >= min(8, letters * 0.15)


class OllamaChat:
    def __init__(self, base_url: str = CLOUD_URL, api_key: str = "", model: str = "auto", *,
                 timeout: float = 60.0, system: str = "", prefer_uncensored: bool = True) -> None:
        self.base = (base_url or CLOUD_URL).rstrip("/")
        if self.base.endswith("/v1"):  # tolerate the OpenAI-style URL
            self.base = self.base[:-3]
        self.key = api_key
        self.model = (model or "auto").strip() or "auto"
        self.timeout = timeout
        self.system = system
        self.prefer_uncensored = prefer_uncensored
        self._catalog: list[str] = []
        self._catalog_until = 0.0
        self._model: str | None = None        # last model that worked
        self._model_until = 0.0
        self._bad_until: dict[str, float] = {}  # models that answered 403/404/5xx recently
        self._no_think: set[str] = set()        # models that reject the `think` parameter
        self._blocked_until = 0.0               # usage limit reached / key rejected
        self.last_error = ""

    # ---------------------------------------------------------------- http
    async def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object, dict]:
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.request(method, self.base + path, json=payload, headers=headers)
        except Exception as exc:  # network down, timeout ...
            return 0, short_error(exc), {}
        try:
            body: object = r.json()
        except Exception:
            body = r.text
        return r.status_code, body, {k.lower(): v for k, v in r.headers.items()}

    # ------------------------------------------------------------- models
    async def catalog(self) -> list[str]:
        now = time.monotonic()
        if self._catalog and now < self._catalog_until:
            return self._catalog
        status, body, _ = await self._request("GET", "/api/tags")
        names: list[str] = []
        if status == 200 and isinstance(body, dict):
            for m in body.get("models") or []:
                n = (m or {}).get("model") or (m or {}).get("name")
                if n:
                    names.append(str(n))
        if names:
            self._catalog, self._catalog_until = names, now + MODEL_MEMORY_SECONDS
            return names
        log.info("Ollama model list unavailable (HTTP %s) - using the built-in fallback list", status)
        return list(self._catalog) or list(FALLBACK_MODELS)

    async def candidates(self) -> list[str]:
        if self.model != "auto":
            return [self.model]
        now = time.monotonic()
        ranked = [m for m in rank_models(await self.catalog(), self.prefer_uncensored)
                  if self._bad_until.get(m, 0) <= now]
        if self._model and now < self._model_until and self._model in ranked:
            ranked.remove(self._model)
            ranked.insert(0, self._model)
        return ranked

    # --------------------------------------------------------------- chat
    async def _chat(self, model: str, text: str) -> tuple[int, str, dict, object]:
        payload: dict = {
            "model": model, "stream": False, "options": {"temperature": 0.2},
            "messages": ([{"role": "system", "content": self.system}] if self.system else [])
                        + [{"role": "user", "content": text}],
        }
        if model not in self._no_think:
            payload["think"] = False  # reasoning models: skip the (slow, quota-hungry) thinking phase
        status, body, headers = await self._request("POST", "/api/chat", payload)
        if status == 400 and "think" in payload and "think" in str(body).lower():
            self._no_think.add(model)
            payload.pop("think")
            status, body, headers = await self._request("POST", "/api/chat", payload)
        content = ""
        if status == 200 and isinstance(body, dict):
            content = str((body.get("message") or {}).get("content") or "")
        return status, _THINK.sub("", content).strip(), headers, body

    async def translate(self, text: str) -> str | None:
        """Bangla translation or None (paused / nothing worked). Never raises."""
        now = time.monotonic()
        if now < self._blocked_until:
            return None
        try:
            for model in (await self.candidates())[:MAX_TRIES]:
                status, out, headers, body = await self._chat(model, text)
                if status == 200:
                    if looks_translated(text, out):
                        self._model, self._model_until = model, time.monotonic() + MODEL_MEMORY_SECONDS
                        self.last_error = ""
                        return out
                    log.info("%s did not return a Bangla translation (refusal?) - trying the next model", model)
                    continue
                if status == 429:
                    wait = _retry_after(headers)
                    self._blocked_until = time.monotonic() + (wait or 600)
                    self.last_error = "Ollama usage limit reached"
                    log.warning("Ollama Cloud usage limit reached - translation paused for %ds "
                                "(posts continue without translation)", wait or 600)
                    return None
                if status == 401:
                    self._blocked_until = time.monotonic() + 3600
                    self.last_error = "Ollama API key rejected"
                    log.error("Ollama API key rejected (HTTP 401) - check OLLAMA_API_KEY")
                    return None
                self._bad_until[model] = time.monotonic() + 3600  # 403 plan/model, 404 retired, 5xx
                self.last_error = f"{model}: HTTP {status} {short_error(str(body), 120)}"
                log.warning("Ollama model %s unusable (%s) - trying the next one", model, self.last_error)
        except Exception as exc:
            log.warning("ollama translate failed: %s", short_error(exc))
        return None

    async def describe(self) -> str:
        """Human-readable status for the .ai command."""
        now = time.monotonic()
        ranked = await self.candidates()
        lines = [f"Ollama · {self.base}"]
        if self.model != "auto":
            lines.append(f"Model (pinned): {self.model}")
        else:
            current = self._model if self._model and now < self._model_until else None
            lines.append(f"Model (auto): {current or 'chosen on first translation'}")
            lines.append("Next candidates: " + (", ".join(ranked[:3]) or "none found"))
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
            if status == 429:
                return "HTTP 429 - usage limit reached right now (the key itself is fine): " + short_error(str(body), 160), "", ""
            self._bad_until[model] = time.monotonic() + 3600
        return last or self.last_error or "no model answered (catalogue empty?) - check the key and your connection", "", ""


def _retry_after(headers: dict) -> int:
    try:
        return max(1, min(int(float(headers.get("retry-after", "0"))), 6 * 3600))
    except (TypeError, ValueError):
        return 0
