"""Link removal for forwarded text (plus the ban list on top)."""
from unified_services import banwords as bw


class E:
    def __init__(self, offset, length, url=None):
        self.offset, self.length, self.url = offset, length, url


class MessageEntityUrl(E):
    pass


class MessageEntityTextUrl(E):
    pass


class MessageEntityBold(E):
    pass


def strip(text, ents=None):
    return bw.strip_links(text, ents)[0]


def test_visible_links_are_removed():
    assert strip("Hello https://t.me/foo/12 world") == "Hello world"
    assert strip("see www.example.com/path?x=1 now") == "see now"
    assert strip("join t.me/joinchat/AAA now") == "join now"
    assert strip("Visit bit.ly/xyz and instagram.com/user") == "Visit and"
    assert strip("Line one\nhttps://spam.site/abc\nLine three") == "Line one\nLine three"   # whole line goes
    assert strip("https://only.link") == ""


def test_not_links_stay():
    assert strip("No link here! e.g. file.txt and 3.5 ok") == "No link here! e.g. file.txt and 3.5 ok"
    assert strip("mail a@b.com ok") == "mail a@b.com ok"
    assert strip("follow @mychannel") == "follow @mychannel"      # mentions are for the ban list


def test_entities_follow_the_cut():
    text = "Go https://x.com/a Bold"
    ents = [MessageEntityUrl(3, 15), MessageEntityBold(19, 4)]
    out, new = bw.strip_links(text, ents)
    assert out == "Go Bold"
    assert [(type(e).__name__, e.offset, e.length) for e in new] == [("MessageEntityBold", 3, 4)]


def test_hidden_link_keeps_its_words():
    out, new = bw.strip_links("Click here now", [MessageEntityTextUrl(0, 10, "http://a.b")])
    assert out == "Click here now" and new == []


def test_has_link_and_ban_list_work_together():
    assert bw.has_link("x http://a.com", None) and not bw.has_link("plain", None)
    text, ents = bw.strip_links("Great video https://t.me/x @spamchan", None)
    text, _ = bw.strip_banned(text, ents, ["@spamchan"])
    assert text == "Great video"
