# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Members: invite, edit, deactivate, reactivate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.members import Capabilities, InvalidMemberInput, Member, MemberRuleViolation, Role
from dinnerbell.services import members as member_service
from dinnerbell.services.clock import Clock
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, Database, Principal
from dinnerbell.web.forms import RowId
from dinnerbell.web.render import is_htmx, render

router = APIRouter(prefix="/admin")

NOTICES = {
    "invited": "Invitation saved. They can sign in now.",
    "updated": "Member updated.",
    "deactivated": "Member deactivated and signed out everywhere.",
    "reactivated": "Member reactivated.",
}


FIELD_LIMIT = 320
"""Upper bound on any posted text field. The domain enforces the real limits (SEC-4)."""

Text = Annotated[str, Form(max_length=FIELD_LIMIT)]
Checkbox = Annotated[str | None, Form(max_length=FIELD_LIMIT)]


@dataclass(frozen=True, slots=True)
class PostedMember:
    """The member form as posted. Kept as typed so the page can redisplay it after an error."""

    display_name: str
    email: str
    role: str
    can_cook: bool
    can_pickup: bool
    can_order: bool

    def to_input(self) -> member_service.MemberInput:
        """Convert for the service, which normalizes and validates. Raises InvalidMemberInput."""
        try:
            role = Role(self.role)
        except ValueError:
            raise InvalidMemberInput("Choose a role from the list.") from None
        capabilities = Capabilities(self.can_cook, self.can_pickup, self.can_order)
        return member_service.MemberInput(self.display_name, self.email, role, capabilities)


def posted_member(
    display_name: Text = "",
    email: Text = "",
    role: Text = Role.MEMBER.value,
    can_cook: Checkbox = None,
    can_pickup: Checkbox = None,
    can_order: Checkbox = None,
) -> PostedMember:
    return PostedMember(display_name, email, role, can_cook is not None, can_pickup is not None, can_order is not None)


Posted = Annotated[PostedMember, Depends(posted_member)]


def _stored(member: Member) -> PostedMember:
    caps = member.capabilities
    return PostedMember(
        member.display_name, member.email, member.role.value, caps.can_cook, caps.can_pickup, caps.can_order
    )


def _list_context(principal: Principal, members: list[Member], **extra: Any) -> dict[str, Any]:
    return {"members": members, "roles": list(Role), "me": principal.member} | extra


@router.get("/members")
async def members_page(
    request: Request, principal: AdminPrincipal, db: Database, notice: str | None = None
) -> Response:
    members = await member_service.list_members(db, principal.member)
    return await render(
        request, "admin_members.html", _list_context(principal, members, notice=NOTICES.get(notice or ""))
    )


@router.post("/members")
async def invite(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    posted: Posted,
) -> Response:
    try:
        await member_service.invite_member(db, principal.member, posted.to_input(), now=context.clock())
        return RedirectResponse("/admin/members?notice=invited", status_code=303)
    except (InvalidMemberInput, MemberRuleViolation) as exc:
        error = str(exc)
    members = await member_service.list_members(db, principal.member)
    return await render(
        request, "admin_members.html", _list_context(principal, members, error=error, draft=posted), status_code=400
    )


@router.get("/members/{member_id}")
async def edit_page(request: Request, principal: AdminPrincipal, db: Database, member_id: RowId) -> Response:
    member = await member_service.get_member(db, principal.member, member_id)
    context = {"member": member, "values": _stored(member), "roles": list(Role)}
    return await render(request, "admin_member_edit.html", context)


@router.post("/members/{member_id}")
async def update(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    member_id: RowId,
    posted: Posted,
) -> Response:
    member = await member_service.get_member(db, principal.member, member_id)
    if member.is_linked and not posted.email:
        posted = replace(posted, email=member.email)  # the field is read-only once linked
    try:
        await member_service.update_member(db, principal.member, member_id, posted.to_input(), now=context.clock())
        return RedirectResponse("/admin/members?notice=updated", status_code=303)
    except (InvalidMemberInput, MemberRuleViolation) as exc:
        error = str(exc)
    # Redisplay what the admin typed, not the stored values, so nothing they changed is lost.
    page = {"member": member, "values": posted, "roles": list(Role), "error": error}
    return await render(request, "admin_member_edit.html", page, status_code=400)


@router.post("/members/{member_id}/deactivate")
async def deactivate(
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, member_id: RowId
) -> Response:
    return await _set_active(request, principal, context.clock, db, member_id, active=False)


@router.post("/members/{member_id}/reactivate")
async def reactivate(
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, member_id: RowId
) -> Response:
    return await _set_active(request, principal, context.clock, db, member_id, active=True)


async def _set_active(
    request: Request, principal: Principal, clock: Clock, db: Database, member_id: RowId, *, active: bool
) -> Response:
    try:
        member = await member_service.set_member_active(db, principal.member, member_id, active, now=clock())
    except MemberRuleViolation as exc:
        target = await member_service.get_member(db, principal.member, member_id)
        if is_htmx(request):
            return await render(
                request,
                "_member_row.html",
                {"m": target, "me": principal.member, "row_error": str(exc)},
                status_code=409,
            )
        members = await member_service.list_members(db, principal.member)
        return await render(
            request, "admin_members.html", _list_context(principal, members, error=str(exc)), status_code=409
        )
    if is_htmx(request):
        return await render(request, "_member_row.html", {"m": member, "me": principal.member})
    return RedirectResponse(f"/admin/members?notice={'reactivated' if active else 'deactivated'}", status_code=303)
