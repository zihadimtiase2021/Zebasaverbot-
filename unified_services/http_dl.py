"""Resilient streaming HTTP downloads."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiofiles
import httpx

from unified_services.models import DownloadError

log = logging.getLogger("zeba.http")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=60.0),
        follow_redirects=True,
        headers={"User-Agent": UA},
    )


async def download_file(client: httpx.AsyncClient, url: str, dest: Path, max_bytes: int, retries: int = 3) -> Path:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                length = resp.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise DownloadError("File exceeds the upload size limit")
                size = 0
                async with aiofiles.open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise DownloadError("File exceeds the upload size limit")
                        await fh.write(chunk)
            if size == 0:
                raise DownloadError("Empty file received")
            return dest
        except DownloadError:
            dest.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            dest.unlink(missing_ok=True)
            log.debug("download attempt %d failed for %s: %s", attempt + 1, url, exc)
            await asyncio.sleep(1.5 * (attempt + 1))
    raise DownloadError(f"Download failed: {last}")
