""".ai ollama|openai|g4f|off|auto: providers can be switched at any time; g4f is optional and can never block a post."""
import asyncio
import sys
import types

import pytest

pytest.importorskip("pydantic_settings")
pytest.importorskip("aiosqlite")

BN = "এটি একটি দারুণ ভিডিও যা আমি সবাইকে দেখাতে চাই"
EN = "This is a really great video that I want to show everybody today"


class _FakeG4F:
    """Installs a fake `g4f.client.AsyncClient` for the duration of a test."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def __enter__(self):
        outer = self

        class Completions:
            async def create(self, **kw):
                outer.calls += 1
                a = outer.answers.pop(0) if outer.answers else BN
                if isinstance(a, Exception):
                    raise a
                if a == "hang":
                    await asyncio.sleep(10)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=a))])

        class AsyncClient:
            chat = types.SimpleNamespace(completions=Completions())

        self._saved = {k: sys.modules.get(k) for k in ("g4f", "g4f.client")}
        pkg, client = types.ModuleType("g4f"), types.ModuleType("g4f.client")
        client.AsyncClient = AsyncClient
        pkg.client = client
        sys.modules["g4f"], sys.modules["g4f.client"] = pkg, client
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _translator(monkeypatch, **kw):
    from unified_core.config import settings
    from unified_services import translator as tr

    base = dict(TRANSLATE_ENABLED=True, TRANSLATE_PROVIDER="auto", OLLAMA_API_KEY="k", OLLAMA_BASE_URL="https://ollama.com",
                OLLAMA_MODEL="auto", OPENAI_API_KEY="", OPENAI_BASE_URL="https://api.openai.com/v1", OPENAI_MODEL="m",
                TRANSLATE_TIMEOUT=40.0, TRANSLATE_CONCURRENCY=1, OLLAMA_PREFER_UNCENSORED=True,
                TRANSLATE_FALLBACK_G4F=False, G4F_MODEL="", G4F_TIMEOUT=1.0, OPENROUTER_API_KEY="", OPENROUTER_MODEL="auto")
    base.update(kw)
    for k, v in base.items():
        monkeypatch.setattr(settings, k, v)
    return tr, tr.Translator()


def test_switch_between_ollama_g4f_and_off(monkeypatch):
    from unified_core.database import Database

    tr, t = _translator(monkeypatch)
    used = []

    async def ollama(text):
        used.append("ollama")
        return BN

    t._ollama.translate = ollama

    async def go():
        d = Database()
        await d.connect(":memory:")
        monkeypatch.setattr(tr, "db", d)
        try:
            assert await t.translate(EN + " 1") == BN and used == ["ollama"]          # default: Ollama
            assert "not installed" in await t.set_mode("g4f")                          # g4f missing -> clear message
            with _FakeG4F([BN]) as g:
                assert (await t.set_mode("g4f")).startswith("Translation mode: g4f")
                assert await t.translate(EN + " 2") == BN and g.calls == 1 and used == ["ollama"]   # g4f used now
                t2 = tr.Translator()
                await t2.load_mode()                                                   # remembered after a restart
                assert t2.mode == "g4f"
            assert (await t.set_mode("off")).startswith("Translation mode: off")
            assert await t.translate(EN + " 3") is None and not t.enabled
            assert "Usage" in await t.set_mode("banana")
            await t.set_mode("auto")
            assert await t.translate(EN + " 4") == BN and used == ["ollama", "ollama"]
        finally:
            await d.close()

    asyncio.run(go())


def test_g4f_never_blocks_and_rejects_refusals(monkeypatch):
    from unified_services.g4f_chat import G4FChat

    async def go():
        with _FakeG4F(["Sorry, I can't help with that.", "hang", RuntimeError("provider down"), BN]):
            g = G4FChat("sys", timeout=0.2)
            assert await g.translate(EN) is None            # refusal (no Bangla) rejected
            assert await g.translate(EN) is None            # hangs -> hard timeout, post is not delayed
            assert await g.translate(EN) is None            # exception -> None, never raises
            assert await g.translate(EN) is None            # 3 failures in a row -> paused for a few minutes
            assert "paused" in g.describe()

    asyncio.run(go())


def test_g4f_as_last_resort_fallback(monkeypatch):
    tr, t = _translator(monkeypatch, TRANSLATE_FALLBACK_G4F=True)

    async def limited(text):
        return None                                           # e.g. Ollama free limit reached

    t._ollama.translate = limited
    with _FakeG4F([BN]) as g:
        assert asyncio.run(t.translate(EN)) == BN and g.calls == 1
    tr, t = _translator(monkeypatch, TRANSLATE_FALLBACK_G4F=False)
    t._ollama.translate = limited
    with _FakeG4F([BN]) as g:
        assert asyncio.run(t.translate(EN)) is None and g.calls == 0     # off by default


def test_auto_chain_tries_every_configured_provider(monkeypatch):
    tr, t = _translator(monkeypatch, OPENROUTER_API_KEY="or-key", OPENAI_API_KEY="")
    order = []

    async def ollama(text):
        order.append("ollama")
        return None                                   # e.g. free limit reached

    async def openrouter(text):
        order.append("openrouter")
        return BN

    t._ollama.translate = ollama
    t._get_openrouter().translate = openrouter
    assert t._order() == ["ollama", "openrouter"]
    assert asyncio.run(t.translate(EN)) == BN and order == ["ollama", "openrouter"]
