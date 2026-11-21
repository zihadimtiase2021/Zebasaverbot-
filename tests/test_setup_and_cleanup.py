"""Tests for the setup wizard helpers, temp cleanup and the delete-after-upload guarantee."""
import asyncio
import os
import time
import types
from pathlib import Path

import pytest

from unified_core import cleanup, setup_wizard as sw
from unified_core.tg import parse_chat_ref
from unified_core.x_accounts import cookies_from_netscape, has_x_session, netscape_for_x


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(sw, "ENV_FILE", path)
    for k in ("API_ID", "API_HASH", "TARGET_CHANNEL", "OPENAI_API_KEY", "CONTROL_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return path


def test_env_roundtrip_and_in_place_update(env_file):
    sw.save("API_ID", 123)
    sw.save("OPENAI_API_KEY", "key with space # and hash")
    sw.save("API_ID", 456)  # replaced, not duplicated
    text = env_file.read_text()
    assert text.count("API_ID=") == 1
    assert sw.read_env_file()["OPENAI_API_KEY"] == "key with space # and hash"
    assert sw.effective("API_ID") == "456"
    if os.name == "posix":
        assert (env_file.stat().st_mode & 0o777) == 0o600


def test_placeholders_and_comment_only_values_count_as_unset(env_file):
    env_file.write_text(
        "API_ID=your_api_id\nAPI_HASH=\nCONTROL_BOT_TOKEN=   # optional admin bot\n"
        "TARGET_CHANNEL=-100123  # inline comment\n"
    )
    assert sw.effective("API_ID") == ""
    assert sw.effective("API_HASH") == ""
    assert sw.effective("CONTROL_BOT_TOKEN") == ""
    assert sw.effective("TARGET_CHANNEL") == "-100123"


def test_real_environment_wins_over_file(env_file, monkeypatch):
    env_file.write_text("API_ID=1\n")
    monkeypatch.setenv("API_ID", "2")
    assert sw.effective("API_ID") == "2"


def test_validators():
    assert sw._v_phone("+880 1712-345678") == "+8801712345678"
    assert sw._v_phone("8801712345678") == "+8801712345678"
    for bad in ("abc", "12"):
        with pytest.raises(ValueError):
            sw._v_phone(bad)
    assert sw._v_api_hash("A" * 32) == "a" * 32
    with pytest.raises(ValueError):
        sw._v_api_hash("short")
    with pytest.raises(ValueError):
        sw._v_api_id("12ab")
    assert sw._v_bot_token("123456789:" + "A" * 35)
    with pytest.raises(ValueError):
        sw._v_bot_token("nope")
    assert sw._v_ids("1, 2 3") == "1,2,3"
    with pytest.raises(ValueError):
        sw._v_cookie("auth_token=abc")


def test_parse_chat_ref():
    assert parse_chat_ref("@MyChannel") == "MyChannel"
    assert parse_chat_ref("https://t.me/MyChannel/55") == "MyChannel"
    assert parse_chat_ref("https://t.me/c/1234567/9") == -1001234567
    assert parse_chat_ref("-1001234567890") == -1001234567890
    assert parse_chat_ref("4364751190") == 4364751190
    with pytest.raises(ValueError):
        parse_chat_ref("https://t.me/+AbCdEf")
    with pytest.raises(ValueError):
        parse_chat_ref("!!")


def test_missing_required_lists_everything(env_file, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    missing = " ".join(sw.missing_required())
    assert "API_ID" in missing and "API_HASH" in missing and "TARGET_CHANNEL" in missing and "session" in missing


def test_ensure_ready_without_terminal_fails_cleanly(env_file, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setattr(sw, "interactive", lambda: False)
    with pytest.raises(SystemExit) as e:
        sw.ensure_ready()
    assert e.value.code == 2
    assert "API_ID" in capsys.readouterr().err


def test_x_cookie_helpers(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(netscape_for_x("a" * 40, "b" * 40) + "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tNID\tzzz\n")
    header = cookies_from_netscape(f)
    assert has_x_session(header) and "NID" not in header


def test_safe_rmtree_and_purge(tmp_path):
    d = tmp_path / "job"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "v.mp4").write_bytes(b"1" * 100)
    assert cleanup.dir_size(d) == 100
    assert cleanup.safe_rmtree(d) is True and not d.exists()
    assert cleanup.safe_rmtree(d) is True  # idempotent

    tmp = tmp_path / "tmp"
    old, new = tmp / "old", tmp / "new"
    old.mkdir(parents=True)
    new.mkdir()
    (old / "x").write_text("x")
    past = time.time() - 10_000
    os.utime(old, (past, past))
    assert cleanup.purge_tmp(tmp, older_than=3600) == 1
    assert not old.exists() and new.exists()
    assert cleanup.purge_tmp(tmp) == 1 and not any(tmp.iterdir())


# ---------------------------------------------------------------- pipeline
def _pipeline_env(tmp_path, monkeypatch):
    pytest.importorskip("telethon")
    pytest.importorskip("pydantic_settings")
    pytest.importorskip("aiosqlite")
    from unified_core.config import settings
    from unified_services import pipeline as pl
    from unified_services.models import MediaItem, PostData

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "MAX_COMMENTS", 5)
    return settings, pl, MediaItem, PostData


class _Translator:
    async def translate(self, text):
        return None


def test_files_deleted_right_after_successful_upload(tmp_path, monkeypatch):
    settings, pl, MediaItem, PostData = _pipeline_env(tmp_path, monkeypatch)
    from unified_core.database import Database
    from unified_services.models import Comment

    state = {}

    async def fake_fetch(url, workdir, userbot, max_bytes):
        f = workdir / "a.mp4"
        f.write_bytes(b"x" * 1000)
        return PostData(source_url=url, platform="generic", caption="hi",
                        media=[MediaItem(f, "video")], comments=[Comment("nice", "bob")])

    class Bot:
        async def send_file(self, target, file, **kw):
            state["file_existed_during_upload"] = Path(file).exists()
            return types.SimpleNamespace(id=7)

    async def fake_comments(self, msg, tag, comments):
        state["tmp_left_when_commenting"] = [p.name for p in settings.tmp_dir.rglob("*")]
        return 1

    monkeypatch.setattr(pl, "fetch_post", fake_fetch)
    monkeypatch.setattr(pl.Pipeline, "_post_comments", fake_comments)
    monkeypatch.setattr(pl, "db", Database())

    async def go():
        await pl.db.connect(":memory:")
        try:
            p = pl.Pipeline(Bot(), None, None, False, _Translator())
            res = await p.run(pl.Job(url="https://example.org/video"))
            state["comments_posted_before_job_returned"] = "tmp_left_when_commenting" in state
            await p.drain(5)  # comments are posted in the background after the job is done
            return res
        finally:
            await pl.db.close()

    res = asyncio.run(go())
    assert res.status == "done" and res.comments == 1
    assert state["file_existed_during_upload"] is True
    assert state["tmp_left_when_commenting"] == []          # already gone before comments
    assert not any(settings.tmp_dir.rglob("*"))              # and nothing left afterwards


def test_files_deleted_when_upload_fails(tmp_path, monkeypatch):
    settings, pl, MediaItem, PostData = _pipeline_env(tmp_path, monkeypatch)
    from unified_core.database import Database

    async def fake_fetch(url, workdir, userbot, max_bytes):
        f = workdir / "a.mp4"
        f.write_bytes(b"x" * 1000)
        return PostData(source_url=url, platform="generic", media=[MediaItem(f, "video")])

    class Bot:
        async def send_file(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(pl, "fetch_post", fake_fetch)
    monkeypatch.setattr(pl, "db", Database())

    async def go():
        await pl.db.connect(":memory:")
        try:
            p = pl.Pipeline(Bot(), None, None, False, _Translator())
            return await p.run(pl.Job(url="https://example.org/video"))
        finally:
            await pl.db.close()

    res = asyncio.run(go())
    assert res.status == "failed"
    assert not any(settings.tmp_dir.rglob("*"))


# ------------------------------------------------------------ batch files
def test_json_custom_caption(tmp_path):
    from unified_core.utils import items_from_file

    f = tmp_path / "b.json"
    f.write_text(
        '[{"url": "https://x.com/a/status/1?s=20", "custom_caption": "  My caption  "},'
        ' {"link": "https://www.instagram.com/reel/ABC/", "custom_caption": ""},'
        ' {"url": "https://x.com/b/status/2"}, "https://youtu.be/xyz",'
        ' {"url": "https://twitter.com/a/status/1", "note": "dup"}]',
        encoding="utf-8",
    )
    got = dict(items_from_file(f))
    assert got["https://x.com/a/status/1?s=20"] == "My caption"   # duplicate keeps the caption
    assert got["https://www.instagram.com/reel/ABC/"] is None      # empty caption ignored
    assert got["https://x.com/b/status/2"] is None
    assert len(got) == 4

    t = tmp_path / "b.txt"
    t.write_text("https://x.com/a/status/9 and https://x.com/a/status/9")
    assert items_from_file(t) == [("https://x.com/a/status/9", None)]


def test_custom_caption_replaces_scraped_caption(tmp_path, monkeypatch):
    settings, pl, MediaItem, PostData = _pipeline_env(tmp_path, monkeypatch)
    from unified_core.database import Database

    seen = {}

    async def fake_fetch(url, workdir, userbot, max_bytes):
        f = workdir / "a.jpg"
        f.write_bytes(b"x")
        return PostData(source_url=url, platform="generic", caption="SCRAPED", media=[MediaItem(f, "photo")])

    class Bot:
        async def send_file(self, target, file, caption=None, **kw):
            seen["caption"] = caption
            return types.SimpleNamespace(id=1)

    class Boom:
        async def translate(self, text):
            raise AssertionError("must not translate a custom caption")

    monkeypatch.setattr(pl, "fetch_post", fake_fetch)
    monkeypatch.setattr(pl, "db", Database())

    async def go():
        await pl.db.connect(":memory:")
        try:
            p = pl.Pipeline(Bot(), None, None, False, Boom())
            return await p.run(pl.Job(url="https://example.org/p", custom_caption="Hello <b>& co"))
        finally:
            await pl.db.close()

    assert asyncio.run(go()).status == "done"
    assert "Hello &lt;b&gt;&amp; co" in seen["caption"] and "SCRAPED" not in seen["caption"]


def test_all_settings_attributes_used_in_code_exist():
    """Regression: code once used settings.twscrape_db while the field is TWSCRAPE_DB."""
    import re

    pytest.importorskip("pydantic_settings")
    from unified_core.config import Settings

    root = Path(__file__).resolve().parent.parent
    known = set(Settings.model_fields) | {n for n in dir(Settings) if not n.startswith("_")}
    for py in list(root.glob("unified_*/**/*.py")) + [root / "unified_main.py"]:
        for attr in re.findall(r"\bsettings\.(\w+)", py.read_text()):
            assert attr in known, f"{py.name}: settings.{attr} does not exist"


def test_notifications_prefer_control_bot_with_saved_messages_fallback(tmp_path, monkeypatch):
    settings, pl, _, _ = _pipeline_env(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "ADMIN_IDS", "11,22")
    monkeypatch.setattr(settings, "NOTIFY_SAVED_MESSAGES", True)
    sent = {"bot": [], "me": []}

    class Bot:
        async def send_message(self, uid, text, **kw):
            if uid == 22:
                raise RuntimeError("user has not started the bot")
            sent["bot"].append(uid)

    class User:
        async def send_message(self, to, text, **kw):
            sent["me"].append(to)

    async def go():
        p = pl.Pipeline(User(), None, None, False, _Translator())
        await p.notify("a")                      # no bot yet -> Saved Messages
        p.control_bot = Bot()
        await p.notify("b")                      # bot reaches admin 11 -> no fallback
        monkeypatch.setattr(settings, "ADMIN_IDS", "22")
        await p.notify("c")                      # bot cannot reach anyone -> fallback

    asyncio.run(go())
    assert sent["bot"] == [11] and sent["me"] == ["me", "me"]
