# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Stats page (FR-38, FR-40): a person's own counts and badges, and for members the
household's totals and newest badges; and Admin > Badges, where an admin sets the five tier
counts (FR-43).

The page never ranks anyone. A person sees their own numbers; the household list is by recency,
with no member's count beside another's. A guest sees only their own pitching-in badges."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from dinnerbell.domain.badges import TIERS, InvalidThresholds
from dinnerbell.domain.contributions import PITCHING_IN, Kind
from dinnerbell.services import contributions, stats
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, Database, GuestOrMember
from dinnerbell.web.forms import BadFormValue
from dinnerbell.web.render import render

router = APIRouter()

KIND_LABELS = {
    Kind.COOKED: "Meals cooked",
    Kind.PICKED_UP: "Meals picked up",
    Kind.ORDERED: "Meals ordered",
    Kind.COORDINATED: "Meals coordinated",
    Kind.PARTICIPATED: "Meals shared",
    Kind.RESPONDED_ON_TIME: "Quick answers",
    Kind.SUGGESTED: "Ideas that won",
    Kind.ADDED_ITEM: "Catalog finds that were chosen",
    Kind.EXPLORED: "New places or cuisines that won",
    Kind.HOSTED: "Meals with guests",
    Kind.GAVE_FEEDBACK: "Meals you gave feedback on",
}
"""Every kind's label for the person's own page, in the order it lists them."""
HOUSEHOLD_LABELS = KIND_LABELS | {
    Kind.GAVE_FEEDBACK: "Feedback given",
}
"""The same kinds for the household's totals, worded for nobody in particular."""


@router.get("/stats")
async def stats_page(request: Request, principal: GuestOrMember, context: Context, db: Database) -> Response:
    member = principal.member
    guest = member.is_guest
    mine = await (stats.guest_stats(db, member) if guest else stats.my_stats(db, member))
    page: dict[str, Any] = {
        "guest": guest,
        "mine": mine,
        "fresh": stats.newly_earned(mine, context.clock()),
        "kinds": [
            (label, mine.counts.get(kind, 0)) for kind, label in KIND_LABELS.items() if not guest or kind in PITCHING_IN
        ],
        "household": None if guest else await stats.household_stats(db, member),
        "kind_labels": HOUSEHOLD_LABELS,
    }
    return await render(request, "stats.html", page)


# Admin > Badges (FR-43) ----------------------------------------------------------------------

FIELDS = [f"threshold_{tier.value}" for tier in TIERS]
WHOLE = re.compile(r"\s*(\d{1,6})\s*")
NOTICES = {"saved": "Badge counts saved."}
Field = Annotated[str, Form(max_length=40)]


def _page(values: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {"values": values, "tiers": [(tier, f"threshold_{tier.value}") for tier in TIERS]} | extra


def _shown(ladder: tuple[int, ...]) -> dict[str, str]:
    """The stored counts as the form shows them, so saving it untouched changes nothing."""
    return {name: str(n) for name, n in zip(FIELDS, ladder, strict=True)}


def _posted(form: FormData) -> dict[str, str]:
    return {name: str(form.get(name) or "")[:40] for name in FIELDS}


def _parse(posted: dict[str, str]) -> list[int]:
    counts: list[int] = []
    for tier, name in zip(TIERS, FIELDS, strict=True):
        found = WHOLE.fullmatch(posted[name])
        if found is None:
            raise BadFormValue(f"The {tier.label} count must be a whole number.")
        counts.append(int(found.group(1)))
    return counts


@router.get("/admin/badges")
async def admin_badges_page(request: Request, principal: AdminPrincipal, db: Database, notice: str = "") -> Response:
    ladder = await contributions.thresholds(db, principal.member.household_id)
    return await render(request, "admin_badges.html", _page(_shown(ladder), notice=NOTICES.get(notice)))


@router.post("/admin/badges")
async def save_badges(request: Request, principal: AdminCSRFChecked, context: Context, db: Database) -> Response:
    posted = _posted(await request.form())
    try:
        await contributions.set_thresholds(db, principal.member, _parse(posted), now=context.clock())
    except (InvalidThresholds, BadFormValue) as exc:
        return await render(request, "admin_badges.html", _page(posted, error=str(exc)), status_code=400)
    return RedirectResponse("/admin/badges?notice=saved", status_code=303)
