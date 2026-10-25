# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests (#38): inviting them to a call, answering for a guest without Slack, guest records,
and joining from the Slack channel.

``domain.guests`` says what a guest may do; ``gate`` applies it to a call in any use case. Every
change here writes an audit row. Joining is the only thing here that needs no member: it takes
a Slack user that the Slack adapter (or Sign in with Slack) has checked is a real person in the
household's channel and workspace (``SlackPerson``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import AssignmentRepository, EventRepository, InvitationRepository, ResponseRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository, SessionRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import TagKind
from dinnerbell.domain.events import (
    AssignmentRole,
    Event,
    EventRuleViolation,
    EventState,
    RoleOffer,
    Status,
    check_can_respond,
    check_run,
    with_offer,
    with_status,
)
from dinnerbell.domain.guests import (
    GuestAction,
    GuestInput,
    can_answer_for,
    check_can_invite,
    check_dietary_needs,
    check_guest,
    check_guest_input,
)
from dinnerbell.domain.members import (
    Capabilities,
    InvalidMemberInput,
    Member,
    MemberRuleViolation,
    Role,
    normalize_display_name,
    normalize_email,
    normalize_slack_user_id,
)
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import load, locked_event, publish
from dinnerbell.services.common import audit, require_admin
from dinnerbell.services.errors import Forbidden, NotFound

log = logging.getLogger(__name__)

JOIN_SETTING = "join_from_channel"
"""Household setting: whether people in the Dinnerbell channel may join as guests on their own
(#38). On unless an admin turns it off."""
NO_SUCH_GUEST = "No such guest."
WORK_ROLES = (AssignmentRole.COOK, AssignmentRole.PICKUP, AssignmentRole.ORDER)
MANAGE_GUESTS = "Only admins can manage guests."


# The gate --------------------------------------------------------------------------------------


async def gate(db: AsyncSession, event: Event, actor: Member, action: GuestAction) -> None:
    """``domain.guests.check_guest`` for ``actor`` on ``event``, with whether they're invited.
    Members pass without a read."""
    if not actor.is_guest:
        return
    invited = actor.id in await InvitationRepository(db).for_event(event.household_id, event.id)
    check_guest(event, actor, action, invited=invited)


def refuse_guest(actor: Member, action: GuestAction) -> None:
    """For an action that has no call yet, such as starting one or making an offer."""
    check_guest(None, actor, action, invited=False)


# Inviting (#38 rules 1 and 5) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Invited:
    guest: Member
    new: bool
    """A guest record made for this invitation."""


async def _dietary_tag_ids(db: AsyncSession, household_id: int) -> list[int]:
    return [t.id for t in await CatalogRepository(db).tags(household_id) if t.kind is TagKind.DIETARY]


async def invite(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    guest_id: int | None = None,
    new_guest: GuestInput | None = None,
    run: int | None = None,
    now: datetime,
) -> Invited:
    """Invite a saved guest (``guest_id``) or a new one (``new_guest``) to the call. A guest
    without Slack is in at once: inviting them is the inviter's answer for them, and the inviter
    answers and offers roles for them from then on. A guest with Slack is told, and answers
    themselves. Audited as ``event.guest_invited``."""
    if (guest_id is None) == (new_guest is None):
        raise InvalidMemberInput("Pick a saved guest or add a new one, but not both.")
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_run(event, run)
        check_can_invite(event, actor)
        members = MemberRepository(db)
        created = False
        if new_guest is not None:
            clean = check_guest_input(new_guest, dietary_tag_ids=await _dietary_tag_ids(db, actor.household_id))
            guest = await members.add_guest(
                household_id=actor.household_id,
                display_name=clean.display_name,
                dietary_needs=clean.dietary_needs,
                now=now,
            )
            created = True
            payload = {"name": guest.display_name, "dietary_needs": sorted(guest.dietary_needs), "by": "invitation"}
            await audit(db, actor.household_id, actor, "guest.added", f"member:{guest.id}", payload, now)
        else:
            found = await members.get(actor.household_id, guest_id or 0)
            if found is None or not found.is_guest or not found.active:
                raise NotFound(NO_SUCH_GUEST)
            guest = found
        if not await InvitationRepository(db).add(actor.household_id, event.id, guest.id, actor.id, now):
            raise EventRuleViolation(f"{guest.display_name} is invited already.")
        ctx = await load(db, event)
        sent: list[note.Notification] = []
        if guest.is_registered_guest:
            sent.append(note.GuestInvited(cc.event_info(ctx), ctx.person(guest.id), ctx.person(actor.id)))
        else:
            answer = with_status(ctx.response_of(guest.id), guest.id, Status.IN, now)
            await ResponseRepository(db).save(actor.household_id, event.id, answer, now)
            ctx = await load(db, event)
        await publish(db, ctx, cc.call_updated(ctx), *sent, now=now)
        invited: dict[str, object] = {"guest_id": guest.id, "registered": guest.is_registered_guest, "new": created}
        await audit(db, actor.household_id, actor, "event.guest_invited", f"event:{event.id}", invited, now)
        return Invited(guest, created)


