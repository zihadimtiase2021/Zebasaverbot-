import asyncio
import subprocess
import types

import pytest
from PIL import Image

from unified_services import runtime_config as rc
from unified_services import watermark as wm


def cfg(**kw):
    base = dict(WM_TEXT="@zeba", WM_ENABLED=True, WM_ON_URL=True, WM_ON_FWD=False, WM_FONT="DejaVuSans-Bold",
                WM_SIZE=6, WM_TEXT_COLOR="#FFFFFF", WM_BG_COLOR="#112233", WM_BG_OPACITY=60, WM_OPACITY=90,
                WM_POSITION="br", WM_MARGIN=2, DATA_DIR=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_parsers():
    assert wm.parse_color("fc0") == "#FFCC00" and wm.parse_color("none", True) is None
    assert wm.parse_position("top-left") == "tl" and wm.parse_position("center") == "mc"
    assert wm.parse_position("bottom right") == "br"
    with pytest.raises(ValueError):
        wm.parse_color("nope")
    with pytest.raises(ValueError):
        wm.parse_position("middle-of-nowhere")


def test_is_active_per_source():
    c = cfg()
    assert wm.is_active(c, "bot") and wm.is_active(c, "url") and not wm.is_active(c, "fwd")
    assert not wm.is_active(cfg(WM_TEXT=""), "bot")


@pytest.mark.parametrize("pos", list(wm.POSITIONS))
def test_overlay_inside_picture(pos):
    st = wm.Style.from_cfg(cfg(WM_POSITION=pos))
    ov, x, y = wm.render_overlay(st, 800, 500)
    assert x >= 0 and y >= 0 and x + ov.width <= 800 and y + ov.height <= 500


def test_image_changes_and_keeps_size(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (640, 480), (10, 120, 200)).save(p)
    out = asyncio.run(wm.apply(p, cfg()))
    assert out.endswith("_wm.jpg") and not p.exists()
    im = Image.open(out)
    assert im.size == (640, 480) and im.getpixel((600, 450)) != (10, 120, 200)  # bottom-right badge drawn
    assert im.getpixel((5, 5)) == pytest.approx((10, 120, 200), abs=4)


def test_no_text_or_bad_file_returns_original(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGB", (50, 50)).save(p)
    assert asyncio.run(wm.apply(p, cfg(WM_TEXT=""))) == str(p)
    bad = tmp_path / "b.jpg"
    bad.write_bytes(b"not an image")
    assert asyncio.run(wm.apply(bad, cfg())) == str(bad)


@pytest.mark.skipif(not wm._ffmpeg(), reason="ffmpeg missing")
def test_video(tmp_path):
    v = tmp_path / "v.mp4"
    subprocess.run([wm._ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:d=1", str(v)], check=True)
    out = asyncio.run(wm.apply(v, cfg(), "video"))
    assert out.endswith("_wm.mp4") and not v.exists()
    assert asyncio.run(wm.video_size(wm._ffmpeg(), tmp_path / "v_wm.mp4")) == (320, 240)


class FakeRT:
    def __init__(self):
        self.cfg = cfg(WM_TEXT="")
        self.saved = {}

    async def save(self):
        pass

    set_toggle = rc.RuntimeConfig.set_toggle
    set_number = rc.RuntimeConfig.set_number
    set_string = rc.RuntimeConfig.set_string


def test_wm_command():
    rt = FakeRT()
    run = lambda a: asyncio.run(rc.wm_command(rt, a))  # noqa: E731
    assert "Set a text first" in run("on")
    assert "Saved" in run("text Hello\\nWorld") and rt.cfg.WM_TEXT == "Hello\nWorld"
    assert "ON" in run("on") and rt.cfg.WM_ENABLED
    assert "ON" in run("fwd on") and rt.cfg.WM_ON_FWD
    run("color red"); run("bg none"); run("pos top-left"); run("size 99")
    assert (rt.cfg.WM_TEXT_COLOR, rt.cfg.WM_BG_COLOR, rt.cfg.WM_POSITION, rt.cfg.WM_SIZE) == ("#FF0000", "none", "tl", 30)
    assert "unknown colour" in run("color zzz")
    assert run("preview") == "__PREVIEW__"
    run("reset")
    assert rt.cfg.WM_TEXT == "" and not rt.cfg.WM_ENABLED
