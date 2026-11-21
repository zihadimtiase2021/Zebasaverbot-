"""Settings you can change while the bot is running - no redeploy, no restart.

Environment variables are only the *defaults*. Anything changed from the admin panel (/admin) or with the commands
.target / .source / .comments is stored as a small JSON message in your Telegram "Saved Messages"
(tagged #zeba_config), because a free Render instance has no disk that survives restarts. At start-up that
message is read and applied on top of the environment, so the choices survive restarts and redeploys.

Changed live:  target channel, source link in the caption, reposting comments, comment count line,
               original caption next to the translation, Bangla translation, max comments per post.
(Worker counts, API keys etc. are start-up settings: change those in Render -> Environment.)
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time

from unified_core.utils import NOTIFY_MARK

log = logging.getLogger("zeba.runtime")

TAG = "#zeba_config"

# key -> (label, description)
TOGGLES: dict[str, tuple[str, str]] = {
    "INCLUDE_SOURCE_LINK": ("Source link", "Adds a link to the original post under every media post."),
    "COMMENTS_ENABLED": ("Repost comments", "Copies the source post's comments under the channel post."),
    "INCLUDE_COMMENT_COUNT": ("Comment count", "Shows how many comments the source post had."),
    "INCLUDE_ORIGINAL_CAPTION": ("Original caption", "Keeps the original text below the translation."),
    "TRANSLATE_ENABLED": ("Bangla translation", "Translates the caption into Bangla."),
    "WM_ENABLED": ("Watermark: media sent to bot", "Any photo/video you send to the bot (or Saved Messages) comes back watermarked."),
    "WM_ON_URL": ("Watermark: downloaded links", "Watermark media downloaded from links (.dl / auto) before posting."),
    "WM_ON_FWD": ("Watermark: forwarded media", "Watermark media copied by the forwarder (.fwd) before posting."),
}
NUMBERS: dict[str, tuple[str, int, int]] = {
    "MAX_COMMENTS": ("Max comments per post", 1, 200),
    "WM_SIZE": ("Watermark text size (% of width)", 1, 30),
    "WM_MARGIN": ("Watermark margin (% of width)", 0, 20),
    "WM_OPACITY": ("Watermark text opacity %", 5, 100),
    "WM_BG_OPACITY": ("Watermark box opacity %", 0, 100),
    "WM_ANGLE": ("Watermark rotation (degrees)", -180, 180),
}
# Watermark text settings: key -> validator(value) -> cleaned value (raises ValueError)
def _v_text(v: str) -> str:
    v = (v or "").strip()[:200]
    return v


def _v_font(v: str) -> str:
    v = (v or "").strip()[:120]
    if not v:
        raise ValueError("font name is empty - see .wm fonts")
    return v


def _v_color(v: str) -> str:
    from unified_services.watermark import parse_color
    return parse_color(v)


def _v_bg(v: str) -> str:
    from unified_services.watermark import parse_color
    return parse_color(v, allow_none=True) or "none"


def _v_pos(v: str) -> str:
    from unified_services.watermark import parse_position
    return parse_position(v)


STRINGS = {"WM_TEXT": _v_text, "WM_FONT": _v_font, "WM_TEXT_COLOR": _v_color, "WM_BG_COLOR": _v_bg,
           "WM_POSITION": _v_pos}


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _parse(text: str) -> dict | None:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        data = json.loads(text[a:b + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _title(ent) -> str:
    return (getattr(ent, "title", None) or getattr(ent, "first_name", None) or getattr(ent, "username", None) or "chat")


class RuntimeConfig:
    def __init__(self, userbot, cfg=None) -> None:
        if cfg is None:
            from unified_core.config import settings as cfg  # imported lazily so tests can pass a plain object
        self.userbot = userbot
        self.cfg = cfg
        self.app = None
        # False for an extra account: its saved toggles/numbers must not overwrite the shared (primary) settings
        self.apply_toggles = True
        self.saved: dict = {}          # only what was changed at runtime; everything else follows the environment
        self.msg_id: int | None = None
        self._lock = asyncio.Lock()
        self._chan_cache: tuple[float, list[dict]] = (0.0, [])

    # ------------------------------------------------------------------ start-up
    async def load(self) -> None:
        """Read the saved message and apply the toggles. The target channel is applied by main (it must resolve it)."""
        self.saved = await self._read()
        if not self.apply_toggles:
            return
        for key in TOGGLES:
            if isinstance(self.saved.get(key), bool):
                setattr(self.cfg, key, self.saved[key])
        for key, (_, lo, hi) in NUMBERS.items():
            if isinstance(self.saved.get(key), int) and not isinstance(self.saved.get(key), bool):
                setattr(self.cfg, key, _clamp(self.saved[key], lo, hi))
        for key in STRINGS:
            if isinstance(self.saved.get(key), str):
                setattr(self.cfg, key, self.saved[key])
        if self.saved:
            log.info("Applied %d runtime setting(s) saved in Saved Messages", len(self.saved))

    @property
    def saved_target(self) -> int | None:
        v = self.saved.get("TARGET_CHANNEL")
        return v if isinstance(v, int) and not isinstance(v, bool) and v else None

    @property
    def saved_comment_chat(self) -> int:
        v = self.saved.get("COMMENT_CHAT")
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    def bind(self, app) -> None:
        self.app = app

    # ------------------------------------------------------------------ persistence (Saved Messages)
    async def _read(self) -> dict:
        try:
            async for m in self.userbot.iter_messages("me", search=TAG, limit=10):
                text = getattr(m, "raw_text", None) or ""
                if getattr(m, "out", True) and TAG in text:
                    data = _parse(text)
                    if data is not None:
                        self.msg_id = m.id
                        return data
        except Exception as exc:
            log.warning("could not read the saved settings: %s", exc)
        return {}

    async def save(self) -> None:
        async with self._lock:
            text = (f"{NOTIFY_MARK}\u2699\ufe0f Zeba runtime settings (please do not delete)\n{TAG}\n"
                    f"{json.dumps(self.saved, sort_keys=True)}")
            if self.msg_id:
                try:
                    await self.userbot.edit_message("me", self.msg_id, text, parse_mode=None)
                    return
                except Exception as exc:
                    if "not modified" in str(exc).lower():
                        return
                    log.info("settings message could not be edited (%s) - sending a new one", exc)
                    self.msg_id = None
            m = await self.userbot.send_message("me", text, parse_mode=None)
            self.msg_id = m.id

    # ------------------------------------------------------------------ changes
    async def set_toggle(self, key: str, value: bool) -> None:
        if key not in TOGGLES:
            raise KeyError(key)
        setattr(self.cfg, key, bool(value))
        self.saved[key] = bool(value)
        await self.save()

    async def set_number(self, key: str, value: int) -> int:
        if key not in NUMBERS:
            raise KeyError(key)
        _, lo, hi = NUMBERS[key]
        value = _clamp(int(value), lo, hi)
        setattr(self.cfg, key, value)
        self.saved[key] = value
        await self.save()
        return value

    async def set_string(self, key: str, value: str) -> str:
        if key not in STRINGS:
            raise KeyError(key)
        value = STRINGS[key](value)
        setattr(self.cfg, key, value)
        self.saved[key] = value
        await self.save()
        return value

    async def set_many(self, values: dict) -> None:
        """Change several watermark strings at once (admin form): all validated first, one save."""
        clean = {k: STRINGS[k](v) for k, v in values.items() if k in STRINGS}
        for k, v in clean.items():
            setattr(self.cfg, k, v)
            self.saved[k] = v
        await self.save()

    async def list_channels(self, force: bool = False) -> list[dict]:
        """Channels this account can post to: [{'id': -100..., 'name': ...}] (cached for a minute)."""
        ts, cached = self._chan_cache
        if cached and not force and time.time() - ts < 60:
            return cached
        from telethon import utils

        out: list[dict] = []
        try:
            async for d in self.userbot.iter_dialogs(limit=400):
                ent = d.entity
                if not getattr(ent, "broadcast", False):
                    continue
                rights = getattr(ent, "admin_rights", None)
                if getattr(ent, "creator", False) or (rights and getattr(rights, "post_messages", False)):
                    out.append({"id": utils.get_peer_id(ent), "name": d.name})
        except Exception as exc:
            log.warning("could not list channels: %s", exc)
        self._chan_cache = (time.time(), out)
        return out

    async def set_target(self, raw) -> str:
        """Switch the channel media is posted to. Returns its title. Raises ValueError with a readable reason."""
        if self.app is None:
            raise ValueError("the bot is still starting")
        from telethon import utils

        from unified_core.tg import parse_chat_ref, resolve_peer

        ref = parse_chat_ref(str(raw))  # ValueError for garbage
        try:
            ent = await resolve_peer(self.userbot, ref)
        except RuntimeError as exc:
            raise ValueError("that chat was not found - the account must be a member of it") from exc
        if getattr(ent, "broadcast", False):
            try:
                me = await self.userbot.get_me()
                perms = await self.userbot.get_permissions(ent, me)
                if not (getattr(perms, "is_creator", False) or getattr(perms, "post_messages", False)):
                    raise ValueError("this account cannot post there - make it an admin with 'Post messages'")
            except ValueError:
                raise
            except Exception as exc:  # could not verify: let the first post tell
                log.warning("permission check skipped: %s", exc)
        new_id = utils.get_peer_id(ent)
        pipe, ignored = self.app.pipeline, self.app.ignored_chats
        for old in (pipe.target, pipe.comment_chat):
            if old is not None:
                try:
                    ignored.discard(utils.get_peer_id(old))
                except Exception:  # an odd entity must never block the switch
                    pass
        pipe.target = ent
        pipe.comment_chat = None  # the old discussion group does not belong to the new channel
        ignored.add(new_id)
        self.cfg.TARGET_CHANNEL = new_id
        self.cfg.COMMENT_CHAT = 0
        self.saved["TARGET_CHANNEL"] = new_id
        self.saved["COMMENT_CHAT"] = 0
        await self.save()
        log.info("target channel switched to %s (%s)", _title(ent), new_id)
        return _title(ent)

    # ------------------------------------------------------------------ read-outs
    def target_info(self) -> dict:
        ent = self.app.pipeline.target if self.app else None
        return {"id": getattr(self.cfg, "TARGET_CHANNEL", 0), "title": _title(ent) if ent is not None else "-"}

    def snapshot(self) -> dict:
        return {"target": self.target_info(),
                "toggles": {k: bool(getattr(self.cfg, k)) for k in TOGGLES},
                "numbers": {k: int(getattr(self.cfg, k)) for k in NUMBERS},
                "strings": {k: str(getattr(self.cfg, k, "")) for k in STRINGS}}


# ---------------------------------------------------------------------- Telegram commands
def _onoff(v: bool) -> str:
    return "ON \u2705" if v else "OFF \u274c"


def summary(rt: RuntimeConfig) -> str:
    s = rt.snapshot()
    t = s["target"]
    lines = ["\u2699\ufe0f <b>Zeba settings</b>", f"\U0001f3af Target: <b>{html.escape(str(t['title']))}</b> <code>{t['id']}</code>"]
    for key, (label, _) in TOGGLES.items():
        lines.append(f"\u2022 {label}: <b>{_onoff(s['toggles'][key])}</b>")
    lines.append(f"\u2022 Max comments: <b>{s['numbers']['MAX_COMMENTS']}</b>")
    lines.append("\U0001f4a7 Watermark: <code>.wm</code> (text, font, colours, position, on/off)")
    lines += ["", "<code>.target @channel</code> \u00b7 <code>.target list</code> \u00b7 <code>.source on|off</code> \u00b7 "
                  "<code>.comments on|off|10</code> \u00b7 <code>.settings</code>"]
    return "\n".join(lines)


async def handle_command(rt: RuntimeConfig, cmd: str) -> str:
    """.settings | .target [list|<ref>] | .source [on|off] | .comments [on|off|<n>]  ->  reply text (HTML)."""
    name, _, arg = cmd.strip().partition(" ")
    arg = arg.strip()
    low = arg.lower()
    if name == ".settings":
        return summary(rt)
    if name == ".target":
        if not arg:
            t = rt.target_info()
            return (f"\U0001f3af Target channel: <b>{html.escape(str(t['title']))}</b> <code>{t['id']}</code>\n"
                    "Change: <code>.target @channel</code> or <code>.target -100123...</code> \u00b7 list: <code>.target list</code>")
        if low == "list":
            chans = await rt.list_channels(force=True)
            if not chans:
                return "\u274c No channel with posting rights found on this account."
            return "\U0001f4cb <b>Your channels</b>\n" + "\n".join(
                f"\u2022 {html.escape(c['name'])} \u2014 <code>{c['id']}</code>" for c in chans[:40])
        try:
            title = await rt.set_target(arg)
        except ValueError as exc:
            return f"\u274c {html.escape(str(exc))}"
        return (f"\u2705 Now posting to <b>{html.escape(title)}</b>. Saved \u2014 no redeploy needed.\n"
                "<i>Comments need a linked discussion group on that channel.</i>")
    if name == ".wm":
        return await wm_command(rt, arg)
    if name in (".source", ".comments"):
        key = "INCLUDE_SOURCE_LINK" if name == ".source" else "COMMENTS_ENABLED"
        label = TOGGLES[key][0]
        if name == ".comments" and low.isdigit():
            n = await rt.set_number("MAX_COMMENTS", int(low))
            await rt.set_toggle("COMMENTS_ENABLED", True)
            return f"\u2705 Comments <b>ON</b>, up to <b>{n}</b> per post."
        if low in ("on", "off"):
            await rt.set_toggle(key, low == "on")
            return f"\u2705 {label}: <b>{_onoff(low == 'on')}</b> (saved)"
        return f"\u2139\ufe0f {label} is <b>{_onoff(bool(getattr(rt.cfg, key)))}</b>. Use <code>{name} on</code> / <code>{name} off</code>"
    return summary(rt)


# ---------------------------------------------------------------------- watermark command
WM_HELP = (
    "\U0001f4a7 <b>Watermark</b>\n"
    "<code>.wm text Your text</code> \u00b7 <code>.wm font NotoSansBengali</code> \u00b7 <code>.wm fonts</code>\n"
    "<code>.wm color #ffffff</code> \u00b7 <code>.wm bg #000000</code> (or <code>none</code>) \u00b7 <code>.wm pos br</code> "
    "(tl tc tr ml mc mr bl bc br)\n"
    "<code>.wm size 5</code> \u00b7 <code>.wm opacity 90</code> \u00b7 <code>.wm bgopacity 50</code> \u00b7 <code>.wm margin 2</code> \u00b7 <code>.wm rotate 30</code> (-180..180)\n"
    "<code>.wm on|off</code> media sent to the bot \u00b7 <code>.wm url on|off</code> downloaded links \u00b7 "
    "<code>.wm fwd on|off</code> forwarded media\n"
    "<code>.wm preview</code> sample picture \u00b7 <code>.wm reset</code> clear the text and switch everything off\n"
    "Send a <b>.ttf/.otf</b> file with caption <code>.wmfont</code> to add your own font."
)
_NUM = {"angle": "WM_ANGLE", "rotate": "WM_ANGLE", "size": "WM_SIZE", "opacity": "WM_OPACITY", "bgopacity": "WM_BG_OPACITY", "margin": "WM_MARGIN"}
_STR = {"text": "WM_TEXT", "font": "WM_FONT", "color": "WM_TEXT_COLOR", "bg": "WM_BG_COLOR", "pos": "WM_POSITION"}
_SW = {"url": "WM_ON_URL", "fwd": "WM_ON_FWD"}


def wm_status(rt: RuntimeConfig) -> str:
    c = rt.cfg
    return ("\U0001f4a7 <b>Watermark</b>\n"
            f"\u2022 Text: <b>{html.escape(c.WM_TEXT) or '-'}</b>\n"
            f"\u2022 Font: <b>{html.escape(c.WM_FONT)}</b> \u00b7 size <b>{c.WM_SIZE}%</b> \u00b7 opacity <b>{c.WM_OPACITY}%</b>\n"
            f"\u2022 Colour: <b>{c.WM_TEXT_COLOR}</b> \u00b7 box: <b>{c.WM_BG_COLOR}</b> ({c.WM_BG_OPACITY}%)\n"
            f"\u2022 Position: <b>{c.WM_POSITION}</b> \u00b7 margin <b>{c.WM_MARGIN}%</b> \u00b7 rotation <b>{getattr(c, 'WM_ANGLE', 0)}\u00b0</b>\n"
            f"\u2022 Media sent to bot: <b>{_onoff(c.WM_ENABLED)}</b> \u00b7 links: <b>{_onoff(c.WM_ON_URL)}</b> \u00b7 "
            f"forwarded: <b>{_onoff(c.WM_ON_FWD)}</b>\n<i>.wm help for all commands</i>")


async def wm_command(rt: RuntimeConfig, arg: str) -> str:
    from unified_services.watermark import list_fonts

    sub, _, val = arg.strip().partition(" ")
    sub, val = sub.lower(), val.strip()
    try:
        if sub in ("", "status"):
            return wm_status(rt)
        if sub == "help":
            return WM_HELP
        if sub == "fonts":
            names = list_fonts(getattr(rt.cfg, "DATA_DIR", None))
            return "\U0001f524 <b>Fonts</b>\n" + html.escape(", ".join(names[:80]) or "none found")
        if sub == "preview":
            return "__PREVIEW__"
        if sub in ("on", "off"):
            if sub == "on" and not rt.cfg.WM_TEXT:
                return "\u274c Set a text first: <code>.wm text Your text</code>"
            await rt.set_toggle("WM_ENABLED", sub == "on")
            return f"\u2705 Watermark on media sent to the bot: <b>{_onoff(sub == 'on')}</b>"
        if sub in _SW:
            if val not in ("on", "off"):
                return f"Use <code>.wm {sub} on</code> or <code>.wm {sub} off</code>"
            if val == "on" and not rt.cfg.WM_TEXT:
                return "\u274c Set a text first: <code>.wm text Your text</code>"
            await rt.set_toggle(_SW[sub], val == "on")
            return f"\u2705 Watermark ({sub}): <b>{_onoff(val == 'on')}</b>"
        if sub in _NUM:
            if not val.lstrip("-").isdigit():
                return f"\u274c Use a whole number, e.g. <code>.wm {sub} 5</code>"
            n = await rt.set_number(_NUM[sub], int(val))
            return f"\u2705 {NUMBERS[_NUM[sub]][0]}: <b>{n}</b>"
        if sub in _STR:
            if sub == "text" and val == "":
                return "Usage: <code>.wm text Your text</code> (new line = \\n)"
            res = await rt.set_string(_STR[sub], val.replace("\\n", "\n"))
            return f"\u2705 Saved: <b>{html.escape(res)}</b>"
        if sub == "reset":
            await rt.set_string("WM_TEXT", "")
            for k in ("WM_ENABLED", "WM_ON_URL", "WM_ON_FWD"):
                await rt.set_toggle(k, False)
            return "\u2705 Watermark cleared and switched off."
    except ValueError as exc:
        return f"\u274c {html.escape(str(exc))}"
    return WM_HELP
