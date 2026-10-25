# SPDX-License-Identifier: AGPL-3.0-or-later
"""A guest's own page (#38 rule 4): their dietary needs, the one thing about themselves a guest
changes. Members set preferences instead, on the Preferences page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.members import InvalidMemberInput
from dinnerbell.services import guests
from dinnerbell.web.auth import Context, Database, GuestCSRFChecked, GuestOrMember
from dinnerbell.web.forms import parse_id
from dinnerbell.web.render import render

router = APIRouter()
Needs = Annotated[list[str] | None, Form()]


@router.get("/me")
async def my_needs(request: Request, principal: GuestOrMember, db: Database, saved: str | None = None) -> Response:
    if not principal.member.is_guest:
        return RedirectResponse("/preferences", status_code=303)
    tags = await guests.dietary_tags(db, principal.member)
    page = {"tags": tags, "mine": principal.member.dietary_needs, "saved": saved == "1"}
    return await render(request, "me.html", page)


@router.post("/me")
async def save_my_needs(
    request: Request, principal: GuestCSRFChecked, context: Context, db: Database, needs: Needs = None
) -> Response:
    chosen = frozenset(i for v in needs or [] if (i := parse_id(v)) is not None)
    try:
        await guests.set_my_needs(db, principal.member, chosen, now=context.clock())
    except InvalidMemberInput as exc:
        tags = await guests.dietary_tags(db, principal.member)
        page = {"tags": tags, "mine": chosen, "error": str(exc)}
        return await render(request, "me.html", page, status_code=400)
    return RedirectResponse("/me?saved=1", status_code=303)