async def uninvite(db: AsyncSession, actor: Member, event_id: int, guest_id: int, *, now: datetime) -> None:
    """Take a guest off the call while answers are open: whoever invited them, or an admin. Their
    answer goes with the invitation. Audited as ``event.guest_uninvited``."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_invite(event, actor)
        ctx = await load(db, event)
        if guest_id not in ctx.invitations:
            raise NotFound(NO_SUCH_GUEST)
        if not (actor.is_admin or ctx.invitations[guest_id] == actor.id):
            raise Forbidden("Only whoever invited them, or an admin, can take a guest off the call.")
        await InvitationRepository(db).remove(actor.household_id, event.id, guest_id)
        await _drop_from_call(db, event, guest_id, now)
        await audit(
            db, actor.household_id, actor, "event.guest_uninvited", f"event:{event.id}", {"guest_id": guest_id}, now
        )


async def answer_for(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    guest_id: int,
    *,
    status: Status | None = None,
    role: RoleOffer | None = None,
    offered: bool = True,
    run: int | None = None,
    now: datetime,
) -> None:
    """#38: the member who invited a guest without Slack answers for them, In or Out, and offers
    or withdraws a role for them. An admin may too. Guests answer In or Out, never maybe."""
    if (status is None) == (role is None):
        raise InvalidMemberInput("Pick an answer or a role.")
    if status is Status.MAYBE:
        raise EventRuleViolation("A guest is in or out.")
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_run(event, run)
        ctx = await load(db, event)
        guest = ctx.members.get(guest_id)
        if guest is None or guest_id not in ctx.invitations:
            raise NotFound(NO_SUCH_GUEST)
        if not can_answer_for(guest, actor, ctx.invitations[guest_id]):
            raise Forbidden("Only whoever invited them, or an admin, answers for a guest without Slack.")
        check_can_respond(event, guest, invited=True)  # invited: it's in ctx.invitations
        current = ctx.response_of(guest_id)
        if status is not None:
            answer = with_status(current, guest_id, status, now)
        else:
            changed = with_offer(current, guest_id, role, offered, now) if role is not None else None
            if changed is None:
                return
            answer = changed
        await ResponseRepository(db).save(actor.household_id, event.id, answer, now)
        ctx = await load(db, event)
        await publish(db, ctx, cc.call_updated(ctx), now=now)


async def _drop_from_call(db: AsyncSession, event: Event, guest_id: int, now: datetime) -> None:
    """Take a guest out of a call they no longer belong to: their answer is out, and if they
    were coordinating, nobody is, so a member can claim it. Updates the call's messages."""
    ctx = await load(db, event)
    current = ctx.response_of(guest_id)
    if current is not None and current.status is not Status.OUT:
        out = replace(with_status(current, guest_id, Status.OUT, now), guests=0, guest_needs=frozenset())
        await ResponseRepository(db).save(event.household_id, event.id, out, now)
    if ctx.event.coordinator_id == guest_id:
        await EventRepository(db).save(replace(ctx.event, coordinator_id=None), now)
        await AssignmentRepository(db).unassign(event.household_id, event.id, AssignmentRole.COORDINATOR)
    vacated = tuple(a.role for a in ctx.assignments if a.member_id == guest_id and a.role in WORK_ROLES)
    for role in vacated:
        await AssignmentRepository(db).unassign(event.household_id, event.id, role)
    ctx = await load(db, event)
    told: list[note.Notification] = []
    if vacated:
        # Whoever can reassign it hears: the coordinator, or each admin when nobody coordinates.
        coordinator = ctx.members.get(ctx.event.coordinator_id) if ctx.event.coordinator_id is not None else None
        deciders = (
            [coordinator]
            if coordinator is not None and coordinator.active
            else [m for m in ctx.active_members if m.is_admin]
        )
        info, left = cc.event_info(ctx), ctx.person(guest_id)
        told = [note.RoleVacated(info, ctx.person(m.id), left, vacated) for m in deciders]
    await publish(db, ctx, cc.message_updated(ctx), *cc.decision_updated(ctx), *told, now=now)


# Guest records ---------------------------------------------------------------------------------


