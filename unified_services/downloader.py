"""Source dispatcher: Telegram / X (twscrape+FxTwitter) / yt-dlp for everything else."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
from pathlib import Path

import yt_dlp

from unified_core.config import settings
from unified_core.utils import ffmpeg_location, platform_of, short_error
from unified_services.models import Comment, DownloadError, MediaItem, PostData
from unified_services.telegram_source import fetch_telegram
from unified_services.twitter import cookie_files, fetch_twitter, looks_rate_limited, mark_cooldown

log = logging.getLogger("zeba.downloader")

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT
# yt-dlp leftovers of an interrupted download / merge: "<id>.f137.mp4" (one stream), "*.part", "*.ytdl", "*.temp.*"
_INTERMEDIATE_RE = re.compile(r"\.f[0-9A-Za-z_-]+$|\.temp$")
_CANCEL_WAIT = 15.0  # seconds a cancelled yt-dlp thread gets to stop before we give up waiting


def _is_final_media(p: Path) -> bool:
    """A finished media file (not a directory, not a half-downloaded / unmerged yt-dlp stream)."""
    return p.suffix.lower() in MEDIA_EXT and not _INTERMEDIATE_RE.search(p.stem) and p.is_file()


def _free_name(path: Path, suffix: str) -> Path:
    """`path` with another suffix that does not clash with an existing file (never overwrite a sibling)."""
    out = path.with_suffix(suffix)
    n = 0
    while out.exists():
        n += 1
        out = path.with_name(f"{path.stem}_n{n}{suffix}")
    return out


def _normalize_image_sync(path: Path) -> Path:
    """Convert webp / oversized images to Telegram-friendly JPEG (best effort)."""
    try:
        from PIL import Image
    except ImportError:
        return path
    out: Path | None = None
    try:
        with Image.open(path) as img:
            w, h = img.size
            too_big = path.stat().st_size > 9 * 1024 * 1024 or (w + h) > 9500
            if path.suffix.lower() != ".webp" and not too_big:
                return path
            if img.mode in ("RGBA", "LA", "P") and ("transparency" in img.info or img.mode != "P"):
                rgba = img.convert("RGBA")  # flatten transparency onto white (plain convert("RGB") makes it black)
                out_img = Image.new("RGB", rgba.size, (255, 255, 255))
                out_img.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
            else:
                out_img = img.convert("RGB")
        try:
            if too_big:
                out_img.thumbnail((2560, 2560))
            out = _free_name(path, ".jpg")
            out_img.save(out, "JPEG", quality=90)
        finally:
            out_img.close()
        return out
    except Exception as exc:
        log.warning("image normalize failed for %s: %s", path.name, exc)
        if out is not None:
            out.unlink(missing_ok=True)  # never leave a half-written file behind
        return path


def _ytdlp_opts(out_dir: Path, max_bytes: int, cookies: Path | None, fmt: str | None) -> dict:
    opts: dict = {
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "restrictfilenames": True,
        "cachedir": False,  # never write yt-dlp cache files to disk
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,  # avoids per-connection throttling (YouTube & co)
        "noplaylist": False,
        "playlist_items": "1-10",
        "max_filesize": max_bytes,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
    }
    if fmt:
        opts["format"] = fmt
    else:
        opts["format_sort"] = [f"res:{settings.MAX_VIDEO_HEIGHT}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
    if cookies:
        opts["cookiefile"] = str(cookies)
    ff = ffmpeg_location()
    if ff:
        opts["ffmpeg_location"] = ff
    if settings.YTDLP_FETCH_COMMENTS and settings.comments_on:
        n = str(settings.MAX_COMMENTS)
        opts["getcomments"] = True
        opts["extractor_args"] = {"youtube": {"max_comments": [n, n, "0", "0"]}}
    return opts


def _ytdlp_run(url: str, opts: dict, cancel: threading.Event | None = None):
    opts = dict(opts)
    if cancel is not None:
        cancelled = getattr(yt_dlp.utils, "DownloadCancelled", Exception)

        def _hook(_d) -> None:  # runs inside the download thread: the only way to stop yt-dlp mid-download
            if cancel.is_set():
                raise cancelled("job cancelled")

        opts["progress_hooks"] = [*opts.get("progress_hooks", []), _hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.sanitize_info(info) if info else None


async def _ytdlp_run_async(url: str, opts: dict):
    """Run yt-dlp in a thread that STOPS when the job is cancelled / times out.

    `asyncio.to_thread` cannot be interrupted: without this the thread would keep downloading into a work
    directory that the pipeline has already deleted, re-creating files nobody cleans up.
    """
    cancel = threading.Event()
    fut = asyncio.ensure_future(asyncio.to_thread(_ytdlp_run, url, opts, cancel))
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # mark the exception as retrieved
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        cancel.set()
        await asyncio.wait({fut}, timeout=_CANCEL_WAIT)  # let it unwind before the caller deletes the files
        raise


def _has_media(out_dir: Path) -> bool:
    return any(_is_final_media(p) for p in out_dir.iterdir())


async def _cookie_candidates(url: str, workdir: Path, data: PostData) -> list[tuple[str | None, Path | None]]:
    """(x_account_or_None, cookies.txt) pairs to try, in order. yt-dlp rewrites its jar -> always private copies."""
    out: list[tuple[str | None, Path | None]] = []
    if data.platform == "twitter":  # each healthy X account is a fallback for the previous one
        out += [(name, path) for name, path in await cookie_files(workdir)]
    src = settings.cookies_path
    if src:
        shared = workdir / "cookies.txt"
        try:
            await asyncio.to_thread(shutil.copy, src, shared)
            out.append((None, shared))
        except OSError as exc:  # optional file: a missing / unreadable one must not block the download
            log.warning("cookies file %s unusable, continuing without it: %s", src, exc)
    return out or [(None, None)]


async def _fetch_ytdlp(url: str, workdir: Path, max_bytes: int, data: PostData) -> None:
    out_dir = workdir / "ytdlp"
    out_dir.mkdir(exist_ok=True)

    info, last_err = None, None
    for account, cookies in await _cookie_candidates(url, workdir, data):
        for fmt in (None, "best"):
            try:
                info = await _ytdlp_run_async(url, _ytdlp_opts(out_dir, max_bytes, cookies, fmt))
                if info:
                    break
            except Exception as exc:
                last_err = exc
                log.info("yt-dlp attempt (format=%s, x_account=%s) failed: %s", fmt, account, short_error(exc))
                if account and looks_rate_limited(exc):
                    mark_cooldown(account)
                    break  # this account is limited: go straight to the next one
        if info or _has_media(out_dir):
            break
    if info is None and not _has_media(out_dir):  # (only stray .part files don't count: report the real error)
        raise DownloadError(short_error(last_err) if last_err else "yt-dlp returned nothing")

    # Metadata
    first = info
    if info and info.get("entries"):
        first = next((e for e in info["entries"] if e), info)
    if info and not data.caption:
        if data.platform == "youtube":
            data.caption = (first.get("title") or "") if first else ""
        else:
            data.caption = (first.get("description") or first.get("title") or "") if first else ""
    if info and not data.total_comments:
        data.total_comments = (first or {}).get("comment_count") or info.get("comment_count") or 0
    if info and not data.comments and settings.YTDLP_FETCH_COMMENTS:
        for c in ((first or {}).get("comments") or [])[: settings.MAX_COMMENTS]:
            if c.get("text"):
                data.comments.append(Comment(c["text"].strip(), c.get("author") or ""))

    # Media files, in playlist / carousel order (file names alone would sort them alphabetically)
    order = {str(e.get("id")): i for i, e in enumerate((info or {}).get("entries") or []) if e and e.get("id")}
    files = sorted(out_dir.iterdir(), key=lambda p: (order.get(p.stem, len(order)), p.name))
    for p in files:
        ext = p.suffix.lower()
        if not _is_final_media(p) or p.stat().st_size == 0:
            continue
        if ext in VIDEO_EXT:
            data.media.append(MediaItem(p, "video"))
        elif ext in IMAGE_EXT:
            p = await asyncio.to_thread(_normalize_image_sync, p)
            data.media.append(MediaItem(p, "photo"))


async def fetch_post(url: str, workdir: Path, userbot, max_bytes: int) -> PostData:
    platform = platform_of(url)
    data = PostData(source_url=url, platform=platform)

    try:
        if platform == "telegram":
            await fetch_telegram(url, workdir, userbot, max_bytes, data)
        else:
            if platform == "twitter":
                try:
                    await fetch_twitter(url, workdir, data, max_bytes)
                except DownloadError as exc:
                    log.warning("twitter source: %s", exc)
            if not data.media:
                await _fetch_ytdlp(url, workdir, max_bytes, data)

        if not data.media:
            raise DownloadError("No downloadable media found at this URL")
    except BaseException:
        task = data.comments_task
        if task is not None:  # nobody will consume the replies
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())  # no "never retrieved" noise
        raise
    return data
