"""/find, /addnote, /removenote and /defnote in the control bot - hidden details of every copied message ("product").

The details live in the bot's database (backed up in the Saved Messages of the main account, see fwd_backup.py) and
are shown only to admins in the control bot chat; nothing of it is ever posted into the destination.

* ``/find 42`` (or ``/find ZF-000042``)   the product card + its files as an album / preview, in one message.
                                          When the database has forgotten the serial (free hosting without a disk,
                                          90 day clean-up) the serial is searched in the destination group / channel /
                                          chat itself - the copy carries it - and the product is restored from there.
* ``/find some words``                    one match = the card, several = a short list with tap-to-open buttons;
                                          nothing in the database = the destination chats are searched for the words
* ``/addnote 42 price 15$``               add a private note (a new line when the product already has one)
* ``/removenote 42``                      remove the note of that product
* ``/defnote``                            sources + their default note; ``/defnote 2 Supplier Rahim`` sets it,
                                          ``/defnote 2 -`` deletes it. The default note goes on every NEW copy that
                                          comes from that source group / channel.
"""
from __future__ import annotations

import html
import logging
import re
import tempfile
from datetime import datetime, timezone

from telethon import events

from unified_core.cleanup import safe_rmtree
from unified_core.config import settings
from unified_core.database import NOTE_MAX, db
from unified_core.utils import NOTIFY_MARK
from unified_services.forwarder import msg_link, new_uid

log = logging.getLogger(__name__)
esc = html.escape

MAX_LIST = 10                    # a text search lists at most this many products (tap one to open it)
CAPTION_LIMIT = 1024             # a bot cannot send a longer caption under a file / album
SCAN_LIMIT = 2000                # last resort: read this many newest messages of a destination chat
MAX_PREVIEW_BYTES = 200 * 1024 * 1024   # bigger albums are not downloaded for a preview (the Copy link opens them)

FIND_USAGE = ("🔎 <b>Find</b>\n"
              "<code>/find 42</code> — serial 42 (also <code>ZF-000042</code>)\n"
              "<code>/find words</code> — serial · source · caption · note\n\n"
              "<code>/addnote 42 text</code> · <code>/removenote 42</code>\n"
              "<code>/defnote</code> — default note per source\n\n"
              "<i>Searches the database first, then the destination chats themselves.</i>")


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


def uid_in_text(uid: str, text: str | None) -> bool:
    """True when the serial stands in the text as a whole token (ZF-000020 is not inside ZF-0000201)."""
    return bool(text) and re.search(rf"(?<![A-Za-z0-9]){re.escape(uid)}(?!\d)", text, re.I) is not None


def _uid_re() -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(settings.FWD_ID_PREFIX)}-\d{{4,}}(?!\d)", re.I)


