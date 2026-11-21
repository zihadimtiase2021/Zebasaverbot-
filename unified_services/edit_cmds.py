"""Live **Edit** of a product found with /find: file / caption / details, changed from the bot chat.

Every product shown by /find has a ``✏️ Edit`` button.  It opens a small panel:

* 🖼 **File**    send the new photo / video / document.  It replaces the file of the copy in the destination channel
                 (an album asks which of its files).  The caption of that message is kept.
* 📝 **Caption** send the new caption (formatting is kept, ``-`` clears it).  The serial number is added at the end again.
* 🔒 **Details** the private note of the product (never posted anywhere; ``-`` deletes it).

Every step has 🔙 Back and ❌ Cancel.  On save the destination message is edited FIRST (by the account that posted
it, another account that can open the chat is tried when that one may not), and only when Telegram accepted it the
database record is changed - so the record and the live message never disagree.

* The destination message was deleted:  a caption / details change is still stored in the record (and says so); a new
  file cannot be saved (there is nothing to put it on).
* FloodWait:  waited out when short; when Telegram asks for a long wait nothing is changed and the wait is reported.
* No right to edit, caption too long, file refused by Telegram ...: a plain explanation, nothing is changed.
"""
from __future__ import annotations

import asyncio
import html
import logging
import tempfile
import time

from telethon import Button, errors, events

from unified_core.cleanup import safe_rmtree
from unified_core.config import settings
from unified_core.database import NOTE_MAX, db
from unified_core.utils import short_error
from unified_services import ui
from unified_services.forwarder import TEXT_LIMIT, build_html, msg_link

log = logging.getLogger("zeba.edit")
esc = html.escape

EDIT_TTL = 900            # seconds an unfinished edit waits for the next message
MAX_FLOOD_WAIT = 120      # a longer FloodWait is reported instead of waited for


def _errs(*names: str) -> tuple:
    return tuple(getattr(errors, n) for n in names if hasattr(errors, n))


GONE = _errs("MessageIdInvalidError", "MessageNotFoundError")
NOT_ALLOWED = _errs("MessageAuthorRequiredError", "ChatAdminRequiredError", "ChatWriteForbiddenError",
                    "MessageEditTimeExpiredError", "UserNotParticipantError", "ChannelPrivateError",
                    "ChatRestrictedError", "ChatAdminRequiredError")
NOT_MODIFIED = _errs("MessageNotModifiedError")
FLOOD = _errs("FloodWaitError", "SlowModeWaitError")


class EditError(Exception):
    """A problem that can be shown to the admin as it is (nothing was changed)."""


class MessageGone(Exception):
    """The message of the product no longer exists in the destination chat."""


class Ctx:
    """What a product looks like in the destination chat right now."""

    def __init__(self, caption_msg, album: list) -> None:
        self.caption_msg = caption_msg      # the message that carries the caption + serial
        self.album = album                  # the file messages of the product ([] = text only)


async def _flood(factory):
    """Run one Telegram request; a short FloodWait is waited out, a long one becomes an EditError."""
    for attempt in range(3):
        try:
            return await factory()
        except FLOOD as exc:
            wait = int(getattr(exc, "seconds", 0) or 1)
            if wait > MAX_FLOOD_WAIT or attempt == 2:
                raise EditError(f"Telegram asks to wait {wait} s (flood limit). Nothing was changed - "
                                "try again later.") from exc
            log.warning("edit: FloodWait %ss", wait)
            await asyncio.sleep(wait + 1)