async def saved_guests(db: AsyncSession, actor: Member) -> list[Member]:
    """Active guests any member can invite, by name. Guests never see this list."""
    if not actor.is_member:
        raise Forbidden("Only household members can see the guest list.")
    members = await MemberRepository(db).list(actor.household_id)
    return sorted((m for m in members if m.is_guest and m.active), key=lambda m: m.display_name.casefold())


async def dietary_tags(db: AsyncSession, actor: Member) -> list[Any]:
    """The household's dietary tags, for guest forms."""
    if not actor.active:
        raise Forbidden("Sign in again.")
    tags = await CatalogRepository(db).tags(actor.household_id)
    return [t for t in tags if t.kind is TagKind.DIETARY]


async def guests_for_admin(db: AsyncSession, actor: Member) -> list[Member]:
    require_admin(actor, MANAGE_GUESTS)
    members = await MemberRepository(db).list(actor.household_id)
    return [m for m in members if m.is_guest]


async def _guest(db: AsyncSession, actor: Member, guest_id: int) -> Member:
    guest = await MemberRepository(db).get(actor.household_id, guest_id)
    if guest is None or not guest.is_guest:
        raise NotFound(NO_SUCH_GUEST)
    return guest


async def add_guest(db: AsyncSession, actor: Member, data: GuestInput, *, now: datetime) -> Member:
    """An admin adds a guest record on Admin > Members, for members to invite. Audited."""
    require_admin(actor, MANAGE_GUESTS)
    clean = check_guest_input(data, dietary_tag_ids=await _dietary_tag_ids(db, actor.household_id))
    guest = await MemberRepository(db).add_guest(
        household_id=actor.household_id, display_name=clean.display_name, dietary_needs=clean.dietary_needs, now=now
    )
    payload = {"name": guest.display_name, "dietary_needs": sorted(guest.dietary_needs), "by": "admin"}
    await audit(db, actor.household_id, actor, "guest.added", f"member:{guest.id}", payload, now)
    await db.commit()
    return guest


async def update_guest(db: AsyncSession, actor: Member, guest_id: int, data: GuestInput, *, now: datetime) -> Member:
    """An admin renames a guest or changes their dietary needs. Audited."""
    require_admin(actor, MANAGE_GUESTS)
    guest = await _guest(db, actor, guest_id)
    clean = check_guest_input(data, dietary_tag_ids=await _dietary_tag_ids(db, actor.household_id))
    updated = await MemberRepository(db).update(
        actor.household_id, guest.id, now, display_name=clean.display_name, dietary_needs=clean.dietary_needs
    )
    payload = {"name": updated.display_name, "dietary_needs": sorted(updated.dietary_needs)}
    await audit(db, actor.household_id, actor, "guest.updated", f"member:{guest.id}", payload, now)
    await db.commit()
    return updated


async def set_my_needs(db: AsyncSession, actor: Member, needs: frozenset[int], *, now: datetime) -> Member:
    """Someone sets their own dietary needs: hard constraints whenever they're in or maybe. The
    one thing a guest changes about themselves (#38 rule 4); a member, such as a promoted guest,
    sets them on Preferences."""
    if not actor.active:
        raise Forbidden("Sign in again.")
    check_dietary_needs(needs, dietary_tag_ids=await _dietary_tag_ids(db, actor.household_id))
    # The picks of a call that's voting passed everyone's needs as they were (FR-29); a change
    # now would rule them out after the fact, as a member's guests' needs can't change then.
    events = EventRepository(db)
    if needs != actor.dietary_needs:
        for event_id in await events.open_ids(actor.household_id):  # any day, not just today
            event = await events.get(actor.household_id, event_id)
            if event is None or event.state is not EventState.VOTING:
                continue
            responses = await ResponseRepository(db).for_event(actor.household_id, event.id)
            answer = next((r for r in responses if r.member_id == actor.id), None)
            if answer is None or answer.status is Status.OUT:
                continue
            raise EventRuleViolation(
                "A call you're in is voting now, and its picks already fit your needs as they were. "
                "Change them once it's decided."
            )
    updated = await MemberRepository(db).update(actor.household_id, actor.id, now, dietary_needs=needs)
    payload = {"dietary_needs": sorted(needs)}
    await audit(db, actor.household_id, actor, "guest.needs_updated", f"member:{actor.id}", payload, now)
    await db.commit()
    return updated


