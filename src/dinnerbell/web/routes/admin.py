# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Members: invite, edit, deactivate, reactivate, and link Slack users (FR-42); guests,
joining from the channel, and adding people from the channel (#38)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.members import Capabilities, InvalidMemberInput, Member, MemberRuleViolation, Role
from dinnerbell.services import guests as guest_service
from dinnerbell.services import members as member_service
from dinnerbell.services.guests import SlackPerson
from dinnerbell.web.auth import AdminCSRFChecked, AdminPrincipal, Context, Database, Principal
from dinnerbell.web.context import AppContext
from dinnerbell.web.forms import RowId, parse_id
from dinnerbell.web.render import is_htmx, render

router = APIRouter(prefix="/admin")

NOTICES = {
    "invited": "Invitation saved. They can sign in now.",
    "updated": "Member updated.",
    "deactivated": "Member deactivated and signed out everywhere.",
    "reactivated": "Member reactivated.",
    "slack_set": "Slack link saved.",
    "slack_cleared": "Slack link removed.",
    "slack_automatic": "Dinnerbell will link this member automatically again.",
}


FIELD_LIMIT = 320
"""Upper bound on any posted text field. The domain enforces the real limits (SEC-4)."""

Text = Annotated[str, Form(max_length=FIELD_LIMIT)]
Checkbox = Annotated[str | None, Form(max_length=FIELD_LIMIT)]


MEMBER_ROLES = [Role.ADMIN, Role.MEMBER]
"""Roles the member forms offer. Guests come from invitations and joining (#38)."""


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
        if role not in MEMBER_ROLES:  # a guest is added by an invitation or by joining, never here
            raise InvalidMemberInput("Choose a role from the list.")
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
        member.display_name, member.email or "", member.role.value, caps.can_cook, caps.can_pickup, caps.can_order
    )


async def _members_page(
    request: Request, principal: Principal, context: AppContext, db: Database, *, status_code: int = 200, **extra: Any
) -> Response:
    """Admin > Members: members and admins, then guests (#38), with the joining setting."""
    everyone = await member_service.list_members(db, principal.member)
    page = {
        "members": [m for m in everyone if not m.is_guest],
        "guests": [m for m in everyone if m.is_guest],
        "roles": MEMBER_ROLES,
        "me": principal.member,
        "joining": await guest_service.joining_on(db, principal.member.household_id),
        "slack_connected": context.slack_people is not None,
        "dietary_tags": await guest_service.dietary_tags(db, principal.member),
    }
    return await render(request, "admin_members.html", page | extra, status_code=status_code)


@router.get("/members")
async def members_page(
    request: Request, principal: AdminPrincipal, context: Context, db: Database, notice: str | None = None
) -> Response:
    return await _members_page(request, principal, context, db, notice=NOTICES.get(notice or ""))


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
    return await _members_page(request, principal, context, db, status_code=400, error=error, draft=posted)


@router.get("/members/{member_id}")
async def edit_page(request: Request, principal: AdminPrincipal, db: Database, member_id: RowId) -> Response:
    member = await member_service.get_member(db, principal.member, member_id)
    context = {"member": member, "values": _stored(member), "roles": MEMBER_ROLES}
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
        posted = replace(posted, email=member.email or "")  # the field is read-only once linked
    try:
        await member_service.update_member(db, principal.member, member_id, posted.to_input(), now=context.clock())
        return RedirectResponse("/admin/members?notice=updated", status_code=303)
    except (InvalidMemberInput, MemberRuleViolation) as exc:
        error = str(exc)
    # Redisplay what the admin typed, not the stored values, so nothing they changed is lost.
    page = {"member": member, "values": posted, "roles": MEMBER_ROLES, "error": error}
    return await render(request, "admin_member_edit.html", page, status_code=400)