class ProductEditor:
    """The Telegram + database side of an edit. `h` = the helpers returned by register_find_commands."""

    def __init__(self, h) -> None:
        self.h = h

    # ---------------------------------------------------------------- finding the live message
    def _candidates(self, row: dict) -> list:
        """[(userbot, account id, forwarder)] - the account that posted the copy first."""
        return sorted(self.h.clients(), key=lambda c: c[1] != row.get("account"))

    async def _context(self, ub, fw, row: dict):
        """Ctx of the product, None when the message is gone. Raises another error when this account has no access."""
        dst, mid = row["dst_chat"], row["dst_msg"]
        m = await _flood(lambda: ub.get_messages(dst, ids=mid))
        if not m:
            return None
        text_only = row.get("media") == 0
        album = [] if text_only else await _flood(lambda: self.h.album_of(ub, dst, mid))
        cap = m
        if getattr(m, "sticker", None) or getattr(m, "video_note", None):
            # these cannot carry a caption: the text with the serial is a reply posted next to them
            cap = await self.h.find_message(ub, dst, row["uid"])
        elif not getattr(m, "media", None) and album:
            cap = album[0]
        return Ctx(cap, album)

    async def _run(self, row: dict, op):
        """Run op(userbot, forwarder, ctx) with the first account that may do it."""
        cands = self._candidates(row)
        if not cands:
            raise EditError("No logged-in account is running - the destination chat cannot be reached.")
        refused, gone, no_access = None, False, 0
        for ub, _me, fw in cands:
            try:
                ctx = await self._context(ub, fw, row)
            except EditError:
                raise
            except GONE:
                gone = True
                break
            except Exception as exc:               # this account cannot open the chat: try the next one
                log.debug("edit: account cannot open %s: %s", row.get("dst_chat"), exc)
                no_access += 1
                continue
            if ctx is None:
                gone = True
                break
            try:
                return await op(ub, fw, ctx)
            except GONE:
                gone = True
                break
            except NOT_ALLOWED as exc:
                refused = exc
                continue
            except EditError:
                raise
            except errors.RPCError as exc:
                raise EditError(f"Telegram refused the change: {short_error(exc)}") from exc
        if gone:
            raise MessageGone()
        if refused is not None:
            raise EditError("No account may edit that message (it needs the account that posted it, or an admin "
                            f"with the 'Edit messages' right): {short_error(refused)}")
        raise EditError("No logged-in account can open the destination chat any more.")

    # ---------------------------------------------------------------- caption
    async def edit_caption(self, row: dict, raw: str, entities) -> list[str]:
        """Returns the result lines for the admin. Raises EditError (nothing changed)."""
        uid = row["uid"]

        async def op(ub, fw, ctx):
            msg = ctx.caption_msg
            if msg is None:
                raise EditError("The text message of this product could not be found in the chat.")
            is_media = bool(getattr(msg, "media", None)) and not getattr(msg, "web_preview", None)
            limit = getattr(fw.pipeline, "caption_limit", 1024) if is_media else TEXT_LIMIT
            if len(raw) + len(uid) + 3 > limit:
                raise EditError(f"Too long: this message holds {limit} characters including the serial, "
                                f"your text has {len(raw)}.")
            text = build_html(raw, entities, uid, limit)
            try:
                await _flood(lambda: ub.edit_message(row["dst_chat"], msg.id, text, parse_mode="html",
                                                     link_preview=bool(getattr(msg, "web_preview", None))))
            except NOT_MODIFIED:
                pass                                   # the channel already shows exactly this
            return msg.id

        try:
            await self._run(row, op)
        except MessageGone:
            await db.fwd_set_caption(uid, raw)
            return ["✅ Record updated in the database.",
                    "⚠️ The message was <b>deleted from the channel</b>, so there was nothing to edit there."]
        await db.fwd_set_caption(uid, raw)
        return ["✅ Database record updated.", "✅ Channel message edited" + self._link(row)]

    # ---------------------------------------------------------------- file
    async def replace_file(self, row: dict, index: int, path: str, *, force_document: bool = False,
                           streaming: bool = False) -> list[str]:
        async def op(ub, fw, ctx):
            if not ctx.album:
                raise EditError("This product has no file to replace (it is a text-only copy).")
            if not 0 <= index < len(ctx.album):
                raise EditError(f"This product has {len(ctx.album)} file(s) - there is no file {index + 1}.")
            target = ctx.album[index]
            kw: dict = {"file": path, "force_document": force_document, "supports_streaming": streaming}
            if getattr(target, "message", ""):         # the caption must survive the media change
                kw.update(formatting_entities=list(getattr(target, "entities", None) or []), parse_mode=None)
                args = (row["dst_chat"], target.id, target.message)
            else:
                args = (row["dst_chat"], target.id)
            try:
                await _flood(lambda: ub.edit_message(*args, **kw))
            except NOT_MODIFIED:
                pass

        try:
            await self._run(row, op)
        except MessageGone:
            raise EditError("The message was deleted from the channel - a new file cannot be saved there. "
                            "(Nothing was changed.)") from None
        return ["✅ File replaced in the channel message" + self._link(row)]

    @staticmethod
    def _link(row: dict) -> str:
        link = msg_link(row["dst_chat"], row["dst_msg"])
        return f' · <a href="{esc(link)}">open ↗</a>' if link else ""


