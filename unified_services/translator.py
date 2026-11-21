"""Optional AI caption translation with switchable providers.

Providers:  ollama (Ollama Cloud / local Ollama, auto model) | openrouter (free models only) | openai (any OpenAI-compatible API) | g4f (GPT4Free)
Switch at any time from Telegram with  .ai ollama | .ai openrouter | .ai openai | .ai g4f | .ai off | .ai auto  (remembered across restarts).
`auto` tries every configured provider in turn (ollama -> openrouter -> openai), so one limited provider never stops translation.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import urlsplit

from unified_core.config import settings
from unified_core.database import db
from unified_core.utils import is_mostly_bengali, short_error
from unified_services.g4f_chat import G4FChat
from unified_services.ollama_cloud import CLOUD_URL, OllamaChat
from unified_services.openrouter import OpenRouterChat

log = logging.getLogger("zeba.translate")

MODES = ("auto", "ollama", "openrouter", "openai", "g4f", "off")
_SYSTEM = (
    "You are a professional translator. Translate the user's social media caption into natural, "
    "fluent Bangla (Bengali). Keep hashtags, @mentions, URLs, emojis and line breaks unchanged. "
    "Translate faithfully, including slang, profanity or sensitive wording - do not refuse, censor, soften, "
    "moralize or add notes. Output ONLY the translation, nothing else."
)


def ollama_settings(force: bool = False) -> tuple[str, str, str] | None:
    """(base_url, api_key, model) when Ollama can be used, else None. `force` ignores TRANSLATE_PROVIDER."""
    provider = (settings.TRANSLATE_PROVIDER or "auto").lower()
    if provider in ("openai", "g4f") and not force:
        return None
    if provider == "ollama" or settings.OLLAMA_API_KEY:
        return settings.OLLAMA_BASE_URL or CLOUD_URL, settings.OLLAMA_API_KEY, settings.OLLAMA_MODEL or "auto"
    # an older setup that pointed the OpenAI settings at ollama.com keeps working (and gains auto model choice)
    host = (urlsplit(settings.OPENAI_BASE_URL).hostname or "").lower()
    if (provider == "auto" or force) and host.endswith("ollama.com") and settings.OPENAI_API_KEY:
        model = settings.OPENAI_MODEL if settings.OPENAI_MODEL not in ("", "auto") else settings.OLLAMA_MODEL
        return CLOUD_URL, settings.OPENAI_API_KEY, model or "auto"
    return None


def _make_ollama(cfg: tuple[str, str, str]) -> OllamaChat | None:
    base, key, model = cfg
    is_cloud = (urlsplit(base).hostname or "").lower().endswith("ollama.com")
    if is_cloud and not key:  # local Ollama needs no key, the cloud does
        return None
    return OllamaChat(base, key, model, timeout=max(settings.TRANSLATE_TIMEOUT, 60.0), system=_SYSTEM,
                      prefer_uncensored=settings.OLLAMA_PREFER_UNCENSORED)


class Translator:
    def __init__(self) -> None:
        self.mode = "auto"  # runtime choice (.ai ...); "auto" = whatever .env says
        self._ollama: OllamaChat | None = None
        cfg = ollama_settings()
        if cfg and settings.TRANSLATE_ENABLED:
            self._ollama = _make_ollama(cfg)
        self._openrouter: OpenRouterChat | None = None
        self._g4f: G4FChat | None = None
        self._client = None
        self._cache: dict[str, str] = {}
        self._sems = {"ollama": asyncio.Semaphore(settings.TRANSLATE_CONCURRENCY),
                      "openrouter": asyncio.Semaphore(1), "openai": asyncio.Semaphore(8), "g4f": asyncio.Semaphore(3)}

    # ------------------------------------------------------------ providers
    def _get_ollama(self) -> OllamaChat | None:
        if self._ollama is None:
            cfg = ollama_settings(force=True)  # `.ai ollama` while .env says something else
            self._ollama = _make_ollama(cfg) if cfg else None
        return self._ollama

    def _get_openrouter(self) -> OpenRouterChat | None:
        if self._openrouter is None and settings.OPENROUTER_API_KEY:
            self._openrouter = OpenRouterChat(
                settings.OPENROUTER_API_KEY, settings.OPENROUTER_MODEL or "auto",
                timeout=max(settings.TRANSLATE_TIMEOUT, 60.0), system=_SYSTEM,
                prefer_uncensored=settings.OLLAMA_PREFER_UNCENSORED)
        return self._openrouter

    def _get_g4f(self) -> G4FChat | None:
        if self._g4f is None:
            if not G4FChat.available():
                return None
            self._g4f = G4FChat(_SYSTEM, settings.G4F_MODEL, settings.G4F_TIMEOUT)
        return self._g4f

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,
                timeout=settings.TRANSLATE_TIMEOUT,
                max_retries=1,
            )
        return self._client

    async def _openai(self, text: str) -> str | None:
        if not settings.OPENAI_API_KEY:
            return None
        try:
            resp = await self._get_client().chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0.2,
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": text}],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            log.warning("translation failed: %s", short_error(exc))
            return None

    def _default_chain(self) -> list[str]:
        """What .env asks for (mode 'auto'): one explicit provider, or every configured one in turn."""
        provider = (settings.TRANSLATE_PROVIDER or "auto").lower()
        if provider == "g4f":
            return ["g4f"]
        if provider in ("ollama", "openrouter", "openai"):
            return [provider]
        chain = []
        if self._ollama is not None:
            chain.append("ollama")
        if settings.OPENROUTER_API_KEY:
            chain.append("openrouter")
        if settings.OPENAI_API_KEY:
            chain.append("openai")
        return chain

    def _available(self, name: str) -> bool:
        if name == "ollama":
            return self._get_ollama() is not None
        if name == "openrouter":
            return self._get_openrouter() is not None
        if name == "openai":
            return bool(settings.OPENAI_API_KEY)
        if name == "g4f":
            return self._get_g4f() is not None
        return False

    def _order(self) -> list[str]:
        if not settings.TRANSLATE_ENABLED or self.mode == "off":
            return []
        wanted = self._default_chain() if self.mode == "auto" else [self.mode]
        order = [n for n in wanted if self._available(n)]
        if settings.TRANSLATE_FALLBACK_G4F and "g4f" not in order and self._available("g4f"):
            order.append("g4f")
        return order

    @property
    def enabled(self) -> bool:
        return bool(self._order())

    # ----------------------------------------------------------- switching
    async def load_mode(self) -> None:
        """Restore the mode chosen with `.ai ...` before the last restart."""
        try:
            saved = await db.get_setting("translate_mode")
        except Exception:
            saved = None
        self.mode = saved if saved in MODES else "auto"

    async def set_mode(self, mode: str) -> str:
        """Switch provider (persisted). Returns the message to show (HTML-safe plain text)."""
        mode = (mode or "").strip().lower()
        if mode not in MODES:
            return "Usage: .ai [test | auto | ollama | openrouter | openai | g4f | off]"
        if mode == "g4f" and not G4FChat.available():
            return ("GPT4Free is not installed. Install it once, then try again:\n"
                    "  .venv/bin/pip install -U g4f    (Windows: .venv\\Scripts\\pip install -U g4f)")
        if mode == "openrouter" and self._get_openrouter() is None:
            return "OpenRouter is not configured: set OPENROUTER_API_KEY in .env (free key: openrouter.ai/keys)."
        if mode == "ollama" and self._get_ollama() is None:
            return "Ollama is not configured: set OLLAMA_API_KEY in .env (free key: ollama.com/settings/keys)."
        if mode == "openai" and not settings.OPENAI_API_KEY:
            return "No OpenAI-compatible API key is configured (OPENAI_API_KEY)."
        self.mode = mode
        await db.set_setting("translate_mode", mode)
        note = ""
        if mode == "g4f":
            note = "\nNote: g4f uses unofficial public endpoints - captions go to third parties, quality/uptime vary."
        return f"Translation mode: {mode}{note}"

    async def describe(self) -> str:
        lines = [f"Mode: {self.mode}" + (" (= .env setting)" if self.mode == "auto" else "")
                 + ("" if settings.TRANSLATE_ENABLED else "  [TRANSLATE_ENABLED=false]")]
        order = self._order()
        if not order:
            lines.append("Active: none - translation is OFF (no key configured, or mode off).")
        for name in order:
            if name == "ollama":
                lines.append(await self._get_ollama().describe())
            elif name == "openrouter":
                lines.append(await self._get_openrouter().describe())
            elif name == "openai":
                lines.append(f"OpenAI-compatible · {settings.OPENAI_BASE_URL} · model {settings.OPENAI_MODEL}")
            else:
                lines.append(self._get_g4f().describe())
        lines.append("Switch: .ai ollama | .ai openrouter | .ai openai | .ai g4f | .ai off | .ai auto  ·  Test: .ai test")
        return "\n".join(lines)

    # ------------------------------------------------------------ translate
    async def _run(self, name: str, text: str) -> str | None:
        async with self._sems[name]:
            if name == "ollama":
                return await self._get_ollama().translate(text)
            if name == "openrouter":
                return await self._get_openrouter().translate(text)
            if name == "g4f":
                return await self._get_g4f().translate(text)
            return await self._openai(text)

    async def translate(self, text: str) -> str | None:
        """Return the Bangla translation, or None (disabled / unnecessary / failed)."""
        text = (text or "").strip()
        if not text or is_mostly_bengali(text) or sum(c.isalpha() for c in text) < 3:
            return None
        order = self._order()
        if not order:
            return None
        text = text[:4000]
        key = hashlib.sha1(text.encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        for name in order:
            out = await self._run(name, text)
            if out:
                if len(self._cache) > 512:
                    self._cache.clear()
                self._cache[key] = out
                return out
        return None

    async def diagnose(self) -> str:
        """Run a real test translation through EVERY configured provider and report the exact outcome."""
        sample = "Hello, how are you today my friend?"
        rows: list[str] = []
        if not settings.TRANSLATE_ENABLED:
            rows.append("⚠️ TRANSLATE_ENABLED=false in .env - translation is switched off globally")
        for name in ("ollama", "openrouter", "openai", "g4f"):
            try:
                if name == "ollama":
                    bot = self._get_ollama()
                    if bot is None:
                        rows.append("➖ ollama: not configured (OLLAMA_API_KEY)")
                        continue
                    err, model, out = await bot.check()
                elif name == "openrouter":
                    bot = self._get_openrouter()
                    if bot is None:
                        rows.append("➖ openrouter: not configured (OPENROUTER_API_KEY)")
                        continue
                    err, model, out = await bot.check()
                elif name == "openai":
                    if not settings.OPENAI_API_KEY:
                        rows.append("➖ openai: not configured (OPENAI_API_KEY)")
                        continue
                    err, model, out = "", settings.OPENAI_MODEL, ""
                    try:
                        resp = await self._get_client().chat.completions.create(
                            model=settings.OPENAI_MODEL, temperature=0.2,
                            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": sample}])
                        out = (resp.choices[0].message.content or "").strip()
                    except Exception as exc:
                        err = short_error(exc, 300)
                else:
                    g = self._get_g4f()
                    if g is None:
                        rows.append("➖ g4f: not installed")
                        continue
                    out = await g.translate(sample) or ""
                    err, model = ("" if out else (g.last_error or "no answer")), "default"
            except Exception as exc:
                rows.append(f"❌ {name}: {short_error(exc, 300)}")
                continue
            rows.append(f"✅ {name} · {model} → {out[:60]}" if not err and out else f"❌ {name}: {err or 'empty answer'}")
        used = " → ".join(self._order()) or "none"
        rows.append(f"Active order right now: {used}")
        return "\n".join(rows)
