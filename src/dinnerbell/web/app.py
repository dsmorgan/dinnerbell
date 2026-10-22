# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from dinnerbell.config import Settings
from dinnerbell.scheduler.runner import JobContext, SchedulerState, build_scheduler
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.clock import Clock, utc_now
from dinnerbell.services.database import open_database
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.web.auth import AccessDenied, CSRFRejected, NotSignedIn, session_cookie_writer
from dinnerbell.web.context import AppContext, CookieNames
from dinnerbell.web.middleware import (
    OriginCheckMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    SessionCookieMiddleware,
)
from dinnerbell.web.oidc import OIDCClient
from dinnerbell.web.ratelimit import SlidingWindowLimiter
from dinnerbell.web.render import error_page, is_htmx
from dinnerbell.web.routes import admin, auth, health, pages
from dinnerbell.web.signing import SignedPayload, derive_key

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
AUTH_RATE_LIMIT = 20
AUTH_RATE_WINDOW_SECONDS = 60.0
OIDC_TIMEOUT_SECONDS = 10.0
LOGIN_STATE_MAX_AGE = timedelta(minutes=10)


def create_app(
    settings: Settings,
    *,
    clock: Clock = utc_now,
    oidc_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. ``oidc_transport`` lets tests route provider calls to a fake in-process."""
    cookies = CookieNames.for_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = open_database(settings.async_database_url)
        sessionmaker = database.sessionmaker
        async with sessionmaker() as db:
            await bootstrap(
                db,
                household_name=settings.household_name,
                timezone=settings.timezone,
                admin_email=settings.admin_email,
                now=clock(),
            )
        http = httpx.AsyncClient(transport=oidc_transport, timeout=OIDC_TIMEOUT_SECONDS, follow_redirects=False)
        secret = settings.secret_key.get_secret_value()
        state = SchedulerState()
        app.state.dinnerbell = AppContext(
            settings=settings,
            clock=clock,
            cookies=cookies,
            csrf_key=derive_key(secret, "csrf"),
            login_state=SignedPayload(derive_key(secret, "login-state"), LOGIN_STATE_MAX_AGE, clock),
            sessionmaker=sessionmaker,
            oidc=_oidc_client(settings, http, clock),
            scheduler_state=state,
        )
        scheduler = build_scheduler(
            JobContext(
                state=state,
                clock=clock,
                zone=settings.zone,
                database_path=settings.database_path,
                backup_dir=settings.backup_dir,
                sessionmaker=sessionmaker,
            )
        )
        scheduler.start()
        log.info("dinnerbell started")
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            await http.aclose()
            await database.close()

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in (health.router, pages.router, auth.router, admin.router):
        app.include_router(router)
    _add_exception_handlers(app)

    # Starlette runs the last-added middleware first, so this list reads innermost to outermost.
    app.add_middleware(SessionCookieMiddleware, writer=session_cookie_writer(cookies, settings))
    app.add_middleware(OriginCheckMiddleware, base_url=settings.base_url)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=SlidingWindowLimiter(AUTH_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS),
        session_cookie=cookies.session,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, trusted_proxies=settings.trusted_proxies)
    return app


def _oidc_client(settings: Settings, http: httpx.AsyncClient, clock: Clock) -> OIDCClient:
    # Slack's `team` parameter skips the workspace picker. Other providers ignore unknown parameters.
    extra = {"team": settings.slack_team_id} if settings.slack_team_id else {}
    return OIDCClient(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        scopes=settings.oidc_scopes,
        redirect_uri=f"{settings.base_url}/auth/callback",
        http=http,
        clock=clock,
        extra_auth_params=extra,
    )


def _add_exception_handlers(app: FastAPI) -> None:
    async def not_signed_in(request: Request, _exc: Exception) -> Response:
        if is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": "/"})
        return RedirectResponse("/", status_code=303)

    async def denied(request: Request, exc: Exception) -> Response:
        return error_page(request, 403, "Not allowed", str(exc))

    async def csrf_rejected(request: Request, _exc: Exception) -> Response:
        log.warning("CSRF token missing or wrong", extra={"path": request.url.path})
        return error_page(request, 403, "Request refused", "This form expired. Reload the page and try again.")

    async def not_found(request: Request, exc: Exception) -> Response:
        return error_page(request, 404, "Not found", str(exc) or "That page doesn't exist.")

    async def http_error(request: Request, exc: Exception) -> Response:
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        if status == 404:
            return error_page(request, 404, "Not found", "That page doesn't exist.")
        title = "Request refused" if status < 500 else "Something went wrong"
        return error_page(request, status, title, "Go back and try again.")

    async def unexpected(request: Request, _exc: Exception) -> Response:
        log.exception("unhandled error")
        return error_page(request, 500, "Something went wrong", "The error was logged. Try again in a moment.")

    app.add_exception_handler(NotSignedIn, not_signed_in)
    app.add_exception_handler(AccessDenied, denied)
    app.add_exception_handler(Forbidden, denied)
    app.add_exception_handler(CSRFRejected, csrf_rejected)
    app.add_exception_handler(NotFound, not_found)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(Exception, unexpected)
