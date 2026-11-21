"""Button menus of the control bot: every live setting can be changed with taps, no commands needed.

Screens: Watermark (text, font, colours, position, size, switches, preview) · Settings (target channel, caption and
comment switches) · Translation (provider) · Forward routes (watch on/off, Forward all, progress, links, ban words, delete, add). Text input (watermark text, hex colour,
channel, route chats) is asked in chat: tap the button, then send the value (Cancel button on every prompt).

Several Telegram accounts: the Forward screens work on ONE account at a time (👤 Account button = pick it). When a new
route is added and more than one account is a member of both chats, you choose which account does the copying.
"""
from __future__ import annotations

import contextvars
import html
import logging
import time

from telethon import Button, errors, events

from unified_core.utils import short_error
from unified_services import ui, watermark
from unified_services.runtime_config import NUMBERS, TOGGLES, wm_status

log = logging.getLogger("zeba.menus")
B = Button.inline
MAIN = ui.main_row()
ON = ui.onoff

WM_SW = {"WM_ENABLED": "📥 Media sent to bot", "WM_ON_URL": "🔗 Downloaded links", "WM_ON_FWD": "🔁 Forwarded media"}
SET_SW = {"INCLUDE_SOURCE_LINK": "🔗 Source link", "COMMENTS_ENABLED": "💬 Repost comments",
          "INCLUDE_COMMENT_COUNT": "🔢 Comment count", "INCLUDE_ORIGINAL_CAPTION": "📝 Original caption",
          "TRANSLATE_ENABLED": "🌐 Bangla translation"}
STEP = {"WM_ANGLE": 15, "WM_SIZE": 1, "WM_MARGIN": 1, "WM_OPACITY": 10, "WM_BG_OPACITY": 10, "MAX_COMMENTS": 5}
COLORS = [("⚪ White", "FFFFFF"), ("⚫ Black", "000000"), ("🔴 Red", "EF4444"), ("🟡 Yellow", "FFD400"),
          ("🟢 Green", "22C55E"), ("🔵 Blue", "3B82F6"), ("🟣 Purple", "A855F7"), ("🟠 Orange", "F97316"),
          ("🩶 Gray", "808080")]
GRID = [["tl", "tc", "tr"], ["ml", "mc", "mr"], ["bl", "bc", "br"]]
ARROW = {"tl": "↖️", "tc": "⬆️", "tr": "↗️", "ml": "⬅️", "mc": "⏺", "mr": "➡️", "bl": "↙️", "bc": "⬇️", "br": "↘️"}
FONTS_PER_PAGE = 8