async def set_guest_slack_user(
    db: AsyncSession, actor: Member, guest_id: int, slack_user_id: str, *, now: datetime
) -> Member:
    """An admin links a guest to a Slack user by hand, or unlinks them (spec 10, "Identity"):
    the hourly email match never links a Slack guest. Locked, like a member's. Audited."""
    require_admin(actor, MANAGE_GUESTS)
    guest = await _guest(db, actor, guest_id)
    value = normalize_slack_user_id(slack_user_id)
    members = MemberRepository(db)
    holder = await members.by_slack_user_id(actor.household_id, value) if value else None
    if holder is not None and holder.id != guest.id:
        raise MemberRuleViolation(f"{holder.display_name} is already linked to that Slack user.")
    changes: dict[str, Any] = {"slack_user_id": value, "slack_link_locked": True}
    if value != guest.slack_user_id:
        # Whoever signed in with the old Slack account isn't this guest any more.
        changes["oidc_subject"] = None
        await SessionRepository(db).delete_for_member(guest.id)
    updated = await members.update(actor.household_id, guest.id, now, **changes)
    action = "member.slack_user_set" if value else "member.slack_user_cleared"
    await audit(
        db, actor.household_id, actor, action, f"member:{guest.id}", {"from": guest.slack_user_id, "to": value}, now
    )
    await db.commit()
    return updated


async def promote(
    db: AsyncSession, actor: Member, guest_id: int, role: Role, *, email: str | None = None, now: datetime
) -> Member:
    """Only an admin makes a guest a member or an admin (#38 addendum). Every member has an
    email, as members sign in by email first (FR-41), so one is required. Audited as
    ``member.promoted``."""
    require_admin(actor, MANAGE_GUESTS)
    if role not in {Role.MEMBER, Role.ADMIN}:
        raise InvalidMemberInput("Make them a member or an admin.")
    guest = await _guest(db, actor, guest_id)
    if not guest.active:
        raise MemberRuleViolation("Reactivate them first, by inviting or adding them again.")
    if not email:
        raise InvalidMemberInput("Enter their email: members sign in with it.")
    clean = normalize_email(email)
    members = MemberRepository(db)
    holder = await members.by_email(actor.household_id, clean)
    if holder is not None and holder.id != guest.id:
        raise MemberRuleViolation("A member with that email already exists.")
    # A guest's dietary needs come along: a need ("must be nut-free") has no exact veto to turn
    # into, so it stays a need, a hard constraint for members too, set on Preferences.
    updated = await members.update(actor.household_id, guest.id, now, role=role, email=clean, slack_email=None)
    payload = {"to": role.value}
    await audit(db, actor.household_id, actor, "member.promoted", f"member:{guest.id}", payload, now)
    await db.commit()
    return updated


class LinkNeedsOverride(MemberRuleViolation):
    """The guest's Slack email isn't the member's, or they use a Slack guest account, so linking
    needs the admin to look at both emails and type the member's name (``link_to_member``).
    Carries both people, for the confirmation step."""

    def __init__(self, guest: Member, target: Member, message: str) -> None:
        super().__init__(message)
        self.guest = guest
        self.target = target


LINK_GONE = "That guest isn't active any more, so there's nothing to link. Reload the page."


def emails_match(guest: Member, target: Member) -> bool:
    """Whether the email Slack shared for ``guest`` is ``target``'s, ignoring case."""
    return bool(guest.slack_email and target.email and guest.slack_email.casefold() == target.email.casefold())


