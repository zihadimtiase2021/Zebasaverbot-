"""Admin control panel (Telegram bot)."""
from __future__ import annotations

import html
import logging

from telethon import Button, errors, events

from unified_core.config import settings
from unified_core.database import db
from unified_core.utils import short_error
from unified_services import ui
from unified_services.context import App

log = logging.getLogger("zeba.control")

INFO_TEXT = (
    ui.head("✨", "Zeba Post Scraper") + "\n\n"
    "This bot is a private control panel. Only admins can use it."
)

# Help is split into short topic pages (one tap each) instead of one wall of text.
HELP_PAGES: dict[str, tuple[str, str]] = {
    "dl": ("📥 Download & repost", (
        "• <code>.dl &lt;url&gt;</code> in any chat — download &amp; repost (<code>.dl -f</code> to force)\n"
        "• <code>.dl &lt;url&gt; your caption</code> — post with your own caption (no translation)\n"
        "• <code>.dlauto on</code> / <code>.dlauto off</code> — in that chat, links (even without <code>.dl</code>) are "
        "downloaded and posted <b>in the same chat</b>\n"
        "• <code>.autodl</code> / <code>.autodl off</code> — same auto links, but posted to your channel\n"
        "• Links sent in <b>Saved Messages</b> are always auto\n"
        "• Send a <b>.json / .txt</b> file with URLs (auto chat, or caption <code>.batch</code>)\n"
        "• <code>.zstat</code> — queue status\n"
        "• Unfinished batches continue automatically after a restart")),
    "set": ("⚙️ Settings, translation & X", (
        "• <code>.settings</code> — live settings · <code>.target @channel</code> — switch the target channel · "
        "<code>.target list</code>\n"
        "• <code>.source on|off</code> — source link in captions · <code>.comments on|off|10</code> — repost comments "
        "(changes are instant and saved)\n"
        "• <code>.ai</code> — translation status · <code>.ai test</code> — test every provider · "
        "<code>.ai ollama|openrouter|openai|g4f|off|auto</code> — switch\n"
        "• <code>.xacc</code> — X accounts &amp; rate-limit status · <code>.xadd user auth_token ct0</code> — add a "
        "fallback account · <code>.xdel user</code> · <code>.xunlock</code>\n"
        "• Web panel: <code>/admin</code> on your service URL (same switches, no redeploy)")),
    "wm": ("💧 Watermark", (
        "• <code>/wm</code> — watermark settings (text, font, colours, position)\n"
        "• Send any <b>photo / video</b> here and it comes back watermarked\n"
        "• Send a <b>.ttf / .otf</b> file to add your own font")),
    "fwd": ("🔁 Forward & accounts", (
        "• <b>Forward:</b> 🔁 Forward in the main menu — add routes, Forward all, ban words, bulk delete\n"
        "• <b>Several accounts:</b> <code>/login</code> add another Telegram account · <code>/accounts</code> list &amp; "
        "logout · <code>/logout 2</code> · <code>/resume &lt;key&gt;</code> log back in with the session key you got at login\n"
        "• 🔁 Forward → <b>👤 Account</b> picks whose routes you manage; when you add a route and more than one account "
        "is in both chats, you choose which account does the copying")),
    "find": ("🔎 Find a product", (
        "• <code>/find 42</code> (serial) or <code>/find words</code> — full details + private note\n"
        "• <code>/addnote 42 text</code> · <code>/removenote 42</code> · <code>/defnote</code> default note per source "
        "group / channel\n"
        "• Notes are visible only here, never in the channel, and are backed up in Saved Messages")),
}
HELP_TEXT = ui.head("📖", "Guide") + "\n\n" + "\n\n".join(f"<b>{t}</b>\n{b}" for t, b in HELP_PAGES.values())


def help_index() -> tuple[str, list]:
    rows = ui.grid([ui.btn(title, f"help:{key}") for key, (title, _b) in HELP_PAGES.items()], 2)
    text = (ui.head("📖", "Guide", "Pick a topic.") + "\n"
            + ui.hint("Everything can also be done with the buttons in the main menu — commands are optional."))
    return text, rows + [ui.nav_row(None, close=True)]


