# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template rendering with the context every page needs."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from starlette.responses import Response

from dinnerbell import __version__
from dinnerbell.web.context import TEMPLATES, app_context

HTMX_FILE = "htmx-2.0.11.min.js"


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def render(request: Request, template: str, context: dict[str, Any] | None = None, status_code: int = 200) -> Response:
    settings = app_context(request).settings
    principal = getattr(request.state, "principal", None)
    base = {
        "principal": principal,
        "csrf_token": principal.csrf_token if principal else None,
        "household_name": principal.household.name if principal else settings.household_name,
        "version": __version__,
        "htmx_file": HTMX_FILE,
        "provider_is_slack": settings.oidc_issuer.rstrip("/") == "https://slack.com",
    }
    return TEMPLATES.TemplateResponse(request, template, base | (context or {}), status_code=status_code)


def error_page(request: Request, status_code: int, title: str, message: str) -> Response:
    return render(request, "error.html", {"title": title, "message": message}, status_code=status_code)
