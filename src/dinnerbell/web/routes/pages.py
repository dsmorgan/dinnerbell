# SPDX-License-Identifier: AGPL-3.0-or-later
"""Member-facing pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from dinnerbell.web.auth import MaybePrincipal
from dinnerbell.web.render import render

router = APIRouter()


@router.api_route("/", methods=["GET", "HEAD"])
async def today(request: Request, principal: MaybePrincipal) -> Response:
    if principal is None:
        return await render(request, "signin.html")
    return await render(request, "today.html")
