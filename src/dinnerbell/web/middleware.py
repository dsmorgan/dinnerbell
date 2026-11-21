# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure ASGI middleware: request context and access logs, security headers, origin checks,
rate limits, and session-cookie updates."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dinnerbell.logs import member_id_var, start_unit
from dinnerbell.web.clientip import IPNetwork, resolve_client_ip
from dinnerbell.web.ratelimit import SlidingWindowLimiter, client_key

log = logging.getLogger("dinnerbell.web.access")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
# Static files are never pages. An SVG opened directly is a document that could run script or
# fetch other resources, so every file served from /static gets a policy that allows nothing. Browsers
# ignore a CSP on scripts, stylesheets, and images loaded by a page, so this costs nothing there.
STATIC_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), browsing-topics=()",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
QUIET_PATHS = frozenset({"/healthz"})


def _state(scope: Scope) -> dict[str, Any]:
    state: dict[str, Any] = scope.setdefault("state", {})
    return state


class RequestContextMiddleware:
    """Assign a request ID, resolve the client IP, and write one access-log line per request."""

    def __init__(self, app: ASGIApp, trusted_proxies: Sequence[IPNetwork]) -> None:
        self.app = app
        self.trusted_proxies = tuple(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        peer = scope["client"][0] if scope.get("client") else None
        # A proxy chain may send several X-Forwarded-For lines; together they form one list.
        forwarded = ", ".join(Headers(scope=scope).getlist("x-forwarded-for")) or None
        client_ip = resolve_client_ip(peer, forwarded, self.trusted_proxies)
        _state(scope).update(request_id=request_id, client_ip=client_ip)
        end = start_unit("web", request_id)
        status = 500
        started = time.perf_counter()
        log.debug("client address", extra={"peer_ip": peer, "client_ip": client_ip})

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            self._log(scope, status, started, client_ip)
            end()

    def _log(self, scope: Scope, status: int, started: float, client_ip: str) -> None:
        member_id = _state(scope).get("member_id")
        if member_id is not None:
            member_id_var.set(member_id)
        level = logging.DEBUG if scope["path"] in QUIET_PATHS and status < 500 else logging.INFO
        log.log(
            level,
            "request",
            extra={
                "method": scope["method"],
                "path": scope["path"],
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "client_ip": client_ip,
            },
        )


class SecurityHeadersMiddleware:
    """Add SEC-2 headers to every response, lock /static down to a no-op CSP, and forbid caching
    outside /static."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cacheable = scope["path"].startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if not cacheable:
                    headers["Cache-Control"] = "no-store"
                elif message["status"] < 400:
                    # A miss under /static renders the normal error page, which keeps its policy.
                    headers["Content-Security-Policy"] = STATIC_CONTENT_SECURITY_POLICY
            await send(message)

        await self.app(scope, receive, send_with_headers)


def origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return None
    default_port = {"http": 80, "https": 443}.get(parts.scheme)
    port = f":{parts.port}" if parts.port and parts.port != default_port else ""
    return f"{parts.scheme}://{parts.hostname}{port}".lower()


class OriginCheckMiddleware:
    """Reject state-changing requests whose Origin (or Referer) is not this site."""

    def __init__(self, app: ASGIApp, base_url: str) -> None:
        self.app = app
        expected = origin_of(base_url)
        if expected is None:
            raise ValueError("base_url must be an absolute URL")
        self.expected = expected

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS and not self._same_origin(scope):
            log.warning("cross-origin request rejected", extra={"path": scope["path"]})
            response = PlainTextResponse("Cross-origin request refused.", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _same_origin(self, scope: Scope) -> bool:
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is not None:
            return origin != "null" and origin_of(origin) == self.expected
        referer = headers.get("referer")
        return referer is not None and origin_of(referer) == self.expected


class RateLimitMiddleware:
    """Limit requests under ``prefix`` per client IP and per session cookie (SEC-3)."""

    def __init__(
        self,
        app: ASGIApp,
        limiter: SlidingWindowLimiter,
        session_cookie: str,
        prefix: str = "/auth/",
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.session_cookie = session_cookie
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            retry_after = self._check(scope)
            if retry_after is not None:
                log.warning("rate limit exceeded", extra={"path": scope["path"]})
                response = PlainTextResponse(
                    "Too many sign-in attempts. Wait a minute and try again.",
                    status_code=429,
                    headers={"Retry-After": str(int(retry_after) + 1)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _check(self, scope: Scope) -> float | None:
        # The IP key is checked first and alone when it is over the limit, so a client can't
        # mint new session-cookie keys once its address is limited.
        retry_after = self.limiter.hit(client_key(_state(scope).get("client_ip", "unknown")))
        if retry_after is not None:
            return retry_after
        cookie = _cookie(scope, self.session_cookie)
        return self.limiter.hit(f"session:{cookie[:16]}") if cookie else None


def _cookie(scope: Scope, name: str) -> str | None:
    header = Headers(scope=scope).get("cookie")
    if not header:
        return None
    parsed: SimpleCookie = SimpleCookie()
    try:
        parsed.load(header)
    except Exception:
        return None
    morsel = parsed.get(name)
    return morsel.value if morsel else None


ErrorRenderer = Callable[[Scope], Awaitable[Response]]


class ErrorPageMiddleware:
    """Turn unhandled exceptions into the friendly 500 page.

    It sits inside the other middleware so the 500 page still gets security headers, the
    request ID, and an access-log line. Starlette's own handler for ``Exception`` runs in
    ServerErrorMiddleware, outside all user middleware, so it would get none of those.
    """

    def __init__(self, app: ASGIApp, render: ErrorRenderer) -> None:
        self.app = app
        self.render = render

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except ClientDisconnect:
            # The browser went away mid-request. Nothing failed and nobody is left to answer.
            log.debug("client disconnected", extra={"path": scope["path"]})
            if started:
                raise
        except Exception:
            if started:
                raise  # too late for an error page; the server logs the exception itself
            log.exception("unhandled error", extra={"path": scope["path"]})
            response = await self._error_response(scope)
            await response(scope, receive, send)

    async def _error_response(self, scope: Scope) -> Response:
        try:
            return await self.render(scope)
        except Exception:
            log.exception("error page failed to render")
            return PlainTextResponse("Something went wrong.", status_code=500)


CookieWriter = Callable[[MutableHeaders, Scope], None]


class SessionCookieMiddleware:
    """Apply session-cookie changes that auth dependencies request via ``scope['state']``.

    Dependencies cannot reliably add headers to responses that routes return directly, so they
    leave an instruction (renew or clear) and this middleware writes the header.
    """

    def __init__(self, app: ASGIApp, writer: CookieWriter) -> None:
        self.app = app
        self.writer = writer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.writer(MutableHeaders(scope=message), scope)
            await send(message)

        await self.app(scope, receive, send_with_cookie)


class BodyLimitMiddleware:
    """Refuse request bodies over a size limit before anything reads them.

    FastAPI parses form bodies before any dependency runs, so without this an anonymous client
    could make the app read, and spool to /tmp, a body of any size. A declared Content-Length
    over the limit gets 413 without reading a byte; a chunked or undeclared body is cut off as
    soon as it passes the limit. It sits just inside RequestContextMiddleware and outside the
    security-headers middleware, so it sets the security headers on its own 413.
    """

    def __init__(self, app: ASGIApp, default_limit: int, path_limits: Sequence[tuple[str, int]] = ()) -> None:
        self.app = app
        self.default_limit = default_limit
        self.path_limits = tuple(path_limits)

    def limit_for(self, path: str) -> int:
        return next((limit for prefix, limit in self.path_limits if path.startswith(prefix)), self.default_limit)

    @staticmethod
    def declared_length(headers: Sequence[tuple[bytes, bytes]]) -> int | str | None:
        """The Content-Length as an int, None if absent, or "invalid"."""
        for name, value in headers:
            if name.lower() == b"content-length":
                text = value.decode("latin-1")
                return int(text) if text.isascii() and text.isdigit() else "invalid"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        declared = self.declared_length(scope.get("headers") or [])
        if declared == "invalid":
            await _plain(400, "Bad request.")(scope, receive, send)
            return
        if isinstance(declared, int) and declared > limit:
            log.warning("request body too large", extra={"path": scope["path"], "declared": declared})
            await too_large(scope["path"])(scope, receive, send)
            return
        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    log.warning("request body too large", extra={"path": scope["path"], "received": received})
                    # To the app this looks like the client went away, which it already handles.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded and not started:
                return  # the 413 below replaces whatever the app tries to say
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded or started:
                raise
        if exceeded and not started:
            await too_large(scope["path"])(scope, receive, send)


def _plain(status: int, text: str) -> PlainTextResponse:
    headers = SECURITY_HEADERS | {"Cache-Control": "no-store", "Connection": "close"}
    return PlainTextResponse(text, status_code=status, headers=headers)


def too_large(path: str) -> PlainTextResponse:
    hint = " Catalog files can be up to 1 MB." if path.startswith("/catalog/import/") else ""
    return _plain(413, f"That request is too large.{hint}")


class TrailingSlashMiddleware:
    """Serve ``/catalog/`` as ``/catalog`` without a redirect.

    Starlette's own slash redirect builds its ``Location`` from the Host header and the scheme
    uvicorn sees, which is always ``http`` behind the proxy: it downgraded HTTPS links and echoed
    any Host. Dropping the slash before routing needs no URL at all. ``/`` and ``/static`` stay as
    they are."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            if len(path) > 1 and path.endswith("/") and not path.startswith("/static/"):
                trimmed = path.rstrip("/") or "/"
                scope = dict(scope, path=trimmed, raw_path=trimmed.encode())
        await self.app(scope, receive, send)
