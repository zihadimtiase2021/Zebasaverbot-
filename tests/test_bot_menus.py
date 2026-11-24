"""Drives the button menus with a fake bot (telethon is stubbed when it is not installed)."""
import asyncio
import sys
import types

try:
    import telethon  # noqa: F401
except ImportError:  # minimal stand-in: only what bot_menus touches
    t = types.ModuleType("telethon")
    t.Button = types.SimpleNamespace(inline=lambda text, data=None: (text, data))
    t.errors = types.SimpleNamespace(MessageNotModifiedError=type("MessageNotModifiedError", (Exception,), {}))
    t.events = types.SimpleNamespace(CallbackQuery=lambda **kw: ("cb", kw), NewMessage=lambda **kw: ("nm", kw))
    sys.modules["telethon"] = t

from unified_services import bot_menus  # noqa: E402
from unified_services import runtime_config as rc  # noqa: E402


class FakeBot:
    def __init__(self):
        self.h = {}

    def on(self, spec):
        def deco(fn):
            self.h[spec[0]] = fn
            return fn
        return deco


class Ev:
    def __init__(self, data=b"", text="", uid=1):
        self.data, self.raw_text, self.sender_id, self.chat_id, self.out = data, text, uid, uid, []

    async def edit(self, text, buttons=None):
        self.out.append(("edit", text, buttons))

    async def respond(self, text, buttons=None):
        self.out.append(("respond", text, buttons))

    async def answer(self, text="", alert=False):
        self.out.append(("answer", text, alert))


def make():
    names = dict(TARGET_CHANNEL=0, COMMENT_CHAT=0, DATA_DIR=None, WM_TEXT="", WM_FONT="DejaVuSans-Bold",
                 WM_TEXT_COLOR="#FFFFFF", WM_BG_COLOR="#000000", WM_POSITION="br")
    c = types.SimpleNamespace(**names, **{k: False for k in rc.TOGGLES}, **{k: 5 for k in rc.NUMBERS})
    c.WM_OPACITY = 90
    c.WM_ANGLE = 0
    rt = rc.RuntimeConfig(None, c)

    async def noop(*a, **k):
        pass
    rt.save = noop
    app = types.SimpleNamespace(runtime=rt, forwarder=None, pipeline=None)
    bot = FakeBot()
    bot_menus.register_bot_menus(app, bot, lambda uid: uid == 1)
    return rt, bot.h["cb"], bot.h["nm"]


def run(coro):
    return asyncio.run(coro)


def test_menu_flow():
    rt, cb, nm = make()
    ev = Ev(b"m:wm")
    run(cb(ev))
    assert "Watermark" in ev.out[0][1]
    # switching on without text is refused with an alert
    ev = Ev(b"m:t:WM_ENABLED")
    run(cb(ev))
    assert ev.out[0][0] == "answer" and ev.out[0][2] and not rt.cfg.WM_ENABLED
    # ask for the text, then send it
    run(cb(Ev(b"m:ask:WM_TEXT")))
    ev = Ev(text="@zeba")
    run(nm(ev))
    assert rt.cfg.WM_TEXT == "@zeba" and "Saved" in ev.out[0][1]
    run(cb(Ev(b"m:t:WM_ENABLED")))
    assert rt.cfg.WM_ENABLED
    # colour, position, number steppers, font
    run(cb(Ev(b"m:cs:WM_TEXT_COLOR:EF4444")))
    run(cb(Ev(b"m:cs:WM_BG_COLOR:none")))
    run(cb(Ev(b"m:ps:tl")))
    run(cb(Ev(b"m:n:WM_SIZE:+")))
    run(cb(Ev(b"m:n:WM_OPACITY:-")))
    assert (rt.cfg.WM_TEXT_COLOR, rt.cfg.WM_BG_COLOR, rt.cfg.WM_POSITION) == ("#EF4444", "none", "tl")
    assert rt.cfg.WM_SIZE == 6 and rt.cfg.WM_OPACITY == 80
    ev = Ev(b"m:fp:a:0")
    run(cb(ev))
    assert "Font" in ev.out[0][1]
    run(cb(Ev(b"m:fs:a:0")))
    assert rt.cfg.WM_FONT
    # bad colour typed -> error + retry, nothing changed
    run(cb(Ev(b"m:ask:WM_TEXT_COLOR")))
    ev = Ev(text="nonsense")
    run(nm(ev))
    assert "unknown colour" in ev.out[0][1] and rt.cfg.WM_TEXT_COLOR == "#EF4444"
    run(cb(Ev(b"m:cancel")))
    # reset
    run(cb(Ev(b"m:wmreset2")))
    assert rt.cfg.WM_TEXT == "" and not rt.cfg.WM_ENABLED


