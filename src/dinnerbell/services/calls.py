# SPDX-License-Identifier: AGPL-3.0-or-later
"""What members do with a call: start one, respond, coordinate, propose, vote, lock, mark done,
call it off, and close a window now (spec 5.1, FR-3, FR-7 to FR-21, FR-56).

Every function takes the acting member and the event ID, and looks the event up only inside the
actor's household, so another household's ID is simply not found. Members change only their own
response; no function takes another member's response.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.config import is_test_instance
from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import (
    AssignmentRepository,
    EventRepository,
    InvitationRepository,
    ProposalRepository,
    ResponseRepository,
    SuggestionRepository,
    VoteRepository,
)
from dinnerbell.db.meals import household_settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import (
    CatalogItem,
    InvalidCatalogInput,
    ItemKind,
    Mode,
    TagKind,
    normalize_item_name,
)
from dinnerbell.domain.events import (
    MODE_ROLE,
    NOT_IN_JOIN_LATE,
    ActingAs,
    AssignmentRole,
    Details,
    Event,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    JoinLateFirst,
    Proposal,
    ProposalSource,
    Response,
    RoleOffer,
    Status,
    Window,
    can_do,
    check_ad_hoc_day,
    check_can_adjust,
    check_can_cancel,
    check_can_close_window,
    check_can_join_late,
    check_can_lock,
    check_can_manage_decision,
    check_can_mark_done,
    check_can_offer_role,
    check_can_post_now,
    check_can_propose,
    check_can_respond,
    check_can_switch_late,
    check_can_vote,
    check_claim_coordinator,
    check_coordinator_in,
    check_details,
    check_expected_window,
    check_handoff,
    check_in_to_add,
    check_proposal_room,
    check_run,
    check_skip_day,
    clean_cancel_note,
    drops_coordinating,
    friendly,
    is_coordinator,
    not_on_cook_list,
    transition,
    with_coordinating,
    with_details,
    with_late_join,
    with_late_switches,
    with_offer,
    with_status,
)
from dinnerbell.domain.guests import GuestAction, answer_action
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import CALL_OFF_SUPERSEDES, SUPERSEDED_BY_CALL_OFF
from dinnerbell.domain.schedule import slot_schedule
from dinnerbell.services import activity, contributions, lifecycle, picks
from dinnerbell.services import call_context as cc
from dinnerbell.services import guests as guest_rules
from dinnerbell.services.call_context import CallContext, load, locked_event, publish
from dinnerbell.services.common import audit, is_unique_violation, require_admin
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import group_preferences
from dinnerbell.services.variety import household_today

NO_SUCH_PROPOSAL = "No such proposal."
NO_SUCH_MEMBER = "No such member."
NOT_LATE = "They answered on time, so there's nothing to switch."


def _target(event: Event) -> str:
    return f"event:{event.id}"


# Starting a call (FR-3) -----------------------------------------------------------------------


async def start_call(
    db: AsyncSession, actor: Member, slot: Slot, day: date, *, now: datetime, instance_label: str = ""
) -> Event:
    """An ad-hoc call. For today it opens now; for a later day, at the slot's usual time.
    ``instance_label`` is ``DINNERBELL_INSTANCE_LABEL``: on a test instance (#60), a called-off
    call starts again even after the usual meal time plus two hours."""
    restart_any_time = is_test_instance(instance_label)
    if not actor.active:
        raise EventRuleViolation("Only active members can start a call.", code="inactive")
    guest_rules.refuse_guest(actor, GuestAction.START)
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    today = household_today(household, now)
    check_ad_hoc_day(day, today)
    schedule = slot_schedule(await household_settings(db, household.id), slot)
    opens_at = now if day == today else schedule.opens_at(day, ZoneInfo(household.timezone))
    try:
        # Under the write lock, so a cooking offer posted at the same moment (FR-5) either sees
        # this call or keeps it from starting.
        async with write_transaction(db):
            event = await lifecycle.create_call(
                db, household, slot, day, opens_at=opens_at, actor=actor, now=now, restart_any_time=restart_any_time
            )
            activity.action(db, "start", event, actor, started=True)
            await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return event


async def restart_call(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    run: int | None = None,
    now: datetime,
    instance_label: str = "",
) -> Event:
    """Start the called-off call ``event_id`` again (FR-56), as ``start_call`` does for its slot
    and day. ``run``, when given, is the run the actor saw (``check_run``): a page left open
    across a restart and a second call-off doesn't start the newer run again. On a test instance
    (``instance_label`` set, #60) the too-late rule doesn't apply."""
    if not actor.active:
        raise EventRuleViolation("Only active members can start a call.", code="inactive")
    guest_rules.refuse_guest(actor, GuestAction.RESTART)
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    check_ad_hoc_day(event.day, household_today(household, now))
    try:
        any_time = is_test_instance(instance_label)
        async with write_transaction(db):
            fresh = await lifecycle.restart(db, event, actor=actor, run=run, now=now, any_time=any_time)
            activity.action(db, "restart", fresh, actor, before=EventState.CANCELLED)
            await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return fresh


async def post_now(db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime) -> Event:
    """Post the scheduled call ``event_id`` now instead of at its usual time (issue #59), such
    as tomorrow's lunch the evening before. Anyone who could start the call may
    (``check_can_post_now``). Responses open at once; the response deadline, the reminder, and
    voting keep that day's times. It takes the same path as the scheduler's tick
    (``lifecycle.open_call``) under the event's lock, so it races the tick safely: whichever
    comes second finds the call posted and changes nothing, as does posting it twice. Audited
    as ``event.opened`` by the actor, with when it was due. ``run``, when given, is the run the
    actor saw (``check_run``). Returns the call as it is now."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.POST_NOW)
        check_run(event, run)
        if check_can_post_now(event, actor, now=now):
            opened = await lifecycle.open_call(db, event, now, actor=actor)
            activity.action(db, "post_now", event, actor, after=opened.state)
            event = opened
        return event


# Responding (FR-7 to FR-9)--------------------------------------------------------------------


async def respond(
    db: AsyncSession, actor: Member, event_id: int, status: Status, *, run: int | None = None, now: datetime
) -> None:
    """``run``, when given, is the run the actor saw (``check_run``), as on a Slack card."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, answer_action(status))
        check_run(event, run)
        check_can_respond(event, actor, invited=await _invited(db, event, actor))
        ctx = await load(db, event)
        new = with_status(ctx.response_of(actor.id), actor.id, status, now)
        await ResponseRepository(db).save(actor.household_id, event.id, new, now)
        if drops_coordinating(event, new):  # whoever coordinates is in (#154)
            await cc.release_coordinator(db, event, actor, why="out", now=now)  # updates the messages
        else:
            await _roster_changed(db, event, now)
        activity.action(db, "answer", event, actor, answer=status.value)


async def offer_role(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    role: RoleOffer,
    offered: bool,
    *,
    run: int | None = None,
    now: datetime,
) -> None:
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.OFFER_ROLE)
        check_run(event, run)
        ctx = await load(db, event)
        check_can_offer_role(event, actor, ctx.response_of(actor.id), invited=await _invited(db, event, actor))
        new = with_offer(ctx.response_of(actor.id), actor.id, role, offered, now)
        if new is None:
            return
        await ResponseRepository(db).save(actor.household_id, event.id, new, now)
        await _roster_changed(db, event, now)
        activity.action(db, "offer_role", event, actor, role=role.value, offered=offered)


async def toggle_role(db: AsyncSession, actor: Member, event_id: int, role: RoleOffer, *, now: datetime) -> bool:
    """Offer ``role`` if the actor hasn't, or withdraw it if they have, reading their offer under
    the event's lock. For buttons shared by everyone, such as on a Slack message. Returns
    whether the actor now offers the role."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.OFFER_ROLE)
        current = (await load(db, event)).response_of(actor.id)
        check_can_offer_role(event, actor, current, invited=await _invited(db, event, actor))
        offered = current is None or role not in current.offers
        new = with_offer(current, actor.id, role, offered, now)
        if new is not None:
            await ResponseRepository(db).save(actor.household_id, event.id, new, now)
            await _roster_changed(db, event, now)
            activity.action(db, "offer_role", event, actor, role=role.value, offered=offered)
        return offered


async def save_details(
    db: AsyncSession, actor: Member, event_id: int, details: Details, *, run: int | None = None, now: datetime
) -> None:
    """FR-8: guests, guest needs, eat window, suggestions, and a note."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.DETAILS)
        check_run(event, run)
        check_can_respond(event, actor, invited=await _invited(db, event, actor))
        ctx = await load(db, event)
        clean = check_details(
            details,
            dietary_tag_ids=[t.id for t in ctx.tags.values() if t.kind is TagKind.DIETARY],
            active_item_ids=[i.id for i in ctx.items.values() if i.active],
        )
        new = with_details(ctx.response_of(actor.id), actor.id, clean, now)
        await ResponseRepository(db).save(actor.household_id, event.id, new, now)
        await SuggestionRepository(db).replace(
            actor.household_id, event.id, actor.id, clean.suggestion_item_ids, clean.suggestion_texts, now
        )
        await _roster_changed(db, event, now)
        suggestions = len(clean.suggestion_item_ids) + len(clean.suggestion_texts)
        note_length = len(clean.note or "")  # the note is theirs to write, so only its length (SEC-5)
        activity.action(
            db, "details", event, actor, guests=clean.guests, suggestions=suggestions, note_length=note_length
        )


# Joining late (#76) -------------------------------------------------------------------------------


async def join_late(db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime) -> bool:
    """**I'm in (late)**: a member who missed the answers joins while the call is voting
    (``check_can_join_late``). They're in, marked late, and their vote shows but doesn't count
    until the coordinator says so. Returns whether they joined; someone already in changes
    nothing. ``run``, when given, is the run the actor saw (``check_run``)."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.JOIN_LATE)
        check_run(event, run)
        ctx = await load(db, event)
        return await _joined_late(db, ctx, actor, now=now) is not ctx


async def _joined_late(db: AsyncSession, ctx: CallContext, actor: Member, *, now: datetime) -> CallContext:
    """Join ``actor`` to ``ctx``'s call late, under the event's lock, and return the call read
    again; or ``ctx`` itself when they're in already."""
    mine = ctx.response_of(actor.id)
    if not check_can_join_late(ctx.event, actor, mine):
        return ctx
    await ResponseRepository(db).save(actor.household_id, ctx.event.id, with_late_join(mine, actor.id, now), now)
    joined = await load(db, ctx.event)
    await publish(db, joined, cc.call_updated(joined), cc.proposals_updated(joined), now=now)
    before = "never" if mine is None else ("maybe" if mine.maybe_at_deadline else mine.status.value)
    activity.action(db, "join_late", ctx.event, actor, answer_before=before)
    return joined


async def _in_or_join_late(
    db: AsyncSession, ctx: CallContext, actor: Member, *, join_late: bool, coordinator_ok: bool, now: datetime
) -> CallContext:
    """For a vote or a pick while voting: someone who isn't in joins late first, once they've
    said yes to ``JOIN_LATE_PROMPT`` (``join_late``); otherwise ``JoinLateFirst`` says why
    nothing happened. ``coordinator_ok``: a coordinator who isn't in may act without joining,
    as they always could vote."""
    mine = ctx.response_of(actor.id)
    if (mine is not None and mine.is_in) or (coordinator_ok and actor.active and is_coordinator(ctx.event, actor)):
        return ctx
    if ctx.event.state is not EventState.VOTING:
        return ctx  # the use case's own check says why
    check_can_join_late(ctx.event, actor, mine)
    if not join_late:
        raise JoinLateFirst(NOT_IN_JOIN_LATE)
    return await _joined_late(db, ctx, actor, now=now)


async def switch_late_joiner(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    member_id: int,
    *,
    taking_part: bool | None = None,
    vote_counts: bool | None = None,
    run: int | None = None,
    now: datetime,
) -> None:
    """The coordinator's switches for a late joiner (#76; ``check_can_switch_late``): **taking
    part**, on by default, and **vote counts**, off by default. Switching taking part off makes
    them out for this call, and they're told. A switch already where it's asked changes nothing.
    Otherwise it's audited as ``event.late_joiner_changed`` and the call and proposals messages
    are updated."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.LATE_SWITCHES)
        check_run(event, run)
        ctx = await load(db, event)
        coordinator = ctx.members.get(event.coordinator_id) if event.coordinator_id is not None else None
        capacity = check_can_switch_late(event, actor, coordinator)
        target = ctx.response_of(member_id)
        if member_id not in ctx.members:
            raise NotFound(NO_SUCH_MEMBER)
        if target is None or not target.late:
            raise EventRuleViolation(NOT_LATE, code="not_late")
        new = with_late_switches(target, taking_part=taking_part, vote_counts=vote_counts)
        if new == target:
            return
        await ResponseRepository(db).save(actor.household_id, event.id, new, now)
        if drops_coordinating(event, new):  # switched off, they're out, so they don't coordinate (#154)
            event = await cc.release_coordinator(db, event, actor, why="taken_off", now=now, update=False)
        ctx = await load(db, event)
        sent: list[note.Notification] = [cc.call_updated(ctx), cc.proposals_updated(ctx)]
        person = ctx.members[member_id]
        if new.is_in != target.is_in and person.active and member_id != actor.id:
            sent.append(
                note.LateJoinChanged(cc.event_info(ctx), ctx.person(member_id), new.is_in, ctx.person(actor.id), now)
            )
        await publish(db, ctx, *sent, now=now)
        payload = {
            "member_id": member_id,
            "taking_part": new.is_in,
            "vote_counts": new.vote_counts,
            "as": capacity.value,
        }
        await audit(db, actor.household_id, actor, "event.late_joiner_changed", _target(event), payload, now)
        activity.action(
            db,
            "late_switch",
            event,
            actor,
            to_member_id=member_id,
            to_member=person.display_name,
            taking_part=new.is_in,
            vote_counts=new.vote_counts,
            capacity=capacity.value,
        )


async def _invited(db: AsyncSession, event: Event, actor: Member) -> bool:
    """Whether ``actor``, a guest, is invited to ``event``, for the domain's own guard (#38).
    Members never need it, so it reads nothing for them."""
    return actor.is_guest and actor.id in await InvitationRepository(db).for_event(event.household_id, event.id)


async def _roster_changed(db: AsyncSession, event: Event, now: datetime) -> None:
    ctx = await load(db, event)
    await publish(db, ctx, cc.call_updated(ctx), now=now)


# Coordinating (FR-10) -------------------------------------------------------------------------


async def claim_coordinator(
    db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime
) -> None:
    """The first member to claim coordinating gets it, and is in (#154): someone who was out,
    a maybe, or hadn't answered is in now, or joins late while voting (``set_coordinator``).
    Claiming it again, as from a double tap or a retried Slack delivery, changes nothing and
    isn't an error."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.COORDINATE)
        check_run(event, run)
        if actor.active and is_coordinator(event, actor):
            return
        mine = (await load(db, event)).response_of(actor.id)
        check_claim_coordinator(event, actor, invited=await _invited(db, event, actor), mine=mine)
        await set_coordinator(db, event, actor.id, now=now)
        await audit(db, actor.household_id, actor, "event.coordinator_claimed", _target(event), {}, now)
        activity.action(db, "coordinate", event, actor)


def handoff_target(ctx: CallContext, actor: Member, member_id: int) -> Member:
    """Whom ``actor`` may hand coordinating to: a member, or a guest invited to the call. Once
    it's decided, only someone who's in (``check_coordinator_in``)."""
    target = ctx.members.get(member_id)
    if target is None or (target.is_guest and not ctx.invited(target.id)):
        raise NotFound(NO_SUCH_MEMBER)
    check_handoff(ctx.event, actor, target)
    check_coordinator_in(ctx.event, ctx.response_of(target.id))
    return target


async def hand_off(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    member_id: int,
    *,
    run: int | None = None,
    expect: Callable[[CallContext], object] | None = None,
    now: datetime,
) -> None:
    """Hand coordinating to someone else, who is in from now on (``set_coordinator``).
    ``expect``, from Slack's Adjust, checks under the lock that the call is still as the modal
    showed it, and raises if not."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.HAND_OFF)
        check_run(event, run)
        ctx = await load(db, event)
        if expect is not None:
            expect(ctx)
        target = handoff_target(ctx, actor, member_id)
        await set_coordinator(db, event, member_id, by=actor, now=now)
        await audit(
            db, actor.household_id, actor, "event.coordinator_handed_off", _target(event), {"to": member_id}, now
        )
        activity.action(db, "hand_off", event, actor, to_member_id=target.id, to_member=target.display_name)


async def set_coordinator(
    db: AsyncSession, event: Event, member_id: int | None, *, by: Member | None = None, now: datetime
) -> None:
    """Change the coordinator, and the coordinator assignment once decided. Whoever coordinates
    is in (#154, ``with_coordinating``): someone who wasn't is in now, or joins late while
    voting; once decided, they must be in already (``check_coordinator_in``). When ``by``, who
    handed it off or assigned it, put someone else in that way, they're told
    (``CoordinatingGiven``). The caller checks who may, and audits. The same coordinator again
    changes nothing and sends nothing."""
    if member_id == event.coordinator_id:
        return
    joined: Response | None = None
    if member_id is not None:
        before = await load(db, event)
        current = before.response_of(member_id)
        check_coordinator_in(event, current)
        person = before.members.get(member_id)
        joined = with_coordinating(event, current, member_id, now, guest=person is not None and person.is_guest)
        if joined is not None:
            await ResponseRepository(db).save(event.household_id, event.id, joined, now)
        if current is not None and current.is_in:
            joined = None  # only a changed vote: they were in already
    event = await EventRepository(db).save(replace(event, coordinator_id=member_id), now)
    assignments = AssignmentRepository(db)
    new_assignment = None
    if event.state is EventState.DECIDED:
        if member_id is None:
            await assignments.unassign(event.household_id, event.id, AssignmentRole.COORDINATOR)
        else:
            new_assignment = await assignments.assign(
                event.household_id, event.id, AssignmentRole.COORDINATOR, member_id, now
            )
    ctx = await load(db, event)
    await publish(db, ctx, *cc.messages_for_state(ctx), now=now)
    if joined is not None and member_id is not None and by is not None and by.id != member_id:
        given = note.CoordinatingGiven(
            cc.event_info(ctx),
            ctx.person(member_id),
            ctx.person(by.id),
            now,
            late=joined.late,
            via=cc.answering_for(ctx, member_id),
        )
        await publish(db, ctx, given, now=now)
    decision = cc.decision_info(ctx)
    if new_assignment is not None and decision is not None:
        dm = cc.assignment_made(ctx, new_assignment.member_id, new_assignment.id, decision)
        await publish(db, ctx, dm, now=now)


# Proposals and votes (FR-13 to FR-15) ---------------------------------------------------------


async def add_proposal(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    mode: Mode,
    *,
    item_id: int | None = None,
    free_text: str | None = None,
    cuisine_id: int | None = None,
    join_late: bool = False,
    run: int | None = None,
    now: datetime,
) -> Proposal:
    """FR-13: a catalog item, or free text with a cuisine, during the vote window. A proposal
    must pass the same filters ranking applies (ADR 0005: a veto is a guarantee). Whoever adds it
    does it (#74): they must be in, and they offer the role its mode needs before it's checked,
    as an offer does (``picks.volunteer``). The exception is a home meal whose cook list they're
    not on: it's added if a cook on the list who's in offered to cook, and they take no role. A
    call has one pick per place, in any mode (#77).

    Someone who isn't in joins late first when ``join_late`` says they agreed to (#76), and is
    refused with ``JoinLateFirst`` otherwise. A late joiner's pick passes the stances and needs
    of everyone who answered on time; one that misses only their own is added, with a warning.
    ``run``, when given, is the run the actor saw (``check_run``), as on the Slack modal that
    asked them to join late."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.PROPOSE)
        check_run(event, run)
        check_can_propose(event, actor)
        ctx = await load(db, event)
        named = ctx.items.get(item_id) if item_id is not None else free_text
        activity.about(item=named, item_id=item_id, mode=mode)
        ctx = await _in_or_join_late(db, ctx, actor, join_late=join_late, coordinator_ok=False, now=now)
        check_in_to_add(ctx.response_of(actor.id))
        check_proposal_room(ctx.proposals, actor)
        item = await _proposed_item(db, ctx, mode, item_id, free_text)
        candidate = _candidate(ctx, actor, mode, item, free_text, cuisine_id)
        existing = picks.picked(ctx, candidate)
        if existing is not None:
            raise picks.already_a_pick(ctx, existing)
        doer = can_do(item, mode, actor.id)
        if doer:
            ctx = await picks.volunteer(db, ctx, actor, mode, join=False, now=now)
        group = await group_preferences(db, actor.household_id, ctx.veto_people)
        problem = lifecycle.misfit(ctx, candidate, group)
        if problem is not None and item is not None and not doer and problem.code == lifecycle.NO_COVERAGE:
            problem = EventRuleViolation(not_on_cook_list(item, "Ask a cook on it to add it"), code="cook_list")
        if problem is not None:
            raise problem
        proposal = await ProposalRepository(db).add(
            household_id=actor.household_id,
            event_id=event.id,
            item_id=candidate.item_id,
            free_text=candidate.free_text,
            free_cuisine_id=candidate.free_cuisine_id,
            mode=mode,
            rank=await ProposalRepository(db).next_rank(actor.household_id, event.id),
            source=candidate.source,
            reasons=candidate.reasons,
            proposed_by=actor.id,
            now=now,
        )
        await picks.announce(db, event, now=now)
        activity.action(db, "propose", event, actor, **activity.pick(ctx.items, proposal), does_it=doer)
        return proposal


async def _proposed_item(
    db: AsyncSession, ctx: CallContext, mode: Mode, item_id: int | None, free_text: str | None
) -> CatalogItem | None:
    """The catalog item proposed, including free text that names one. None for new free text."""
    if item_id is not None:
        if item_id not in ctx.items:
            raise EventRuleViolation(
                "That isn't in the catalog anymore. Reload the page and try again.", code="not_in_catalog"
            )
        return ctx.items[item_id]
    kind = ItemKind.HOME if mode is Mode.COOK else ItemKind.RESTAURANT
    # A match counts even when inactive, so its tags and item vetoes are checked (and an inactive
    # item is refused) rather than slipping through as new free text.
    return await CatalogRepository(db).item_by_name(ctx.event.household_id, kind, _free_name(free_text))


def _free_name(free_text: str | None) -> str:
    try:
        return normalize_item_name(free_text or "")
    except InvalidCatalogInput as exc:
        raise EventRuleViolation(str(exc), code="invalid_name") from None


def _candidate(
    ctx: CallContext, actor: Member, mode: Mode, item: CatalogItem | None, free_text: str | None, cuisine_id: int | None
) -> Proposal:
    source = ProposalSource.COORDINATOR if is_coordinator(ctx.event, actor) else ProposalSource.MEMBER
    reasons = (f"Added by {actor.display_name}.",)
    if item is not None:
        return Proposal(0, ctx.event.id, mode, 0, source, item_id=item.id, reasons=reasons)
    tag = ctx.tags.get(cuisine_id) if cuisine_id is not None else None
    if tag is None or tag.kind is not TagKind.CUISINE:
        raise EventRuleViolation("Pick a cuisine for it, so variety and vetoes still work.", code="no_cuisine")
    name = _free_name(free_text)
    return Proposal(0, ctx.event.id, mode, 0, source, free_text=name, free_cuisine_id=tag.id, reasons=reasons)


def _proposal_in(ctx: CallContext, proposal_id: int) -> Proposal:
    proposal = ctx.proposal(proposal_id)
    if proposal is None:
        raise NotFound(NO_SUCH_PROPOSAL)
    return proposal


async def vote(
    db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, join_late: bool = False, now: datetime
) -> None:
    """FR-14: one vote per member per proposal; voting twice changes nothing. Someone who isn't
    in joins late first when ``join_late`` says they agreed to (#76), and is refused with
    ``JoinLateFirst`` otherwise."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.VOTE)
        ctx = await load(db, event)
        _proposal_in(ctx, proposal_id)
        ctx = await _in_or_join_late(db, ctx, actor, join_late=join_late, coordinator_ok=True, now=now)
        check_can_vote(event, actor, ctx.response_of(actor.id))
        proposal = _proposal_in(ctx, proposal_id)
        if await VoteRepository(db).add(actor.household_id, event.id, proposal_id, actor.id, now):
            await _votes_changed(db, event, now)
            activity.action(db, "vote", event, actor, **activity.pick(ctx.items, proposal))


async def unvote(db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, now: datetime) -> None:
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.VOTE)
        ctx = await load(db, event)
        check_can_vote(event, actor, ctx.response_of(actor.id))
        proposal = _proposal_in(ctx, proposal_id)
        if await VoteRepository(db).remove(actor.household_id, proposal_id, actor.id):
            await _votes_changed(db, event, now)
            activity.action(db, "unvote", event, actor, **activity.pick(ctx.items, proposal))


async def toggle_vote(db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, now: datetime) -> bool:
    """Vote for the proposal, or take the vote back, reading the actor's vote under the event's
    lock. For shared buttons, such as on a Slack message. Returns whether the actor now votes
    for it. Someone who isn't in gets ``JoinLateFirst``: joining late takes their yes, and a
    toggle can't be retried safely, so they join and vote with ``vote(..., join_late=True)``."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.VOTE)
        ctx = await load(db, event)
        _proposal_in(ctx, proposal_id)
        await _in_or_join_late(db, ctx, actor, join_late=False, coordinator_ok=True, now=now)
        check_can_vote(event, actor, ctx.response_of(actor.id))
        proposal = _proposal_in(ctx, proposal_id)
        votes = VoteRepository(db)
        voted = actor.id not in ctx.votes.get(proposal_id, [])
        if voted:
            await votes.add(actor.household_id, event.id, proposal_id, actor.id, now)
        else:
            await votes.remove(actor.household_id, proposal_id, actor.id)
        await _votes_changed(db, event, now)
        activity.action(db, "vote" if voted else "unvote", event, actor, **activity.pick(ctx.items, proposal))
        return voted


async def _votes_changed(db: AsyncSession, event: Event, now: datetime) -> None:
    ctx = await load(db, event)
    await publish(db, ctx, cc.proposals_updated(ctx), now=now)


async def lock(db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, now: datetime) -> None:
    """FR-15: the coordinator, or an admin, decides the call now. Audited (SEC-9)."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.LOCK)
        check_can_lock(event, actor)
        ctx = await load(db, event)
        proposal = _proposal_in(ctx, proposal_id)
        activity.about(**activity.pick(ctx.items, proposal))
        await lifecycle.decide(db, event, now, locked=proposal, actor=actor)
        activity.action(db, "lock", event, actor, after=EventState.DECIDED, **activity.pick(ctx.items, proposal))


# After the decision (FR-19 to FR-21) ----------------------------------------------------------


def meal_at(ctx: CallContext, at: time) -> datetime:
    """``at``, a wall time on the call's date, as the meal time: never before the decision (FR-19)."""
    event = ctx.event
    meal = datetime.combine(event.day, at, tzinfo=ctx.zone).astimezone(UTC)
    if event.decided_at is not None and meal < event.decided_at:
        raise EventRuleViolation("Pick a meal time after the decision. Check AM and PM.", code="bad_time")
    return meal


async def set_meal_time(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    at: time,
    *,
    run: int | None = None,
    as_coordinator: bool = False,
    expect: Callable[[CallContext], object] | None = None,
    now: datetime,
) -> None:
    """FR-19: the coordinator (or an admin) changes the meal time, on the call's date.
    ``as_coordinator``, from Slack's Adjust, allows only the coordinator (spec 5.7), and
    ``expect`` checks under the lock that the call is still as the modal showed it."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.MANAGE_DECISION)
        check_run(event, run)
        check_can_manage_decision(event, actor)
        if as_coordinator:
            check_can_adjust(event, actor)
        if event.state is not EventState.DECIDED:
            raise EventRuleViolation("This meal is already done.", code="not_decided")
        ctx = await load(db, event)
        if expect is not None:
            expect(ctx)
        meal = meal_at(ctx, at)
        event = await EventRepository(db).save(replace(event, meal_time=meal), now)
        ctx = await load(db, event)
        # The call message names the result, time included.
        await publish(db, ctx, *cc.decision_updated(ctx), cc.message_updated(ctx), now=now)
        payload = {"meal_time": meal.isoformat()}
        await audit(db, actor.household_id, actor, "event.meal_time_changed", _target(event), payload, now)
        activity.action(db, "meal_time", event, actor, meal_time=meal.isoformat())


ROLE_CHANGES = (AssignmentRole.COOK, AssignmentRole.PICKUP, AssignmentRole.ORDER)


async def change_assignment(
    db: AsyncSession, actor: Member, event_id: int, role: AssignmentRole, member_id: int | None, *, now: datetime
) -> None:
    """Who cooks, picks up, or orders: the coordinator picks while decided (two volunteer cooks,
    for example); the admin can also correct it after the meal (spec 5.7). Only the decided
    mode's role, and only someone who's in. Giving a role to whoever has it already changes
    nothing and sends nothing. Audited.

    A correction after the call is done moves the role's count and the badges it reaches
    (``contributions.recount_role``, #162), in the same transaction."""
    if role not in ROLE_CHANGES:
        raise EventRuleViolation("Change the coordinator with hand off instead.", code="wrong_role")
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.MANAGE_DECISION)
        check_can_manage_decision(event, actor)
        ctx = await load(db, event)
        mode = event.decided_mode
        if mode is None:  # pragma: no cover - a decided or done call always has a mode
            raise EventRuleViolation("There's no decision to change.", code="not_decided")
        if role is not MODE_ROLE[mode]:
            raise EventRuleViolation(
                f"This meal is {mode.label.lower()}, so only {MODE_ROLE[mode].value} is assigned.", code="wrong_role"
            )
        current = next((a.member_id for a in ctx.assignments if a.role is role), None)
        if member_id == current:
            return
        repo = AssignmentRepository(db)
        if member_id is None:
            await repo.unassign(actor.household_id, event.id, role)
            assigned = None
        else:
            target = ctx.members.get(member_id)
            if target is None or not target.active:
                raise NotFound(NO_SUCH_MEMBER)
            response = ctx.response_of(member_id)
            if response is None or not response.is_in:
                raise EventRuleViolation("Pick someone who's in.", code="not_in")
            assigned = await repo.assign(actor.household_id, event.id, role, member_id, now)
        was_pending = event.roles_due_at is not None
        if was_pending:  # choosing the role ends the coordinator's time to (#87)
            event = await EventRepository(db).save(replace(event, roles_due_at=None), now)
        ctx = await load(db, event)
        decision = cc.decision_info(ctx)
        await publish(db, ctx, *cc.decision_updated(ctx), now=now)
        # News only while the meal is ahead: afterward it corrects the record (#154).
        ahead = event.state is EventState.DECIDED and cc.before_the_meal(event, now)
        if ahead and current is not None and not was_pending:
            await publish(db, ctx, *cc.assignment_ended(ctx, current, (role,), actor, now), now=now)
        if assigned is not None and decision is not None and ahead:
            await publish(db, ctx, cc.assignment_made(ctx, assigned.member_id, assigned.id, decision), now=now)
        payload = {"role": role.value, "member_id": member_id}
        await audit(db, actor.household_id, actor, "event.assignment_changed", _target(event), payload, now)
        to = ctx.members.get(member_id) if member_id is not None else None
        activity.action(
            db,
            "assign",
            event,
            actor,
            role=role.value,
            to_member_id=member_id,
            to_member=to.display_name if to else None,
        )
        if event.state is EventState.DONE:
            await contributions.recount_role(db, actor, event, role, now=now)


async def mark_done(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> None:
    """FR-21: any participant, the coordinator, or an admin marks the meal done."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.MARK_DONE)
        ctx = await load(db, event)
        check_can_mark_done(event, actor, ctx.response_of(actor.id), now=now)
        await lifecycle.finish(db, event, now, actor=actor)
        activity.action(db, "mark_done", event, actor, after=EventState.DONE)


# Calling it off and closing early (FR-56) -----------------------------------------------------

CANCEL_ROLES = (AssignmentRole.COOK, AssignmentRole.PICKUP, AssignmentRole.ORDER)
"""Assignees told directly when a decided call is called off: the people who'd shop or drive."""


async def cancel_call(
    db: AsyncSession, actor: Member, event_id: int, *, note: str | None = None, run: int | None = None, now: datetime
) -> None:
    """Call off an open call (``check_can_cancel``): it ends as cancelled, and nothing goes in
    the meal log (spec 8). ``note`` is an optional reason, cleaned by ``clean_cancel_note``.
    Undelivered assignment and reminder direct messages for the call are closed unsent.

    A call that was posted gets an ``EventCancelled`` reply in its thread, mentioning everyone
    who was in or maybe and the coordinator, and its messages are updated (a decision is struck
    through). A decided call's active cooks, pickups, and orderers each get ``AssignmentCancelled``.
    A call that was never posted, a scheduled one, posts nothing; its starter gets
    ``StartedCallCancelled`` when someone else called it off. Nobody is messaged about their own
    call-off. Audited as ``event.cancelled`` with the state it left, the capacity the actor
    acted in, and the note. ``run``, when given, is the run the actor saw (``check_run``).
    """
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.CALL_OFF)
        check_run(event, run)
        capacity = check_can_cancel(event, actor)
        reason = clean_cancel_note(note)
        try:
            state = transition(event.state, EventState.CANCELLED)
        except IllegalTransition as exc:  # pragma: no cover - check_can_cancel refuses finished calls
            raise friendly(exc) from None
        before = await load(db, event)
        event = await EventRepository(db).save(replace(event, state=state, ended_by=actor.id, end_note=reason), now)
        ctx = await load(db, event)
        await OutboxRepository(db).close_unsent(
            event.household_id, event.id, CALL_OFF_SUPERSEDES, note=SUPERSEDED_BY_CALL_OFF, now=now
        )
        await publish(db, ctx, *_called_off(before, ctx, actor, reason), now=now)
        payload = {"from": before.event.state.value, "as": capacity.value, "note": reason}
        await audit(db, actor.household_id, actor, "event.cancelled", _target(event), payload, now)
        note_length = len(reason or "")  # the reason is theirs to write, so only its length (SEC-5)
        activity.action(
            db, "call_off", before.event, actor, after=state, capacity=capacity.value, note_length=note_length
        )


def _called_off(before: CallContext, ctx: CallContext, actor: Member, reason: str | None) -> list[note.Notification]:
    """What a call-off sends, given the call ``before`` it and after (``ctx``)."""
    info, by = cc.event_info(ctx), ctx.person(actor.id)
    sent: list[note.Notification] = []
    if before.event.state is EventState.SCHEDULED:
        starter = ctx.members.get(before.event.created_by) if before.event.created_by is not None else None
        if starter is not None and starter.active and starter.id != actor.id:
            sent.append(note.StartedCallCancelled(info, ctx.person(starter.id), by, reason))
        return sent
    participants = tuple(ctx.person(r.member_id) for r in before.participants)
    sent.append(note.EventCancelled(info, participants, by, reason, _audience(before)))
    sent += [
        cc.message_updated(ctx),
        *([cc.proposals_updated(ctx)] if ctx.proposals else []),
        *cc.decision_updated(ctx),
    ]
    decision = cc.decision_info(before)
    if before.event.state is EventState.DECIDED and decision is not None:
        order = list(AssignmentRole)
        roles: dict[int, set[AssignmentRole]] = {}
        for a in before.assignments:
            assignee = ctx.members.get(a.member_id)
            if a.role in CANCEL_ROLES and a.member_id != actor.id and assignee is not None and assignee.active:
                roles.setdefault(a.member_id, set()).add(a.role)
        for member_id, theirs in roles.items():
            mine = tuple(sorted(theirs, key=order.index))
            via = cc.answering_for(ctx, member_id)
            sent.append(note.AssignmentCancelled(info, ctx.person(member_id), mine, decision, by, reason, via))
    return sent


def _audience(ctx: CallContext) -> tuple[note.PersonInfo, ...]:
    """Who a call-off reply mentions: active members and invited guests (#38) who were in or
    maybe, and the coordinator."""
    who = {r.member_id for r in ctx.responses if r.status is not Status.OUT}
    if ctx.event.coordinator_id is not None:
        who.add(ctx.event.coordinator_id)
    people = [m for m in [*ctx.active_members, *ctx.guests] if m.id in who]
    return tuple(ctx.person(m.id) for m in sorted(people, key=lambda m: m.display_name.casefold()))


async def close_window(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    run: int | None = None,
    expected_window: Window | None = None,
    now: datetime,
) -> None:
    """Close the open window now (``check_can_close_window``): "Start the vote now" while
    collecting, "Decide now" while voting. The call then advances at once, as at its deadline:
    to voting or on-your-own, or to a decision. ``run``, when given, is the run the actor saw
    (``check_run``). ``expected_window``, when given, is the window they confirmed closing
    (``check_expected_window``): if another is open, or that one already closed, nothing
    changes. Audited as ``event.window_closed``."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.CLOSE_WINDOW)
        check_run(event, run)
        window = check_can_close_window(event, actor)
        check_expected_window(event, window, expected_window, now=now)
        current, moved = await lifecycle.move_window(db, event, window, None, now)
        capacity = ActingAs.COORDINATOR if is_coordinator(event, actor) else ActingAs.ADMIN
        payload = {"window": window.value, "from": current.isoformat(), "to": moved.isoformat(), "as": capacity.value}
        await audit(db, actor.household_id, actor, "event.window_closed", _target(event), payload, now)
        activity.action(db, "close_window", event, actor, window=window.value, capacity=capacity.value)
    await lifecycle.advance(db, actor.household_id, event_id, now)


async def skip_day(
    db: AsyncSession, actor: Member, slot: Slot, day: date, *, note: str | None = None, now: datetime
) -> Event:
    """An admin skips the daily call for ``slot`` on ``day``, today or up to a week ahead, before
    it exists: a cancelled call takes its place, which keeps the scheduler from opening one
    (FR-2). Nothing is posted. Any member can still start it again (``start_call``). To end a
    call that already exists, call it off; to stop a slot for good, turn it off in the
    schedule. A slot that's off in the schedule has no daily call to skip. Audited as
    ``event.skipped``."""
    require_admin(actor, "Only an admin can skip a day's call.")
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    check_skip_day(day, household_today(household, now))
    reason = clean_cancel_note(note)
    schedule = slot_schedule(await household_settings(db, household.id), slot)
    events = EventRepository(db)
    try:
        existing = await events.call_for(household.id, slot, day)
        if existing is not None:
            raise EventRuleViolation(_not_skippable(existing), code="call_exists")
        if not schedule.enabled:  # no daily call to skip; checked second, as an ad-hoc call can still exist
            raise EventRuleViolation(f"{slot.value.capitalize()} is off in the schedule.", code="slot_off")
        opens_at = schedule.opens_at(day, ZoneInfo(household.timezone))
        event = await events.add(
            household_id=household.id,
            slot=slot,
            day=day,
            state=EventState.CANCELLED,
            opens_at=opens_at,
            respond_deadline=opens_at + schedule.response_window,
            reminder_minutes=schedule.reminder_minutes,
            vote_minutes=schedule.vote_minutes,
            created_by=None,
            ended_by=actor.id,
            end_note=reason,
            now=now,
        )
        payload = {"slot": slot.value, "date": day.isoformat(), "note": reason}
        await audit(db, household.id, actor, "event.skipped", _target(event), payload, now)
        activity.action(db, "skip_day", event, actor, started=True, note_length=len(reason or ""))
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not is_unique_violation(exc):
            raise
        # Whoever won the race, the scheduler or a member, just opened or scheduled the call.
        raise EventRuleViolation(
            f"There's already a {slot.value} call for that day. Call it off instead.", code="call_exists"
        ) from None
    except BaseException:
        await db.rollback()
        raise
    return event


def _not_skippable(existing: Event) -> str:
    """Why a day that already has a call can't be skipped, and what to do instead."""
    slot = existing.slot.value
    if existing.state.is_open:
        return f"There's already a {slot} call for that day. Call it off instead."
    if existing.state is EventState.CANCELLED:
        return f"The {slot} call for that day is already off."
    return f"The {slot} call for that day already finished."
