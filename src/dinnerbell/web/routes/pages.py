# SPDX-License-Identifier: AGPL-3.0-or-later
"""Member-facing pages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from dinnerbell.config import is_test_instance
from dinnerbell.domain.fun import fit_step
from dinnerbell.services import event_views, fun, offers
from dinnerbell.services.event_views import TodayView
from dinnerbell.web.auth import Context, Database, MaybePrincipal, Principal
from dinnerbell.web.render import render

router = APIRouter()


async def today_page(db: Database, principal: Principal, view: TodayView, now: datetime) -> dict[str, Any]:
    """What every render of Today needs: the calls, and with fun mode on, the den (#153), with
    any badge it hasn't flipped for yet, and how wide the greeting reads."""
    den = None
    if principal.member.fun_mode:
        den = await fun.den(db, principal.member, principal.household, now=now, with_badge=True)
    return {
        "today": view,
        "from_page": "today",
        "den": den,
        "greeting_fit": fit_step(f"Hi, {principal.member.display_name}"),
    }


@router.api_route("/", methods=["GET", "HEAD"])
async def today(request: Request, principal: MaybePrincipal, context: Context, db: Database) -> Response:
    """Today's calls with the roster, proposals, and actions (spec 11), or the sign-in page."""
    if principal is None:
        return await render(request, "signin.html")
    any_time = is_test_instance(context.settings.instance_label)  # #60: see routes/events.py
    now = context.clock()
    view = await event_views.today_view(db, principal.member, now=now, any_time=any_time)
    page = await today_page(db, principal, view, now)
    if principal.member.is_guest:  # only the calls they're invited to (#38)
        return await render(request, "today.html", page | {"guest": True})
    form = await offers.offer_form(db, principal.member, now=now)
    return await render(request, "today.html", page | {"offer_form": form})
