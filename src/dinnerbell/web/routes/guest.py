# SPDX-License-Identifier: AGPL-3.0-or-later
"""A guest's own page (#38 rule 4): their dietary needs, the one thing about themselves a guest
changes that matters to anyone else. Members set preferences instead, on the Preferences page.

``/me/fun`` turns fun mode (#153) on or off for whoever is signed in, member or guest: the
form is on this page for a guest and on Preferences for a member."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.domain.members import InvalidMemberInput
from dinnerbell.services import activity, guests
from dinnerbell.services.fun import set_my_fun_mode
from dinnerbell.web.auth import Context, Database, GuestCSRFChecked, GuestOrMember
from dinnerbell.web.forms import parse_id
from dinnerbell.web.render import render

router = APIRouter()
Needs = Annotated[list[str] | None, Form()]
FUN_CHOICES = {"on": True, "off": False}


@router.get("/me")
async def my_needs(request: Request, principal: GuestOrMember, db: Database, saved: str | None = None) -> Response:
    if not principal.member.is_guest:
        return RedirectResponse("/preferences", status_code=303)
    tags = await guests.dietary_tags(db, principal.member)
    page = {"tags": tags, "mine": principal.member.dietary_needs, "saved": saved == "1", "fun_saved": saved == "fun"}
    return await render(request, "me.html", page)


@router.post("/me")
async def save_my_needs(
    request: Request, principal: GuestCSRFChecked, context: Context, db: Database, needs: Needs = None
) -> Response:
    chosen = frozenset(i for v in needs or [] if (i := parse_id(v)) is not None)
    try:
        await guests.set_my_needs(db, principal.member, chosen, now=context.clock())
    except (InvalidMemberInput, EventRuleViolation) as exc:
        activity.refused(exc, str(exc), principal.member, path=request.url.path)
        tags = await guests.dietary_tags(db, principal.member)
        page = {"tags": tags, "mine": chosen, "error": str(exc)}
        return await render(request, "me.html", page, status_code=400)
    if not principal.member.is_guest:  # a member's form is on Preferences
        return RedirectResponse("/preferences?notice=settings", status_code=303)
    return RedirectResponse("/me?saved=1", status_code=303)


@router.post("/me/fun")
async def save_my_fun_mode(
    request: Request,
    principal: GuestCSRFChecked,
    context: Context,
    db: Database,
    fun: Annotated[str, Form(max_length=3)] = "",
) -> Response:
    """Fun mode on or off for the signed-in person (#153), from Preferences or a guest's page."""
    on = FUN_CHOICES.get(fun)
    if on is None:
        message = "Choose On or Off."
        return await render(request, "error.html", {"title": "Not saved", "message": message}, status_code=400)
    await set_my_fun_mode(db, principal.member, on, now=context.clock())
    # Back to the card, which says it saved.
    if principal.member.is_guest:
        return RedirectResponse("/me?saved=fun#fun-mode", status_code=303)
    return RedirectResponse("/preferences?notice=fun#fun-mode", status_code=303)