@router.post("/members/{member_id}/slack")
async def set_slack(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    member_id: RowId,
    slack_user_id: Text = "",
) -> Response:
    """FR-42: link a member to a Slack user ID, or unlink them with a blank value."""
    try:
        updated = await member_service.set_slack_user(
            db, principal.member, member_id, slack_user_id, now=context.clock()
        )
    except (InvalidMemberInput, MemberRuleViolation) as exc:
        member = await member_service.get_member(db, principal.member, member_id)
        page = {"member": member, "values": _stored(member), "roles": MEMBER_ROLES, "slack_error": str(exc)}
        page["slack_value"] = slack_user_id
        return await render(request, "admin_member_edit.html", page, status_code=400)
    notice = "slack_set" if updated.slack_user_id else "slack_cleared"
    return RedirectResponse(f"/admin/members?notice={notice}", status_code=303)


@router.post("/members/{member_id}/slack/automatic")
async def slack_automatic(principal: AdminCSRFChecked, context: Context, db: Database, member_id: RowId) -> Response:
    """FR-42: unlock a Slack link the admin set or cleared, so sign-in and email matching apply."""
    await member_service.unlock_slack_link(db, principal.member, member_id, now=context.clock())
    return RedirectResponse("/admin/members?notice=slack_automatic", status_code=303)


@router.post("/members/{member_id}/deactivate")
async def deactivate(
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, member_id: RowId
) -> Response:
    return await _set_active(request, principal, context, db, member_id, active=False)


@router.post("/members/{member_id}/reactivate")
async def reactivate(
    request: Request, principal: AdminCSRFChecked, context: Context, db: Database, member_id: RowId
) -> Response:
    return await _set_active(request, principal, context, db, member_id, active=True)


async def _set_active(
    request: Request, principal: Principal, context: AppContext, db: Database, member_id: RowId, *, active: bool
) -> Response:
    try:
        member = await member_service.set_member_active(db, principal.member, member_id, active, now=context.clock())
    except MemberRuleViolation as exc:
        target = await member_service.get_member(db, principal.member, member_id)
        if is_htmx(request):
            return await render(
                request,
                "_member_row.html",
                {"m": target, "me": principal.member, "row_error": str(exc)},
                status_code=409,
            )
        return await _members_page(request, principal, context, db, status_code=409, error=str(exc))
    if is_htmx(request):
        return await render(request, "_member_row.html", {"m": member, "me": principal.member})
    return RedirectResponse(f"/admin/members?notice={'reactivated' if active else 'deactivated'}", status_code=303)


# Guests (#38) ------------------------------------------------------------------------------------

GuestNeeds = Annotated[list[str] | None, Form()]


def _needs(values: list[str] | None) -> frozenset[int]:
    return frozenset(i for v in values or [] if (i := parse_id(v)) is not None)


GUEST_NOTICES = {
    "joining_on": "People in the Dinnerbell channel can join as guests.",
    "joining_off": "Joining from the channel is off. Add people here instead.",
    "guest_added": "Guest added. Members can invite them to calls.",
    "guest_updated": "Guest updated.",
    "guest_promoted": "They're a member now.",
    "guest_removed": "Guest removed. They can't join from the channel again unless you add them back.",
    "guest_linked": "Linked. Their Slack account belongs to the member now, and the guest record is retired.",
    "imported": "Added from the channel.",
}
NOTICES.update(GUEST_NOTICES)
GUEST_ERRORS = (InvalidMemberInput, MemberRuleViolation)


async def _guest_error(
    request: Request, principal: Principal, context: AppContext, db: Database, exc: Exception
) -> Response:
    return await _members_page(request, principal, context, db, status_code=400, error=str(exc))


@router.post("/joining")
async def joining(principal: AdminCSRFChecked, context: Context, db: Database, on: Text = "") -> Response:
    """The **Join from the channel** setting (#38), on by default."""
    await guest_service.set_joining(db, principal.member, on == "1", now=context.clock())
    return RedirectResponse(f"/admin/members?notice=joining_{'on' if on == '1' else 'off'}", status_code=303)


@router.post("/guests")
async def add_guest(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    display_name: Text = "",
    dietary_needs: GuestNeeds = None,
) -> Response:
    try:
        data = GuestInput(display_name, _needs(dietary_needs))
        await guest_service.add_guest(db, principal.member, data, now=context.clock())
    except GUEST_ERRORS as exc:
        return await _guest_error(request, principal, context, db, exc)
    return RedirectResponse("/admin/members?notice=guest_added", status_code=303)


