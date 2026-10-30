# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template rendering with the context every page needs."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from starlette.responses import Response

from dinnerbell.config import is_test_instance
from dinnerbell.services.household import current_household
from dinnerbell.services.stuck import StuckCall, stuck_calls
from dinnerbell.web import filters
from dinnerbell.web.context import TEMPLATES, app_context

HTMX_FILE = "htmx-2.0.11.min.js"
FALLBACK_NAME = "Dinnerbell"
log = logging.getLogger(__name__)
filters.register(TEMPLATES.env)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def _household_name(request: Request) -> str:
    """The household row is the source of truth, including on signed-out pages."""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        name: str = principal.household.name
        return name
    try:
        async with app_context(request).sessionmaker() as db:
            return (await current_household(db)).name
    except Exception:
        log.warning("could not read the household name", exc_info=True)
        return FALLBACK_NAME


async def _stuck_calls(request: Request) -> list[StuckCall]:
    """The household's stuck calls (#142), for the banner on every Admin tab. Admins only, and
    never for an HTMX fragment, which has no banner; a failed check shows no banner rather than
    breaking the page."""
    principal = getattr(request.state, "principal", None)
    if principal is None or not principal.member.is_admin:
        return []
    if not request.url.path.startswith("/admin") or is_htmx(request):
        return []
    context = app_context(request)
    now = context.clock()
    try:
        async with context.sessionmaker() as db:
            return await stuck_calls(db, principal.member, now, running_since=context.scheduler_state.started_at or now)
    except Exception:
        log.warning("stuck call check failed", exc_info=True)
        return []


async def render(
    request: Request, template: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> Response:
    settings = app_context(request).settings
    principal = getattr(request.state, "principal", None)
    base = {
        "principal": principal,
        "csrf_token": principal.csrf_token if principal else None,
        "household_name": await _household_name(request),
        "version": settings.version_label,
        "htmx_file": HTMX_FILE,
        "provider_is_slack": settings.oidc_issuer.rstrip("/") == "https://slack.com",
        "instance_label": settings.instance_label,
        "test_instance": is_test_instance(settings.instance_label),  # what only test may do (#60)
        "stuck_calls": await _stuck_calls(request),
    }
    return TEMPLATES.TemplateResponse(request, template, base | (context or {}), status_code=status_code)


async def error_page(request: Request, status_code: int, title: str, message: str) -> Response:
    return await render(request, "error.html", {"title": title, "message": message}, status_code=status_code)
