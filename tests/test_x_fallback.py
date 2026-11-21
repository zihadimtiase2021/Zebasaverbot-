"""X/Twitter: fast path first, several accounts as fallback, never blocked by a rate-limited account."""
import asyncio
from pathlib import Path

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _accounts(*names):
    return [{"username": n, "active": True, "locked_until": None,
             "cookies": {"auth_token": f"tok_{n}", "ct0": f"ct_{n}"}} for n in names]


def test_fxtwitter_first_twscrape_only_when_no_media(tmp_path, monkeypatch):
    from unified_services import twitter as tw
    from unified_services.models import MediaItem, PostData

    calls = []

    async def accounts():
        return _accounts("a", "b")

    async def fx(username, status_id, workdir, client, max_bytes, data):
        calls.append("fx")
        data.media.append(MediaItem(tmp_path / "x.mp4", "video"))

    async def twscrape(*a, **k):
        calls.append("twscrape")

    async def replies(status_id, data):
        calls.append("replies")

    monkeypatch.setattr(tw, "active_accounts", accounts)
    monkeypatch.setattr(tw, "_via_fxtwitter", fx)
    monkeypatch.setattr(tw, "_via_twscrape", twscrape)
    monkeypatch.setattr(tw, "_fetch_replies", replies)
    monkeypatch.setattr(tw, "make_client", lambda: _Client())

    async def go():
        data = PostData(source_url="u", platform="twitter")
        await tw.fetch_twitter("https://x.com/u/status/5", tmp_path, data, 10**9)
        assert data.comments_task is not None       # replies are fetched in the background
        await data.comments_task
        return data

    data = asyncio.run(go())
    assert "twscrape" not in calls and calls[0] == "fx" and "replies" in calls
    assert len(data.media) == 1


def test_all_accounts_rate_limited_falls_through_without_waiting(tmp_path, monkeypatch):
    from unified_services import twitter as tw
    from unified_services.models import PostData

    async def accounts():
        return _accounts("a")

    async def fx(*a, **k):
        raise RuntimeError("fx down")

    async def twscrape(*a, **k):
        raise RuntimeError("No account available for queue TweetDetail")  # what raise_when_no_account does

    async def replies(status_id, data):
        return

    monkeypatch.setattr(tw, "active_accounts", accounts)
    monkeypatch.setattr(tw, "_via_fxtwitter", fx)
    monkeypatch.setattr(tw, "_via_twscrape", twscrape)
    monkeypatch.setattr(tw, "_fetch_replies", replies)
    monkeypatch.setattr(tw, "make_client", lambda: _Client())

    async def go():
        data = PostData(source_url="u", platform="twitter")
        await asyncio.wait_for(tw.fetch_twitter("https://x.com/u/status/5", tmp_path, data, 10**9), 5)
        return data

    assert asyncio.run(go()).media == []     # returns promptly -> downloader continues with yt-dlp


def test_cookie_files_rotate_and_skip_rate_limited_accounts(tmp_path, monkeypatch):
    from unified_services import twitter as tw

    async def accounts():
        return _accounts("a", "b", "c")

    monkeypatch.setattr(tw, "active_accounts", accounts)
    monkeypatch.setattr(tw, "_cooldown", {})
    monkeypatch.setattr(tw, "_rotation", 0)

    async def names():
        return [n for n, _ in await tw.cookie_files(tmp_path, limit=3)]

    first = asyncio.run(names())
    second = asyncio.run(names())
    assert first == ["a", "b", "c"] and second == ["b", "c", "a"]     # load is spread over the accounts

    tw.mark_cooldown("b")
    assert "b" not in asyncio.run(names())                              # limited account is skipped...
    path = asyncio.run(tw.cookie_files(tmp_path))[0][1]
    assert Path(path).read_text().count("auth_token") == 1 and "tok_" in Path(path).read_text()
    assert tw.looks_rate_limited("HTTP Error 429: Too Many Requests") and not tw.looks_rate_limited("not found")
