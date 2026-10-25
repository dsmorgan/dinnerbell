# SPDX-License-Identifier: AGPL-3.0-or-later
"""Member-facing pages."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from dinnerbell.config import is_test_instance
from dinnerbell.services import event_views
from dinnerbell.web.auth import Context, Database, MaybePrincipal
from dinnerbell.web.render import render

router = APIRouter()


@router.api_route("/", methods=["GET", "HEAD"])
async def today(request: Request, principal: MaybePrincipal, context: Context, db: Database) -> Response:
    """Today's calls with the roster, proposals, and actions (spec 11), or the sign-in page."""
    if principal is None:
        return await render(request, "signin.html")
    any_time = is_test_instance(context.settings.instance_label)  # #60: see routes/events.py
    view = await event_views.today_view(db, principal.member, now=context.clock(), any_time=any_time)
    return await render(request, "today.html", {"today": view, "from_page": "today"})