async def link_to_member(
    db: AsyncSession, actor: Member, guest_id: int, member_id: int, *, confirm: str = "", now: datetime
) -> Member:
    """The guest is really a member whose Slack account Dinnerbell couldn't match: move the
    guest's Slack user to the member, locked as a link the admin set (FR-42), and retire the
    guest record. The guest's history stays under the guest; they leave any open call, and are
    signed out. A join from that Slack user then finds the member.

    A display name proves nothing: anyone in the channel can call themselves "Kim". So linking
    is never offered to an admin, who sets their own Slack member ID by hand, and when the
    email Slack shared for the guest isn't the member's, or the guest uses a Slack guest
    account, the admin must type the member's name (``confirm``) after seeing both emails
    (``LinkNeedsOverride``). Audited as
    ``guest.linked_to_member``, with ``override`` true for such a link."""
    require_admin(actor, MANAGE_GUESTS)
    async with write_transaction(db):  # a second link of the same guest reads the first's result
        guest = await _guest(db, actor, guest_id)
        if not guest.active or guest.join_refused:
            raise MemberRuleViolation(LINK_GONE)
        members = MemberRepository(db)
        target = await members.get(actor.household_id, member_id)
        if target is None or not target.is_member:
            raise NotFound("No such member.")
        if target.is_admin:
            raise MemberRuleViolation(
                "A guest can't be linked to an admin. An admin sets their own Slack member ID on their row."
            )
        if guest.slack_user_id is None:
            raise MemberRuleViolation(f"{guest.display_name} has no Slack account to link.")
        if target.slack_user_id is not None:
            raise MemberRuleViolation(f"{target.display_name} is linked to a Slack account already.")
        # A Slack guest account is never linked on its email alone, as joining and the hourly
        # match never link one (``_linkable``).
        override = guest.slack_guest_account or not emails_match(guest, target)
        if override and not confirm:
            why = (
                "uses a Slack guest account"
                if emails_match(guest, target)
                else "has a Slack email that isn't the member's"
            )
            raise LinkNeedsOverride(guest, target, f"{guest.display_name} {why}. Check both before linking.")
        if override and confirm.strip().casefold() != target.display_name.casefold():
            raise LinkNeedsOverride(guest, target, f"Type {target.display_name} exactly to link anyway.")
        slack_id = guest.slack_user_id
        try:
            await members.update(actor.household_id, guest.id, now, slack_user_id=None, oidc_subject=None, active=False)
            await SessionRepository(db).delete_for_member(guest.id)
            linked = await members.update(
                actor.household_id, target.id, now, slack_user_id=slack_id, slack_link_locked=True
            )
        except IntegrityError:
            # Another link took this Slack account first: answer as a replay would.
            await db.rollback()
            raise MemberRuleViolation(LINK_GONE) from None
        await _leave_open_calls(db, actor, guest, now)
        payload = {"guest_id": guest.id, "slack_user_id": slack_id, "override": override}
        await audit(db, actor.household_id, actor, "guest.linked_to_member", f"member:{target.id}", payload, now)
        await db.commit()
    return linked


async def _leave_open_calls(db: AsyncSession, actor: Member, guest: Member, now: datetime) -> None:
    events = EventRepository(db)
    for event_id in await invited_events(db, guest):
        event = await events.get(actor.household_id, event_id)
        if event is not None and event.state in {EventState.COLLECTING, EventState.VOTING, EventState.DECIDED}:
            await events.lock(actor.household_id, event_id, now)
            await _drop_from_call(db, event, guest.id, now)


async def unlinked_members(db: AsyncSession, actor: Member) -> list[Member]:
    """Who a guest might really be: active members, not admins, with no Slack account yet."""
    require_admin(actor, MANAGE_GUESTS)
    return [m for m in await MemberRepository(db).list(actor.household_id) if _link_target(m)]


def _link_target(member: Member) -> bool:
    """Whether a guest may be linked to ``member`` (``link_to_member``): an active member with
    no Slack account. Never an admin: a guest linked to an admin would act as that admin in
    Slack, so admins set their own Slack member ID by hand on their row."""
    return member.is_member and not member.is_admin and member.slack_user_id is None


async def remove(db: AsyncSession, actor: Member, guest_id: int, *, now: datetime) -> Member:
    """An admin removes a guest: they're deactivated, signed out, and refused if they try to
    join from the channel again (#38 addendum). Their history stays. Audited."""
    require_admin(actor, MANAGE_GUESTS)
    guest = await _guest(db, actor, guest_id)
    updated = await MemberRepository(db).update(actor.household_id, guest.id, now, active=False, join_refused=True)
    await SessionRepository(db).delete_for_member(guest.id)
    await _leave_open_calls(db, actor, guest, now)
    await audit(db, actor.household_id, actor, "guest.removed", f"member:{guest.id}", {}, now)
    await db.commit()
    return updated


# Joining from the channel (#38 addendum, options A to C) ---------------------------------------


@dataclass(frozen=True, slots=True)
class SlackPerson:
    """A Slack user the adapter has checked is a real person in the household's workspace and
    in its channel right now: not a bot, not deactivated, not from another organization (Slack
    Connect). ``full_member`` is False for a Slack guest account. ``email`` is for matching an
    invited member only; never log it."""

    slack_user_id: str
    name: str
    email: str | None = None
    full_member: bool = True


class NotJoinable(StrEnum):
    """Why someone can't join. The values are audit reasons; ``JOIN_REFUSALS`` has the words."""

    OFF = "off"
    REMOVED = "removed"
    ASK_ADMIN = "ask_admin"
    """Their email belongs to someone the admin must link by hand, or Slack couldn't list the
    whole channel."""
    NOT_IN_CHANNEL = "not_in_channel"
    NOT_A_PERSON = "not_a_person"
    TOO_MANY = "rate_limited"
    UNAVAILABLE = "unavailable"


