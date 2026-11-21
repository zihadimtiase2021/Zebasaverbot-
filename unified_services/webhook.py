"""Tiny HTTP server (standard library only): health check for Render + a webhook to queue downloads from any site.

    GET  /health            -> {"status": "ok"}                       (no auth; Render health check)
    POST /webhook           -> queue one or more links               (auth required)
    GET  /status            -> queue counters                         (auth required)
    GET  /admin             -> control panel (target channel, source link, comments on/off; unlock with the secret)

Auth: header  Authorization: Bearer <WEBHOOK_SECRET>   or   X-Webhook-Secret: <WEBHOOK_SECRET>
      or an HMAC-SHA256 of the raw body in  X-Signature: sha256=<hex>  (GitHub style).
Body (JSON, form-encoded or plain text with links):
      {"url": "https://...", "caption": "optional custom caption", "force": false}
      {"urls": ["https://...", ...]}
      {"items": [{"url": "https://...", "caption": "..."}, "https://..."]}
Without WEBHOOK_SECRET the /webhook endpoint is disabled (404): it never accepts unauthenticated requests.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from typing import Callable
from urllib.parse import parse_qs

from unified_core.utils import extract_urls
from unified_services.pipeline import Job

log = logging.getLogger("zeba.webhook")

MAX_BODY = 64 * 1024
MAX_URLS = 50
MAX_CAPTION = 1000
MAX_CONNS = 256          # simultaneous connections; extra ones are dropped (file-descriptor / slowloris guard)
MAX_TRACKED_IPS = 4096   # bound on the failed-login table (it is keyed by client address)
REASONS = {200: "OK", 202: "Accepted", 204: "No Content", 303: "See Other", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
           405: "Method Not Allowed", 408: "Request Timeout", 411: "Length Required", 413: "Payload Too Large",
           429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class WebhookServer:
    def __init__(self, queue, *, secret: str = "", max_queue: int = 200, cors_origins: str = "",
                 rate_per_min: int = 60, started_at: float | None = None,
                 job_factory: Callable[..., Job] = Job, admin=None) -> None:
        self.queue = queue
        self.admin = admin  # optional AdminPanel: serves /admin (HTML) next to the JSON endpoints
        self.secret = (secret or "").strip()
        self.max_queue = max_queue
        self.cors = {o.strip() for o in (cors_origins or "").split(",") if o.strip()}
        self.rate = max(1, rate_per_min)
        self.started_at = started_at or time.time()
        self._job = job_factory
        self._hits: dict[str, deque] = defaultdict(deque)
        self._fails: dict[str, deque] = defaultdict(deque)
        self._server: asyncio.AbstractServer | None = None
        self._conns = 0

    # ---------------------------------------------------------------- lifecycle
    async def start(self, host: str, port: int) -> int:
        self._server = await asyncio.start_server(self._on_connection, host, port, limit=MAX_BODY + 16384)
        bound = self._server.sockets[0].getsockname()[1]
        log.info("HTTP server listening on %s:%s (webhook %s)", host, bound, "ENABLED" if self.secret else "disabled - set WEBHOOK_SECRET")
        return bound

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except Exception:
                pass

    # ---------------------------------------------------------------- http plumbing
    @staticmethod
    def _close(writer) -> None:
        try:
            writer.close()
        except Exception:
            pass

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._conns >= MAX_CONNS:
            self._close(writer)
            return
        self._conns += 1
        try:
            await self._serve(reader, writer)
        finally:
            self._conns -= 1
            self._close(writer)  # also covers the early returns below and the /admin branch

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers_out: dict[str, str] = {}
        method = ""
        try:
            try:
                method, path, headers, body, ip = await asyncio.wait_for(self._read(reader, writer), 15)
            except asyncio.TimeoutError:
                raise _HttpError(408, "request timeout")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                return
            origin = headers.get("origin", "")
            if origin and (origin in self.cors or "*" in self.cors):
                headers_out.update({"Access-Control-Allow-Origin": "*" if "*" in self.cors else origin, "Vary": "Origin",
                                    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Webhook-Secret, X-Signature",
                                    "Access-Control-Allow-Methods": "POST, GET, OPTIONS"})
            if self.admin is not None and (path == "/admin" or path.startswith("/admin/")):
                a_status, a_headers, a_body = await self.admin.handle(method, path, headers, body, ip)
                await self._write_raw(writer, a_status, a_headers, a_body)
                return
            status, payload = await self._route(method, path, headers, body, ip)
        except _HttpError as exc:
            status, payload = exc.status, {"ok": False, "error": exc.message}
            if exc.status == 429:
                headers_out["Retry-After"] = "30"
        except Exception:
            log.exception("webhook handler crashed")
            status, payload = 500, {"ok": False, "error": "internal error"}
        try:
            data = b"" if status == 204 else json.dumps(payload, ensure_ascii=False).encode()
            head = [f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}", "Content-Type: application/json; charset=utf-8",
                    f"Content-Length: {len(data)}", "Connection: close", "Cache-Control: no-store",
                    "X-Content-Type-Options: nosniff"]
            head += [f"{k}: {v}" for k, v in headers_out.items()]
            if method == "HEAD":
                data = b""  # a HEAD response carries the headers (incl. Content-Length) but never a body
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
            await asyncio.wait_for(writer.drain(), 10)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _write_raw(self, writer, status: int, headers: dict, data: bytes) -> None:
        head = [f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}", f"Content-Length: {len(data)}", "Connection: close"]
        head += [f"{k}: {v}" for k, v in headers.items()]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
        await asyncio.wait_for(writer.drain(), 10)

    async def _read(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            raise _HttpError(400, "bad request line")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "content-length" and headers.get(k, v) != v:
                    raise _HttpError(400, "conflicting Content-Length headers")
                headers[k] = v
        if "chunked" in headers.get("transfer-encoding", "").lower():
            raise _HttpError(411, "send a Content-Length (chunked bodies are not supported)")
        raw_len = headers.get("content-length", "0") or "0"
        if not (raw_len.isascii() and raw_len.isdigit()):  # int() would also accept "+5", "1_0", "-0" and non-ASCII digits
            raise _HttpError(400, "bad Content-Length")
        length = int(raw_len)
        if length > MAX_BODY:
            raise _HttpError(413, f"body larger than {MAX_BODY} bytes")
        body = await reader.readexactly(length) if length else b""
        peer = writer.get_extra_info("peername") or ("?",)
        # The LAST entry is the one our own proxy appended; the first is client-supplied and can be spoofed to dodge
        # the failed-login limiter or to fill its table with fake addresses.
        fwd = headers.get("x-forwarded-for", "").split(",")[-1].strip()
        return method.upper(), target.split("?", 1)[0], headers, body, fwd or str(peer[0])

    # ---------------------------------------------------------------- routing
    def _limited(self, store: dict[str, deque], key: str, limit: int, record: bool = True) -> bool:
        now = time.monotonic()
        dq = store.get(key)
        if dq is None:
            if not record:
                return False
            dq = store[key] = deque()
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= limit:
            return True
        if record:
            dq.append(now)
        elif not dq:
            store.pop(key, None)
        return False

    def _note_fail(self, ip: str) -> None:
        now = time.monotonic()
        if ip not in self._fails and len(self._fails) >= MAX_TRACKED_IPS:
            for k in [k for k, d in self._fails.items() if not d or now - d[-1] > 60]:
                del self._fails[k]
            while len(self._fails) >= MAX_TRACKED_IPS:  # still full: forget the oldest address
                del self._fails[next(iter(self._fails))]
        self._fails[ip].append(now)

    def _authorized(self, headers: dict, body: bytes) -> bool:
        if not self.secret:
            return False
        secret = self.secret.encode()
        auth = headers.get("authorization", "")
        cand = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-webhook-secret", "")
        if cand and hmac.compare_digest(cand.encode(), secret):
            return True
        sig = (headers.get("x-signature") or headers.get("x-hub-signature-256") or "").strip()
        if sig:
            expect = hmac.new(secret, body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig.removeprefix("sha256=").lower().encode(), expect.encode())
        return False

    async def _route(self, method: str, path: str, headers: dict, body: bytes, ip: str):
        path = path.rstrip("/") or "/"
        if method == "OPTIONS":
            return 204, {}
        if path in ("/", "/health", "/healthz") and method in ("GET", "HEAD"):
            return 200, {"status": "ok", "uptime_s": int(time.time() - self.started_at)}
        if path not in ("/webhook", "/api/download", "/download", "/status"):
            raise _HttpError(404, "not found")
        if not self.secret:
            raise _HttpError(404, "webhook is disabled (set WEBHOOK_SECRET)")
        if self._limited(self._fails, ip, 10, record=False):
            raise _HttpError(429, "too many failed attempts")
        if not self._authorized(headers, body):
            self._note_fail(ip)  # 10 failures/min from one address -> blocked
            raise _HttpError(401, "invalid or missing secret")
        if self._limited(self._hits, "all", self.rate):
            raise _HttpError(429, "rate limit exceeded")
        if path == "/status":
            return 200, {"ok": True, **self.queue.snapshot()}
        if method != "POST":
            raise _HttpError(405, "use POST")
        return await self._enqueue(headers, body)

    # ---------------------------------------------------------------- webhook payload
    @staticmethod
    def parse_payload(headers: dict, body: bytes) -> tuple[list[tuple[str, str | None]], bool, list[str]]:
        """-> ([(url, caption)], force, invalid). Raises _HttpError(400) on garbage."""
        text = body.decode("utf-8", "replace").strip()
        ctype = headers.get("content-type", "").lower()
        data: object
        if "application/x-www-form-urlencoded" in ctype:
            q = parse_qs(text)
            data = {k: (v if len(v) > 1 or k in ("urls",) else v[0]) for k, v in q.items()}
        else:
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {"urls": extract_urls(text) or [text]}  # plain text: every link in it is queued
        raw: list[tuple[str, str | None]] = []
        if isinstance(data, str):
            data = {"urls": [data]}
        if isinstance(data, list):
            data = {"items": data}
        if not isinstance(data, dict):
            raise _HttpError(400, "send a JSON object like {\"url\": \"https://...\"}")
        force = str(data.get("force", "")).lower() in ("1", "true", "yes")
        top_caption = data.get("caption") if isinstance(data.get("caption"), str) else None
        plain: list[str] = []
        if isinstance(data.get("url"), str):
            plain.append(data["url"])
        urls = data.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if isinstance(urls, list):
            plain += [u for u in urls if isinstance(u, str)]
        if top_caption and len(plain) > 1:
            raise _HttpError(400, "a caption with several links is ambiguous - use \"items\": [{\"url\":..., \"caption\":...}]")
        raw += [(u, top_caption if len(plain) == 1 else None) for u in plain]
        items = data.get("items")
        if isinstance(items, (str, dict)):  # a bare string/object used to be iterated character by character / key by key
            items = [items]
        if not isinstance(items, list):
            items = []
        for it in items:
            if isinstance(it, str):
                raw.append((it, None))
            elif isinstance(it, dict) and isinstance(it.get("url"), str):
                cap = it.get("caption") if isinstance(it.get("caption"), str) else None
                raw.append((it["url"], cap))
        if len(raw) > MAX_URLS:  # never drop links silently
            raise _HttpError(400, f"too many links (max {MAX_URLS} per request)")
        out, invalid = [], []
        for u, cap in raw:
            found = extract_urls(u, 1)
            if found:
                out.append((found[0], (cap or "").strip()[:MAX_CAPTION] or None))
            else:
                invalid.append(u[:200])
        if not out and not invalid:
            raise _HttpError(400, "no link found: send {\"url\": \"https://...\"}")
        return out, force, invalid

    async def _enqueue(self, headers: dict, body: bytes):
        items, force, invalid = self.parse_payload(headers, body)
        if not items:
            raise _HttpError(400, f"none of the links is supported: {invalid}")
        if self.queue.snapshot()["pending"] + len(items) > self.max_queue:
            raise _HttpError(503, "queue is full, try again later")
        queued, duplicates, errors = [], [], []
        for url, caption in items:
            try:
                added = self.queue.enqueue(self._job(url=url, force=force, custom_caption=caption))
            except Exception:
                log.exception("webhook: could not queue %s", url)
                errors.append(url)
                continue
            (queued if added else duplicates).append(url)
        if errors and not queued and not duplicates:
            raise _HttpError(500, "could not queue the links")
        log.info("webhook: queued %d link(s), %d duplicate(s), %d invalid, %d error(s)",
                 len(queued), len(duplicates), len(invalid), len(errors))
        resp = {"ok": True, "queued": len(queued), "urls": queued, "duplicates": duplicates, "invalid": invalid}
        if errors:
            resp["errors"] = errors
        return 202, resp