def register_control_bot(app: App, accounts=None) -> None:
    bot = app.control_bot
    assert bot is not None
    bot.parse_mode = "html"

    def is_admin(uid: int | None) -> bool:
        return uid in settings.admin_ids

    async def menu(uid: int):
        if not is_admin(uid):
            return INFO_TEXT, None
        auto = await db.get_auto_global()
        s = app.queue.snapshot()
        text = (
            ui.head("✨", "ZEBA POST SCRAPER PRO") + "\n\n"
            f"🤖 <b>Auto mode:</b> {'⚡ ON' if auto else '⚪ OFF'}\n"
            f"⚙️ <b>Queue:</b> {s['pending']} pending · {s['active']} active\n"
            f"📈 <b>Session:</b> ✅ {s['ok']} · ⏭ {s['skipped']} · ❌ {s['failed']}\n\n"
            + ui.hint("Select an action 👇")
        )
        buttons = [
            [Button.inline(f"🤖 Auto Mode: {'ON ⚡' if auto else 'OFF ⚪'}", b"toggle_auto")],
            [Button.inline("💧 Watermark", b"m:wm"), Button.inline("⚙️ Settings", b"m:set")],
            [Button.inline("🌐 Translation", b"m:tr"), Button.inline("🔁 Forward", b"m:fw")],
            [Button.inline("📊 Stats", b"stats"), Button.inline("📖 Help", b"help")],
            [Button.inline("🔄 Reset", b"reset_config"), Button.inline("♻️ Restart", b"restart_server")],
            ui.close_row(),
        ]
        if accounts is not None:
            n = accounts.total
            buttons.insert(3, [Button.inline(f"👥 Accounts ({n})", b"acc:list")])
        return text, buttons

    from unified_services.bot_menus import register_bot_menus
    register_bot_menus(app, bot, is_admin, accounts)
    from unified_services.find_cmds import register_find_commands
    found = register_find_commands(bot, is_admin, accounts, app)
    from unified_services.edit_cmds import register_edit_commands
    register_edit_commands(bot, is_admin, app, found)   # ✏️ Edit under every /find product
    if accounts is not None:
        from unified_services.account_bot import register_account_commands
        register_account_commands(app, accounts, is_admin)

    async def show(event, edit: bool) -> None:
        text, buttons = await menu(event.sender_id)
        if edit:
            try:
                await event.edit(text, buttons=buttons)
            except errors.MessageNotModifiedError:
                pass
        else:
            await event.respond(text, buttons=buttons)

    async def admin_only(event) -> bool:
        if is_admin(event.sender_id):
            return True
        await event.answer("Unauthorized", alert=True)
        return False

    @bot.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$", func=lambda e: e.is_private))
    async def start(event):
        await db.register_user(event.sender_id)
        if is_admin(event.sender_id):
            await event.respond(ui.ok("<b>Status updates will be sent here.</b>"))
        await show(event, edit=False)

    @bot.on(events.NewMessage(pattern=r"^/wm(?:@\w+)?(?:\s|$)", func=lambda e: e.is_private))
    async def wm_cmd(event):
        if not is_admin(event.sender_id) or app.runtime is None:
            return
        from unified_services import wm_handlers
        from unified_services.runtime_config import wm_command

        arg = event.raw_text.partition(" ")[2].strip()
        if not arg:  # plain /wm -> the button menu
            from unified_services.bot_menus import s_wm_entry
            text, buttons = s_wm_entry(app)
            return await event.respond(text, buttons=buttons)
        try:
            answer = await wm_command(app.runtime, arg)
            if answer == "__PREVIEW__":
                return await wm_handlers.send_preview(bot, event.chat_id)
        except Exception as exc:
            return await event.respond(ui.fail("Failed", short_error(exc)), buttons=[ui.nav_row(b"m:wm")])
        await event.respond(answer, buttons=[ui.nav_row(b"m:wm", back_label="💧 Watermark menu")])

    async def run_wm(event, msgs):
        from unified_services import wm_handlers

        if not (settings.WM_TEXT and settings.WM_ENABLED):
            return await event.respond(ui.info("Watermark is off. Set a text and switch it on from the menu."),
                                       buttons=[[Button.inline("💧 Watermark menu", b"m:wm")], ui.nav_row(None)])
        status = await event.respond(ui.busy("Adding watermark…"))
        err = await wm_handlers.process_media(bot, msgs, event.chat_id)
        try:
            if err:
                await status.edit(ui.fail("Watermark failed", str(err)))
            else:
                await status.delete()
        except Exception:
            pass

    @bot.on(events.Album(func=lambda e: e.is_private and e.sender_id not in getattr(app, "editing", ())))
    async def wm_album(event):
        from unified_services import wm_handlers

        if is_admin(event.sender_id) and any(wm_handlers.media_kind(m) for m in event.messages):
            await run_wm(event, list(event.messages))

    @bot.on(events.NewMessage(func=lambda e: e.is_private and e.media is not None and not e.grouped_id
                              and e.sender_id not in getattr(app, "editing", ())))
    async def wm_media(event):
        if not is_admin(event.sender_id):
            return
        from unified_services import wm_handlers

        fname = (getattr(event.file, "name", "") or "").lower()
        if fname.endswith((".ttf", ".otf", ".ttc")):  # a font file: store it, offer to use it
            try:
                name = await wm_handlers.save_font(bot, event.message)
                return await event.respond(ui.ok(f"Font added: <b>{html.escape(str(name))}</b>"),
                                           buttons=[[Button.inline("✅ Use this font", f"m:fu:{name}"[:60].encode())]])
            except ValueError as exc:
                return await event.respond(ui.fail("Could not add the font", str(exc)))
        if wm_handlers.media_kind(event.message) is None:
            return
        await run_wm(event, event.message)

    @bot.on(events.CallbackQuery(data=b"toggle_auto"))
    async def toggle_auto(event):
        if not await admin_only(event):
            return
        new = not await db.get_auto_global()
        await db.set_auto_global(new)
        await event.answer(f"Auto mode {'enabled ⚡' if new else 'disabled'}")   # a toast: no popup to dismiss
        await show(event, edit=True)

    @bot.on(events.CallbackQuery(data=b"reset_config"))
    async def reset(event):
        if not await admin_only(event):
            return
        await event.answer()
        await event.edit(ui.head("🔄", "Reset to defaults?", "Auto mode goes back to its default setting."),
                         buttons=[ui.confirm_row("🔄 Yes, reset", b"reset_config:y", ui.D_MAIN)])

    @bot.on(events.CallbackQuery(data=b"reset_config:y"))
    async def reset_yes(event):
        if not await admin_only(event):
            return
        await db.delete_setting("auto_dl")
        await event.answer("Reset to defaults ✅")
        await show(event, edit=True)

    @bot.on(events.CallbackQuery(data=b"stats"))
    async def stats(event):
        if not await admin_only(event):
            return
        st = await db.job_stats()
        users = await db.user_count()
        text = (
            ui.head("📊", "Stats", "All time") + "\n\n"
            f"✅ Done: <b>{st['done']}</b> (last 24h: {st['last_24h']})\n"
            f"❌ Failed: <b>{st['failed']}</b>\n"
            f"🔄 Running: <b>{st['running']}</b>\n"
            f"👥 Bot users: <b>{users}</b>"
        )
        try:
            await event.edit(text, buttons=[[Button.inline(ui.L_REFRESH, b"stats")], ui.nav_row(None, close=True)])
        except errors.MessageNotModifiedError:
            await event.answer("Up to date")

    @bot.on(events.CallbackQuery(data=b"help"))
    async def help_cb(event):
        if not await admin_only(event):
            return
        text, buttons = help_index()
        await event.edit(text, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=rb"^help:(\w+)$"))
    async def help_page(event):
        if not await admin_only(event):
            return
        page = HELP_PAGES.get(event.pattern_match.group(1).decode())
        if page is None:
            return await event.answer("Unknown topic")
        title, body = page
        await event.edit(ui.head("📖", title) + "\n\n" + body, buttons=[ui.nav_row(b"help", back_label="🔙 Guide")])

    @bot.on(events.CallbackQuery(data=b"menu"))
    async def back(event):
        await show(event, edit=True)

    @bot.on(events.CallbackQuery(data=ui.D_CLOSE))
    async def close(event):
        """✖️ Close: remove the panel message (admins only; nothing else is touched)."""
        if not await admin_only(event):
            return
        try:
            await event.delete()
        except Exception:
            await event.answer("Could not close it")
        else:
            await event.answer()

    @bot.on(events.CallbackQuery(data=b"restart_server"))
    async def restart(event):
        if not await admin_only(event):
            return
        await event.answer()
        await event.edit(ui.head("♻️", "Restart the bot?", "It is back in a few seconds. Unfinished batches "
                                                          "continue automatically afterwards."),
                         buttons=[ui.confirm_row("♻️ Yes, restart", b"restart_server:y", ui.D_MAIN)])

    @bot.on(events.CallbackQuery(data=b"restart_server:y"))
    async def restart_yes(event):
        if not await admin_only(event):
            return
        await event.answer("Restarting…")
        try:
            await event.edit(ui.busy("Restarting… the panel is back in a few seconds (send /start)."))
        except Exception:
            pass
        log.warning("Restart requested by admin %s", event.sender_id)
        app.request_stop(code=3)
