import asyncio

import pytest

from unified_core.database import Database
from unified_core.utils import (
    clip_escaped, extract_urls, is_mostly_bengali, normalize_url, platform_of,
)
from unified_services.captions import build_caption


def test_extract_urls_basic_and_dedupe():
    text = (
        "see https://x.com/user/status/123?s=20 and https://twitter.com/u/status/123 "
        "also (https://www.instagram.com/reel/ABC/?igsh=zzz). nope https://evil.com/x.com/a"
    )
    urls = extract_urls(text)
    assert len(urls) == 2
    assert urls[1].startswith("https://www.instagram.com/reel/ABC")
    assert not urls[1].endswith(")")


def test_regex_rejects_lookalike_hosts():
    assert extract_urls("https://x.com.evil.org/a") == []
    assert extract_urls("https://notx.com/a") == []
    assert extract_urls("https://x.com@evil.com/a") == []


def test_normalize_url():
    assert normalize_url("https://twitter.com/a/status/9?s=1") == normalize_url("https://x.com/b/status/9")
    assert normalize_url("https://youtu.be/abc?si=1") == "youtube.com/watch?v=abc"
    assert normalize_url("https://www.youtube.com/watch?v=abc&t=5") == "youtube.com/watch?v=abc"
    assert normalize_url("https://instagram.com/p/X/?utm_source=a&igsh=b") == "instagram.com/p/X"


def test_platform_of():
    assert platform_of("https://t.me/c/1/2") == "telegram"
    assert platform_of("https://vm.tiktok.com/abc/") == "tiktok"
    assert platform_of("https://example.org/x") == "generic"


def test_clip_escaped_respects_limit():
    s = "a<b>&" * 500
    out = clip_escaped(s, 100)
    assert len(out) <= 100 and out.endswith("…")
    assert clip_escaped("short", 100) == "short"


def test_bengali_detection():
    assert is_mostly_bengali("আমি বাংলায় কথা বলি")
    assert not is_mostly_bengali("hello world")


@pytest.mark.parametrize("limit", [1024, 2048])
def test_caption_within_limit(limit):
    cap = build_caption(
        translated="ট" * 3000, original="o<&>" * 3000, source_url="https://x.com/a/status/1",
        comment_count=5, tag="#zeba_abcd1234", limit=limit,
    )
    assert len(cap) <= limit
    assert "#zeba_abcd1234" in cap and "Source" in cap


def test_caption_minimal():
    cap = build_caption(translated=None, original="", source_url="https://x.com/a/status/1",
                        comment_count=0, tag="#zeba_1", limit=1024)
    assert cap.count("#zeba_1") == 1


def test_database_jobs_and_settings():
    async def run():
        d = Database()
        await d.connect(":memory:")
        assert await d.job_get("k") is None
        await d.job_start("k", "https://x.com/a/status/1")
        await d.job_finish("k", "done", link="https://t.me/c/1/2")
        job = await d.job_get("k")
        assert job["status"] == "done" and job["posted_link"] == "https://t.me/c/1/2"
        await d.job_start("k2", "u")
        assert await d.recover_stale() == 1
        assert (await d.job_get("k2"))["status"] == "failed"
        await d.add_auto_chat(5)
        assert await d.is_auto_chat(5)
        await d.remove_auto_chat(5)
        assert not await d.is_auto_chat(5)
        await d.set_auto_global(True)
        assert await d.get_auto_global() is True
        st = await d.job_stats()
        assert st["done"] == 1 and st["failed"] == 1
        await d.close()

    asyncio.run(run())


def test_dl_inline_custom_caption():
    from unified_core.utils import split_url_and_caption as sp

    assert sp("https://x.com/u/status/1 My own caption") == (["https://x.com/u/status/1"], "My own caption")
    assert sp("https://x.com/u/status/1\nline1\nline2")[1] == "line1\nline2"
    assert sp("https://x.com/u/status/1") == (["https://x.com/u/status/1"], None)
    urls, cap = sp("https://x.com/u/status/1 https://youtu.be/abc")
    assert len(urls) == 2 and cap is None          # two links: no caption, both are queued
    assert sp("no link here") == ([], None)
