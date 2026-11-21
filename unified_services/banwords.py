"""Ban list for forwarded text: banned words / URLs are cut out of the caption (formatting of the rest is kept).

* a word or phrase ("join now")      -> that text is removed (whole words only for a single word)
* a URL / domain / @username / t.me/x -> the whole link (or mention) is removed, whatever follows the domain
Hidden links (text with a link behind it) whose address contains a banned entry are removed with their text.

``strip_links`` removes *every* link from a text (http/https/www/t.me, bare domains, Telegram url entities);
text that only carries a hidden link keeps its words but loses the link.
"""
from __future__ import annotations

import copy
import re

_TRAIL = ".,;:!?)]}\"'»”’"


def _is_token_ban(b: str) -> bool:
    return " " not in b and ("." in b or "/" in b or b.startswith("@") or ":" in b)


def split_bans(text: str) -> list[str]:
    """'a, b\\nc d' -> ['a', 'b', 'c d'] (comma or new line separated, trimmed, no duplicates)."""
    out: list[str] = []
    for part in re.split(r"[,\n]", text or ""):
        p = part.strip()
        if p and p.lower() not in (x.lower() for x in out):
            out.append(p[:200])
    return out


def _spans(s: str, ents, bans: list[str]) -> list[list[int]]:
    low = s.lower()
    spans: list[list[int]] = []
    for ban in bans:
        b = ban.lower().strip()
        if not b:
            continue
        if _is_token_ban(b):
            for m in re.finditer(re.escape(b), low):
                a, e = m.start(), m.end()
                while a > 0 and not s[a - 1].isspace():
                    a -= 1
                while e < len(s) and not s[e].isspace():
                    e += 1
                while e > m.end() and s[e - 1] in _TRAIL:
                    e -= 1
                spans.append([a, e])
        else:
            pat = re.escape(b)
            if re.fullmatch(r"\w+", b):
                pat = rf"(?<!\w){pat}(?!\w)"
            spans += [[m.start(), m.end()] for m in re.finditer(pat, low)]
    for en in ents or []:  # hidden links: text shown, address elsewhere
        url = (getattr(en, "url", None) or "").lower()
        if url and any(b.lower() in url for b in bans if b.strip()):
            spans.append([en.offset, en.offset + en.length])
    return spans


def _widen(s: str, spans: list[list[int]]) -> list[list[int]]:
    """Swallow the space before a removed piece, and the whole line when nothing else is left on it."""
    out = []
    for a, e in spans:
        while a > 0 and s[a - 1] in " \t":
            a -= 1
        if (a == 0 or s[a - 1] == "\n") and (e >= len(s) or s[e] == "\n"):
            if e < len(s):
                e += 1
            elif a > 0:
                a -= 1
        out.append([a, e])
    merged: list[list[int]] = []
    for a, e in sorted(out):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([a, e])
    return merged


def _cut(s: str, ents, spans: list[list[int]]):
    def gone_before(x: int) -> int:
        return sum(max(0, min(x, e) - a) for a, e in spans)

    out, pos = [], 0
    for a, e in spans:
        out.append(s[pos:a])
        pos = e
    out.append(s[pos:])
    new = []
    for en in ents or []:
        a = en.offset - gone_before(en.offset)
        e = en.offset + en.length - gone_before(en.offset + en.length)
        if e > a:
            c = copy.copy(en)
            c.offset, c.length = a, e - a
            new.append(c)
    return "".join(out), new


def _surrogates():
    try:
        from telethon.helpers import add_surrogate, del_surrogate
        return add_surrogate, del_surrogate
    except ImportError:  # tests without telethon: plain str offsets
        return (lambda x: x), (lambda x: x)


def _finish(s: str, ents, spans: list[list[int]]):
    """Cut the spans out, then trim blank space left at both ends of the text."""
    s2, e2 = _cut(s, ents, spans)
    lead, trail = len(s2) - len(s2.lstrip()), len(s2) - len(s2.rstrip())
    edge = [[0, lead]] if lead else []
    if trail and len(s2) > lead:
        edge.append([len(s2) - trail, len(s2)])
    if edge:
        s2, e2 = _cut(s2, e2, edge)
    return s2, e2


def strip_banned(raw: str, ents, bans: list[str]):
    """Return (text, entities) without the banned pieces. Untouched when nothing matches."""
    if not raw or not bans:
        return raw, ents
    add_surrogate, del_surrogate = _surrogates()
    s = add_surrogate(raw)
    spans = _widen(s, _spans(s, ents, bans))
    if not spans:
        return raw, ents
    s2, e2 = _finish(s, ents, spans)
    return del_surrogate(s2), e2


# ---------------------------------------------------------------------------------------------- links
_SCHEME = r"(?:(?:https?|ftp|tg)://|www\.|(?:t|telegram)\.me/|telegra\.ph/)"
_URL_RE = re.compile(rf"(?i)(?<![\w@]){_SCHEME}[^\s<>]+")
_TLDS = ("com|net|org|io|me|co|in|bd|info|xyz|app|ly|gl|be|tv|to|cc|link|site|online|store|shop|live|club|top|"
         "pro|dev|ai|gg|fm|vip|biz|us|uk|de|ru|ir|pk|id|asia|page|click|cloud|world|news|win|fun")
_DOMAIN_RE = re.compile(rf"(?i)(?<![\w@.\-/])(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+(?:{_TLDS})(?![\w@\-])(?:[/?#][^\s<>]*)?")


def _trim_trail(s: str, a: int, e: int) -> int:
    while e > a and s[e - 1] in _TRAIL:
        e -= 1
    return e


def strip_links(raw: str, ents):
    """Return (text, entities) with every link removed.

    * visible URLs (``https://x.y/z``, ``www.x.y``, ``t.me/name``, ``x.com/path``) are cut out with their line
      when nothing else is on it
    * a hidden link (words with an address behind them) keeps its words but loses the address
    * e-mail addresses and plain ``@mentions`` are not links and stay (use the ban list for those)
    """
    if not raw:
        return raw, ents
    add_surrogate, del_surrogate = _surrogates()
    s = add_surrogate(raw)
    spans: list[list[int]] = []
    kept = []
    hidden_removed = False
    for en in ents or []:
        kind = type(en).__name__
        if kind == "MessageEntityTextUrl":
            hidden_removed = True          # words stay, the address goes
            continue
        kept.append(en)
        if kind == "MessageEntityUrl":
            spans.append([en.offset, en.offset + en.length])
    for rx in (_URL_RE, _DOMAIN_RE):
        for m in rx.finditer(s):
            spans.append([m.start(), _trim_trail(s, m.start(), m.end())])
    spans = [sp for sp in spans if sp[1] > sp[0]]
    if not spans:
        return (raw, kept) if hidden_removed else (raw, ents)
    s2, e2 = _finish(s, kept, _widen(s, spans))
    return del_surrogate(s2), e2


def has_link(raw: str, ents) -> bool:
    """True when stripping would change the text (used to decide about link previews)."""
    text, _ = strip_links(raw, ents)
    return text != (raw or "")
