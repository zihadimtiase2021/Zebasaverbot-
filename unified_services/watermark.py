"""Text watermark for photos and videos (Pillow for images, ffmpeg for videos).

Everything is driven by the WM_* settings, which can be changed live (.wm command, /admin panel):
text, font, size, text colour, background colour/opacity, position, margin.
`apply()` never raises: if anything goes wrong the original file is used, so a post is never lost.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

log = logging.getLogger("zeba.watermark")

POSITIONS = {"tl": "Top left", "tc": "Top center", "tr": "Top right",
             "ml": "Middle left", "mc": "Center", "mr": "Middle right",
             "bl": "Bottom left", "bc": "Bottom center", "br": "Bottom right"}
_POS_WORDS = {"top": "t", "bottom": "b", "middle": "m", "center": "c", "centre": "c", "left": "l", "right": "r"}
FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "/Library/Fonts", "/System/Library/Fonts",
             "C:/Windows/Fonts")
DEFAULT_FONTS = ("DejaVuSans-Bold", "DejaVuSans", "LiberationSans-Bold", "Arial")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif", ".3gp", ".ts"}
SOURCES = {"url": "WM_ON_URL", "fwd": "WM_ON_FWD", "bot": "WM_ENABLED"}


# ------------------------------------------------------------------ parsing helpers
def parse_color(value: str, allow_none: bool = False) -> str | None:
    """'#fff' / 'ffcc00' / 'red' -> '#RRGGBB'. 'none' -> None when allowed. ValueError otherwise."""
    v = (value or "").strip().lower()
    if allow_none and v in ("none", "off", "no", "transparent", ""):
        return None
    if re.fullmatch(r"[0-9a-f]{3}|[0-9a-f]{6}", v):
        v = "#" + v
    try:
        r, g, b = ImageColor.getrgb(v)[:3]
    except ValueError:
        raise ValueError(f"unknown colour '{value}' - use a name (white) or hex (#ffcc00)") from None
    return f"#{r:02X}{g:02X}{b:02X}"


def parse_position(value: str) -> str:
    v = (value or "").strip().lower().replace("-", " ").replace("_", " ")
    if v in POSITIONS:
        return v
    code = "".join(_POS_WORDS.get(w, "?") for w in v.split())
    if code == "c":
        code = "mc"
    if len(code) == 2 and code[0] in "tmb" and code[1] in "lcr":
        return code if code in POSITIONS else "mc"
    if len(code) == 2 and code[0] in "lcr" and code[1] in "tmb":  # "left top"
        return code[1] + code[0]
    raise ValueError("position: tl tc tr / ml mc mr / bl bc br  (or: top-left, bottom right, center)")


_font_index: dict[str, str] | None = None


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", s).lower()


def _font_dirs(data_dir=None) -> list[Path]:
    dirs = [Path(d).expanduser() for d in FONT_DIRS]
    if data_dir:
        dirs.insert(0, Path(data_dir) / "fonts")
    return [d for d in dirs if d.is_dir()]


def font_index(data_dir=None, refresh: bool = False) -> dict[str, str]:
    """normalised font file name -> path (own fonts in DATA_DIR/fonts win over system fonts)."""
    global _font_index
    if _font_index is None or refresh:
        idx: dict[str, str] = {}
        for d in reversed(_font_dirs(data_dir)):
            for p in d.rglob("*"):
                if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    idx[_norm(p.stem)] = str(p)
        _font_index = idx
    return _font_index


def list_fonts(data_dir=None) -> list[str]:
    names = sorted({Path(p).stem for p in font_index(data_dir).values()}, key=str.lower)
    return names


def resolve_font(name: str, data_dir=None) -> str | None:
    """Font name (or file path) -> file path. Exact name first, then a unique partial match."""
    if name and Path(name).is_file():
        return name
    idx = font_index(data_dir)
    key = _norm(name or "")
    if key in idx:
        return idx[key]
    part = [p for k, p in idx.items() if key and key in k]
    if part:
        return sorted(part, key=len)[0]
    for d in DEFAULT_FONTS:
        if _norm(d) in idx:
            return idx[_norm(d)]
    return None


def _load_font(path: str | None, size: int):
    try:
        if path:
            return ImageFont.truetype(path, size)
    except OSError as exc:
        log.warning("font %s not usable: %s", path, exc)
    return ImageFont.load_default(size)


# ------------------------------------------------------------------ style
@dataclass
class Style:
    text: str = ""
    font: str = "DejaVuSans-Bold"
    size: int = 5            # font height in % of the media width
    color: str = "#FFFFFF"
    bg: str | None = "#000000"
    bg_opacity: int = 50     # 0-100
    opacity: int = 90        # text opacity 0-100
    position: str = "br"
    margin: int = 2          # % of the media width
    angle: int = 0           # degrees, counter-clockwise
    data_dir: object = None

    @classmethod
    def from_cfg(cls, cfg) -> "Style":
        bg = getattr(cfg, "WM_BG_COLOR", "#000000") or None
        return cls(text=getattr(cfg, "WM_TEXT", "") or "", font=getattr(cfg, "WM_FONT", cls.font),
                   size=int(getattr(cfg, "WM_SIZE", 5)), color=getattr(cfg, "WM_TEXT_COLOR", "#FFFFFF"),
                   bg=None if str(bg).lower() == "none" else bg, bg_opacity=int(getattr(cfg, "WM_BG_OPACITY", 50)),
                   opacity=int(getattr(cfg, "WM_OPACITY", 90)), position=getattr(cfg, "WM_POSITION", "br"),
                   margin=int(getattr(cfg, "WM_MARGIN", 2)), angle=int(getattr(cfg, "WM_ANGLE", 0)), data_dir=getattr(cfg, "DATA_DIR", None))


def is_active(cfg, source: str) -> bool:
    """source: 'url' (downloaded links) | 'fwd' (forwarded chats) | 'bot' (media sent to the bot)."""
    return bool((getattr(cfg, "WM_TEXT", "") or "").strip() and getattr(cfg, SOURCES[source], False))


# ------------------------------------------------------------------ rendering
def render_overlay(style: Style, base_w: int, base_h: int) -> tuple[Image.Image, int, int]:
    """RGBA badge (text on an optional translucent box) and its x, y on a base_w x base_h picture."""
    px = max(10, round(base_w * max(1, style.size) / 100))
    font = _load_font(resolve_font(style.font, style.data_dir), px)
    lines = style.text.strip().splitlines() or [""]
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    boxes = [probe.textbbox((0, 0), ln or " ", font=font) for ln in lines]
    lh = max(b[3] - b[1] for b in boxes)
    gap = round(px * 0.25)
    tw = max(b[2] - b[0] for b in boxes)
    th = lh * len(lines) + gap * (len(lines) - 1)
    pad = round(px * 0.45) if style.bg else 0
    w, h = tw + 2 * pad, th + 2 * pad
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if style.bg:
        r, g, b = ImageColor.getrgb(style.bg)[:3]
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=round(px * 0.3),
                            fill=(r, g, b, round(255 * max(0, min(100, style.bg_opacity)) / 100)))
    r, g, b = ImageColor.getrgb(style.color)[:3]
    alpha = round(255 * max(0, min(100, style.opacity)) / 100)
    for i, (ln, bb) in enumerate(zip(lines, boxes)):
        lw = bb[2] - bb[0]
        x = pad + {"l": 0, "c": (tw - lw) // 2, "r": tw - lw}[style.position[1]] - bb[0]
        d.text((x, pad + i * (lh + gap) - boxes[0][1]), ln, font=font, fill=(r, g, b, alpha))
    if style.angle % 360:
        img = img.rotate(style.angle, resample=Image.BICUBIC, expand=True)
        w, h = img.size
    m = round(base_w * max(0, style.margin) / 100)
    v, hz = style.position[0], style.position[1]
    x = {"l": m, "c": (base_w - w) // 2, "r": base_w - w - m}[hz]
    y = {"t": m, "m": (base_h - h) // 2, "b": base_h - h - m}[v]
    return img, max(0, x), max(0, y)


def watermark_image(src: Path, dst: Path, style: Style) -> Path:
    with Image.open(src) as im:
        fmt = (im.format or "JPEG").upper()
        im = ImageOps.exif_transpose(im).convert("RGBA")
    ov, x, y = render_overlay(style, *im.size)
    im.alpha_composite(ov, (x, y))
    if fmt in ("JPEG", "MPO"):
        out = dst.with_suffix(".jpg")
        im.convert("RGB").save(out, "JPEG", quality=95, subsampling=0)
    elif fmt == "WEBP":
        out = dst.with_suffix(".webp")
        im.save(out, "WEBP", quality=95)
    else:
        out = dst.with_suffix(".png")
        im.save(out, "PNG")
    return out


def _ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


async def _run(args: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "timeout"
    return proc.returncode or 0, err.decode("utf-8", "ignore")


async def video_size(ffmpeg: str, src: Path) -> tuple[int, int]:
    """Displayed (rotation-aware) width, height of the first video stream, parsed from `ffmpeg -i`."""
    _, err = await _run([ffmpeg, "-hide_banner", "-i", str(src)], 60)
    line = next((ln for ln in err.splitlines() if "Video:" in ln), "")
    m = re.search(r"[,\s](\d{2,5})x(\d{2,5})[,\s\[]", line + " ")
    if not m:
        raise RuntimeError("could not read the video size")
    w, h = int(m.group(1)), int(m.group(2))
    rot = re.search(r"rotat\w*\s*(?:of|:)?\s*(-?\d+)", err)
    if rot and abs(int(rot.group(1))) % 180 == 90:
        w, h = h, w
    return w, h


async def watermark_video(src: Path, dst: Path, style: Style, timeout: float = 1500) -> Path:
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    w, h = await video_size(ff, src)
    ov, x, y = render_overlay(style, w, h)
    png = dst.with_suffix(".wm.png")
    ov.save(png)
    out = dst.with_suffix(".mp4")
    flt = f"[0:v][1:v]overlay={x}:{y}:format=auto,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[v]"
    base = [ff, "-hide_banner", "-y", "-i", str(src), "-i", str(png), "-filter_complex", flt, "-map", "[v]",
            "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
    tail = ["-movflags", "+faststart", str(out)]
    try:
        code, err = await _run(base + ["-c:a", "copy"] + tail, timeout)
        if code != 0:  # e.g. audio codec that mp4 cannot hold: re-encode the audio
            code, err = await _run(base + ["-c:a", "aac", "-b:a", "128k"] + tail, timeout)
        if code != 0 or not out.exists():
            raise RuntimeError("ffmpeg failed: " + err.strip().splitlines()[-1] if err.strip() else "ffmpeg failed")
    finally:
        png.unlink(missing_ok=True)
    return out


async def apply(path: str | Path, cfg=None, kind: str | None = None) -> str:
    """Watermark one file. Returns the new file's path (the original is deleted), or the original path on failure."""
    if cfg is None:
        from unified_core.config import settings as cfg
    src = Path(path)
    style = Style.from_cfg(cfg)
    if not style.text.strip():
        return str(src)
    ext = src.suffix.lower()
    dst = src.with_name(src.stem + "_wm")
    try:
        if ext in IMG_EXT and kind not in ("video", "gif"):
            out = await asyncio.to_thread(watermark_image, src, dst, style)
        elif ext in VID_EXT or kind in ("video", "gif", "video_note"):
            out = await watermark_video(src, dst, style)
        else:
            return str(src)
    except Exception as exc:
        log.warning("watermark skipped for %s: %s", src.name, exc)
        return str(src)
    if out != src:
        src.unlink(missing_ok=True)
    return str(out)


def sample_preview(cfg) -> bytes:
    """A JPEG sample (1280x720) showing the current style - for the .wm preview command."""
    import io

    base = Image.new("RGB", (1280, 720))
    px = base.load()
    for yy in range(720):
        for xx in range(0, 1280):
            px[xx, yy] = (40 + xx * 90 // 1280, 90 + yy * 90 // 720, 150 - xx * 60 // 1280)
    base = base.convert("RGBA")
    st = Style.from_cfg(cfg)
    st.text = st.text or "Your watermark"
    ov, x, y = render_overlay(st, 1280, 720)
    base.alpha_composite(ov, (x, y))
    buf = io.BytesIO()
    base.convert("RGB").save(buf, "JPEG", quality=88)
    return buf.getvalue()
