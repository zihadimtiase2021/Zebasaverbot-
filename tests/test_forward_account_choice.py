"""Forward menu with several accounts: pick the account whose routes are shown, and choose which account copies a
new route when more than one account is a member of both chats."""
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


class FW:
    """One account's forwarder. `reasons` = {(src, dst): why this account cannot make the route}."""

    def __init__(self, name, reasons=None):
        self.name, self.reasons = name, reasons or {}
        self.routes, self.bans, self.calls = [], [], []
        self.strip_links = True

    async def can_route(self, src, dst):
        return self.reasons.get((src, dst))

    async def _cmd_fwd(self, arg):
        self.calls.append(arg)
        if arg.startswith("del"):
            self.routes.pop(int(arg.split()[1]) - 1)
        elif " " in arg and not arg.split()[0] in ("links", "unban", "ban", "watch", "unwatch"):
            src, dst = arg.split()
            self.routes.append({"src": 1, "dst": 2, "src_name": src, "dst_name": dst, "active": True})
        return f"✅ Route #{len(self.routes)}: via {self.name}"

    def _fmt_routes(self):
        return f"routes of {self.name}" if self.routes else "no routes"

    def _job_for(self, r):
        return None

    def _fmt_progress(self):
        return f"progress of {self.name}"


def make(extra=2, reasons=None):
    """Primary account 'main' + `extra` extra accounts (user ids 11, 12 ...)."""
    rt = rc.RuntimeConfig(None, types.SimpleNamespace(
        TARGET_CHANNEL=0, COMMENT_CHAT=0, DATA_DIR=None, WM_TEXT="", **{k: False for k in rc.TOGGLES},
        **{k: 5 for k in rc.NUMBERS}))
    reasons = reasons or {}
    app = types.SimpleNamespace(runtime=rt, me_id=10, forwarder=FW("main", reasons.get(10)), pipeline=None)
    insts = {}
    for i in range(extra):
        uid = 11 + i
        insts[uid] = types.SimpleNamespace(
            me_id=uid, label=f"@acc{uid}",
            app=types.SimpleNamespace(me_id=uid, forwarder=FW(f"acc{uid}", reasons.get(uid))))
    mgr = types.SimpleNamespace(primary_username="mainuser", primary_name="Main", instances=insts)
    bot = FakeBot()
    bot_menus.register_bot_menus(app, bot, lambda uid: True, mgr)
    return app, insts, bot.h["cb"], bot.h["nm"]


def run(coro):
    return asyncio.run(coro)


def add_route(cb, nm, src="@src", dst="@dst"):
    run(cb(Ev(b"m:fa")))
    run(nm(Ev(text=src)))
    ev = Ev(text=dst)
    run(nm(ev))
    return ev


def buttons_of(ev):
    return [b for row in (ev.out[-1][2] or []) for b in row]


def test_two_accounts_both_members_ask_which_one():
    app, insts, cb, nm = make()
    ev = add_route(cb, nm)
    assert "Step 3/3" in ev.out[0][1]
    data = [d for _t, d in buttons_of(ev)]
    assert b"m:fcs:10" in data and b"m:fcs:11" in data and b"m:fcs:12" in data and b"m:cancel" in data
    # nothing was created yet
    assert not app.forwarder.routes and not insts[11].app.forwarder.routes
    # choose the second extra account
    ev = Ev(b"m:fcs:12")
    run(cb(ev))
    assert insts[12].app.forwarder.calls == ["@src @dst"]
    assert app.forwarder.calls == [] and insts[11].app.forwarder.calls == []
    assert "@acc12" in ev.out[0][1] and "via acc12" in ev.out[0][1]
    # the Forward screen now shows that account
    assert "@acc12" in ev.out[1][1]


def test_only_accounts_that_are_members_are_offered():
    reasons = {10: {("@src", "@dst"): "that chat was not found - this account must be a member of it"}}
    app, insts, cb, nm = make(reasons=reasons)
    ev = add_route(cb, nm)
    data = [d for _t, d in buttons_of(ev)]
    assert b"m:fcs:10" not in data and b"m:fcs:11" in data and b"m:fcs:12" in data
    assert "Not available" in ev.out[0][1] and "mainuser" in ev.out[0][1]


def test_single_possible_account_is_used_without_asking():
    both = {("@src", "@dst"): "this account cannot post there"}
    app, insts, cb, nm = make(reasons={10: both, 12: both})
    ev = add_route(cb, nm)
    assert insts[11].app.forwarder.calls == ["@src @dst"]
    assert "only account" in ev.out[0][1] and "@acc11" in ev.out[0][1]


def test_no_account_can_do_it_and_retry():
    why = {("@src", "@dst"): "this account cannot post there"}
    app, insts, cb, nm = make(reasons={10: why, 11: why, 12: why})
    ev = add_route(cb, nm)
    assert "no account can make this route" in ev.out[0][1]
    assert all(not i.app.forwarder.calls for i in insts.values()) and not app.forwarder.calls
    # the destination can be typed again
    ev = Ev(text="@other")
    run(nm(ev))
    assert "Step 3/3" in ev.out[0][1]


def test_choice_expires_and_cancel_goes_back_to_forward():
    app, insts, cb, nm = make()
    add_route(cb, nm)
    ev = Ev(b"m:cancel")
    run(cb(ev))
    assert "Forward routes" in ev.out[0][1] or "No routes" in ev.out[0][1]
    ev = Ev(b"m:fcs:11")                 # the choice is gone now
    run(cb(ev))
    assert ev.out[0][0] == "answer" and ev.out[0][2] and not insts[11].app.forwarder.calls


def test_account_picker_changes_which_routes_are_shown_and_edited():
    app, insts, cb, nm = make()
    insts[11].app.forwarder.routes.append(
        {"src": 5, "dst": 6, "src_name": "S", "dst_name": "D", "active": True})
    ev = Ev(b"m:fw")
    run(cb(ev))
    assert "mainuser" in ev.out[0][1] and b"m:fac" in [d for _t, d in buttons_of(ev)]
    ev = Ev(b"m:fac")
    run(cb(ev))
    data = [d for _t, d in buttons_of(ev)]
    assert data[:3] == [b"m:fas:10", b"m:fas:11", b"m:fas:12"]
    run(cb(Ev(b"m:fas:11")))
    ev = Ev(b"m:fw")
    run(cb(ev))
    assert "@acc11" in ev.out[0][1]
    run(cb(Ev(b"m:fd:1")))                 # delete route 1 -> in account 11, not in the main one
    assert insts[11].app.forwarder.calls == ["del 1"] and app.forwarder.calls == []
    # an account that was logged out in the meantime falls back to the primary account
    del insts[11]
    ev = Ev(b"m:fw")
    run(cb(ev))
    assert "mainuser" in ev.out[0][1]


def test_each_admin_has_their_own_selected_account():
    app, insts, cb, nm = make()
    run(cb(Ev(b"m:fas:12", uid=1)))
    ev = Ev(b"m:fw", uid=2)
    run(cb(ev))
    assert "mainuser" in ev.out[0][1]      # admin 2 still sees the primary account
    ev = Ev(b"m:fw", uid=1)
    run(cb(ev))
    assert "@acc12" in ev.out[0][1]


def test_single_account_behaves_as_before():
    app, insts, cb, nm = make(extra=0)
    ev = Ev(b"m:fw")
    run(cb(ev))
    assert "Account" not in ev.out[0][1] and b"m:fac" not in [d for _t, d in buttons_of(ev)]
    ev = add_route(cb, nm)
    assert app.forwarder.calls == ["@src @dst"] and "Route" in ev.out[0][1]
