# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Stats page (FR-38, FR-40), in two tabs (#153): Yours, a person's own counts and badges, and
for members Household, the household's totals and newest badges. Also Admin > Badges, where an
admin sets the five tier counts of each of the three ladders (FR-43, #130).

The page never ranks anyone. A person sees their own numbers; the household list is by recency,
with no member's count beside another's. A guest sees only their own pitching-in badges."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from dinnerbell.domain.badges import BADGES, LADDERS, TIERS, InvalidThresholds, Ladder
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
    """The Yours tab: the person's own badges and counts. A guest sees only this."""
    member = principal.member
    guest = member.is_guest
    mine = await (stats.guest_stats(db, member) if guest else stats.my_stats(db, member))
    page: dict[str, Any] = {
        "guest": guest,
        "tab": "mine",
        "mine": mine,
        "fresh": stats.newly_earned(mine, context.clock()),
        "kinds": [
            (label, mine.counts.get(kind, 0)) for kind, label in KIND_LABELS.items() if not guest or kind in PITCHING_IN
        ],
    }
    return await render(request, "stats.html", page)


@router.get("/stats/household")
async def household_stats_page(request: Request, principal: GuestOrMember, db: Database) -> Response:
    """The Household tab, for members: the household's totals and its newest badges. A guest who
    follows a channel post's Stats link gets their own badges instead (#153)."""
    if principal.member.is_guest:
        return RedirectResponse("/stats", status_code=303)
    page: dict[str, Any] = {
        "guest": False,
        "tab": "household",
        "household": await stats.household_stats(db, principal.member),
        "kind_labels": HOUSEHOLD_LABELS,
    }
    return await render(request, "stats.html", page)


# Admin > Badges (FR-43) ----------------------------------------------------------------------

FIELDS = {ladder: [f"threshold_{ladder.value}_{tier.value}" for tier in TIERS] for ladder in LADDERS}
"""Each ladder's five form fields, one per tier."""
MAX_DIGITS = 6
NOTICES = {"saved": "Badge counts saved.", "reset": "Badge counts reset to the defaults."}


def _page(values: dict[str, str], **extra: Any) -> dict[str, Any]:
    rows = [
        {
            "ladder": ladder,
            "badges": ", ".join(b.name for b in BADGES if b.ladder is ladder),
            "fields": list(zip(TIERS, FIELDS[ladder], strict=True)),
        }
        for ladder in LADDERS
    ]
    return {"values": values, "ladders": rows} | extra


def _shown(ladders: dict[Ladder, tuple[int, ...]]) -> dict[str, str]:
    """The stored counts as the form shows them, so saving it untouched changes nothing."""
    return {name: str(n) for ladder in LADDERS for name, n in zip(FIELDS[ladder], ladders[ladder], strict=True)}


def _posted(form: FormData) -> dict[str, str]:
    return {name: str(form.get(name) or "")[:40] for names in FIELDS.values() for name in names}


def _parse(posted: dict[str, str]) -> dict[Ladder, list[int]]:
    ladders: dict[Ladder, list[int]] = {}
    for ladder in LADDERS:
        counts: list[int] = []
        for tier, name in zip(TIERS, FIELDS[ladder], strict=True):
            text = posted[name].strip()  # no regex: a pattern over form input can backtrack
            if not (text.isascii() and text.isdigit() and len(text) <= MAX_DIGITS):
                raise BadFormValue(f"{ladder.label} ladder: The {tier.label} count must be a whole number.")
            counts.append(int(text))
        ladders[ladder] = counts
    return ladders


@router.get("/admin/badges")
async def admin_badges_page(request: Request, principal: AdminPrincipal, db: Database, notice: str = "") -> Response:
    ladders = await contributions.ladders(db, principal.member.household_id)
    return await render(request, "admin_badges.html", _page(_shown(ladders), notice=NOTICES.get(notice)))


@router.post("/admin/badges")
async def save_badges(request: Request, principal: AdminCSRFChecked, context: Context, db: Database) -> Response:
    posted = _posted(await request.form())
    try:
        await contributions.set_thresholds(db, principal.member, _parse(posted), now=context.clock())
    except (InvalidThresholds, BadFormValue) as exc:
        return await render(request, "admin_badges.html", _page(posted, error=str(exc)), status_code=400)
    return RedirectResponse("/admin/badges?notice=saved", status_code=303)


@router.post("/admin/badges/reset")
async def reset_badges(principal: AdminCSRFChecked, context: Context, db: Database) -> Response:
    await contributions.reset_thresholds(db, principal.member, now=context.clock())
    return RedirectResponse("/admin/badges?notice=reset", status_code=303)
