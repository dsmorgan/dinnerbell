# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure ASGI middleware: request context and access logs, security headers, origin checks,
rate limits, and session-cookie updates."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dinnerbell.logs import member_id_var, request_id_var
from dinnerbell.web.clientip import IPNetwork, resolve_client_ip
from dinnerbell.web.ratelimit import SlidingWindowLimiter

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
        client_ip = resolve_client_ip(peer, Headers(scope=scope).get("x-forwarded-for"), self.trusted_proxies)
        _state(scope).update(request_id=request_id, client_ip=client_ip)
        request_token = request_id_var.set(request_id)
        member_token = member_id_var.set(None)
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
            request_id_var.reset(request_token)
            member_id_var.reset(member_token)

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
    """Add SEC-2 headers to every response and forbid caching outside /static."""

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
        keys = [f"ip:{_state(scope).get('client_ip', 'unknown')}"]
        cookie = _cookie(scope, self.session_cookie)
        if cookie:
            keys.append(f"session:{cookie[:16]}")
        waits = [wait for key in keys if (wait := self.limiter.hit(key)) is not None]
        return max(waits) if waits else None


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