def test_non_admin_and_settings():
    rt, cb, nm = make()
    ev = Ev(b"m:wm", uid=2)
    run(cb(ev))
    assert ev.out[0][1] == "Unauthorized"
    run(cb(Ev(b"m:t:INCLUDE_SOURCE_LINK")))
    assert rt.cfg.INCLUDE_SOURCE_LINK is True
    run(cb(Ev(b"m:n:MAX_COMMENTS:+")))
    assert rt.cfg.MAX_COMMENTS == 10


def test_forward_and_translation_and_target_screens():
    rt, _, _ = make()

    class FW:
        routes = [{"src": 1, "dst": 2, "src_name": "A", "dst_name": "B", "active": True}]
        bans = []
        calls = []
        strip_links = True
        started, stopped = [], []

        def _fmt_routes(self):
            return "routes\n\nmore"

        def _job_for(self, r):
            return None

        def start_all(self, routes):
            self.started.append(len(routes))
            return "📥 <b>Forward all</b> started"

        def stop_all(self, routes):
            self.stopped.append(len(routes))
            return len(routes)

        def _fmt_progress(self):
            return "progress text"

        async def _cmd_fwd(self, arg):
            self.calls.append(arg)
            if arg.startswith("unwatch"):
                self.routes[0]["active"] = False
            if arg.startswith("links"):
                self.strip_links = arg.endswith("on")
            return "✅ Route #2: A ➜ B"

    class TR:
        mode = "auto"

        async def describe(self):
            return "Mode: auto"

        async def set_mode(self, m):
            self.mode = m
            return "ok"

    async def chans(force=False):
        return [{"id": -1001, "name": "My channel"}]
    rt.list_channels = chans
    names = dict(WM_TEXT="", TARGET_CHANNEL=0)
    app = types.SimpleNamespace(runtime=rt, forwarder=FW(), pipeline=types.SimpleNamespace(translator=TR()))
    bot = FakeBot()
    bot_menus.register_bot_menus(app, bot, lambda uid: True)
    cb, nm = bot.h["cb"], bot.h["nm"]
    ev = Ev(b"m:fo:1")
    run(cb(ev))
    assert app.forwarder.calls == ["unwatch 1"] and ("Route" in str(ev.out) or "routes" in ev.out[0][1])
    run(cb(Ev(b"m:fl:1")))                       # Forward all for route 1
    run(cb(Ev(b"m:fl:0")))                       # ... for every route
    assert app.forwarder.started == [1, 1]
    ev = Ev(b"m:fg")
    run(cb(ev))
    assert "progress text" in ev.out[0][1]
    run(cb(Ev(b"m:fk")))                         # link removal switch
    assert app.forwarder.calls[-1] == "links off"
    run(cb(Ev(b"m:fd:1")))
    assert app.forwarder.calls[-1] == "del 1"
    run(cb(Ev(b"m:fa")))
    run(nm(Ev(text="@src")))
    ev = Ev(text="@dst")
    run(nm(ev))
    assert app.forwarder.calls[-1] == "@src @dst" and "Route" in ev.out[0][1]
    ev = Ev(b"m:trm:off")
    run(cb(ev))
    assert app.pipeline.translator.mode == "off"
    ev = Ev(b"m:tg")
    run(cb(ev))
    assert "target channel" in ev.out[0][1]


def test_stepper_layout_and_rotate_and_bans():
    rt, cb, nm = make()
    ev = Ev(b"m:look")
    run(cb(ev))
    rows = ev.out[0][2]
    assert all(len(r) <= 2 for r in rows)                      # never 3 buttons squeezed in one row
    assert any("Rotate" in b[0] for r in rows for b in r)
    run(cb(Ev(b"m:n:WM_ANGLE:+")))
    run(cb(Ev(b"m:n:WM_ANGLE:+")))
    assert rt.cfg.WM_ANGLE == 30
    run(cb(Ev(b"m:n:WM_ANGLE:0")))
    assert rt.cfg.WM_ANGLE == 0

    class FW:
        routes, bans, calls = [], ["spam"], []

        def _fmt_routes(self):
            return "x"

        async def _cmd_fwd(self, arg):
            self.calls.append(arg)
            return "✅ Banned: x\nrest"
    app = types.SimpleNamespace(runtime=rt, forwarder=FW(), pipeline=None)
    bot = FakeBot()
    bot_menus.register_bot_menus(app, bot, lambda uid: True)
    cb2, nm2 = bot.h["cb"], bot.h["nm"]
    ev = Ev(b"m:bn")
    run(cb2(ev))
    assert "Ban list" in ev.out[0][1]
    run(cb2(Ev(b"m:bd:0")))
    assert app.forwarder.calls[-1] == "unban 1"
    run(cb2(Ev(b"m:ask:BAN")))
    run(nm2(Ev(text="foo, bar")))
    assert app.forwarder.calls[-1] == "ban foo, bar"