JOIN_REFUSALS = {
    NotJoinable.OFF: "Joining from the channel is turned off. Ask the admin to add you.",
    NotJoinable.REMOVED: "The admin removed you from Dinnerbell. Ask them if that's a mistake.",
    NotJoinable.ASK_ADMIN: "Ask the admin to add you. They've been told you tried.",
    NotJoinable.NOT_IN_CHANNEL: "Join the Dinnerbell channel first, then try again.",
    NotJoinable.NOT_A_PERSON: "Only people in this workspace can join Dinnerbell.",
    NotJoinable.TOO_MANY: "That's a lot of tries. Wait a few minutes, then try again.",
    NotJoinable.UNAVAILABLE: "Dinnerbell can't check with Slack right now. Try again in a minute.",
}


class JoinRefused(Exception):
    def __init__(self, reason: NotJoinable) -> None:
        super().__init__(JOIN_REFUSALS[reason])
        self.reason = reason


class SlackPeople(Protocol):
    """What the Slack adapter knows about people, for joining from the web (option B) and for
    importing the channel (option C). Implemented by the Slack adapter and wired in by the
    composition root; None when Slack isn't connected."""

    async def person(self, slack_user_id: str) -> SlackPerson | NotJoinable: ...

    async def channel_people(self) -> list[SlackPerson]: ...


class JoinedAs(StrEnum):
    GUEST = "guest"
    MEMBER = "member"
    """An invited member's own Slack account, matched by email: linked, as the hourly match would."""
    ALREADY = "already"


@dataclass(frozen=True, slots=True)
class Joined:
    member: Member
    joined_as: JoinedAs


def join_allowed(settings: dict[str, Any]) -> bool:
    value = settings.get(JOIN_SETTING, True)
    return value if isinstance(value, bool) else True


async def joining_on(db: AsyncSession, household_id: int) -> bool:
    return join_allowed(await household_settings(db, household_id))


async def joining_open(db: AsyncSession) -> bool:
    """Whether the household lets people join from the channel, for someone Dinnerbell doesn't
    know yet (so there's no member to ask for the household)."""
    household = await HouseholdRepository(db).first()
    return household is not None and await joining_on(db, household.id)


JOIN_ATTEMPTS = 5
JOIN_WINDOW = timedelta(minutes=10)


class JoinLimiter:
    """At most ``JOIN_ATTEMPTS`` join attempts per Slack user in ``JOIN_WINDOW`` (#38), so a
    script or a stuck button can't hammer Slack's API or the admins' direct messages. One per
    process: the Slack adapter and the web app each keep theirs."""

    def __init__(self, attempts: int = JOIN_ATTEMPTS, window: timedelta = JOIN_WINDOW) -> None:
        self.attempts = attempts
        self.window = window
        self.seen: dict[str, list[datetime]] = {}
        self.told: set[str] = set()
        """Slack users refused in their current window: only the first refusal is audited and
        answered, so a stuck button can't fill the audit log."""

    def first_refusal(self, slack_user_id: str) -> bool:
        """After ``allow`` said no: whether this is the first refusal in the window."""
        if slack_user_id in self.told:
            return False
        self.told.add(slack_user_id)
        return True

    def allow(self, slack_user_id: str, now: datetime) -> bool:
        recent = [t for t in self.seen.get(slack_user_id, []) if now - t < self.window]
        if len(recent) >= self.attempts:
            self.seen[slack_user_id] = recent
            return False
        self.told.discard(slack_user_id)
        self.seen[slack_user_id] = [*recent, now]
        # Forget people with nothing recent, so the map can't grow without end.
        for user in [u for u, times in self.seen.items() if not any(now - t < self.window for t in times)]:
            del self.seen[user]
            self.told.discard(user)
        return True


async def set_joining(db: AsyncSession, actor: Member, on: bool, *, now: datetime) -> None:
    """The admin's **Join from the channel** setting, on by default. Audited."""
    require_admin(actor, MANAGE_GUESTS)
    async with write_transaction(db):
        settings = await household_settings(db, actor.household_id)
        if join_allowed(settings) == on:
            return
        await HouseholdRepository(db).update_settings(actor.household_id, settings | {JOIN_SETTING: on})
        await audit(
            db,
            actor.household_id,
            actor,
            "household.joining_updated",
            f"household:{actor.household_id}",
            {"on": on},
            now,
        )
        await db.commit()