def register_bot_menus(app, bot, is_admin, accounts=None) -> None:
    pending: dict[int, dict] = {}  # admin id -> what the next text message is for
    choices: dict[int, dict] = {}  # admin id -> new route waiting for "which account should copy it?"
    plans: dict[int, dict] = {}    # admin id -> bulk delete waiting for the confirmation {"plan", "client", "back"}
    sel: dict[int, int] = {}       # admin id -> user id of the account whose Forward screens are shown
    running: dict[int, dict] = {}  # admin id -> bulk delete that is running now {"stop": bool}
    PLAN_TTL = 600                 # seconds a bulk delete confirmation stays valid
    app._menu_pending = pending    # find edit (edit_cmds) clears a half-finished prompt of the same admin
    who: contextvars.ContextVar[int] = contextvars.ContextVar("zeba_menu_admin", default=0)

    # ------------------------------------------------------------------ accounts (primary + extra userbots)
    def primary_label() -> str:
        user = getattr(accounts, "primary_username", "") if accounts is not None else ""
        name = getattr(accounts, "primary_name", "") if accounts is not None else ""
        return f"@{user}" if user else (name or "main account")

    def fw_accounts() -> list[tuple[int, str, object]]:
        """[(user id, label, app)] - the primary account first, then every extra account that is running."""
        rows = [(getattr(app, "me_id", 0), primary_label(), app)]
        if accounts is not None:
            rows += [(inst.me_id, inst.label, inst.app) for inst in accounts.instances.values()]
        return rows

    def fw_entry():
        """The account whose forwarding the current admin is looking at (the primary one by default)."""
        rows = fw_accounts()
        want = sel.get(who.get())
        return next((r for r in rows if r[0] == want), rows[0])

    def fw_app():
        return fw_entry()[2]

    def fwd():
        return fw_app().forwarder

    def rt():
        return app.runtime

    # ------------------------------------------------------------------ screens: each returns (text, buttons)
    def sw_rows(keys: dict, c) -> list:
        return [[B(f"{label}: {ON(getattr(c, k))}", f"m:t:{k}".encode())] for k, label in keys.items()]

    def num_row(key: str, label: str, c, unit: str = "") -> list:
        """Two rows: the full-width value label, then ➖ / ➕ side by side (step shown on the buttons)."""
        st = STEP[key]
        return [[B(f"{label}: {getattr(c, key)}{unit}", b"m:noop")],
                [B(f"➖ {st}", f"m:n:{key}:-".encode()), B(f"➕ {st}", f"m:n:{key}:+".encode())]]

    def s_wm():
        c = rt().cfg
        btn = [[B("✏️ Text", b"m:ask:WM_TEXT"), B("🔤 Font", b"m:fp:a:0")],
               [B("🎨 Text color", b"m:col:WM_TEXT_COLOR"), B("🟫 Box color", b"m:col:WM_BG_COLOR")],
               [B("📍 Position", b"m:pos"), B("📏 Size / rotate", b"m:look")],
               *sw_rows(WM_SW, c),
               [B("👁 Preview", b"m:prev"), B("🧹 Reset", b"m:wmreset")], MAIN]
        return wm_status(rt()) + "\n\n" + ui.hint("Tap a button to change it. The switches decide where the watermark is used."), btn

    def s_look():
        c = rt().cfg
        return (ui.head("📏", "Size, rotation & opacity") + "\n"
                + ui.hint("Size and margin are % of the picture width. Rotation: + turns the text counter-clockwise."),
                [*num_row("WM_SIZE", "📏 Size", c, "%"), *num_row("WM_ANGLE", "🔄 Rotate", c, "°"),
                 *num_row("WM_OPACITY", "💧 Text opacity", c, "%"), *num_row("WM_BG_OPACITY", "🟫 Box opacity", c, "%"),
                 *num_row("WM_MARGIN", "↔️ Margin", c, "%"),
                 [B("↺ Reset rotation", b"m:n:WM_ANGLE:0"), B("👁 Preview", b"m:prev")], ui.nav_row(b"m:wm")])

    def s_pos():
        cur = rt().cfg.WM_POSITION
        rows = [[B(("✅ " if p == cur else "") + ARROW[p], f"m:ps:{p}".encode()) for p in r] for r in GRID]
        return (ui.head("📍", "Where should the watermark go?", f"Now: <b>{watermark.POSITIONS.get(cur, cur)}</b>"),
                rows + [ui.nav_row(b"m:wm")])

    def s_color(key: str):
        cur = getattr(rt().cfg, key)
        title = "Text color" if key == "WM_TEXT_COLOR" else "Box (background) color"
        rows, row = [], []
        for label, hx in COLORS:
            row.append(B(label, f"m:cs:{key}:{hx}".encode()))
            if len(row) == 3:
                rows.append(row)
                row = []
        rows.append(row) if row else None
        extra = [B("✏️ Custom hex", f"m:ask:{key}".encode())]
        if key == "WM_BG_COLOR":
            extra.insert(0, B("🚫 No box", f"m:cs:{key}:none".encode()))
        return ui.head("🎨", title, f"Now: <b>{html.escape(cur)}</b>"), rows + [extra, ui.nav_row(b"m:wm")]

    def fonts_for(flt: str) -> list[str]:
        names = watermark.list_fonts(rt().cfg.DATA_DIR)
        if flt == "b":
            names = [n for n in names if "beng" in n.lower() or "bangla" in n.lower() or "lohit" in n.lower()]
        return names

    def s_fonts(flt: str, page: int):
        names = fonts_for(flt)
        pages = max(1, -(-len(names) // FONTS_PER_PAGE))
        page = max(0, min(page, pages - 1))
        chunk = names[page * FONTS_PER_PAGE:(page + 1) * FONTS_PER_PAGE]
        cur = rt().cfg.WM_FONT
        btns = [B(("✅ " if n == cur else "") + n[:28], f"m:fs:{flt}:{page * FONTS_PER_PAGE + i}".encode())
                for i, n in enumerate(chunk)]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        nav = ui.pager_row(f"m:fp:{flt}:", page, pages)
        sw = B("🇧🇩 Bangla fonts", b"m:fp:b:0") if flt == "a" else B("🔤 All fonts", b"m:fp:a:0")
        msg = (ui.head("🔤", "Font", f"Now: <b>{html.escape(cur)}</b>") + "\n"
               + ui.hint("For Bangla text choose a Bengali font. Send a .ttf / .otf file here to add your own."))
        if not names:
            msg += "\n\n" + ui.hint("No fonts found" + (" (no Bengali font installed)." if flt == "b" else "."))
        return msg, rows + [nav, [sw], ui.nav_row(b"m:wm")]

    def s_settings():
        c = rt().cfg
        t = rt().target_info()
        txt = (ui.head("⚙️", "Settings") + "\n"
               f"🎯 Target channel: <b>{html.escape(str(t['title']))}</b> <code>{t['id']}</code>\n\n"
               + ui.hint("Changes apply to the next post and are saved."))
        return txt, [[B("🎯 Change target channel", b"m:tg")], *sw_rows(SET_SW, c),
                     *num_row("MAX_COMMENTS", "💬 Max comments", c), MAIN]

    async def s_target():
        chans = await rt().list_channels()
        cur = rt().target_info()["id"]
        rows = [[B(("✅ " if ch["id"] == cur else "") + ch["name"][:40], f"m:tp:{i}".encode())]
                for i, ch in enumerate(chans[:20])]
        txt = (ui.head("🎯", "Choose the target channel") + "\n"
               + ui.hint("The account must be an admin with “Post messages”."))
        if not chans:
            txt += "\n\n" + ui.hint("No channel list available — enter it by hand.")
        return txt, rows + [[B("✏️ Enter id / @username / link", b"m:ask:TARGET")], ui.nav_row(b"m:set")]

    async def s_translation():
        tr = app.pipeline.translator
        from unified_services.translator import MODES
        rows, row = [], []
        for m in MODES:
            row.append(B(("✅ " if tr.mode == m else "") + m, f"m:trm:{m}".encode()))
            if len(row) == 3:
                rows.append(row)
                row = []
        rows.append(row) if row else None
        txt = (ui.head("🌐", "Translation") + "\n" + html.escape(await tr.describe()) + "\n\n"
               + ui.hint("Pick a provider (auto = use the .env order)."))
        return txt, rows + [[B("🧪 Test providers", b"m:trt")], MAIN]

    def s_forward():
        fw = fwd()
        rows = []
        for i, r in enumerate(fw.routes, 1):
            job = fw._job_for(r)
            busy = job is not None and job["state"] == "running"
            rows.append([B(f"{'👁' if r['active'] else '⏸'} #{i} {r['src_name'][:11]}➜{r['dst_name'][:11]}",
                           f"m:fo:{i}".encode()),
                         B("⏹ Stop" if busy else "📥 All", f"m:{'fx' if busy else 'fl'}:{i}".encode()),
                         B("🗑", f"m:fd:{i}".encode())])
        head = fw._fmt_routes().split("\n\n")[0] if fw.routes else ui.head("🔁", "Forward routes", "No routes yet.")
        many = len(fw_accounts()) > 1
        if many:
            head = f"👤 <b>Account: {html.escape(fw_entry()[1])}</b>\n" + head
        txt = (head + "\n\n" + ui.hint("👁/⏸ watch new messages on/off (tap the route) · 📥 All = copy the whole source, "
                                       "only what was not copied before · 🗑 deletes the route."))
        extra = []
        if fw.routes:
            extra.append([B("📥 Forward all (every route)", b"m:fl:0"), B("📊 Progress", b"m:fg")])
        if len(fw.routes) > 1:
            from unified_services.forwarder import MIX_BATCH
            extra.append([B(f"🔀 Mixing: {ON(fw.mixing)}", b"m:fm")])
            txt += ("\n" + ui.hint(f"🔀 Mixing ON = Forward all over several routes sends {MIX_BATCH} messages per route "
                                    f"in turns (route 1: 1-{MIX_BATCH}, route 2: 1-{MIX_BATCH}, route 1: "
                                    f"{MIX_BATCH + 1}-{MIX_BATCH * 2} …). OFF = one route after the other."))
        who_row = [[B(f"👤 Account: {fw_entry()[1][:24]}  ▸ change", b"m:fac")]] if many else []
        return txt, who_row + rows + extra + [
            [B("➕ Add route", b"m:fa"), B("🗑 Bulk delete", b"m:bdl")],
            [B(f"🔗 Remove links: {ON(fw.strip_links)}", b"m:fk"), B(f"🚫 Ban words ({len(fw.bans)})", b"m:bn")],
            MAIN]

    def s_progress():
        fw = fwd()
        return (fw._fmt_progress(), [[B(ui.L_REFRESH, b"m:fg")], ui.nav_row(b"m:fw")])

    def s_accounts():
        """Which account's forwarding the Forward screens show / change."""
        cur = fw_entry()[0]
        rows = []
        for uid, label, a in fw_accounts():
            n = len(getattr(getattr(a, "forwarder", None), "routes", []) or [])
            rows.append([B(f"{'✅ ' if uid == cur else ''}{label[:26]} · {n} route(s)", f"m:fas:{uid}".encode())])
        txt = (ui.head("👤", "Choose the account") + "\n"
               + ui.hint("Every account has its own routes. The Forward screen shows and changes the routes of the "
                         "account you pick here — that account reads the source and posts to the destination."))
        return txt, rows + [ui.nav_row(b"m:fw")]

    def s_bans():
        fw = fwd()
        rows = [[B(f"{b[:30]}", b"m:noop"), B("🗑", f"m:bd:{i}".encode())] for i, b in enumerate(fw.bans)]
        txt = (ui.head("🚫", "Ban list") + "\n"
               + ui.hint("These words / links / @names are removed from every forwarded caption (the rest of the text stays)."))
        if not fw.bans:
            txt += "\n\n" + ui.hint("Nothing banned yet.")
        return txt, rows[:40] + [[B("➕ Add words / links", b"m:ask:BAN")], ui.nav_row(b"m:fw")]

    # ------------------------------------------------------------------ plumbing
    async def put(event, screen, edit: bool = True) -> None:
        text, buttons = await screen if hasattr(screen, "__await__") else screen
        if edit:
            try:
                await event.edit(text, buttons=buttons)
            except errors.MessageNotModifiedError:
                pass
        else:
            await event.respond(text, buttons=buttons)

    async def ask(event, kind: str, prompt: str, back: bytes) -> None:
        getattr(app, "editing", {}).pop(event.sender_id, None)   # a product edit in progress is dropped
        pending[event.sender_id] = {"kind": kind, "back": back}
        await event.edit(prompt + "\n\n" + ui.hint("Send it as a message now."), buttons=[ui.cancel_row()])

    PROMPTS = {
        "WM_TEXT": ui.head("✏️", "Watermark text", "Send the text (e.g. <code>@mychannel</code>). Several lines are fine."),
        "WM_TEXT_COLOR": ui.head("🎨", "Text color", "Send a name (<code>white</code>) or hex (<code>#ffcc00</code>)."),
        "WM_BG_COLOR": ui.head("🎨", "Box color", "Send a name, hex, or <code>none</code> for no box."),
        "TARGET": ui.head("🎯", "Target channel", "Send the channel <code>@username</code>, <code>t.me</code> link "
                                                  "or <code>-100…</code> id."),
        "BAN": ui.head("🚫", "Ban words", "Send words, phrases, links or @names — separate several with a comma or a new line."),
        "BD_START": ui.head("🗑", "Bulk delete") + "\n" + ui.step(1, 2, "send the link of the <b>first</b> (oldest) message "
                    "to delete, e.g. <code>https://t.me/c/2940625991/2236</code>"),
        "FWD_SRC": ui.head("📤", "Add route") + "\n" + ui.step(1, 2, "send the <b>source</b> chat "
                    "(<code>@username</code>, link or id)."),
    }
    BACKS = {"WM_TEXT": b"m:wm", "WM_TEXT_COLOR": b"m:wm", "WM_BG_COLOR": b"m:wm", "TARGET": b"m:set",
             "FWD_SRC": b"m:fw", "FWD_DST": b"m:fw", "BAN": b"m:bn",
             "BD_START": b"m:fw", "BD_END": b"m:fw"}

    async def home(event, back: bytes, note: str = "") -> None:
        """After a typed value: confirmation + the screen the person came from, as a new message."""
        if note:
            await event.respond(note)
        await put(event, SCREENS[back](), edit=False)

    app._wm_screen = s_wm  # used by /wm (plain) in control_bot
    SCREENS = {b"m:wm": s_wm, b"m:set": s_settings, b"m:fw": s_forward, b"m:bn": s_bans}

    # ------------------------------------------------------------------ main-menu buttons
    @bot.on(events.CallbackQuery(data=lambda d: d.startswith(b"m:")))
    async def on_cb(event):
        if not is_admin(event.sender_id):
            return await event.answer("Unauthorized", alert=True)
        if rt() is None:
            return await event.answer("The bot is still starting - try again in a few seconds.", alert=True)
        who.set(event.sender_id)
        p = event.data.decode().split(":")
        a = p[1]
        c = rt().cfg
        try:
            if a == "noop":
                return await event.answer()
            if a == "wm":
                return await put(event, s_wm())
            if a == "set":
                return await put(event, s_settings())
            if a == "tr":
                return await put(event, await s_translation())
            if a == "fw":
                if getattr(fw_app(), "forwarder", None) is None:
                    return await event.answer("Forwarding is not running.", alert=True)
                return await put(event, s_forward())
            if a == "cancel":
                plans.pop(event.sender_id, None)
                ch = choices.pop(event.sender_id, None) or {}
                back = pending.pop(event.sender_id, {}).get("back") or ch.get("back", b"m:wm")
                return await put(event, SCREENS[back]())
            if a == "pos":
                return await put(event, s_pos())
            if a == "look":
                return await put(event, s_look())
            if a == "col":
                return await put(event, s_color(p[2]))
            if a == "ask":
                return await ask(event, p[2], PROMPTS[p[2]], BACKS[p[2]])
            if a == "ps":
                await rt().set_string("WM_POSITION", p[2])
                return await put(event, s_pos())
            if a == "cs":
                await rt().set_string(p[2], "#" + p[3] if p[3] != "none" else "none")
                return await put(event, s_color(p[2]))
            if a == "fp":
                return await put(event, s_fonts(p[2], int(p[3])))
            if a == "fs":
                names = fonts_for(p[2])
                await rt().set_string("WM_FONT", names[int(p[3])])
                await event.answer(f"Font: {names[int(p[3])]}")
                return await put(event, s_fonts(p[2], int(p[3]) // FONTS_PER_PAGE))
            if a == "fu":  # font that was just uploaded
                await rt().set_string("WM_FONT", ":".join(p[2:]))
                return await event.edit(ui.ok("Font selected."), buttons=[[B("💧 Watermark menu", b"m:wm")], MAIN])
            if a == "t":
                key = p[2]
                if key not in TOGGLES:
                    return await event.answer()
                if key.startswith("WM_") and not getattr(c, key) and not c.WM_TEXT:
                    return await event.answer("Set the watermark text first (✏️ Text).", alert=True)
                await rt().set_toggle(key, not getattr(c, key))
                return await put(event, s_wm() if key.startswith("WM_") else s_settings())
            if a == "n":
                key, sign = p[2], p[3]
                if key not in NUMBERS:
                    return await event.answer()
                new = 0 if sign == "0" else getattr(c, key) + (STEP[key] if sign == "+" else -STEP[key])
                await rt().set_number(key, new)
                return await put(event, s_look() if key.startswith("WM_") else s_settings())
            if a == "prev":
                from unified_services import wm_handlers
                await event.answer("Sending preview…")
                return await wm_handlers.send_preview(bot, event.chat_id)
            if a == "wmreset":
                return await event.edit(ui.head("🧹", "Reset watermark?", "This clears the watermark text and switches all "
                                                                      "watermark options off."),
                                        buttons=[ui.confirm_row("🧹 Yes, reset", b"m:wmreset2", b"m:wm")])
            if a == "wmreset2":
                await rt().set_string("WM_TEXT", "")
                for k in WM_SW:
                    await rt().set_toggle(k, False)
                await event.answer("Watermark reset")
                return await put(event, s_wm())
            if a == "tg":
                return await put(event, await s_target())
            if a == "tp":
                chans = await rt().list_channels()
                title = await rt().set_target(str(chans[int(p[2])]["id"]))
                await event.answer(f"Now posting to {title}", alert=True)
                return await put(event, s_settings())
            if a == "trm":
                await event.answer(await app.pipeline.translator.set_mode(p[2]))
                return await put(event, await s_translation())
            if a == "trt":
                await event.answer("Testing every provider…")
                res = await app.pipeline.translator.diagnose()
                return await event.edit(ui.head("🧪", "Translation test") + "\n" + html.escape(res)[:3800],
                                        buttons=[ui.nav_row(b"m:tr")])
            if a in ("fo", "fd"):
                i = int(p[2])
                fw = fwd()
                if not 1 <= i <= len(fw.routes):
                    return await put(event, s_forward())
                if a == "fd":
                    await fw._cmd_fwd(f"del {i}")
                else:
                    await fw._cmd_fwd(f"{'unwatch' if fw.routes[i - 1]['active'] else 'watch'} {i}")
                return await put(event, s_forward())
            if a == "fl":      # Forward all (0 = every route)
                i = int(p[2])
                fw = fwd()
                if i and not 1 <= i <= len(fw.routes):
                    return await put(event, s_forward())
                ans = fw.start_all([fw.routes[i - 1]] if i else list(fw.routes))
                await event.answer(ui.plain(ans), alert=True)
                return await put(event, s_forward())
            if a == "fx":      # stop a running Forward all
                i = int(p[2])
                fw = fwd()
                if 1 <= i <= len(fw.routes):
                    fw.stop_all([fw.routes[i - 1]])
                return await put(event, s_forward())
            if a == "fg":
                return await put(event, s_progress())
            if a == "fm":      # Mixing ON / OFF
                fw = fwd()
                await event.answer(ui.plain(await fw.set_mixing(not fw.mixing)), alert=True)
                return await put(event, s_forward())
            if a == "bdl":     # Bulk delete: step 1
                plans.pop(event.sender_id, None)
                choices.pop(event.sender_id, None)
                return await ask(event, "BD_START", PROMPTS["BD_START"], b"m:fw")
            if a == "bds":     # Bulk delete: stop the running run
                run = running.get(event.sender_id)
                if run is None:
                    return await event.answer("No bulk delete is running.", alert=True)
                run["stop"] = True
                return await event.answer("Stopping after the current batch…")
            if a == "bdy":     # Bulk delete: confirmed
                from unified_services import bulk_delete
                st = plans.pop(event.sender_id, None)
                if st is None or time.time() - st.get("ts", 0) > PLAN_TTL:
                    return await event.answer("This confirmation expired - start the bulk delete again.", alert=True)
                if event.sender_id in running:
                    return await event.answer("A bulk delete is already running - stop it first.", alert=True)
                plan = st["plan"]
                run = running[event.sender_id] = {"stop": False}
                stop_btn = [[B("⏹ Stop", b"m:bds")]]
                await event.edit(ui.head("⏳", "Deleting…", f"{plan.total} id(s) in chunks of {bulk_delete.CHUNK}"),
                                 buttons=stop_btn)
                last_edit = [0.0]

                async def progress(res, event=event, plan=plan):
                    if time.time() - last_edit[0] < 3:      # editing on every chunk would earn a FloodWait
                        return
                    last_edit[0] = time.time()
                    checked = res.done_to - plan.start + 1
                    await event.edit(ui.head("⏳", "Deleting…", f"<code>{ui.bar(checked, plan.total)}</code>\n"
                                                                f"{checked}/{plan.total} checked · 🗑 {res.deleted} deleted"),
                                     buttons=stop_btn)
                try:
                    res = await bulk_delete.delete_range(st["client"], plan, progress,
                                                         should_stop=lambda: run["stop"])
                finally:
                    running.pop(event.sender_id, None)
                return await event.edit(res.text(), buttons=[ui.nav_row(b"m:fw", back_label="🔁 Forward menu")])
            if a == "fk":
                fw = fwd()
                await fw._cmd_fwd("links off" if fw.strip_links else "links on")
                return await put(event, s_forward())
            if a == "bn":
                return await put(event, s_bans())
            if a == "bd":
                fw = fwd()
                if 0 <= int(p[2]) < len(fw.bans):
                    await fw._cmd_fwd(f"unban {int(p[2]) + 1}")
                return await put(event, s_bans())
            if a == "fa":
                choices.pop(event.sender_id, None)
                return await ask(event, "FWD_SRC", PROMPTS["FWD_SRC"], b"m:fw")
            if a == "fac":     # pick the account whose forwarding is shown
                return await put(event, s_accounts())
            if a == "fas":
                if not any(r[0] == int(p[2]) for r in fw_accounts()):
                    await event.answer("That account is not running any more.", alert=True)
                    return await put(event, s_accounts())
                sel[event.sender_id] = int(p[2])
                return await put(event, s_forward())
            if a == "fcs":     # the account chosen for the new route
                ch = choices.pop(event.sender_id, None)
                if ch is None:
                    return await event.answer("This choice expired - add the route again (➕ Add route).", alert=True)
                entry = next((r for r in fw_accounts() if r[0] == int(p[2])), None)
                if entry is None:
                    return await event.answer("That account is not running any more.", alert=True)
                ans = await entry[2].forwarder._cmd_fwd(f"{ch['src']} {ch['dst']}")
                sel[event.sender_id] = entry[0]   # show that account's routes next
                return await home(event, b"m:fw", f"👤 <b>{html.escape(entry[1])}</b>\n" + ans)
        except (ValueError, KeyError, IndexError) as exc:
            return await event.answer(str(exc)[:190] or "Failed", alert=True)
        except Exception as exc:
            log.exception("menu action failed")
            return await event.answer(("Failed: " + short_error(exc))[:190], alert=True)

    async def choose_account(event, src: str, dst: str, back: bytes) -> None:
        """New route with several accounts running: find the accounts that are a member of BOTH chats (and may post
        in the destination). Two or more -> the admin chooses; exactly one -> it is used; none -> error + retry."""
        ok, bad = [], []
        for uid, label, a in fw_accounts():
            fw = getattr(a, "forwarder", None)
            if fw is None:
                bad.append((label, "forwarding is not running"))
                continue
            try:
                why = await fw.can_route(src, dst)
            except Exception as exc:
                why = short_error(exc)
            if why:
                bad.append((label, why))
            else:
                ok.append((uid, label, a))
        if not ok:
            lines = "\n".join(f"• {html.escape(lb)}: {html.escape(why)}" for lb, why in bad)
            raise ValueError("no account can make this route:\n" + lines)
        note = ""
        if bad:
            note = "\n\n<i>Not available:</i>\n" + "\n".join(
                f"• {html.escape(lb)}: {html.escape(why)}" for lb, why in bad)
        if len(ok) == 1:
            uid, label, a = ok[0]
            ans = await a.forwarder._cmd_fwd(f"{src} {dst}")
            sel[event.sender_id] = uid
            return await home(event, back, f"👤 <b>{html.escape(label)}</b> is the only account that can do this "
                                           f"route, so it is used.\n{ans}")
        choices[event.sender_id] = {"src": src, "dst": dst, "back": back}
        rows = [[B(f"👤 {label[:30]}", f"m:fcs:{uid}".encode())] for uid, label, _a in ok]
        await event.respond(ui.head("📤", "Add route") + "\n" + ui.step(3, 3, "more than one account is a member of both "
                            "chats.\nWhich account should read the source and post to the destination?") + note,
                            buttons=rows + [ui.cancel_row()])

    # ------------------------------------------------------------------ typed answers
    @bot.on(events.NewMessage(func=lambda e: e.is_private and not e.media and bool(e.raw_text)
                              and not e.raw_text.startswith("/") and e.sender_id in pending))
    async def on_text(event):
        if not is_admin(event.sender_id) or rt() is None:
            return
        who.set(event.sender_id)
        st = pending.pop(event.sender_id)
        kind, back, val = st["kind"], st["back"], event.raw_text.strip()
        try:
            if kind in ("WM_TEXT", "WM_TEXT_COLOR", "WM_BG_COLOR"):
                res = await rt().set_string(kind, event.raw_text.strip() if kind == "WM_TEXT" else val)
                return await home(event, back, ui.ok(f"Saved: <b>{html.escape(res)}</b>"))
            if kind == "TARGET":
                title = await rt().set_target(val)
                return await home(event, back, ui.ok(f"Now posting to <b>{html.escape(title)}</b>"))
            if kind == "BAN":
                ans = await fwd()._cmd_fwd("ban " + event.raw_text)
                return await home(event, back, ans.split("\n")[0])
            if kind == "BD_START":
                from unified_core.tg import parse_message_link
                parse_message_link(val)                    # reject a bad link now, not after the second one
                pending[event.sender_id] = {"kind": "BD_END", "back": back, "start": val}
                return await event.respond(
                    ui.head("🗑", "Bulk delete") + "\n" + ui.step(2, 2, "now send the link of the <b>last</b> (newest) "
                    "message to delete, e.g. <code>https://t.me/c/2940625991/2255</code>"),
                    buttons=[ui.cancel_row()])
            if kind == "BD_END":
                from unified_services import bulk_delete
                client = fwd().userbot
                plan = await bulk_delete.make_plan(client, st["start"], val)
                plans[event.sender_id] = {"plan": plan, "client": client, "back": back, "ts": time.time()}
                return await event.respond(plan.summary(), buttons=[
                    ui.confirm_row(f"🗑 Yes, delete {plan.total}", b"m:bdy", b"m:cancel")])
            if kind == "FWD_SRC":
                pending[event.sender_id] = {"kind": "FWD_DST", "back": back, "src": val}
                return await event.respond(ui.head("📥", "Add route") + "\n" + ui.step(2, 2, "send the <b>destination</b> chat."),
                                           buttons=[ui.cancel_row()])
            if kind == "FWD_DST":
                if len(fw_accounts()) > 1:
                    return await choose_account(event, st["src"], val, back)
                ans = await fwd()._cmd_fwd(f"{st['src']} {val}")
                return await home(event, back, ans)
        except ValueError as exc:
            pending[event.sender_id] = st  # let them try again
            return await event.respond(ui.fail(html.escape(str(exc)), "") + "\n" + ui.hint("Try again, or cancel."),
                                       buttons=[ui.cancel_row()])
        except Exception as exc:
            log.exception("typed answer failed")
            await event.respond(ui.fail("Failed", short_error(exc)), buttons=[ui.nav_row(back)])


def s_wm_entry(app):
    return app._wm_screen()