def make_card(row: dict, account_label: str = "", compact: bool = False) -> str:
    """Minimal product card. `compact` = it is the caption of an album / file (a caption holds 1024 characters)."""
    cap_max, note_max = (420, 220) if compact else (900, 1500)
    when = datetime.fromtimestamp(row["created_at"], timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    lines = [f"<b>{esc(row['uid'])}</b>  ·  <i>{when}</i>"]
    src = (row.get("src_name") or "").strip()
    who = [x for x in (esc(src) if src else "", f"via {esc(account_label)}" if account_label else "") if x]
    if who:
        lines.append("  ·  ".join(who))
    cap = (row.get("caption") or "").strip()
    if cap:
        lines.append(f"<blockquote>{esc(_short(cap, cap_max))}</blockquote>")
    note = (row.get("note") or "").strip()
    if len(note) > note_max:
        note = note[: note_max - 1] + "…"                              # keep the line breaks of a note
    lines.append(f"🔒 {esc(note)}" if note else
                 f"🔒 <i>no note</i> · <code>/addnote {_num(row['uid'])} text</code>")
    links = []
    if row.get("src_chat") and msg_link(row["src_chat"], row["src_msg"]):
        links.append(f"<a href=\"{esc(msg_link(row['src_chat'], row['src_msg']))}\">Source ↗</a>")
    if msg_link(row["dst_chat"], row["dst_msg"]):
        links.append(f"<a href=\"{esc(msg_link(row['dst_chat'], row['dst_msg']))}\">Copy ↗</a>")
    if links:
        lines.append("  ·  ".join(links))
    return "\n\n".join(lines) if not compact else "\n".join(lines)


def _is_truncated(row: dict) -> bool:
    return len((row.get("caption") or "").strip()) > 420 or len((row.get("note") or "").strip()) > 220


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

    def clients() -> list:
        """[(userbot, account id, forwarder)] of every logged-in account that copies messages."""
        return [(fw.userbot, getattr(fw, "me_id", 0), fw) for fw in forwarders() if getattr(fw, "userbot", None) is not None]

    async def dest_pairs() -> list[tuple]:
        """[(userbot, account id, destination chat id)] - every destination of every route, then the destinations the
        database remembers (tried with every account: it has to be a member to read the chat)."""
        pairs, seen = [], set()
        for ub, me, fw in clients():
            for r in fw.routes:
                if (id(ub), r["dst"]) not in seen:
                    seen.add((id(ub), r["dst"]))
                    pairs.append((ub, me, r["dst"]))
        try:
            known = await db.fwd_dst_chats()
        except Exception:
            known = []
        for dst in known:
            for ub, me, _fw in clients():
                if (id(ub), dst) not in seen:
                    seen.add((id(ub), dst))
                    pairs.append((ub, me, dst))
        return pairs

    # ---------------------------------------------------------------- reading the destination chat
    async def album_of(ub, dst: int, mid: int) -> list:
        """The files of the copy that starts at message `mid`: the whole album, or the one file / the file a
        sticker-style note replies to. [] when the message has no file (text only)."""
        m = await ub.get_messages(dst, ids=mid)
        if not m:
            return []
        if not getattr(m, "media", None) and getattr(m, "reply_to_msg_id", None):
            m = await ub.get_messages(dst, ids=m.reply_to_msg_id) or m   # sticker / round video: the id is in a reply
        gid = getattr(m, "grouped_id", None)
        if not gid:
            return [m] if (getattr(m, "photo", None) or getattr(m, "document", None)) else []
        near = await ub.get_messages(dst, ids=list(range(max(1, m.id - 10), m.id + 11)))
        group = [x for x in near if x and getattr(x, "grouped_id", None) == gid]
        return sorted(group, key=lambda x: x.id) or [m]

    async def find_message(ub, dst: int, uid: str):
        """The message of chat `dst` that carries the serial, or None. Telegram search first, then a plain scan."""
        for q in (uid, uid.rpartition("-")[2]):
            try:
                async for m in ub.iter_messages(dst, search=q, limit=50):
                    if uid_in_text(uid, getattr(m, "raw_text", "")):
                        return m
            except Exception as exc:
                log.debug("search for %s in %s failed: %s", uid, dst, exc)
                return None                                   # no access to that chat: do not try again
        try:
            async for m in ub.iter_messages(dst, limit=SCAN_LIMIT):
                if uid_in_text(uid, getattr(m, "raw_text", "")):
                    return m
        except Exception as exc:
            log.debug("scan of %s failed: %s", dst, exc)
        return None

    async def adopt(ub, me: int, dst: int, uid: str, m) -> dict | None:
        """Build the product row from the copy found in the destination chat and store it again."""
        try:
            album = await album_of(ub, dst, m.id)
        except Exception as exc:
            log.debug("album of %s:%s failed: %s", dst, m.id, exc)
            album = []
        first = album[0].id if album else m.id
        caption = (getattr(m, "raw_text", "") or "").replace(NOTIFY_MARK, "")
        caption = re.sub(rf"(?<![A-Za-z0-9]){re.escape(uid)}(?!\d)", "", caption, flags=re.I).strip()
        created = getattr(getattr(m, "date", None), "timestamp", lambda: 0)() or 0
        await db.fwd_adopt(uid, dst, first, int(created), caption, len(album), me)
        return await db.fwd_get(uid)

    async def recover_uid(uid: str) -> dict | None:
        """The database does not know `uid`: look for it in the destination chats (the copy carries its serial)."""
        for ub, me, dst in await dest_pairs():
            m = await find_message(ub, dst, uid)
            if m is not None:
                return await adopt(ub, me, dst, uid, m)
        return None

    async def recover_text(text: str) -> list[dict]:
        """Nothing in the database matches `text`: search the destination chats for it (the caption is posted there)."""
        found, seen_uids, pat = [], set(), _uid_re()
        for ub, me, dst in await dest_pairs():
            try:
                async for m in ub.iter_messages(dst, search=text, limit=30):
                    hit = pat.search(getattr(m, "raw_text", "") or "")
                    if not hit or hit.group(0).upper() in seen_uids:
                        continue
                    uid = hit.group(0).upper()
                    seen_uids.add(uid)
                    row = await db.fwd_get(uid)
                    if not (row and row.get("dst_msg")):
                        row = await adopt(ub, me, dst, uid, m)
                    if row:
                        found.append(row)
                    if len(found) > MAX_LIST:
                        return found
            except Exception as exc:
                log.debug("text search in %s failed: %s", dst, exc)
        return found

    async def get_row(uid: str) -> dict | None:
        row = await db.fwd_get(uid)
        if row and row.get("dst_msg"):
            return row
        return await recover_uid(uid)

    # ---------------------------------------------------------------- files of a product
    async def download_preview(row: dict, tmp: str):
        """(paths, messages) of the product's files, downloaded from its destination chat - or None."""
        if row.get("media") == 0:
            return None                                                # a text-only copy
        for ub, _me, dst in await dest_pairs():
            if dst != row["dst_chat"]:
                continue
            try:
                album = await album_of(ub, dst, row["dst_msg"])
                if not album:
                    continue
                if sum(getattr(getattr(m, "file", None), "size", 0) or 0 for m in album) > MAX_PREVIEW_BYTES:
                    return None
                paths = [await ub.download_media(m, file=tmp + "/") for m in album]
                if all(paths):
                    return paths, album
            except Exception as exc:
                log.debug("preview of %s failed: %s", row.get("uid"), exc)
        return None

    async def send_product(event, row: dict) -> None:
        """One message: the files as an album (or the single file) with the card as its caption."""
        card_full = make_card(row, label(row.get("account")))
        tmp = tempfile.mkdtemp(prefix="zeba-find-")
        try:
            got = await download_preview(row, tmp) if clients() else None
            if not got:
                return await event.respond(card_full, link_preview=False)
            paths, msgs = got
            caption = make_card(row, label(row.get("account")), compact=True)
            kw: dict = {"caption": caption, "parse_mode": "html"}
            if len(msgs) == 1:
                m = msgs[0]
                kw.update(voice_note=bool(getattr(m, "voice", None)), video_note=bool(getattr(m, "video_note", None)),
                          supports_streaming=bool(getattr(m, "video", None)))
                if getattr(m, "video_note", None):
                    kw["caption"] = None                                # a round video cannot carry a caption
            else:
                kw["force_document"] = all(
                    getattr(x, "document", None) and not any(getattr(x, a, None) for a in ("photo", "video", "gif", "audio", "voice"))
                    for x in msgs)
            sent = None
            try:
                sent = await event.client.send_file(event.chat_id, paths if len(paths) > 1 else paths[0], **kw)
            except Exception as exc:                                    # e.g. photos + documents cannot share an album
                log.info("album preview failed (%s) - sending the files one by one", exc)
                for i, p in enumerate(paths):
                    one = await event.client.send_file(event.chat_id, p, caption=caption if i == 0 else None,
                                                       parse_mode="html")
                    sent = sent or one
            if kw.get("caption") is None or _is_truncated(row):         # the caption was cut: the full text follows
                first = sent[0] if isinstance(sent, (list, tuple)) else sent
                await event.respond(card_full, link_preview=False, reply_to=getattr(first, "id", None))
        finally:
            safe_rmtree(tmp)

    async def show_row(event, row: dict) -> None:
        status = await event.respond("⏳ <i>Loading files…</i>") if (clients() and row.get("media") != 0) else None
        try:
            await send_product(event, row)
        finally:
            if status is not None:
                try:
                    await status.delete()
                except Exception:
                    pass

    async def with_status(event, text: str, coro):
        """Run `coro` while a small status message is shown (removed afterwards)."""
        status = await event.respond(text) if clients() else None
        try:
            return await coro
        finally:
            if status is not None:
                try:
                    await status.delete()
                except Exception:
                    pass

    def not_found(uid: str) -> str:
        return (f"❌ No copy with serial <code>{esc(uid)}</code> found — not in the database and not in "
                "any destination chat.")

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
    def listing(rows: list[dict], text: str):
        from telethon import Button
        lines = [f"🔎 <b>{len(rows[:MAX_LIST])}{'+' if len(rows) > MAX_LIST else ''} results</b> for "
                 f"<code>{esc(text[:60])}</code>", ""]
        for r in rows[:MAX_LIST]:
            what = _short(r.get("caption") or r.get("note") or "", 48) or "—"
            lines.append(f"<b>{esc(r['uid'])}</b>  {esc(_short(r.get('src_name'), 22))}  <i>{esc(what)}</i>".replace("  </i>", "</i>"))
        lines.append("\n<i>Tap a serial to open it.</i>")
        btns = [Button.inline(r["uid"], f"fd:{_num(r['uid'])}".encode()) for r in rows[:MAX_LIST]]
        return "\n".join(lines), [btns[i:i + 3] for i in range(0, len(btns), 3)]

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
            if not (row and row.get("dst_msg")):
                row = await with_status(event, f"🔎 <i>Searching {esc(uid)} in the destination chats…</i>",
                                        recover_uid(uid))
            if row:
                return await show_row(event, row)
            if arg.isdigit() or uid.startswith(settings.FWD_ID_PREFIX.upper() + "-"):
                return await event.respond(not_found(uid))
        rows = await db.fwd_search(arg, MAX_LIST + 1)
        if not rows:
            rows = await with_status(event, f"🔎 <i>Searching “{esc(arg[:40])}” in the destination chats…</i>",
                                     recover_text(arg))
        if not rows:
            return await event.respond(f"❌ Nothing found for <code>{esc(arg[:60])}</code>.")
        if len(rows) == 1:
            return await show_row(event, rows[0])
        text, buttons = listing(rows, arg)
        await event.respond(text, buttons=buttons, link_preview=False)

    @bot.on(events.CallbackQuery(pattern=rb"^fd:(\d+)$"))
    async def find_open(event):
        if not is_admin(event.sender_id):
            return await event.answer("Unauthorized", alert=True)
        await event.answer()
        uid = new_uid(serial=int(event.pattern_match.group(1).decode()))
        row = await get_row(uid)
        if row is None:
            return await event.respond(not_found(uid))
        await show_row(event, row)

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
        row = await get_row(uid)
        if not row:
            return await event.respond(not_found(uid))
        old = (row.get("note") or "").strip()
        new = f"{old}\n{text}" if old else text
        if len(new) > NOTE_MAX:
            return await event.respond(f"❌ Too long: a note holds {NOTE_MAX} characters "
                                       f"(now {len(old)}, new text {len(text)}). Shorten it or use /removenote first.")
        await db.fwd_set_note(uid, new)
        await event.respond(f"✅ Note added to <b>{esc(uid)}</b>\n\n🔒 {esc(new)}")

    @bot.on(events.NewMessage(pattern=r"(?is)^/removenote(?:@\w+)?(?:\s+(\S+))?\s*$", func=lambda e: e.is_private))
    async def removenote_cmd(event):
        if not is_admin(event.sender_id):
            return
        ref = event.pattern_match.group(1)
        uid = serial_to_uid(ref) if ref else None
        if uid is None:
            return await event.respond("Usage: <code>/removenote 42</code>")
        row = await get_row(uid)
        if not row:
            return await event.respond(not_found(uid))
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