async def join(db: AsyncSession, person: SlackPerson, *, via: str, now: datetime) -> Joined:
    """``person`` joins on their own (options A and B): they become a guest linked to their
    Slack user, and each active admin gets a direct message with **Make member** and **Remove**.
    Refused while joining is off, or for someone an admin removed. Someone already linked is
    told so. A full member of the workspace whose email matches an invited member's is linked
    to that member instead, as the hourly email match would. Audited as ``member.joined``, or
    ``member.join_refused`` with the reason. ``via`` says where: ``slack`` or ``web``."""
    household = await HouseholdRepository(db).first()
    if household is None:  # pragma: no cover - bootstrap runs before anything else
        raise NotFound("No household.")
    async with write_transaction(db):
        members = MemberRepository(db)
        existing = await members.by_slack_user_id(household.id, person.slack_user_id)
        if existing is not None and existing.join_refused:
            await _refused(db, household.id, person, NotJoinable.REMOVED, via, now)
            raise JoinRefused(NotJoinable.REMOVED)
        if existing is not None:
            return Joined(existing, JoinedAs.ALREADY)
        if not join_allowed(await household_settings(db, household.id)):
            await _refused(db, household.id, person, NotJoinable.OFF, via, now)
            raise JoinRefused(NotJoinable.OFF)
        email = _email_of(person)
        holder = await members.by_email(household.id, email) if email else None
        if holder is None and email:  # another guest who joined with this Slack email
            holder = await members.guest_by_slack_email(household.id, email)
        if holder is not None and not holder.active:
            await _refused(db, household.id, person, NotJoinable.REMOVED, via, now)
            raise JoinRefused(NotJoinable.REMOVED)
        if holder is not None and _linkable(holder, person):
            linked = await members.update(household.id, holder.id, now, slack_user_id=person.slack_user_id)
            payload = {"slack_user_id": person.slack_user_id, "matched_by": "email", "via": via}
            await audit(db, household.id, None, "member.slack_linked", f"member:{linked.id}", payload, now)
            await db.commit()
            return Joined(linked, JoinedAs.MEMBER)
        if holder is not None:
            # Their email belongs to someone Dinnerbell knows, whom this Slack account can't be
            # linked to on its own: a member linked to another Slack account, a Slack guest
            # account, or a guest. A guest record here would only stand in their way (#38).
            await ask_admins(db, household.id, holder, person, "join", now)
            await _refused(db, household.id, person, NotJoinable.ASK_ADMIN, via, now)
            raise JoinRefused(NotJoinable.ASK_ADMIN)
        try:
            name = normalize_display_name(person.name)
        except InvalidMemberInput:
            name = "Guest"
        guest = await members.add_guest(
            household_id=household.id,
            display_name=name,
            dietary_needs=frozenset(),
            slack_user_id=person.slack_user_id,
            slack_guest_account=not person.full_member,
            now=now,
        )
        if email is not None:
            # Kept apart from ``email``, which members sign in with, so the admin can't add a
            # member with it without seeing this guest first (``members.invite_member``).
            guest = await members.update(household.id, guest.id, now, slack_email=email)
        joined: dict[str, object] = {"slack_user_id": person.slack_user_id, "via": via}
        joined["slack_guest"] = not person.full_member
        await audit(db, household.id, None, "member.joined", f"member:{guest.id}", joined, now)
        everyone = await members.list(household.id)
        admins = [m for m in everyone if m.is_member and m.is_admin]
        # Someone invited by email whose Slack email differs joins as a guest; the admin can
        # link them to their membership instead (``link_to_member``), never to an admin's.
        unlinked = tuple(note.PersonInfo(m.id, m.display_name) for m in everyone if _link_target(m))
        joined_guest = note.PersonInfo(guest.id, guest.display_name)
        outbox_now = [
            note.GuestJoined(
                household.id,
                note.PersonInfo(a.id, a.display_name),
                joined_guest,
                via,
                unlinked,
                slack_user_id=person.slack_user_id,
                slack_email=email,
                slack_guest_account=not person.full_member,
            )
            for a in admins
        ]
        for dm in outbox_now:
            await OutboxRepository(db).enqueue(household.id, None, dm, now)
        await db.commit()
    log.info("guest joined", extra={"member_id": guest.id})
    return Joined(guest, JoinedAs.GUEST)


def _email_of(person: SlackPerson) -> str | None:
    """The email Slack shares for ``person``, normalized, or None."""
    if not person.email:
        return None
    try:
        return normalize_email(person.email)
    except InvalidMemberInput:
        return None


def _linkable(holder: Member, person: SlackPerson) -> bool:
    """Whether ``person``'s Slack account links to ``holder``, the member with their email, as
    the hourly email match would: an active member, not linked yet or locked by hand, and a full
    member of the workspace."""
    return holder.is_member and person.full_member and not holder.slack_user_id and not holder.slack_link_locked


