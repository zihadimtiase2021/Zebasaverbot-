"""Forward ledger: copied message ids are kept as ranges, survive a restart, and give out serial numbers."""
import asyncio
import types

from unified_services.fwd_ledger import MAX_CHARS, TAG, Ledger, route_key


def test_ranges_merge_and_lookup():
    lg = Ledger()
    k = route_key(-1001, -1002)
    for i in (5, 3, 4, 10, 1, 2):
        lg.add(k, i)
    assert lg.ranges[k] == [[1, 5], [10, 10]]
    assert lg.has(k, 3) and lg.has(k, 10) and not lg.has(k, 6) and not lg.has(k, 11) and not lg.has("other", 3)
    assert lg.has_all(k, [1, 2, 3]) and not lg.has_all(k, [5, 6])
    assert lg.count(k) == 6
    assert lg.resume_from(k) == 5            # everything up to 5 is done
    lg.add(k, 1, 20)                         # a finished Forward all closes the gaps (deleted ids)
    assert lg.ranges[k] == [[1, 20]] and lg.resume_from(k) == 20
    assert Ledger().resume_from(k) == 0
    lg2 = Ledger()
    lg2.add(k, 7, 9)
    assert lg2.resume_from(k) == 0           # the start of the history is not covered yet


def test_serial_numbers_count_up_and_are_given_back():
    lg = Ledger()
    assert [lg.next_serial() for _ in range(3)] == [1, 2, 3]
    lg.release(3)                            # the copy failed: nothing was posted
    assert lg.next_serial() == 3
    lg.release(1)                            # an older number is never reused
    assert lg.next_serial() == 4


def test_dump_merge_roundtrip_and_size_limit():
    lg = Ledger()
    k = route_key(-1001, -1002)
    lg.add(k, 1, 500)
    lg.next_serial()
    other = Ledger()
    assert other.merge(lg.dump()) and other.ranges == lg.ranges and other.serial == 1
    assert not other.merge("not json") and not other.merge('{"r": 5}')
    big = Ledger()
    for i in range(0, 2000, 2):              # 1000 separate single ids -> would be far over one message
        big.add(k, i + 1)
    text = big.dump()
    assert len(text) <= MAX_CHARS
    assert all(big.has(k, i + 1) for i in range(0, 2000, 2))   # closing gaps never forgets a copied id


def test_saved_messages_persistence():
    class Client:
        def __init__(self):
            self.msgs = {}
            self.n = 0

        async def iter_messages(self, chat, search=None, limit=None):
            for mid, text in list(self.msgs.items()):
                if search in text:
                    yield types.SimpleNamespace(id=mid, raw_text=text, out=True)

        async def send_message(self, chat, text, **kw):
            self.n += 1
            self.msgs[self.n] = text
            return types.SimpleNamespace(id=self.n)

        async def edit_message(self, chat, mid, text, **kw):
            self.msgs[mid] = text

    async def go():
        cl = Client()
        k = route_key(-1, -2)
        a = Ledger(cl)
        a.add(k, 1, 40)
        a.next_serial()
        await a.close()                      # shutdown flush
        a.add(k, 41)
        await a.flush()                      # second write edits the same message
        assert len(cl.msgs) == 1 and TAG in cl.msgs[1]
        b = Ledger(cl)                       # "restart": a new object reads Saved Messages
        await b.load()
        assert b.ranges[k] == [[1, 41]] and b.serial == 1 and b.msg_id == 1
        await b.close()

    asyncio.run(go())
