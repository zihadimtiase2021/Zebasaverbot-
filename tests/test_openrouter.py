"""OpenRouter: FREE models only, auto-selected, never paid; limits pause translation instead of failing posts."""
import asyncio

import pytest

pytest.importorskip("httpx")

from unified_services.openrouter import FREE_ROUTER, OpenRouterChat, free_text_models

BN = "এটি একটি দারুণ ভিডিও যা আমি সবাইকে দেখাতে চাই"
EN = "This is a really great video that I want to show everybody today"

CATALOG = [
    {"id": "z-ai/glm-4.5-air:free", "pricing": {"prompt": "0", "completion": "0"}, "architecture": {"output_modalities": ["text"]}},
    {"id": "meta-llama/llama-3.3-70b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-oss-120b:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "openai/gpt-4o-mini", "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},   # paid
    {"id": "some/model:free", "pricing": {"prompt": "0.000001", "completion": "0"}},                 # claims free, is not
    {"id": "google/lyria:free", "pricing": {"prompt": "0", "completion": "0"}, "architecture": {"output_modalities": ["audio"]}},
]


def test_only_free_text_models_are_listed():
    assert free_text_models(CATALOG) == ["z-ai/glm-4.5-air:free", "meta-llama/llama-3.3-70b-instruct:free", "openai/gpt-oss-120b:free"]


def _bot(script, **kw):
    calls = []
    c = OpenRouterChat("key", kw.pop("model", "auto"), **kw)

    async def fake(method, path, payload=None):
        if path == "/models":
            return 200, {"data": CATALOG}, {}
        calls.append(payload["model"])
        r = script[payload["model"]]
        if r[0] == 200:
            return 200, {"choices": [{"message": {"content": r[1]}}]}, {}
        return r[0], {"error": {"message": r[1]}}, (r[2] if len(r) > 2 else {})

    c._request = fake
    return c, calls


def test_auto_picks_free_models_then_free_router():
    c, calls = _bot({"z-ai/glm-4.5-air:free": (404, "no endpoints"), "meta-llama/llama-3.3-70b-instruct:free": (200, "Sorry, I cannot help."),
                     FREE_ROUTER: (200, BN)})
    assert asyncio.run(c.translate(EN)) == BN
    assert calls == ["z-ai/glm-4.5-air:free", "meta-llama/llama-3.3-70b-instruct:free", FREE_ROUTER]
    assert all(m.endswith(":free") or m == FREE_ROUTER for m in calls)          # never a paid model


def test_pinned_paid_model_is_refused_and_limits_pause():
    c, calls = _bot({"openai/gpt-4o-mini": (200, BN)}, model="openai/gpt-4o-mini")
    assert asyncio.run(c.translate(EN)) is None and calls == []                  # paid model: never called
    err, model, _ = asyncio.run(c.check())
    assert model == "" and "free" in err.lower() or err

    c, calls = _bot({"z-ai/glm-4.5-air:free": (429, "Rate limit exceeded: free-models-per-day")})
    assert asyncio.run(c.translate(EN)) is None
    n = len(calls)
    assert asyncio.run(c.translate(EN)) is None and len(calls) == n              # paused, no more requests
    assert "Paused" in asyncio.run(c.describe())

    c, calls = _bot({"z-ai/glm-4.5-air:free": (401, "No auth credentials found")})
    err, model, _ = asyncio.run(c.check())
    assert "401" in err and model == ""