def test_reset_progress_flow_confirms_and_refuses_while_running():
    rt, _, _ = make()
    r1 = {"src": 1, "dst": 2, "src_name": "A", "dst_name": "B", "active": True}
    r2 = {"src": 3, "dst": 4, "src_name": "C", "dst_name": "D", "active": True}

    class FW:
        routes, bans, calls, resets = [r1, r2], [], [], []
        strip_links, running = True, set()

        def _job_for(self, r):
            return {"state": "running"} if r["src"] in self.running else None

        def _fmt_progress(self):
            return "progress text"

        async def reset_progress(self, routes):
            if any(r["src"] in self.running for r in routes):
                raise ValueError("Forward all is still running - stop it first")
            self.resets.append([r["src"] for r in routes])
            return "🔄 <b>Progress reset</b>: A ➜ B"

    app = types.SimpleNamespace(runtime=rt, forwarder=FW(), pipeline=None)
    bot = FakeBot()
    bot_menus.register_bot_menus(app, bot, lambda uid: True)
    cb = bot.h["cb"]

    ev = Ev(b"m:fg")                                   # the Progress screen has the button
    run(cb(ev))
    assert (("🔄 Reset Progress", b"m:fr") in [b for r in ev.out[0][2] for b in r])

    ev = Ev(b"m:fr")                                   # several routes: pick one (or all)
    run(cb(ev))
    data = [d for r in ev.out[0][2] for _t, d in r]
    assert b"m:frc:1" in data and b"m:frc:2" in data and b"m:frc:0" in data and not app.forwarder.resets

    ev = Ev(b"m:frc:1")                                # nothing is reset before the confirmation
    run(cb(ev))
    assert "Reset progress?" in ev.out[0][1] and not app.forwarder.resets
    assert [d for r in ev.out[0][2] for _t, d in r] == [b"m:fry:1", b"m:fg"]

    run(cb(Ev(b"m:fry:1")))
    run(cb(Ev(b"m:fry:0")))
    assert app.forwarder.resets == [[1], [1, 3]]

    app.forwarder.running = {3}                        # running: warn + Stop button, no reset
    ev = Ev(b"m:frc:0")
    run(cb(ev))
    assert "Stop Forward all first" in ev.out[0][1] and b"m:fx:2" in [d for r in ev.out[0][2] for _t, d in r]
    ev = Ev(b"m:fry:2")                                # even a stale confirm cannot reset a running route
    run(cb(ev))
    assert ev.out[0][0] == "answer" and ev.out[0][2] and app.forwarder.resets == [[1], [1, 3]]

    ev = Ev(b"m:frc:9")                                # stale route number -> popup, no crash
    run(cb(ev))
    assert ev.out[0][0] == "answer" and ev.out[0][2]


def test_watch_toggle_button_and_toast():
    rt, _, _ = make()

    class FW:
        routes, bans, strip_links, watch = [], [], True, True

        def _fmt_routes(self):
            return "routes"

        def _job_for(self, r):
            return None

        async def set_watch(self, on):
            self.watch = on
            return f"👁 Watch <b>{'ON' if on else 'OFF'}</b>: details"

    app = types.SimpleNamespace(runtime=rt, forwarder=FW(), pipeline=None)
    bot = FakeBot()
    bot_menus.register_bot_menus(app, bot, lambda uid: True)
    cb = bot.h["cb"]
    ev = Ev(b"m:fw")
    run(cb(ev))
    assert ("👁 Watch: ON ✅", b"m:fwt") in [b for r in ev.out[0][2] for b in r]
    ev = Ev(b"m:fwt")                                    # tap: OFF, a toast (not a popup), screen redrawn
    run(cb(ev))
    assert app.forwarder.watch is False
    assert ev.out[0][0] == "answer" and "Watch OFF" in ev.out[0][1] and not ev.out[0][2]
    assert ("👁 Watch: OFF ❌", b"m:fwt") in [b for r in ev.out[1][2] for b in r]
    run(cb(Ev(b"m:fwt")))
    assert app.forwarder.watch is True
