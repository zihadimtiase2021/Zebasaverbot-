"""Admin control panel (Telegram bot)."""
from __future__ import annotations

import logging

from telethon import Button, errors, events

from unified_core.config import settings
from unified_core.database import db
from unified_services.context import App

log = logging.getLogger("zeba.control")

INFO_TEXT = (
    "✨ <b>Zeba Post Scraper</b>\n\n"
    "This bot is a private control panel. Only admins can use it."
)
HELP_TEXT = (
    "📖 <b>Guide</b>\n"
    "• <code>.dl &lt;url&gt;</code> in any chat — download &amp; repost (<code>.dl -f</code> to force)\n"
    "• <code>.dl &lt;url&gt; your caption</code> — post with your own caption (no translation)\n"
    "• <code>.dlauto on</code> / <code>.dlauto off</code> — in that chat, links (even without <code>.dl</code>) are downloaded and posted <b>in the same chat</b>\n"
    "• <code>.autodl</code> / <code>.autodl off</code> — same auto links, but posted to your channel\n"
    "• Send links in <b>Saved Messages</b> — always auto\n"
    "• Send a <b>.json / .txt</b> file with URLs (auto chat, or caption <code>.batch</code>)\n"
    "• <code>.zstat</code> — queue status · <code>.ai</code> — translation status · <code>.ai test</code> — test every provider · <code>.ai ollama|openrouter|openai|g4f|off|auto</code> — switch\n"
    "• <code>.xacc</code> — X accounts &amp; rate-limit status · <code>.xadd user auth_token ct0</code> — add a "
    "fallback account · <code>.xdel user</code> · <code>.xunlock</code>\n"
    "• <code>.settings</code> — live settings · <code>.target @channel</code> — switch the target channel · <code>.target list</code> · "
    "<code>.source on|off</code> — source link in captions · <code>.comments on|off|10</code> — repost comments (changes are instant and saved)\n"
    "• <code>/wm</code> — watermark settings (text, font, colours, position) · send any <b>photo/video</b> here and it comes back watermarked\n"
    "• <b>Buttons:</b> the main menu (/start) has Watermark, Settings, Translation and Forward — everything above can be changed by tapping, commands are optional\n"
    "• <b>Accounts:</b> <code>/login</code> add another Telegram account · <code>/accounts</code> list &amp; logout · "
    "<code>/logout 2</code> · <code>/resume &lt;key&gt;</code> log back in with the session key you got at login\n"
    "• <b>Forward with several accounts:</b> 🔁 Forward → <b>👤 Account</b> picks whose routes you manage; when you add a route and "
    "more than one account is in both chats, you choose which account does the copying\n"
    "• Web panel: <code>/admin</code> on your service URL (same switches, no redeploy)\n"
    "• Unfinished batches continue automatically after a restart"
)


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
            "✨ <b>ZEBA POST SCRAPER PRO</b> ✨\n\n"
            f"🤖 <b>Global Auto Mode:</b> {'⚡ ON' if auto else '⚪ OFF'}\n"
            f"⚙️ <b>Queue:</b> {s['pending']} pending · {s['active']} active\n"
            f"📈 <b>Session:</b> ✅ {s['ok']} · ⏭ {s['skipped']} · ❌ {s['failed']}\n\n"
            "👇 <i>Select an action:</i>"
        )
        buttons = [
            [Button.inline(f"🤖 Auto Mode: {'ON ⚡' if auto else 'OFF ⚪'}", b"toggle_auto")],
            [Button.inline("💧 Watermark", b"m:wm"), Button.inline("⚙️ Settings", b"m:set")],
            [Button.inline("🌐 Translation", b"m:tr"), Button.inline("🔁 Forward", b"m:fw")],
            [Button.inline("📊 Stats", b"stats"), Button.inline("📖 Help", b"help")],
            [Button.inline("🔄 Reset", b"reset_config"), Button.inline("♻️ Restart", b"restart_server")],
        ]
        if accounts is not None:
            n = accounts.total
            buttons.insert(3, [Button.inline(f"👥 Accounts ({n})", b"acc:list")])
        return text, buttons

    from unified_services.bot_menus import register_bot_menus
    register_bot_menus(app, bot, is_admin, accounts)
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
            await event.respond("✅ <b>Status updates will be sent here.</b>")
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
        answer = await wm_command(app.runtime, arg)
        if answer == "__PREVIEW__":
            return await wm_handlers.send_preview(bot, event.chat_id)
        await event.respond(answer)

    async def run_wm(event, msgs):
        from unified_services import wm_handlers

        if not (settings.WM_TEXT and settings.WM_ENABLED):
            return await event.respond("ℹ️ Watermark is off. Set a text and switch it on from the menu:",
                                       buttons=[[Button.inline("💧 Watermark menu", b"m:wm")]])
        status = await event.respond("⏳ Adding watermark…")
        err = await wm_handlers.process_media(bot, msgs, event.chat_id)
        try:
            if err:
                await status.edit(f"❌ Watermark failed: {err}")
            else:
                await status.delete()
        except Exception:
            pass

    @bot.on(events.Album(func=lambda e: e.is_private))
    async def wm_album(event):
        from unified_services import wm_handlers

        if is_admin(event.sender_id) and any(wm_handlers.media_kind(m) for m in event.messages):
            await run_wm(event, list(event.messages))

    @bot.on(events.NewMessage(func=lambda e: e.is_private and e.media is not None and not e.grouped_id))
    async def wm_media(event):
        if not is_admin(event.sender_id):
            return
        from unified_services import wm_handlers

        fname = (getattr(event.file, "name", "") or "").lower()
        if fname.endswith((".ttf", ".otf", ".ttc")):  # a font file: store it, offer to use it
            try:
                name = await wm_handlers.save_font(bot, event.message)
                return await event.respond(f"✅ Font added: <b>{name}</b>",
                                           buttons=[[Button.inline("✅ Use this font", f"m:fu:{name}"[:60].encode())]])
            except ValueError as exc:
                return await event.respond(f"❌ {exc}")
        if wm_handlers.media_kind(event.message) is None:
            return
        await run_wm(event, event.message)

    @bot.on(events.CallbackQuery(data=b"toggle_auto"))
    async def toggle_auto(event):
        if not await admin_only(event):
            return
        new = not await db.get_auto_global()
        await db.set_auto_global(new)
        await event.answer(f"Global auto mode {'enabled' if new else 'disabled'}", alert=True)
        await show(event, edit=True)

    @bot.on(events.CallbackQuery(data=b"reset_config"))
    async def reset(event):
        if not await admin_only(event):
            return
        await db.delete_setting("auto_dl")
        await event.answer("Reset to defaults", alert=True)
        await show(event, edit=True)

    @bot.on(events.CallbackQuery(data=b"stats"))
    async def stats(event):
        if not await admin_only(event):
            return
        st = await db.job_stats()
        users = await db.user_count()
        text = (
            "📊 <b>Stats (all time)</b>\n\n"
            f"✅ Done: <b>{st['done']}</b> (last 24h: {st['last_24h']})\n"
            f"❌ Failed: <b>{st['failed']}</b>\n"
            f"🔄 Running: <b>{st['running']}</b>\n"
            f"👥 Bot users: <b>{users}</b>"
        )
        await event.edit(text, buttons=[[Button.inline("🔙 Back", b"menu")]])

    @bot.on(events.CallbackQuery(data=b"help"))
    async def help_cb(event):
        if not await admin_only(event):
            return
        await event.edit(HELP_TEXT, buttons=[[Button.inline("🔙 Back", b"menu")]])

    @bot.on(events.CallbackQuery(data=b"menu"))
    async def back(event):
        await show(event, edit=True)

    @bot.on(events.CallbackQuery(data=b"restart_server"))
    async def restart(event):
        if not await admin_only(event):
            return
        await event.answer("Restarting…", alert=True)
        log.warning("Restart requested by admin %s", event.sender_id)
        app.request_stop(code=3)
