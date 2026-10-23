# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template rendering with the context every page needs."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from starlette.responses import Response

from dinnerbell import __version__
from dinnerbell.services.household import current_household
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


async def render(
    request: Request, template: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> Response:
    settings = app_context(request).settings
    principal = getattr(request.state, "principal", None)
    base = {
        "principal": principal,
        "csrf_token": principal.csrf_token if principal else None,
        "household_name": await _household_name(request),
        "version": __version__,
        "htmx_file": HTMX_FILE,
        "provider_is_slack": settings.oidc_issuer.rstrip("/") == "https://slack.com",
        "instance_label": settings.instance_label,
    }
    return TEMPLATES.TemplateResponse(request, template, base | (context or {}), status_code=status_code)


async def error_page(request: Request, status_code: int, title: str, message: str) -> Response:
    return await render(request, "error.html", {"title": title, "message": message}, status_code=status_code)