async def ask_admins(
    db: AsyncSession, household_id: int, member: Member, person: SlackPerson, reason: str, now: datetime
) -> None:
    """Tell each active admin that ``person``'s Slack account needs a hand link (#38)."""
    admins = [m for m in await MemberRepository(db).list(household_id) if m.is_member and m.is_admin]
    for admin in admins:
        notice = note.LinkNeeded(
            household_id,
            note.PersonInfo(admin.id, admin.display_name),
            note.PersonInfo(member.id, member.display_name),
            person.slack_user_id,
            person.name,
            reason,
            slack_email=member.email or member.slack_email,
            member_is_admin=member.is_admin,
        )
        await OutboxRepository(db).enqueue(household_id, None, notice, now)


async def _refused(
    db: AsyncSession, household_id: int, person: SlackPerson, reason: NotJoinable, via: str, now: datetime
) -> None:
    payload = {"slack_user_id": person.slack_user_id, "reason": reason.value, "via": via}
    await audit(db, household_id, None, "member.join_refused", f"slack:{person.slack_user_id}", payload, now)
    await db.commit()


async def record_refusal(db: AsyncSession, slack_user_id: str, reason: NotJoinable, *, via: str, now: datetime) -> None:
    """Audit a join the adapter refused before it reached ``join``, such as someone outside
    the channel or past the rate limit."""
    household = await HouseholdRepository(db).first()
    if household is None:  # pragma: no cover - bootstrap runs before anything else
        return
    await _refused(db, household.id, SlackPerson(slack_user_id, ""), reason, via, now)


@dataclass(frozen=True, slots=True)
class Candidate:
    """Someone in the channel who isn't in Dinnerbell, for the admin's import (option C)."""

    slack_user_id: str
    name: str
    full_member: bool
    has_email: bool


async def candidates(db: AsyncSession, actor: Member, people: list[SlackPerson]) -> list[Candidate]:
    """``people`` in the channel minus everyone Dinnerbell already knows by Slack user."""
    require_admin(actor, MANAGE_GUESTS)
    known = {m.slack_user_id for m in await MemberRepository(db).list(actor.household_id) if m.slack_user_id}
    return [
        Candidate(p.slack_user_id, p.name, p.full_member, bool(p.email))
        for p in sorted(people, key=lambda p: p.name.casefold())
        if p.slack_user_id not in known
    ]


async def import_person(db: AsyncSession, actor: Member, person: SlackPerson, role: Role, *, now: datetime) -> Member:
    """Option C: the admin adds someone from the channel as a guest or a member, by their choice.
    A member needs the person's email from Slack, as every member signs in by email first.
    Audited as ``member.imported``."""
    require_admin(actor, MANAGE_GUESTS)
    members = MemberRepository(db)
    if await members.by_slack_user_id(actor.household_id, person.slack_user_id) is not None:
        raise MemberRuleViolation("That person is in Dinnerbell already.")
    try:
        name = normalize_display_name(person.name)
    except InvalidMemberInput:
        name = "Guest"
    if role is Role.GUEST:
        added = await members.add_guest(
            household_id=actor.household_id,
            display_name=name,
            dietary_needs=frozenset(),
            slack_user_id=person.slack_user_id,
            slack_guest_account=not person.full_member,
            now=now,
        )
    elif role is Role.MEMBER and person.email and person.full_member:
        email = normalize_email(person.email)
        if await members.by_email(actor.household_id, email) is not None:
            raise MemberRuleViolation("A member with that email already exists. Link them on their own page.")
        added = await members.add(
            household_id=actor.household_id,
            display_name=name,
            email=email,
            role=Role.MEMBER,
            capabilities=Capabilities(),
            now=now,
        )
        added = await members.update(actor.household_id, added.id, now, slack_user_id=person.slack_user_id)
    elif role is Role.MEMBER:
        raise MemberRuleViolation(
            "Slack doesn't share this person's email, or they're a Slack guest, so add them as a guest."
        )
    else:
        raise InvalidMemberInput("Add them as a guest or a member.")
    payload = {"slack_user_id": person.slack_user_id, "role": role.value}
    await audit(db, actor.household_id, actor, "member.imported", f"member:{added.id}", payload, now)
    await db.commit()
    return added


async def invited_events(db: AsyncSession, actor: Member) -> list[int]:
    """The calls a guest is invited to."""
    return await InvitationRepository(db).events_for(actor.household_id, actor.id)


async def check_event_visible(db: AsyncSession, actor: Member, event_id: int) -> None:
    """A guest sees only the calls they're invited to (#38 rule 4); anything else is not found,
    so a guest can't even learn that an event exists."""
    if not actor.is_guest:
        return
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None or not actor.active or event_id not in await invited_events(db, actor):
        raise NotFound(cc.NO_SUCH_CALL)
