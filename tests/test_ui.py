"""Shared UI helpers: navigation rows, confirm rows, progress bar, popup text (telethon is stubbed when missing)."""
import sys
import types

try:
    import telethon  # noqa: F401
except ImportError:
    t = types.ModuleType("telethon")
    t.Button = types.SimpleNamespace(inline=lambda text, data=None: (text, data))
    sys.modules["telethon"] = t

from unified_services import ui  # noqa: E402


def test_nav_rows_have_a_fixed_order():
    assert ui.nav_row(b"m:wm") == [(ui.L_BACK, b"m:wm"), (ui.L_MAIN, ui.D_MAIN)]
    assert ui.nav_row(None) == [(ui.L_MAIN, ui.D_MAIN)]
    assert [b[0] for b in ui.nav_row(b"x", close=True)] == [ui.L_BACK, ui.L_MAIN, ui.L_CLOSE]
    assert ui.cancel_row() == [(ui.L_CANCEL, b"m:cancel")]


def test_confirm_and_pager_rows():
    yes, no = ui.confirm_row("🗑 Yes, delete", "x:y", b"x:n")
    assert yes == ("🗑 Yes, delete", b"x:y") and no == (ui.L_CANCEL, b"x:n")
    assert [b[1] for b in ui.pager_row("m:fp:a:", 0, 3)] == [b"m:noop", b"m:fp:a:1"]
    assert [b[1] for b in ui.pager_row("m:fp:a:", 2, 3)] == [b"m:fp:a:1", b"m:noop"]


def test_bar_and_percent_never_overflow():
    assert len(ui.bar(0, 10)) == len(ui.bar(99, 10)) == len(ui.bar(1, 0))
    assert ui.percent(5, 0) == 100 and ui.percent(20, 10) == 100 and ui.percent(1, 4) == 25


def test_formats_escape_user_text():
    assert ui.fail("Failed", "<x>") == "❌ <b>Failed</b>\n&lt;x&gt;"
    assert ui.usage(".dl <url>") == "ℹ️ <b>Usage:</b> <code>.dl &lt;url&gt;</code>"
    assert ui.plain("<b>Done</b> &amp; more\nsecond line") == "Done & more"
    assert len(ui.plain("x" * 500)) <= 190
