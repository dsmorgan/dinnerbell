# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Members: invite, edit, deactivate, reactivate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from dinnerbell.domain.members import Capabilities, InvalidMemberInput, Member, MemberRuleViolation, Role
from dinnerbell.services import members as member_service
from dinnerbell.services.clock import Clock
from dinnerbell.web.auth import AdminPrincipal, Context, CSRFChecked, Database, Principal
from dinnerbell.web.render import is_htmx, render

router = APIRouter(prefix="/admin")

NOTICES = {
    "invited": "Invitation saved. They can sign in now.",
    "updated": "Member updated.",
    "deactivated": "Member deactivated and signed out everywhere.",
    "reactivated": "Member reactivated.",
}


class MemberForm(BaseModel):
    """Form fields as posted. Domain normalization runs in the service (SEC-4)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    display_name: str = Field(max_length=200)
    email: str = Field(max_length=320)
    role: Role = Role.MEMBER
    can_cook: bool = False
    can_pickup: bool = False
    can_order: bool = False

    def to_input(self) -> member_service.MemberInput:
        return member_service.MemberInput(
            display_name=self.display_name,
            email=self.email,
            role=self.role,
            capabilities=Capabilities(self.can_cook, self.can_pickup, self.can_order),
        )


Checkbox = Annotated[str | None, Form()]


@dataclass(frozen=True, slots=True)
class PostedMember:
    """The member form exactly as posted, kept so the page can redisplay it after an error."""

    display_name: str
    email: str
    role: str
    can_cook: bool
    can_pickup: bool
    can_order: bool

    def validated(self) -> MemberForm | str:
        try:
            return MemberForm(
                display_name=self.display_name,
                email=self.email,
                role=Role(self.role),
                can_cook=self.can_cook,
                can_pickup=self.can_pickup,
                can_order=self.can_order,
            )
        except ValueError:
            return "Check the form: one of the values isn't valid."


def posted_member(
    display_name: Annotated[str, Form(max_length=200)] = "",
    email: Annotated[str, Form(max_length=320)] = "",
    role: Annotated[str, Form(max_length=16)] = Role.MEMBER.value,
    can_cook: Checkbox = None,
    can_pickup: Checkbox = None,
    can_order: Checkbox = None,
) -> PostedMember:
    return PostedMember(display_name, email, role, can_cook is not None, can_pickup is not None, can_order is not None)


Posted = Annotated[PostedMember, Depends(posted_member)]


def _list_context(principal: Principal, members: list[Member], **extra: Any) -> dict[str, Any]:
    return {"members": members, "roles": list(Role), "me": principal.member} | extra


@router.get("/members")
async def members_page(
    request: Request, principal: AdminPrincipal, db: Database, notice: str | None = None
) -> Response:
    members = await member_service.list_members(db, principal.member)
    return render(request, "admin_members.html", _list_context(principal, members, notice=NOTICES.get(notice or "")))


@router.post("/members")
async def invite(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    posted: Posted,
) -> Response:
    form = posted.validated()
    error = form if isinstance(form, str) else None
    if isinstance(form, MemberForm):
        try:
            await member_service.invite_member(db, principal.member, form.to_input(), now=context.clock())
            return RedirectResponse("/admin/members?notice=invited", status_code=303)
        except (InvalidMemberInput, MemberRuleViolation) as exc:
            error = str(exc)
    members = await member_service.list_members(db, principal.member)
    return render(
        request, "admin_members.html", _list_context(principal, members, error=error, draft=posted), status_code=400
    )


@router.get("/members/{member_id}")
async def edit_page(request: Request, principal: AdminPrincipal, db: Database, member_id: int) -> Response:
    member = await member_service.get_member(db, principal.member, member_id)
    return render(request, "admin_member_edit.html", {"member": member, "roles": list(Role)})


@router.post("/members/{member_id}")
async def update(
    request: Request,
    principal: CSRFChecked,
    context: Context,
    db: Database,
    member_id: int,
    posted: Posted,
) -> Response:
    member = await member_service.get_member(db, principal.member, member_id)
    if member.is_linked and not posted.email:
        posted = replace(posted, email=member.email)  # the field is read-only once linked
    form = posted.validated()
    error = form if isinstance(form, str) else None
    if isinstance(form, MemberForm):
        try:
            await member_service.update_member(db, principal.member, member_id, form.to_input(), now=context.clock())
            return RedirectResponse("/admin/members?notice=updated", status_code=303)
        except (InvalidMemberInput, MemberRuleViolation) as exc:
            error = str(exc)
    return render(
        request, "admin_member_edit.html", {"member": member, "roles": list(Role), "error": error}, status_code=400
    )


@router.post("/members/{member_id}/deactivate")
async def deactivate(
    request: Request, principal: CSRFChecked, context: Context, db: Database, member_id: int
) -> Response:
    return await _set_active(request, principal, context.clock, db, member_id, active=False)


@router.post("/members/{member_id}/reactivate")
async def reactivate(
    request: Request, principal: CSRFChecked, context: Context, db: Database, member_id: int
) -> Response:
    return await _set_active(request, principal, context.clock, db, member_id, active=True)


async def _set_active(
    request: Request, principal: Principal, clock: Clock, db: Database, member_id: int, *, active: bool
) -> Response:
    try:
        member = await member_service.set_member_active(db, principal.member, member_id, active, now=clock())
    except MemberRuleViolation as exc:
        target = await member_service.get_member(db, principal.member, member_id)
        if is_htmx(request):
            return render(
                request,
                "_member_row.html",
                {"m": target, "me": principal.member, "row_error": str(exc)},
                status_code=409,
            )
        members = await member_service.list_members(db, principal.member)
        return render(request, "admin_members.html", _list_context(principal, members, error=str(exc)), status_code=409)
    if is_htmx(request):
        return render(request, "_member_row.html", {"m": member, "me": principal.member})
    return RedirectResponse(f"/admin/members?notice={'reactivated' if active else 'deactivated'}", status_code=303)
