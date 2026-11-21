import asyncio
import os
import types
from pathlib import Path

from PIL import Image

try:
    import pydantic_settings  # noqa: F401
except ImportError:  # settings module cannot load without its dependency: stand-in, tests pass their own cfg
    import sys
    sys.modules.setdefault("unified_core.config", types.SimpleNamespace(settings=types.SimpleNamespace()))

from unified_services import banwords as bw
from unified_services import watermark as wm


def E(offset, length, url=None):
    return types.SimpleNamespace(offset=offset, length=length, url=url)


def test_ban_words_urls_and_mentions():
    raw = "Hello https://spam.com/x?a=1, nice join now bold\nvisit @spammer\nkeep this"
    ents = [E(raw.index("bold"), 4)]
    out, e = bw.strip_banned(raw, ents, ["spam.com", "join now", "@spammer"])
    assert out == "Hello, nice bold\nvisit\nkeep this"
    assert out[e[0].offset:e[0].offset + e[0].length] == "bold"


def test_ban_whole_word_only_and_untouched():
    assert bw.strip_banned("class spam spammer", None, ["spam"])[0] == "class spammer"
    raw = "nothing here"
    assert bw.strip_banned(raw, None, ["zzz"])[0] is raw
    assert bw.strip_banned("SPAM only", None, ["spam"])[0] == "only"
    assert bw.strip_banned("spam", None, ["spam"])[0] == ""


def test_ban_hidden_link_removed():
    raw = "click here now"
    out, e = bw.strip_banned(raw, [E(6, 4, "https://bad.example/ref")], ["bad.example"])
    assert out == "click now" and not e


def test_split_bans():
    assert bw.split_bans("a, b\nc d,A") == ["a", "b", "c d"]


def test_rotate_changes_overlay_size():
    st = wm.Style(text="Hello", size=8)
    flat, _, _ = wm.render_overlay(st, 800, 500)
    st.angle = 90
    rot, x, y = wm.render_overlay(st, 800, 500)
    assert (rot.width, rot.height) == (flat.height, flat.width)
    assert x + rot.width <= 800 and y + rot.height <= 500


class Msg:
    def __init__(self, i, text=""):
        self.id, self.raw_text, self.media = i, text, True
        self.photo = True
        self.document = self.video = self.gif = self.video_note = self.sticker = None


class Client:
    def __init__(self):
        self.sent = []

    async def download_media(self, m, file):
        p = Path(file) / f"{m.id}.jpg"
        Image.new("RGB", (300, 200), (200, 50, 50)).save(p)
        return str(p)

    async def send_file(self, dest, files, **kw):
        self.sent.append((dest, files, kw))


def test_album_is_sent_as_one_watermarked_album(tmp_path, monkeypatch):
    from unified_services import wm_handlers as h

    cfg = types.SimpleNamespace(WM_TEXT="@zeba", tmp_dir=tmp_path, DATA_DIR=None, WM_FONT="DejaVuSans-Bold",
                                WM_SIZE=6, WM_TEXT_COLOR="#FFFFFF", WM_BG_COLOR="#000000", WM_BG_OPACITY=50,
                                WM_OPACITY=90, WM_POSITION="br", WM_MARGIN=2, WM_ANGLE=0)
    monkeypatch.setattr(h, "settings", cfg)
    seen = {}
    real_apply = wm.apply

    async def spy(path, c=None, kind=None):
        out = await real_apply(path, cfg, kind)
        seen.setdefault("n", []).append(out)
        return out
    monkeypatch.setattr(h.watermark, "apply", spy)
    cl = Client()
    err = asyncio.run(h.process_media(cl, [Msg(3, "cap"), Msg(1), Msg(2)], 5, mark="⁣"))
    assert err == "" and len(seen["n"]) == 3
    (dest, files, kw), = cl.sent
    assert dest == 5 and len(files) == 3 and kw["caption"] == "⁣cap"
    assert all(f.endswith("_wm.jpg") for f in seen["n"])
