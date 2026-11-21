"""One look & feel for every bot screen: standard buttons (Back / Cancel / Close / Confirm) and message formats.

Rules every screen follows
  * Titles:    ``icon <b>Title</b>`` on the first line, a blank line, then the content, then an optional <i>hint</i>.
  * Status:    ✅ success · ❌ failure · ⚠️ warning · ℹ️ info · ⏳ working · ⏭ skipped · ⏹ stopped.
  * Navigation, always in this order on the LAST row: ``🔙 Back`` (one level up) · ``🏠 Main menu`` · ``✖️ Close``.
  * Prompts that wait for typed text show a single ``✖️ Cancel`` button.
  * Anything destructive asks first: ``✅ Yes, <do it>`` + ``✖️ Cancel``.

Only plain ``Button.inline(text, data)`` is used, so the screens stay easy to test.
"""
from __future__ import annotations

import html
import re

from telethon import Button

# ------------------------------------------------------------------ labels (the single source of truth)
L_BACK = "🔙 Back"
L_MAIN = "🏠 Main menu"
L_CANCEL = "✖️ Cancel"
L_CLOSE = "✖️ Close"
L_REFRESH = "🔄 Refresh"
L_YES = "✅ Yes"

D_MAIN = b"menu"        # callback data of the main menu (control_bot)
D_CLOSE = b"ui:close"   # removes the message the button is under (control_bot)


def btn(label: str, data: bytes | str):
    """Inline button; `data` may be str."""
    return Button.inline(label, data.encode() if isinstance(data, str) else data)


# ------------------------------------------------------------------ button rows
def back_row(data: bytes | str, label: str = L_BACK) -> list:
    return [btn(label, data)]


def main_row() -> list:
    return [btn(L_MAIN, D_MAIN)]


def nav_row(back: bytes | str | None = None, *, main: bool = True, close: bool = False,
            back_label: str = L_BACK) -> list:
    """Last row of a screen: [🔙 Back] [🏠 Main menu] [✖️ Close] (only what applies)."""
    row = []
    if back is not None:
        row.append(btn(back_label, back))
    if main:
        row.append(btn(L_MAIN, D_MAIN))
    if close:
        row.append(btn(L_CLOSE, D_CLOSE))
    return row


def cancel_row(data: bytes | str = b"m:cancel") -> list:
    return [btn(L_CANCEL, data)]


def close_row() -> list:
    return [btn(L_CLOSE, D_CLOSE)]


def confirm_row(yes_label: str, yes_data: bytes | str, no_data: bytes | str, no_label: str = L_CANCEL) -> list:
    """Yes / No pair for a destructive action. `yes_label` should say what happens: ``🗑 Yes, delete``."""
    return [btn(yes_label, yes_data), btn(no_label, no_data)]


def grid(buttons: list, per_row: int = 2) -> list[list]:
    """Lay a flat button list out in rows of `per_row`."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def pager_row(prefix: str, page: int, pages: int) -> list:
    """◀️ 2/5 ▶️ - `prefix` is completed with the page number (``m:fp:a:`` -> ``m:fp:a:1``)."""
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", b"m:noop"))
    if page < pages - 1:
        row.append(btn("▶️", f"{prefix}{page + 1}"))
    return row


# ------------------------------------------------------------------ message formats
def head(icon: str, title: str, sub: str = "") -> str:
    """Screen title: ``icon <b>Title</b>`` (+ an optional second line)."""
    return f"{icon} <b>{title}</b>" + (f"\n{sub}" if sub else "")


def ok(text: str) -> str:
    return f"✅ {text}"


def fail(text: str, detail: str = "") -> str:
    """``❌ <b>text</b>`` + the escaped reason on its own line. `text` is trusted HTML, `detail` is escaped."""
    return f"❌ <b>{text}</b>" + (f"\n{html.escape(detail)}" if detail else "")


def warn(text: str) -> str:
    return f"⚠️ {text}"


def info(text: str) -> str:
    return f"ℹ️ {text}"


def busy(text: str) -> str:
    return f"⏳ <i>{text}</i>"


def usage(syntax: str, note: str = "") -> str:
    """``ℹ️ Usage: <code>.dl &lt;url&gt;</code>`` - `syntax` is plain text and is escaped here."""
    return f"ℹ️ <b>Usage:</b> <code>{html.escape(syntax)}</code>" + (f"\n{note}" if note else "")


def hint(text: str) -> str:
    return f"<i>{text}</i>"


def onoff(value: object) -> str:
    return "ON ✅" if value else "OFF ❌"


def step(n: int, total: int, title: str) -> str:
    """``<b>Step 1/2</b> — title`` for multi-step prompts."""
    return f"<b>Step {n}/{total}</b> — {title}"


def bar(done: int, total: int, width: int = 15) -> str:
    """``[█████░░░░░]`` - always `width` characters inside, never overflows when done > total."""
    if total <= 0:
        filled = width
    else:
        filled = max(0, min(width, int(width * done // total)))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def percent(done: int, total: int) -> float:
    return 100.0 if total <= 0 else max(0.0, min(100.0, done / total * 100))


_TAG = re.compile(r"<[^>]+>")


def plain(text: str, limit: int = 190) -> str:
    """HTML -> plain one-liner for a callback popup (Telegram allows 200 characters there)."""
    first = _TAG.sub("", text).strip().split("\n", 1)[0]
    first = html.unescape(first)
    return first if len(first) <= limit else first[: limit - 1] + "…"
