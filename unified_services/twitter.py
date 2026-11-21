"""X / Twitter source.

Order of attack (fastest first, every step is a fallback for the previous one):
  1. FxTwitter  - one unauthenticated HTTP call, no rate-limit locks -> media + caption
  2. twscrape   - your X accounts (any number); twscrape rotates between them, and when ALL of them are
                  rate-limited it raises immediately instead of waiting -> media + caption
  3. yt-dlp     - handled by downloader.py, rotating through the same X accounts' cookies
Replies (comments) are fetched in the background with twscrape while the media downloads/uploads, so they
never delay the post.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx

from unified_core.config import settings
from unified_core.utils import short_error
from unified_core.x_accounts import _info, netscape_from_cookie_dict
from unified_services.http_dl import download_file, make_client
from unified_services.models import Comment, DownloadError, MediaItem, PostData

log = logging.getLogger("zeba.twitter")

_STATUS_RE = re.compile(r"(?:twitter\.com|x\.com)/([^/]+)/status(?:es)?/(\d+)", re.IGNORECASE)
_RATE_LIMIT_RE = re.compile(r"429|rate.?limit|too many requests|temporarily locked", re.IGNORECASE)
_api = None
_cooldown: dict[str, float] = {}  # account -> monotonic time until which yt-dlp should not use it
_rotation = 0


def _get_api():
    global _api
    if _api is None:
        from twscrape import API  # lazy: optional dependency at import time

        try:  # fail fast when every account is rate-limited instead of sleeping until a limit resets
            _api = API(settings.TWSCRAPE_DB, raise_when_no_account=True)
        except TypeError:  # very old twscrape: no such option (timeouts below still protect us)
            _api = API(settings.TWSCRAPE_DB)
    return _api


async def active_accounts() -> list[dict]:
    """Active X accounts stored for twscrape (empty when none are configured)."""
    if not Path(settings.TWSCRAPE_DB).is_file():
        return []
    try:
        return [a for a in (_info(x) for x in await _get_api().pool.get_all()) if a["active"]]
    except Exception as exc:
        log.warning("could not read X accounts: %s", short_error(exc))
        return []


def looks_rate_limited(error: BaseException | str | None) -> bool:
    return bool(error) and bool(_RATE_LIMIT_RE.search(str(error)))


def mark_cooldown(username: str) -> None:
    _cooldown[username] = time.monotonic() + settings.X_COOLDOWN_SECONDS
    log.warning("X account @%s skipped for %ds (rate limit)", username, settings.X_COOLDOWN_SECONDS)


async def cookie_files(workdir: Path, limit: int = 3) -> list[tuple[str, Path]]:
    """cookies.txt files (inside `workdir`) for yt-dlp, one per healthy X account, rotating start point."""
    global _rotation
    now = time.monotonic()
    ready = [
        a for a in await active_accounts()
        if a["cookies"].get("auth_token") and a["cookies"].get("ct0") and _cooldown.get(a["username"], 0) <= now
    ]
    if not ready:
        return []
    start = _rotation % len(ready)
    _rotation += 1
    ordered = (ready[start:] + ready[:start])[:limit]
    out = []
    for i, a in enumerate(ordered):
        path = workdir / f"x_cookies_{i}.txt"
        path.write_text(netscape_from_cookie_dict(a["cookies"]), encoding="utf-8")
        out.append((a["username"], path))
    return out


def _ext_for(url: str, kind: str) -> str:
    if kind == "video":
        return ".mp4"
    m = re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$|:)", url, re.IGNORECASE)
    return "." + m.group(1).lower() if m else ".jpg"


async def _download_items(
    client: httpx.AsyncClient, items: list[tuple[str, str]], workdir: Path,
    prefix: str, max_bytes: int, data: PostData,
) -> None:
    """Download all media of a tweet in parallel (order is preserved)."""
    sem = asyncio.Semaphore(4)

    async def one(idx: int, kind: str, url: str) -> MediaItem | None:
        if kind == "photo" and "pbs.twimg.com/media" in url and "name=" not in url:
            url += ("&" if "?" in url else "?") + "name=orig"
        dest = workdir / f"{prefix}_{idx}{_ext_for(url, kind)}"
        async with sem:
            try:
                await download_file(client, url, dest, max_bytes)
                return MediaItem(dest, kind)
            except DownloadError as exc:
                log.warning("skip media %s: %s", url, exc)
                return None

    results = await asyncio.gather(*(one(i, k, u) for i, (k, u) in enumerate(items)))
    data.media.extend(r for r in results if r)


async def _via_twscrape(status_id: int, workdir: Path, client: httpx.AsyncClient, max_bytes: int, data: PostData) -> None:
    api = _get_api()
    tweet = await asyncio.wait_for(api.tweet_details(status_id), 45)
    if not tweet:
        raise DownloadError("Tweet not found via twscrape")

    data.caption = data.caption or tweet.rawContent or ""
    data.total_comments = data.total_comments or tweet.replyCount or 0

    src = getattr(tweet, "retweetedTweet", None) or tweet
    media = getattr(src, "media", None)
    items: list[tuple[str, str]] = []
    if media:
        items += [("photo", p.url) for p in (media.photos or [])]
        for v in media.videos or []:
            mp4 = [x for x in v.variants if (x.contentType or "").startswith("video/mp4")] or list(v.variants)
            if mp4:
                items.append(("video", max(mp4, key=lambda x: x.bitrate or 0).url))
        items += [("video", a.videoUrl) for a in (media.animated or [])]
    await _download_items(client, items, workdir, f"tw_{status_id}", max_bytes, data)


async def _fetch_replies(status_id: int, data: PostData) -> None:
    """Background task: fill data.comments. Never raises (comments are best-effort)."""
    from twscrape import gather

    try:
        replies = await asyncio.wait_for(
            gather(_get_api().tweet_replies(status_id, limit=settings.MAX_COMMENTS * 3)), 60
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.info("replies not fetched (all X accounts busy or rate-limited?): %s", short_error(exc))
        return
    for r in replies:
        if r.id == status_id or not (r.rawContent or "").strip():
            continue
        if r.inReplyToTweetId and r.inReplyToTweetId != status_id:
            continue
        author = r.user.username if getattr(r, "user", None) else ""
        data.comments.append(Comment(r.rawContent.strip(), author))
        if len(data.comments) >= settings.MAX_COMMENTS:
            break


async def _via_fxtwitter(username: str, status_id: str, workdir: Path, client: httpx.AsyncClient, max_bytes: int, data: PostData) -> None:
    resp = await client.get(f"https://api.fxtwitter.com/{username}/status/{status_id}")
    if resp.status_code != 200:
        raise DownloadError(f"FxTwitter HTTP {resp.status_code}")
    tweet = (resp.json() or {}).get("tweet") or {}
    if not data.caption:
        data.caption = tweet.get("text", "") or ""
    if not data.total_comments:
        data.total_comments = tweet.get("replies", 0) or 0

    media = tweet.get("media") or {}
    items: list[tuple[str, str]] = []
    if media.get("all"):
        for it in media["all"]:
            url = it.get("url")
            if url:
                items.append(("photo" if it.get("type") == "photo" else "video", url))
    else:
        items += [("video", v["url"]) for v in media.get("videos", []) if v.get("url")]
        items += [("photo", p["url"]) for p in media.get("photos", []) if p.get("url")]
    await _download_items(client, items, workdir, f"fx_{status_id}", max_bytes, data)


async def fetch_twitter(url: str, workdir: Path, data: PostData, max_bytes: int) -> None:
    m = _STATUS_RE.search(url)
    if not m:
        raise DownloadError("Unsupported X/Twitter link")
    username, status_id = m.group(1), m.group(2)

    accounts = await active_accounts()
    if accounts and settings.comments_on:  # runs alongside the media download, awaited by the pipeline later
        data.comments_task = asyncio.create_task(_fetch_replies(int(status_id), data), name=f"zeba-replies-{status_id}")

    async with make_client() as client:
        try:
            await _via_fxtwitter(username, status_id, workdir, client, max_bytes, data)
        except Exception as exc:
            log.info("fxtwitter failed: %s", short_error(exc))
        if not data.media and accounts:
            try:
                await _via_twscrape(int(status_id), workdir, client, max_bytes, data)
            except Exception as exc:  # includes "every account is rate-limited" -> falls through to yt-dlp
                log.warning("twscrape failed: %s", short_error(exc))
