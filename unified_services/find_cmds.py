"""/find, /addnote, /removenote and /defnote in the control bot - hidden details of every copied message ("product").

Everything here is stored only in the bot's database (and backed up in the Saved Messages of the main account, see
fwd_backup.py) and shown only to admins in the control bot chat; nothing of it is ever posted into the destination.

* ``/find 42`` (or ``/find ZF-000042``)   full details of that serial number: source, links, ORIGINAL caption, notes
* ``/find some words``                    full details of every match (serial / source name / original caption / note)
* ``/addnote 42 price 15$``               add a private note (a new line when the product already has one)
* ``/removenote 42``                      remove the note of that product
* ``/defnote``                            sources + their default note; ``/defnote 2 Supplier Rahim`` sets it,
                                          ``/defnote 2 -`` deletes it. The default note goes on every NEW copy that
                                          comes from that source group / channel.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telethon import events

from unified_core.config import settings
from unified_core.database import NOTE_MAX, db
from unified_services.forwarder import msg_link, new_uid

log = logging.getLogger(__name__)
esc = html.escape

MAX_CARDS = 5      # a search answers with the full details of at most this many products

FIND_USAGE = ("🔎 <b>Find a product</b>\n"
              "<code>/find 42</code> — full details of serial 42 (also <code>ZF-000042</code>)\n"
              "<code>/find words</code> — full details of everything that matches (serial, source, caption, notes)\n"
              "<code>/addnote 42 text</code> — add a private note · <code>/removenote 42</code> — remove it\n"
              "<code>/defnote</code> — default note per source group / channel\n"
              "🔒 Only admins see this; the channel never does.")


def serial_to_uid(arg: str) -> str | None:
    """'42' -> ZF-000042, 'zf-000042' -> ZF-000042, anything else (a search text) -> None."""
    arg = arg.strip()
    if arg.isdigit():
        return new_uid(serial=int(arg))
    up = arg.upper()
    head, _, tail = up.partition("-")
    return up if head.isalpha() and tail.isdigit() and " " not in up else None


def _short(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _num(uid: str) -> str:
    tail = uid.rpartition("-")[2]
    return str(int(tail)) if tail.isdigit() else uid


def make_card(row: dict, account_label: str) -> str:
    when = datetime.fromtimestamp(row["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    a, b = msg_link(row["src_chat"], row["src_msg"]), msg_link(row["dst_chat"], row["dst_msg"])
    lines = [f"🆔 <b>{esc(row['uid'])}</b>  ·  {when}"]
    src = esc(row.get("src_name") or str(row["src_chat"]))
    lines.append(f"📥 Source: {src} (<code>{row['src_chat']}</code>) — msg {row['src_msg']}" + (f"\n{esc(a)}" if a else ""))
    lines.append(f"📤 Copy: <code>{row['dst_chat']}</code> — msg {row['dst_msg']}" + (f"\n{esc(b)}" if b else ""))
    if row.get("media") is not None:
        lines.append(f"🖼 Files: {row['media']}")
    if account_label:
        lines.append(f"👤 Copied by: {esc(account_label)}")
    cap = (row.get("caption") or "").strip()
    lines.append("📝 <b>Original caption</b>\n" + (esc(_short(cap, 800)) if cap else "<i>(none)</i>"))
    note = (row.get("note") or "").strip()
    lines.append("🔒 <b>Private note</b>\n" + (esc(note) if note else
                                              f"<i>(none)</i> — <code>/addnote {_num(row['uid'])} text</code>"))
    return "\n\n".join(lines)


def register_find_commands(bot, is_admin, accounts=None, app=None) -> None:
    primary_id = getattr(app, "me_id", 0) if app is not None else 0

    def label(account_id: int | None) -> str:
        if not account_id:
            return ""
        if accounts is not None:
            inst = accounts.instances.get(account_id)
            if inst is not None:
                return inst.label
            if account_id == primary_id:
                user = getattr(accounts, "primary_username", "")
                return f"@{user}" if user else (getattr(accounts, "primary_name", "") or "main account")
        return "main account" if account_id == primary_id else str(account_id)

    def forwarders() -> list:
        fws = [getattr(app, "forwarder", None)] if app is not None else []
        if accounts is not None:
            fws += [getattr(i.app, "forwarder", None) for i in accounts.instances.values()]
        return [f for f in fws if f is not None]

    async def source_list() -> list[tuple[int, str, str | None]]:
        """[(source chat id, name, default note)] - every source of every account's routes, then orphan defaults."""
        defaults = {d["src_chat"]: d for d in await db.fwd_defaults_all()}
        out, seen = [], set()
        for fw in forwarders():
            for r in fw.routes:
                if r["src"] not in seen:
                    seen.add(r["src"])
                    out.append((r["src"], r.get("src_name") or str(r["src"]), (defaults.get(r["src"]) or {}).get("note")))
        for sid, d in defaults.items():
            if sid not in seen:
                out.append((sid, d.get("src_name") or str(sid), d["note"]))
        return out

    # ------------------------------------------------------------------ /find
    @bot.on(events.NewMessage(pattern=r"(?is)^/find(?:@\w+)?(?:\s+(.+))?$", func=lambda e: e.is_private))
    async def find_cmd(event):
        if not is_admin(event.sender_id):
            return
        arg = (event.pattern_match.group(1) or "").strip()
        if not arg:
            return await event.respond(FIND_USAGE)
        uid = serial_to_uid(arg)
        if uid is not None:
            row = await db.fwd_get(uid)
            if row and row.get("dst_msg"):
                return await event.respond(make_card(row, label(row.get("account"))), link_preview=False)
            if arg.isdigit() or uid.startswith(settings.FWD_ID_PREFIX.upper() + "-"):
                return await event.respond(f"❌ No copy with serial <code>{esc(uid)}</code> "
                                           "(copies without your own note are forgotten after 90 days).")
        rows = await db.fwd_search(arg, MAX_CARDS + 15)
        if not rows:
            return await event.respond(f"❌ Nothing found for <code>{esc(arg[:60])}</code>.")
        await event.respond(f"🔎 <b>{len(rows)}{'+' if len(rows) > MAX_CARDS + 14 else ''} result(s)</b> for "
                            f"<code>{esc(arg[:60])}</code> (newest first)")
        for r in rows[:MAX_CARDS]:
            await event.respond(make_card(r, label(r.get("account"))), link_preview=False)
        rest = rows[MAX_CARDS:]
        if rest:
            await event.respond("…and more: " + ", ".join(f"<code>{esc(r['uid'])}</code>" for r in rest)
                                + "\n<code>/find 42</code> shows one in full.")

    # ------------------------------------------------------------------ /addnote, /removenote
    @bot.on(events.NewMessage(pattern=r"(?is)^/addnote(?:@\w+)?(?:\s+(\S+)(?:\s+(.+))?)?$", func=lambda e: e.is_private))
    async def addnote_cmd(event):
        if not is_admin(event.sender_id):
            return
        ref, text = event.pattern_match.group(1), (event.pattern_match.group(2) or "").strip()
        uid = serial_to_uid(ref) if ref else None
        if uid is None or not text:
            return await event.respond("Usage: <code>/addnote 42 your note</code> (a new line when there is a note already) "
                                       "· <code>/removenote 42</code> removes it")
        row = await db.fwd_get(uid)
        if not row or not row.get("dst_msg"):
            return await event.respond(f"❌ No copy with serial <code>{esc(uid)}</code>.")
        old = (row.get("note") or "").strip()
        new = f"{old}\n{text}" if old else text
        if len(new) > NOTE_MAX:
            return await event.respond(f"❌ Too long: a note holds {NOTE_MAX} characters "
                                       f"(now {len(old)}, new text {len(text)}). Shorten it or use /removenote first.")
        await db.fwd_set_note(uid, new)
        await event.respond(f"✅ Note added to <b>{esc(uid)}</b>.\n\n🔒 {esc(new)}")

    @bot.on(events.NewMessage(pattern=r"(?is)^/removenote(?:@\w+)?(?:\s+(\S+))?\s*$", func=lambda e: e.is_private))
    async def removenote_cmd(event):
        if not is_admin(event.sender_id):
            return
        ref = event.pattern_match.group(1)
        uid = serial_to_uid(ref) if ref else None
        if uid is None:
            return await event.respond("Usage: <code>/removenote 42</code>")
        row = await db.fwd_get(uid)
        if not row or not row.get("dst_msg"):
            return await event.respond(f"❌ No copy with serial <code>{esc(uid)}</code>.")
        if not (row.get("note") or "").strip():
            return await event.respond(f"ℹ️ <b>{esc(uid)}</b> has no note.")
        await db.fwd_set_note(uid, None)
        await event.respond(f"🗑 Note removed from <b>{esc(uid)}</b>.")

    # ------------------------------------------------------------------ /defnote
    @bot.on(events.NewMessage(pattern=r"(?is)^/defnote(?:@\w+)?(?:\s+(\S+)(?:\s+(.+))?)?$", func=lambda e: e.is_private))
    async def defnote_cmd(event):
        if not is_admin(event.sender_id):
            return
        ref, text = event.pattern_match.group(1), (event.pattern_match.group(2) or "").strip()
        sources = await source_list()
        if not ref or not text:
            if not sources:
                return await event.respond("ℹ️ No source group / channel yet — add a route first "
                                           "(🔁 Forward → ➕ Add route).")
            lines = ["🏷 <b>Default notes</b> — put on every <b>new</b> product that comes from that source",
                     ""]
            for i, (sid, name, note) in enumerate(sources, 1):
                lines.append(f"{i}. <b>{esc(_short(name, 40))}</b> (<code>{sid}</code>)\n   "
                             + (f"🔒 {esc(_short(note, 200))}" if note else "<i>(no default note)</i>"))
            lines.append("\n<code>/defnote 2 Supplier Rahim, COD</code> sets it · <code>/defnote 2 -</code> deletes it")
            return await event.respond("\n".join(lines))
        hit = None
        if ref.lstrip("-").isdigit():
            n = int(ref)
            hit = sources[n - 1] if 1 <= n <= len(sources) else next((s for s in sources if s[0] == n), None)
        if hit is None:
            return await event.respond("❌ Unknown source — send <code>/defnote</code> to see the numbered list.")
        sid, name, _old = hit
        if text == "-":
            await db.fwd_default_set(sid, name, None)
            return await event.respond(f"🗑 Default note of <b>{esc(name)}</b> deleted (copies already made keep theirs).")
        if len(text) > NOTE_MAX:
            return await event.respond(f"❌ Too long: a note holds {NOTE_MAX} characters.")
        await db.fwd_default_set(sid, name, text)
        await event.respond(f"✅ Default note of <b>{esc(name)}</b> saved:\n🔒 {esc(text)}\n"
                            "It goes on every <b>new</b> product copied from there (and can be changed per product "
                            "with /addnote and /removenote).")