# ------------------------------------------------------------------ the bot side
def register_edit_commands(bot, is_admin, app, h) -> ProductEditor:
    editor = ProductEditor(h)
    edits: dict[int, dict] = {}        # admin id -> {"uid", "mode", "idx", "ts"}
    app.editing = edits                # control_bot: media sent while editing is NOT a watermark request
    B = Button.inline
    stop = getattr(events, "StopPropagation", None)

    def back_cancel(uid: str) -> list:
        return [[B(ui.L_BACK, f"fe:{uid}:p".encode()), B(ui.L_CANCEL, f"fe:{uid}:x".encode())]]

    def panel(row: dict):
        uid = row["uid"]
        rows = []
        first = [B("📝 Caption", f"fe:{uid}:c".encode())]
        if row.get("media") != 0:
            first.insert(0, B("🖼 File", f"fe:{uid}:m".encode()))
        rows.append(first)
        rows.append([B("🔒 Details", f"fe:{uid}:d".encode()), B(ui.L_CLOSE, f"fe:{uid}:x".encode())])
        text = (f"✏️ <b>Edit {esc(uid)}</b>\n"
                "🖼 file and 📝 caption change the <b>live message in the channel</b> and the record; "
                "🔒 details are private (record only).")
        return text, rows

    def forget_menu_prompt(uid: int) -> None:
        getattr(app, "_menu_pending", {}).pop(uid, None)   # a half-finished menu prompt must not eat the next message

    async def load(event, uid: str):
        row = await h.get_row(uid)
        if row is None:
            await event.answer("This product was not found any more.", alert=True)
        return row

    @bot.on(events.CallbackQuery(pattern=rb"^fe:([A-Za-z0-9-]+)(?::(\w+))?$"))
    async def on_edit_button(event):
        if not is_admin(event.sender_id):
            return await event.answer("Unauthorized", alert=True)
        uid = event.pattern_match.group(1).decode()
        act = (event.pattern_match.group(2) or b"").decode()
        sender = event.sender_id

        if act == "x":                                     # Close / Cancel
            edits.pop(sender, None)
            await event.answer()
            try:
                return await event.edit("❎ Edit closed.", buttons=None)
            except errors.MessageNotModifiedError:
                return
        row = await load(event, uid)
        if row is None:
            return
        if not act:                                        # Edit pressed under the product: open the panel below it
            edits.pop(sender, None)
            forget_menu_prompt(sender)
            await event.answer()
            text, rows = panel(row)
            return await event.respond(text, buttons=rows)
        if act == "p":                                     # Back: the panel again (this message is edited)
            edits.pop(sender, None)
            await event.answer()
            text, rows = panel(row)
            try:
                return await event.edit(text, buttons=rows)
            except errors.MessageNotModifiedError:
                return
        forget_menu_prompt(sender)
        if act == "c":
            edits[sender] = {"uid": uid, "mode": "caption", "ts": time.time()}
            cur = (row.get("caption") or "").strip()
            await event.answer()
            return await event.edit(
                f"📝 <b>New caption for {esc(uid)}</b>\nSend the text now (formatting is kept; the serial is added at "
                "the end again). <code>-</code> clears the caption.\n\n"
                + (f"<i>Now:</i>\n<blockquote>{esc(cur[:600])}</blockquote>" if cur else "<i>It has no caption now.</i>"),
                buttons=back_cancel(uid))
        if act == "d":
            edits[sender] = {"uid": uid, "mode": "details", "ts": time.time()}
            cur = (row.get("note") or "").strip()
            await event.answer()
            return await event.edit(
                f"🔒 <b>Details of {esc(uid)}</b> (private, never posted)\nSend the new text. "
                "<code>-</code> deletes it.\n\n" + (f"<i>Now:</i>\n{esc(cur[:600])}" if cur else "<i>No details yet.</i>"),
                buttons=back_cancel(uid))
        if act == "m":
            n = int(row.get("media") or 0)
            if n <= 0:
                return await event.answer("This is a text-only product - it has no file.", alert=True)
            if n > 1:                                      # album: which file?
                btns = [B(f"File {i + 1}", f"fe:{uid}:m{i}".encode()) for i in range(min(n, 10))]
                rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
                await event.answer()
                return await event.edit(f"🖼 <b>{esc(uid)}</b> has {n} files. Which one do you want to replace?",
                                        buttons=rows + back_cancel(uid))
            act = "m0"
        if act.startswith("m") and act[1:].isdigit():
            idx = int(act[1:])
            edits[sender] = {"uid": uid, "mode": "media", "idx": idx, "ts": time.time()}
            await event.answer()
            return await event.edit(
                f"🖼 <b>New file for {esc(uid)}</b>" + (f" (file {idx + 1})" if int(row.get("media") or 0) > 1 else "")
                + "\nSend the photo, video or document now. The caption stays as it is.", buttons=back_cancel(uid))
        await event.answer()

    # ---------------------------------------------------------------- what the admin sends
    def waiting(e) -> bool:
        st = edits.get(e.sender_id)
        if st is None or not e.is_private:
            return False
        if time.time() - st["ts"] > EDIT_TTL:
            edits.pop(e.sender_id, None)
            return False
        return not (e.raw_text or "").startswith("/")

    @bot.on(events.NewMessage(func=waiting))
    async def on_edit_input(event):
        if not is_admin(event.sender_id):
            return
        st = edits.get(event.sender_id)
        if st is None:
            return
        uid, mode = st["uid"], st["mode"]
        retry = back_cancel(uid)

        async def done(lines: list[str]) -> None:
            edits.pop(event.sender_id, None)
            await event.respond(f"<b>{esc(uid)}</b>\n" + "\n".join(lines),
                                buttons=[[B("✏️ Edit again", f"fe:{uid}:p".encode())]], link_preview=False)

        try:
            row = await h.get_row(uid)
            if row is None:
                edits.pop(event.sender_id, None)
                return await event.respond("❌ This product was not found any more.")
            if mode == "details":
                text = (event.raw_text or "").strip()
                if not text or event.media:
                    return await event.respond("Send the details as text (or <code>-</code> to delete them).",
                                               buttons=retry)
                if text == "-":
                    await db.fwd_set_note(uid, None)
                    return await done(["🗑 Details deleted (record only - they were never posted)."])
                if len(text) > NOTE_MAX:
                    return await event.respond(f"❌ Too long: details hold {NOTE_MAX} characters (yours: {len(text)}).",
                                               buttons=retry)
                await db.fwd_set_note(uid, text)
                return await done(["✅ Details saved (record only - they are never posted).", f"🔒 {esc(text)}"])
            if mode == "caption":
                if event.media or not (event.raw_text or "").strip():
                    return await event.respond("Send the new caption as a text message (or <code>-</code>).",
                                               buttons=retry)
                raw = event.message.raw_text.strip()
                ents = event.message.entities
                if raw == "-":
                    raw, ents = "", None
                else:
                    lead = len(event.message.raw_text) - len(event.message.raw_text.lstrip())
                    if lead:                                # entity offsets move when leading blanks are cut
                        ents = [_shift(e, -lead) for e in (ents or []) if e.offset >= lead] or None
                status = await event.respond("⏳ <i>Updating…</i>")
                try:
                    lines = await editor.edit_caption(row, raw, ents)
                finally:
                    await _quiet(status.delete())
                return await done(lines)
            if mode == "media":
                return await _handle_media(event, row, st, done, retry)
        except EditError as exc:
            await event.respond(f"❌ {esc(str(exc))}", buttons=retry)
        except Exception as exc:
            log.exception("product edit failed")
            await event.respond(f"❌ Failed: {esc(short_error(exc))}", buttons=retry)
        finally:
            if stop is not None:
                raise stop                                  # the watermark handlers must not also process this

    async def _handle_media(event, row, st, done, retry):
        msg = event.message
        kind = _kind(msg)
        if kind is None:
            return await event.respond("Send a <b>photo, video or document</b> (not text).", buttons=retry)
        size = getattr(getattr(msg, "file", None), "size", 0) or 0
        limit = max((getattr(getattr(fw, "pipeline", None), "max_bytes", 0) or 0) for _u, _m, fw in h.clients()) \
            if h.clients() else 0
        if limit and size > limit:
            return await event.respond(f"❌ That file is {size // 1048576} MB; the upload limit of the account is "
                                       f"{limit // 1048576} MB.", buttons=retry)
        status = await event.respond("⏳ <i>Downloading the new file…</i>")
        tmp = tempfile.mkdtemp(prefix="zeba-edit-")
        try:
            path = await msg.download_media(file=tmp + "/")
            if not path:
                raise EditError("The file could not be downloaded.")
            if kind in ("photo", "video", "gif") and _wm_active():
                path = await _watermark(path, kind)
            await _quiet(status.edit("⏳ <i>Replacing the file in the channel…</i>"))
            lines = await editor.replace_file(row, st.get("idx", 0), str(path), force_document=kind == "document",
                                              streaming=kind in ("video", "gif"))
        finally:
            await _quiet(status.delete())
            safe_rmtree(tmp)
        await done(lines)

    return editor


# ------------------------------------------------------------------ small helpers
def _kind(msg) -> str | None:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "gif", None):
        return "gif"
    if getattr(msg, "document", None):
        return "document"
    return None


def _wm_active() -> bool:
    try:
        from unified_services import watermark
        return watermark.is_active(settings, "fwd")      # the same switch as for forwarded media
    except Exception:
        return False


async def _watermark(path: str, kind: str) -> str:
    from unified_services import watermark
    try:
        return await watermark.apply(path, settings, kind)
    except Exception as exc:
        log.warning("watermark skipped for the replacement file: %s", exc)
        return path


def _shift(entity, delta: int):
    import copy
    e = copy.copy(entity)
    e.offset = entity.offset + delta
    return e


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception:
        pass
