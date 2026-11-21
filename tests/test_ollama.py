"""Ollama Cloud translation: free-tier friendly, model auto-selected from the live catalogue."""
import asyncio

import pytest

pytest.importorskip("httpx")  # real module in production; stubbed by the sandbox runner

from unified_services.ollama_cloud import FALLBACK_MODELS, OllamaChat, looks_translated, rank_models

BN = "এটি একটি দারুণ ভিডিও যা আমি সবাইকে দেখাতে চাই"
EN = "This is a really great video that I want to show everybody today"


def test_rank_models_prefers_permissive_light_new_models():
    names = ["gpt-oss:120b", "gpt-oss:20b", "deepseek-v4-pro", "deepseek-v4-flash", "glm-4.7", "glm-5.2",
             "kimi-k2.6", "kimi-k2.7-code", "qwen3-coder:480b", "nomic-embed-text", "minimax-m3", "gemma4"]
    ranked = rank_models(names)
    assert ranked[0] == "glm-5.2"                       # newest of the most preferred family
    assert ranked.index("glm-5.2") < ranked.index("glm-4.7")
    assert "kimi-k2.7-code" not in ranked and "qwen3-coder:480b" not in ranked and "nomic-embed-text" not in ranked
    assert ranked.index("deepseek-v4-flash") < ranked.index("deepseek-v4-pro")      # keep the free quota
    assert ranked[-2:] == ["gpt-oss:20b", "gpt-oss:120b"] or ranked[-1].startswith("gpt-oss")  # refusal-prone: last


def test_rank_models_uncensored_first_when_available():
    names = ["glm-5.2", "huihui_ai/qwen3-abliterated:14b", "dolphin3:8b", "kimi-k2.6"]
    assert rank_models(names)[0] in ("huihui_ai/qwen3-abliterated:14b", "dolphin3:8b")
    assert rank_models(names, prefer_uncensored=False)[0] == "glm-5.2"


def test_looks_translated():
    assert looks_translated(EN, BN)
    assert not looks_translated(EN, "I'm sorry, but I can't help with that request.")   # refusal -> next model
    assert looks_translated("hi #fyp #viral https://x.com/a", "ok")                      # too short to judge


def _chat(responses, catalog=None, **kw):
    """OllamaChat whose HTTP layer is replaced by scripted answers: {model: (status, content[, headers])}."""
    calls = []
    c = OllamaChat("https://ollama.com", "key", kw.pop("model", "auto"), **kw)

    async def fake(method, path, payload=None):
        if path == "/api/tags":
            names = catalog if catalog is not None else list(responses)
            return 200, {"models": [{"model": n} for n in names]}, {}
        calls.append(payload["model"])
        r = responses[payload["model"]]
        status, content = r[0], r[1]
        headers = r[2] if len(r) > 2 else {}
        return status, {"message": {"content": content}}, headers

    c._request = fake
    return c, calls


def test_auto_model_falls_back_when_a_model_is_not_allowed_or_refuses():
    c, calls = _chat({"glm-5.2": (403, "this model requires a subscription"),
                      "kimi-k2.6": (200, "Sorry, I cannot assist with that."),
                      "minimax-m2.7": (200, BN)})
    assert asyncio.run(c.translate(EN)) == BN
    assert calls == ["glm-5.2", "kimi-k2.6", "minimax-m2.7"]
    assert asyncio.run(c.translate(EN)) == BN             # next caption starts with the model that worked
    assert calls[3] == "minimax-m2.7" and len(calls) == 4


def test_usage_limit_pauses_translation_without_blocking_posts():
    c, calls = _chat({"glm-5.2": (429, "limit", {"retry-after": "120"}), "kimi-k2.6": (200, BN)})
    assert asyncio.run(c.translate(EN)) is None           # limit reached -> post goes out untranslated
    n = len(calls)
    assert asyncio.run(c.translate(EN)) is None and len(calls) == n    # paused: no further requests
    assert "Paused" in asyncio.run(c.describe())


def test_rejected_key_and_pinned_model():
    c, calls = _chat({"glm-5.2": (401, "unauthorized")})
    assert asyncio.run(c.translate(EN)) is None and calls == ["glm-5.2"]
    err, model, _ = asyncio.run(_chat({"glm-5.2": (401, "x")})[0].check())
    assert "rejected" in err and model == ""

    c, calls = _chat({"my-model": (200, BN)}, catalog=["glm-5.2"], model="my-model")
    assert asyncio.run(c.translate(EN)) == BN and calls == ["my-model"]


def test_catalog_unavailable_uses_builtin_fallback_list():
    c = OllamaChat("https://ollama.com", "key")

    async def down(method, path, payload=None):
        return 503, "down", {}

    c._request = down
    assert asyncio.run(c.candidates())[0] in FALLBACK_MODELS


def test_translator_provider_selection(monkeypatch):
    pytest.importorskip("pydantic_settings")
    from unified_core.config import settings
    from unified_services import translator as tr

    def setup(**kw):
        base = dict(TRANSLATE_ENABLED=True, TRANSLATE_PROVIDER="auto", OLLAMA_API_KEY="", OLLAMA_BASE_URL="https://ollama.com",
                    OLLAMA_MODEL="auto", OPENAI_API_KEY="", OPENAI_BASE_URL="https://api.openai.com/v1", OPENAI_MODEL="gpt-4o-mini",
                    TRANSLATE_TIMEOUT=40.0, TRANSLATE_CONCURRENCY=1, OLLAMA_PREFER_UNCENSORED=True)
        base.update(kw)
        for k, v in base.items():
            monkeypatch.setattr(settings, k, v)
        return tr.Translator()

    t = setup(OLLAMA_API_KEY="k")                                   # key set -> Ollama Cloud, model auto
    assert t.enabled and t._ollama is not None and t._ollama.model == "auto"
    t = setup(OPENAI_API_KEY="sk")                                  # classic OpenAI setup untouched
    assert t.enabled and t._ollama is None
    t = setup(OPENAI_API_KEY="k", OPENAI_BASE_URL="https://ollama.com/v1", OPENAI_MODEL="auto")  # old ollama.com setup
    assert t._ollama is not None and t._ollama.base == "https://ollama.com"
    t = setup()                                                      # nothing configured
    assert not t.enabled
    t = setup(TRANSLATE_PROVIDER="ollama", OLLAMA_BASE_URL="http://localhost:11434")  # local Ollama: no key needed
    assert t.enabled and t._ollama.base == "http://localhost:11434"
    assert asyncio.run(setup(OLLAMA_API_KEY="k", TRANSLATE_ENABLED=False).translate(EN)) is None
