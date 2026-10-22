# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assemble the FastAPI app: routes, middleware, and error pages."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from dinnerbell.config import Settings
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.web.auth import AccessDenied, CSRFRejected, NotSignedIn, session_cookie_writer
from dinnerbell.web.context import CookieNames
from dinnerbell.web.middleware import (
    ErrorPageMiddleware,
    OriginCheckMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    SessionCookieMiddleware,
)
from dinnerbell.web.ratelimit import SlidingWindowLimiter
from dinnerbell.web.render import error_page, is_htmx
from dinnerbell.web.routes import admin, auth, catalog, health, history, pages, preferences

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
AUTH_RATE_LIMIT = 20
AUTH_RATE_WINDOW_SECONDS = 60.0
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_app(settings: Settings, lifespan: Lifespan) -> FastAPI:
    """Assemble routes, middleware, and error handling. The caller supplies the lifespan that
    puts an ``AppContext`` on ``app.state.dinnerbell``; see ``dinnerbell.app_factory``."""
    cookies = CookieNames.for_settings(settings)
    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in (
        health.router,
        pages.router,
        auth.router,
        admin.router,
        catalog.router,
        preferences.router,
        history.router,
    ):
        app.include_router(router)
    _add_exception_handlers(app)

    # Starlette runs the last-added middleware first, so this list reads innermost to outermost.
    app.add_middleware(ErrorPageMiddleware, render=_server_error_page)
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


def _add_exception_handlers(app: FastAPI) -> None:
    async def not_signed_in(request: Request, _exc: Exception) -> Response:
        if is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": "/"})
        return RedirectResponse("/", status_code=303)

    async def denied(request: Request, exc: Exception) -> Response:
        return await error_page(request, 403, "Not allowed", str(exc))

    async def csrf_rejected(request: Request, _exc: Exception) -> Response:
        log.warning("CSRF token missing or wrong", extra={"path": request.url.path})
        return await error_page(request, 403, "Request refused", "This form expired. Reload the page and try again.")

    async def not_found(request: Request, exc: Exception) -> Response:
        return await error_page(request, 404, "Not found", str(exc) or "That page doesn't exist.")

    async def http_error(request: Request, exc: Exception) -> Response:
        status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        if status == 404:
            return await error_page(request, 404, "Not found", "That page doesn't exist.")
        title = "Request refused" if status < 500 else "Something went wrong"
        return await error_page(request, status, title, "Go back and try again.")

    async def invalid_request(request: Request, exc: Exception) -> Response:
        # Never echo the submitted values back; FastAPI's default 422 JSON does.
        errors = exc.errors() if isinstance(exc, RequestValidationError) else []
        if any(error.get("loc", ("",))[0] == "path" for error in errors):
            return await error_page(request, 404, "Not found", "That page doesn't exist.")
        return await error_page(
            request, 400, "Request refused", "Something in that request wasn't valid. Go back and try again."
        )

    app.add_exception_handler(NotSignedIn, not_signed_in)
    app.add_exception_handler(AccessDenied, denied)
    app.add_exception_handler(Forbidden, denied)
    app.add_exception_handler(CSRFRejected, csrf_rejected)
    app.add_exception_handler(NotFound, not_found)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid_request)


async def _server_error_page(scope: Scope) -> Response:
    return await error_page(Request(scope), 500, "Something went wrong", "The error was logged. Try again in a moment.")
