# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preferences: the signed-in member's own vetoes, scores, capabilities, and reminders.

No route here accepts a member ID. Every read and write is for the signed-in member only,
admins included (FR-29).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.catalog import ItemKind, tags_by_kind
from dinnerbell.domain.members import Capabilities
from dinnerbell.domain.preferences import LEVEL_LABELS, LEVELS, NEUTRAL, InvalidPreference, Stance, TargetType
from dinnerbell.services import guests
from dinnerbell.services import preferences as preference_service
from dinnerbell.services.errors import NotFound
from dinnerbell.web.auth import Context, CSRFChecked, Database, Principal, SignedIn
from dinnerbell.web.forms import checked, choice, parse_id
from dinnerbell.web.render import is_htmx, render

router = APIRouter(prefix="/preferences")

Checkbox = Annotated[str | None, Form()]
NOTICES = {"settings": "Settings saved.", "saved": "Preference saved."}


def _levels() -> list[tuple[str, str]]:
    return [(level, LEVEL_LABELS[level]) for level in LEVELS]


@router.get("")
async def preferences_page(request: Request, principal: SignedIn, db: Database, notice: str = "") -> Response:
    mine = await preference_service.my_preferences(db, principal.member)
    return await render(
        request,
        "preferences.html",
        {
            "mine": mine,
            "tag_groups": tags_by_kind(mine.tags),
            "restaurants": [i for i in mine.items if i.kind is ItemKind.RESTAURANT],
            "home_meals": [i for i in mine.items if i.kind is ItemKind.HOME],
            "levels": _levels(),
            "TargetType": TargetType,
            "me": principal.member,
            "notice": NOTICES.get(notice),
            "dietary_tags": await guests.dietary_tags(db, principal.member),
        },
    )


async def _failed_row(
    request: Request, db: Database, principal: Principal, target_type: TargetType, target_id: int, error: str
) -> Response:
    current = await preference_service.my_stance(db, principal.member, target_type, target_id)
    name, stance = current if current is not None else ("Removed", NEUTRAL)
    context = {
        "target": f"{target_type.value}:{target_id}",
        "name": name,
        "stance": stance,
        "levels": _levels(),
        "error": error,
    }
    return await render(request, "_preference_row.html", context)


@router.post("/settings")
async def save_settings(
    principal: CSRFChecked,
    context: Context,
    db: Database,
    can_cook: Checkbox = None,
    can_pickup: Checkbox = None,
    can_order: Checkbox = None,
    mute_dm_reminders: Checkbox = None,
    feedback_reminder: Checkbox = None,
) -> Response:
    settings = preference_service.MySettings(
        capabilities=Capabilities(checked(can_cook), checked(can_pickup), checked(can_order)),
        mute_dm_reminders=checked(mute_dm_reminders),
        feedback_reminder=checked(feedback_reminder),
    )
    await preference_service.update_my_settings(db, principal.member, settings, now=context.clock())
    return RedirectResponse("/preferences?notice=settings", status_code=303)


@router.post("/stance")
async def save_stance(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    target: Annotated[str, Form(max_length=40)] = "",
    level: Annotated[str, Form(max_length=8)] = "",
) -> Response:
    """Set one stance. ``target`` reads ``tag:12`` or ``item:7``.

    HTMX gets the row back. When saving fails, it still gets the row, with status 200 so HTMX
    swaps it in, showing the stance that is actually stored and why the new one wasn't saved.
    A failure HTMX can't swap, such as an expired session, shows the page's save-error banner.
    """
    kind, _, raw_id = target.partition(":")
    target_type = choice(TargetType, kind)
    target_id = parse_id(raw_id)
    if target_type is None or target_id is None:
        message = "Reload the page and try again."
        if is_htmx(request):
            return Response(message, status_code=400)
        return await render(request, "error.html", {"title": "Not saved", "message": message}, status_code=400)
    try:
        stance = Stance.parse(level)
        name = await preference_service.set_my_preference(
            db, principal.member, target_type, target_id, stance, now=context.clock()
        )
    except InvalidPreference as exc:
        if not is_htmx(request):
            return await render(request, "error.html", {"title": "Not saved", "message": str(exc)}, status_code=400)
        return await _failed_row(request, db, principal, target_type, target_id, f"Not saved. {exc}")
    except NotFound:
        if not is_htmx(request):
            raise
        return await _failed_row(
            request,
            db,
            principal,
            target_type,
            target_id,
            "Not saved. It was removed from the catalog; reload the page.",
        )
    if is_htmx(request):
        return await render(
            request,
            "_preference_row.html",
            {"target": target, "name": name, "stance": stance, "levels": _levels(), "saved": True},
        )
    return RedirectResponse(f"/preferences?notice=saved#pref-{target_type.value}-{target_id}", status_code=303)
