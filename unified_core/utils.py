"""Pure helper functions (no Telegram / network dependencies)."""
from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

# Invisible marker put in every message the bot sends itself, so the userbot
# never re-processes its own notifications (prevents infinite loops).
NOTIFY_MARK = "\u2063"

_DOMAINS = (
    r"t\.me|telegram\.me|instagram\.com|twitter\.com|x\.com|youtube\.com|youtu\.be|"
    r"tiktok\.com|facebook\.com|fb\.watch|pinterest\.com|pin\.it|threads\.net|threads\.com"
)
URL_REGEX = re.compile(r"https?://(?:[a-z0-9-]+\.)*(?:" + _DOMAINS + r")/\S*", re.IGNORECASE)
_TRAIL = ".,;:!?)]}>'\"»"

_PLATFORMS = (
    ("t.me", "telegram"), ("telegram.me", "telegram"),
    ("instagram.com", "instagram"),
    ("twitter.com", "twitter"), ("x.com", "twitter"),
    ("youtube.com", "youtube"), ("youtu.be", "youtube"),
    ("tiktok.com", "tiktok"),
    ("facebook.com", "facebook"), ("fb.watch", "facebook"),
    ("pinterest.com", "pinterest"), ("pin.it", "pinterest"),
    ("threads.net", "threads"), ("threads.com", "threads"),
)
_TRACKING = {
    "igsh", "igshid", "si", "fbclid", "feature", "ref", "ref_src", "ref_url",
    "mibextid", "share_id", "gclid", "s", "t",
}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def extract_urls(text: str, limit: int = 0) -> list[str]:
    """Return unique supported URLs found in `text` (order preserved)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_REGEX.finditer(text or ""):
        url = m.group(0).rstrip(_TRAIL)
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out[:limit] if limit else out


def split_url_and_caption(text: str, limit: int = 0) -> tuple[list[str], str | None]:
    """`<url> my caption` -> ([url], "my caption").  Several links (or no text after the link) -> (urls, None)."""
    matches = list(URL_REGEX.finditer(text or ""))
    if not matches:
        return [], None
    first = matches[0]
    tail = (text or "")[first.end():].strip()
    if tail and not URL_REGEX.search(tail):
        return [first.group(0).rstrip(_TRAIL)], tail
    return extract_urls(text, limit), None


def items_from_file(path: Path) -> list[tuple[str, str | None]]:
    """(url, custom_caption) pairs from a .json or plain text file.

    In JSON, an object holding a URL and a `custom_caption` field makes that text the post's caption.
    """
    raw = Path(path).read_text(encoding="utf-8-sig", errors="ignore")
    if str(path).lower().endswith(".json"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            found: list[tuple[str, str | None]] = []

            def walk(o) -> None:
                if isinstance(o, str):
                    found.extend((u, None) for u in extract_urls(o))
                elif isinstance(o, dict):
                    cap = o.get("custom_caption")
                    cap = cap.strip() if isinstance(cap, str) and cap.strip() else None
                    own: list[str] = []
                    for k, v in o.items():
                        if k != "custom_caption" and isinstance(v, str):
                            own.extend(extract_urls(v))
                    if cap and own:
                        found.append((own[0], cap))
                        for v in o.values():  # nested containers still get scanned
                            if isinstance(v, (dict, list)):
                                walk(v)
                    else:
                        for v in o.values():
                            walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)

            walk(data)
            seen: dict[str, tuple[str, str | None]] = {}
            for url, cap in found:
                key = normalize_url(url)
                if key not in seen or (cap and not seen[key][1]):
                    seen[key] = (url, cap)
            return list(seen.values())
    return [(u, None) for u in extract_urls(raw)]


def urls_from_file(path: Path) -> list[str]:
    """Extract URLs from a .json or plain text file."""
    return [u for u, _ in items_from_file(path)]


def normalize_url(url: str) -> str:
    """Canonical key used for de-duplication."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = re.sub(r"/{2,}", "/", p.path).rstrip("/")

    if host in ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com"):
        m = re.search(r"/status(?:es)?/(\d+)", path)
        if m:
            return f"x.com/status/{m.group(1)}"
        host = "x.com"
    elif host == "youtu.be":
        vid = path.lstrip("/").split("/")[0]
        return f"youtube.com/watch?v={vid}"
    elif host.endswith("youtube.com"):
        q = dict(parse_qsl(p.query))
        if "v" in q:
            return f"youtube.com/watch?v={q['v']}"
        host = "youtube.com"

    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(p.query)
        if k.lower() not in _TRACKING and not k.lower().startswith("utm_")
    ))
    return f"{host}{path}" + (f"?{query}" if query else "")


def platform_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for domain, name in _PLATFORMS:
        if host == domain or host.endswith("." + domain):
            return name
    return "generic"


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def clip_escaped(text: str, limit: int) -> str:
    """HTML-escape `text` and truncate so the escaped result is <= limit chars."""
    if limit <= 0 or not text:
        return ""
    escaped = esc(text)
    if len(escaped) <= limit:
        return escaped
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(esc(text[:mid])) + 1 <= limit:
            lo = mid
        else:
            hi = mid - 1
    return esc(text[:lo]).rstrip() + "…" if lo else ""


def is_mostly_bengali(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    bn = sum(1 for c in letters if "\u0980" <= c <= "\u09ff")
    return bn / len(letters) > 0.5


def short_error(exc: BaseException | str, limit: int = 300) -> str:
    msg = _ANSI.sub("", str(exc) or exc.__class__.__name__).strip().replace("\n", " ")
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


_FFMPEG: str | None = None
_FFMPEG_DONE = False


def ffmpeg_location() -> str | None:
    """Path to ffmpeg for yt-dlp: the system one if present, else the pip-bundled binary, else None."""
    global _FFMPEG, _FFMPEG_DONE
    if not _FFMPEG_DONE:
        _FFMPEG_DONE = True
        if shutil.which("ffmpeg"):
            _FFMPEG = None  # yt-dlp finds it on PATH by itself
        else:
            try:
                import imageio_ffmpeg

                _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                _FFMPEG = None
    return _FFMPEG
