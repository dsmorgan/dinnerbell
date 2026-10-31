# SPDX-License-Identifier: AGPL-3.0-or-later
"""Member-facing pages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from dinnerbell.config import is_test_instance
from dinnerbell.domain.fun import Pose, fit_step, followed_state, pose, scene
from dinnerbell.services import event_views, offers
from dinnerbell.services.event_views import TodayView
from dinnerbell.web.auth import Context, Database, MaybePrincipal, Principal
from dinnerbell.web.render import render

router = APIRouter()


def today_page(principal: Principal, view: TodayView, now: datetime) -> dict[str, Any]:
    """What every render of Today needs: the calls, and the den and greeting fit (#153)."""
    return {
        "today": view,
        "from_page": "today",
        "den_pose": den_pose(principal, view, now),
        "greeting_fit": fit_step(f"Hi, {principal.member.display_name}"),
    }


def den_pose(principal: Principal, view: TodayView, now: datetime) -> Pose | None:
    """What Dinnerbone is doing on Today (#153), or None when the viewer has fun mode off. He
    follows the calls this viewer sees, so a guest's den follows the calls they're invited to."""
    if not principal.member.fun_mode:
        return None
    timezone = principal.household.timezone
    return pose(scene(now, timezone), followed_state([v.event for v in view.events], now, timezone))


@router.api_route("/", methods=["GET", "HEAD"])
async def today(request: Request, principal: MaybePrincipal, context: Context, db: Database) -> Response:
    """Today's calls with the roster, proposals, and actions (spec 11), or the sign-in page."""
    if principal is None:
        return await render(request, "signin.html")
    any_time = is_test_instance(context.settings.instance_label)  # #60: see routes/events.py
    now = context.clock()
    view = await event_views.today_view(db, principal.member, now=now, any_time=any_time)
    page = today_page(principal, view, now)
    if principal.member.is_guest:  # only the calls they're invited to (#38)
        return await render(request, "today.html", page | {"guest": True})
    form = await offers.offer_form(db, principal.member, now=now)
    return await render(request, "today.html", page | {"offer_form": form})
