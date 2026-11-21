"""Webhook: queue downloads from any other site. Real sockets, no mocks of the HTTP layer."""
import asyncio
import hashlib
import hmac
import json

import pytest

pytest.importorskip("telethon")
pytest.importorskip("pydantic_settings")

from unified_services.webhook import MAX_BODY, WebhookServer

SECRET = "s3cret-value"


class FakeQueue:
    def __init__(self):
        self.jobs, self.pending = [], 0

    def enqueue(self, job):
        if any(j.url == job.url for j in self.jobs):
            return False
        self.jobs.append(job)
        return True

    def snapshot(self):
        return {"pending": self.pending, "active": 0, "ok": 0, "skipped": 0, "failed": 0}


async def call(port, method="POST", path="/webhook", body=b"", headers=None):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    h = {"Host": "x", "Content-Length": str(len(body))}
    h.update(headers or {})
    w.write((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n").encode() + body)
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, (json.loads(payload) if payload else {}), head.decode()


def run(coro_fn, **server_kw):
    async def go():
        q = FakeQueue()
        srv = WebhookServer(q, secret=server_kw.pop("secret", SECRET), **server_kw)
        port = await srv.start("127.0.0.1", 0)
        try:
            return await coro_fn(port, q, srv)
        finally:
            await srv.stop()

    return asyncio.run(go())


BEARER = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
X = "https://x.com/user/status/123"


def test_health_needs_no_secret_and_webhook_does():
    async def t(port, q, srv):
        assert (await call(port, "GET", "/health"))[0] == 200
        assert (await call(port, body=b'{"url": "%s"}' % X.encode()))[0] == 401
        assert (await call(port, body=b"{}", headers={"Authorization": "Bearer wrong"}))[0] == 401
        assert q.jobs == []
        status, data, _ = await call(port, body=json.dumps({"url": X, "caption": "my cap", "force": True}).encode(), headers=BEARER)
        assert status == 202 and data["queued"] == 1
        job = q.jobs[0]
        assert (job.url, job.custom_caption, job.force) == (X, "my cap", True)
        # the same secret in the other accepted header
        s2, _, _ = await call(port, body=json.dumps({"url": "https://youtu.be/abc"}).encode(), headers={"X-Webhook-Secret": SECRET})
        assert s2 == 202 and len(q.jobs) == 2

    run(t)


def test_hmac_signature_instead_of_secret():
    async def t(port, q, srv):
        body = json.dumps({"url": X}).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert (await call(port, body=body, headers={"X-Signature": sig}))[0] == 202
        assert (await call(port, body=body, headers={"X-Signature": "sha256=" + "0" * 64}))[0] == 401

    run(t)


def test_payload_shapes_duplicates_and_invalid_links():
    async def t(port, q, srv):
        body = {"items": [{"url": X, "caption": "one"}, "https://youtu.be/abc", {"url": "https://evil.example/x"}], "urls": [X]}
        status, data, _ = await call(port, body=json.dumps(body).encode(), headers=BEARER)
        assert status == 202 and data["queued"] == 3 - 1 and data["duplicates"] == [X] and data["invalid"] == ["https://evil.example/x"]
        # form-encoded and plain text bodies
        s, d, _ = await call(port, body=b"url=https%3A%2F%2Fyoutu.be%2Fzzz", headers={**BEARER, "Content-Type": "application/x-www-form-urlencoded"})
        assert s == 202 and d["queued"] == 1
        s, d, _ = await call(port, body=b"look at https://x.com/a/status/9 please", headers={**BEARER, "Content-Type": "text/plain"})
        assert s == 202 and d["queued"] == 1
        # unsupported only -> 400 ; caption + several links is ambiguous -> 400
        assert (await call(port, body=b'{"url": "https://evil.example/x"}', headers=BEARER))[0] == 400
        assert (await call(port, body=json.dumps({"urls": [X, "https://youtu.be/q"], "caption": "c"}).encode(), headers=BEARER))[0] == 400

    run(t)


def test_limits_cors_and_disabled_without_secret():
    async def t(port, q, srv):
        q.pending = 199
        assert (await call(port, body=json.dumps({"urls": [X, "https://youtu.be/a"]}).encode(), headers=BEARER))[0] == 503   # queue full
        q.pending = 0
        assert (await call(port, body=b"x" * (MAX_BODY + 1), headers=BEARER))[0] == 413
        s, _, head = await call(port, "OPTIONS", "/webhook", headers={"Origin": "https://mysite.com"})
        assert s == 204 and "Access-Control-Allow-Origin: https://mysite.com" in head
        s, _, head = await call(port, "OPTIONS", "/webhook", headers={"Origin": "https://other.com"})
        assert "Access-Control-Allow-Origin" not in head
        s, data, _ = await call(port, "GET", "/status", headers=BEARER)
        assert s == 200 and data["pending"] == 0

    run(t, max_queue=200, cors_origins="https://mysite.com")

    async def off(port, q, srv):
        assert (await call(port, "GET", "/health"))[0] == 200
        assert (await call(port, body=b'{"url": "%s"}' % X.encode(), headers=BEARER))[0] == 404   # never open without a secret

    run(off, secret="")


def test_brute_force_and_rate_limit():
    async def t(port, q, srv):
        for _ in range(10):
            assert (await call(port, body=b"{}", headers={"Authorization": "Bearer nope"}))[0] == 401
        assert (await call(port, body=b"{}", headers={"Authorization": "Bearer nope"}))[0] == 429   # blocked after 10 failures

    run(t)

    async def rl(port, q, srv):
        codes = [(await call(port, body=json.dumps({"url": f"https://x.com/u/status/{i}"}).encode(), headers=BEARER))[0] for i in range(4)]
        assert codes == [202, 202, 429, 429]

    run(rl, rate_per_min=2)


def test_keepalive_pings_health_url():
    from unified_services.keepalive import KeepAlive

    async def go():
        seen = []

        async def getter(url):
            seen.append(url)
            return 200

        ka = KeepAlive("https://svc.onrender.com/", interval=60, getter=getter)
        ka.interval = 0.05          # speed the loop up for the test
        ka.start()
        await asyncio.sleep(0.5)
        await ka.stop()
        return seen

    seen = asyncio.run(go())
    assert seen and seen[0] == "https://svc.onrender.com/health"