@router.post("/guests/{guest_id}")
async def update_guest(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    guest_id: RowId,
    display_name: Text = "",
    dietary_needs: GuestNeeds = None,
) -> Response:
    try:
        data = GuestInput(display_name, _needs(dietary_needs))
        await guest_service.update_guest(db, principal.member, guest_id, data, now=context.clock())
    except GUEST_ERRORS as exc:
        return await _guest_error(request, principal, context, db, exc)
    return RedirectResponse("/admin/members?notice=guest_updated", status_code=303)


@router.post("/guests/{guest_id}/slack")
async def guest_slack(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    guest_id: RowId,
    slack_user_id: Text = "",
) -> Response:
    """Link a Slack guest account by hand: the email match never links one (spec 10)."""
    try:
        await guest_service.set_guest_slack_user(db, principal.member, guest_id, slack_user_id, now=context.clock())
    except GUEST_ERRORS as exc:
        return await _guest_error(request, principal, context, db, exc)
    return RedirectResponse("/admin/members?notice=guest_updated", status_code=303)


@router.post("/guests/{guest_id}/promote")
async def promote_guest(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    guest_id: RowId,
    role: Text = Role.MEMBER.value,
    email: Text = "",
) -> Response:
    """Only an admin makes a guest a member or an admin (#38)."""
    try:
        chosen = Role(role) if role in {r.value for r in MEMBER_ROLES} else None
        if chosen is None:
            raise InvalidMemberInput("Choose a role from the list.")
        await guest_service.promote(db, principal.member, guest_id, chosen, email=email or None, now=context.clock())
    except GUEST_ERRORS as exc:
        return await _guest_error(request, principal, context, db, exc)
    return RedirectResponse("/admin/members?notice=guest_promoted", status_code=303)


@router.post("/guests/{guest_id}/link")
async def link_guest(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    guest_id: RowId,
    member_id: Text = "",
) -> Response:
    """The guest is really a member whose Slack account wasn't matched: move the link (#38)."""
    try:
        target = parse_id(member_id)
        if target is None:
            raise InvalidMemberInput("Pick the member they are.")
        await guest_service.link_to_member(db, principal.member, guest_id, target, now=context.clock())
    except GUEST_ERRORS as exc:
        return await _guest_error(request, principal, context, db, exc)
    return RedirectResponse("/admin/members?notice=guest_linked", status_code=303)


@router.post("/guests/{guest_id}/remove")
async def remove_guest(principal: AdminCSRFChecked, context: Context, db: Database, guest_id: RowId) -> Response:
    await guest_service.remove(db, principal.member, guest_id, now=context.clock())
    return RedirectResponse("/admin/members?notice=guest_removed", status_code=303)


@router.get("/channel")
async def channel_page(request: Request, principal: AdminPrincipal, context: Context, db: Database) -> Response:
    """Option C: people in the Dinnerbell channel who aren't in Dinnerbell yet."""
    people = await context.slack_people.channel_people() if context.slack_people is not None else []
    found = await guest_service.candidates(db, principal.member, people)
    page = {"candidates": found, "slack_connected": context.slack_people is not None}
    return await render(request, "admin_channel.html", page)


@router.post("/channel")
async def import_from_channel(
    request: Request,
    principal: AdminCSRFChecked,
    context: Context,
    db: Database,
    slack_user_id: Text = "",
    role: Text = Role.GUEST.value,
) -> Response:
    """Add someone from the channel as a guest or a member. Slack is asked again about the
    person, so a posted ID can't add anyone who isn't in the channel."""
    try:
        chosen = Role(role) if role in {Role.GUEST.value, Role.MEMBER.value} else None
        if chosen is None:
            raise InvalidMemberInput("Add them as a guest or a member.")
        person = await context.slack_people.person(slack_user_id) if context.slack_people is not None else None
        if not isinstance(person, SlackPerson):
            raise MemberRuleViolation("That person isn't in the Dinnerbell channel, or Slack didn't answer.")
        await guest_service.import_person(db, principal.member, person, chosen, now=context.clock())
    except GUEST_ERRORS as exc:
        return await _guest_error(request, principal, context, db, exc)
    return RedirectResponse("/admin/members?notice=imported", status_code=303)
