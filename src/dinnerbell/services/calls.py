# SPDX-License-Identifier: AGPL-3.0-or-later
"""What members do with a call: start one, respond, coordinate, propose, vote, lock, mark done,
call it off, and close a window now (spec 5.1, FR-3, FR-7 to FR-21, FR-56).

Every function takes the acting member and the event ID, and looks the event up only inside the
actor's household, so another household's ID is simply not found. Members change only their own
response; no function takes another member's response.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.events import (
    AssignmentRepository,
    EventRepository,
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
    name_key,
    normalize_item_name,
)
from dinnerbell.domain.events import (
    MODE_ROLE,
    ActingAs,
    AssignmentRole,
    Details,
    Event,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    Proposal,
    ProposalSource,
    RoleOffer,
    Status,
    Window,
    check_ad_hoc_day,
    check_can_cancel,
    check_can_close_window,
    check_can_lock,
    check_can_manage_decision,
    check_can_mark_done,
    check_can_propose,
    check_can_respond,
    check_can_vote,
    check_claim_coordinator,
    check_details,
    check_expected_window,
    check_handoff,
    check_proposal_room,
    check_run,
    check_skip_day,
    clean_cancel_note,
    friendly,
    is_coordinator,
    transition,
    veto_participants,
    with_details,
    with_offer,
    with_status,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import CALL_OFF_SUPERSEDES, SUPERSEDED_BY_CALL_OFF
from dinnerbell.domain.schedule import slot_schedule
from dinnerbell.services import call_context as cc
from dinnerbell.services import lifecycle
from dinnerbell.services.call_context import CallContext, load, locked_event, publish
from dinnerbell.services.common import audit, is_unique_violation, require_admin
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import group_preferences
from dinnerbell.services.variety import household_today

NO_SUCH_PROPOSAL = "No such proposal."
NO_SUCH_MEMBER = "No such member."


def _target(event: Event) -> str:
    return f"event:{event.id}"


# Starting a call (FR-3) -----------------------------------------------------------------------


async def start_call(db: AsyncSession, actor: Member, slot: Slot, day: date, *, now: datetime) -> Event:
    """An ad-hoc call. For today it opens now; for a later day, at the slot's usual time."""
    if not actor.active:
        raise EventRuleViolation("Only active members can start a call.")
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    today = household_today(household, now)
    check_ad_hoc_day(day, today)
    schedule = slot_schedule(await household_settings(db, household.id), slot)
    opens_at = now if day == today else schedule.opens_at(day, ZoneInfo(household.timezone))
    try:
        event = await lifecycle.create_call(db, household, slot, day, opens_at=opens_at, actor=actor, now=now)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return event


async def restart_call(
    db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime
) -> Event:
    """Start the called-off call ``event_id`` again (FR-56), as ``start_call`` does for its slot
    and day. ``run``, when given, is the run the actor saw (``check_run``): a page left open
    across a restart and a second call-off doesn't start the newer run again."""
    if not actor.active:
        raise EventRuleViolation("Only active members can start a call.")
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    check_ad_hoc_day(event.day, household_today(household, now))
    try:
        fresh = await lifecycle.restart(db, event, actor=actor, run=run, now=now)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return fresh


# Responding (FR-7 to FR-9) --------------------------------------------------------------------


async def respond(db: AsyncSession, actor: Member, event_id: int, status: Status, *, now: datetime) -> None:
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_respond(event, actor)
        ctx = await load(db, event)
        new = with_status(ctx.response_of(actor.id), actor.id, status, now)
        await ResponseRepository(db).save(actor.household_id, event.id, new, now)
        await _roster_changed(db, event, now)


async def offer_role(
    db: AsyncSession, actor: Member, event_id: int, role: RoleOffer, offered: bool, *, now: datetime
) -> None:
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_respond(event, actor)
        ctx = await load(db, event)
        new = with_offer(ctx.response_of(actor.id), actor.id, role, offered, now)
        if new is None:
            return
        await ResponseRepository(db).save(actor.household_id, event.id, new, now)
        await _roster_changed(db, event, now)


async def toggle_role(db: AsyncSession, actor: Member, event_id: int, role: RoleOffer, *, now: datetime) -> bool:
    """Offer ``role`` if the actor hasn't, or withdraw it if they have, reading their offer under
    the event's lock. For buttons shared by everyone, such as on a Slack message. Returns
    whether the actor now offers the role."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_respond(event, actor)
        current = (await load(db, event)).response_of(actor.id)
        offered = current is None or role not in current.offers
        new = with_offer(current, actor.id, role, offered, now)
        if new is not None:
            await ResponseRepository(db).save(actor.household_id, event.id, new, now)
            await _roster_changed(db, event, now)
        return offered


async def save_details(db: AsyncSession, actor: Member, event_id: int, details: Details, *, now: datetime) -> None:
    """FR-8: guests, guest needs, eat window, suggestions, and a note."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_respond(event, actor)
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


async def _roster_changed(db: AsyncSession, event: Event, now: datetime) -> None:
    ctx = await load(db, event)
    await publish(db, ctx, cc.call_updated(ctx), now=now)


# Coordinating (FR-10) -------------------------------------------------------------------------


async def claim_coordinator(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> None:
    """The first member to claim coordinating gets it."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_claim_coordinator(event, actor)
        await set_coordinator(db, event, actor.id, now=now)
        await audit(db, actor.household_id, actor, "event.coordinator_claimed", _target(event), {}, now)


async def hand_off(db: AsyncSession, actor: Member, event_id: int, member_id: int, *, now: datetime) -> None:
    async with locked_event(db, actor.household_id, event_id, now) as event:
        target = (await load(db, event)).members.get(member_id)
        if target is None:
            raise NotFound(NO_SUCH_MEMBER)
        check_handoff(event, actor, target)
        await set_coordinator(db, event, member_id, now=now)
        await audit(
            db, actor.household_id, actor, "event.coordinator_handed_off", _target(event), {"to": member_id}, now
        )


async def set_coordinator(db: AsyncSession, event: Event, member_id: int | None, *, now: datetime) -> None:
    """Change the coordinator, and the coordinator assignment once decided. The caller checks
    who may, and audits. The same coordinator again changes nothing and sends nothing."""
    if member_id == event.coordinator_id:
        return
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
    await publish(db, ctx, *_messages_for_state(ctx), now=now)
    decision = cc.decision_info(ctx)
    if new_assignment is not None and decision is not None:
        dm = cc.assignment_made(ctx, new_assignment.member_id, new_assignment.id, decision)
        await publish(db, ctx, dm, now=now)


def _messages_for_state(ctx: CallContext) -> list[note.Notification]:
    """Updates for every message that shows the event: the call, and the proposals or decision."""
    messages: list[note.Notification] = [cc.call_updated(ctx)]
    if ctx.proposals:
        messages.append(cc.proposals_updated(ctx))
    messages += cc.decision_updated(ctx)
    return messages


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
    now: datetime,
) -> Proposal:
    """FR-13: a catalog item, or free text with a cuisine, during the vote window. A proposal
    must pass the same filters ranking applies (ADR 0005: a veto is a guarantee)."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_propose(event, actor)
        ctx = await load(db, event)
        check_proposal_room(ctx.proposals, actor)
        item = await _proposed_item(db, ctx, mode, item_id, free_text)
        candidate = _candidate(ctx, actor, mode, item, free_text, cuisine_id)
        _check_not_proposed(ctx, candidate)
        group = await group_preferences(db, actor.household_id, veto_participants(ctx.responses))
        problem = lifecycle.misfit(ctx, candidate, group)
        if problem is not None:
            raise EventRuleViolation(problem)
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
        ctx = await load(db, event)
        await publish(db, ctx, cc.proposals_updated(ctx), now=now)
        return proposal


async def _proposed_item(
    db: AsyncSession, ctx: CallContext, mode: Mode, item_id: int | None, free_text: str | None
) -> CatalogItem | None:
    """The catalog item proposed, including free text that names one. None for new free text."""
    if item_id is not None:
        if item_id not in ctx.items:
            raise EventRuleViolation("That isn't in the catalog anymore. Reload the page and try again.")
        return ctx.items[item_id]
    kind = ItemKind.HOME if mode is Mode.COOK else ItemKind.RESTAURANT
    # A match counts even when inactive, so its tags and item vetoes are checked (and an inactive
    # item is refused) rather than slipping through as new free text.
    return await CatalogRepository(db).item_by_name(ctx.event.household_id, kind, _free_name(free_text))


def _free_name(free_text: str | None) -> str:
    try:
        return normalize_item_name(free_text or "")
    except InvalidCatalogInput as exc:
        raise EventRuleViolation(str(exc)) from None


def _candidate(
    ctx: CallContext, actor: Member, mode: Mode, item: CatalogItem | None, free_text: str | None, cuisine_id: int | None
) -> Proposal:
    source = ProposalSource.COORDINATOR if is_coordinator(ctx.event, actor) else ProposalSource.MEMBER
    reasons = (f"Added by {actor.display_name}.",)
    if item is not None:
        return Proposal(0, ctx.event.id, mode, 0, source, item_id=item.id, reasons=reasons)
    tag = ctx.tags.get(cuisine_id) if cuisine_id is not None else None
    if tag is None or tag.kind is not TagKind.CUISINE:
        raise EventRuleViolation("Pick a cuisine for it, so variety and vetoes still work.")
    name = _free_name(free_text)
    return Proposal(0, ctx.event.id, mode, 0, source, free_text=name, free_cuisine_id=tag.id, reasons=reasons)


def _check_not_proposed(ctx: CallContext, candidate: Proposal) -> None:
    for existing in ctx.proposals:
        if existing.mode is not candidate.mode:
            continue
        same_item = candidate.item_id is not None and existing.item_id == candidate.item_id
        same_text = candidate.free_text is not None and name_key(existing.free_text or "") == name_key(
            candidate.free_text
        )
        if same_item or same_text:
            raise EventRuleViolation("That's already proposed. Vote for it instead.")


def _proposal_in(ctx: CallContext, proposal_id: int) -> Proposal:
    proposal = ctx.proposal(proposal_id)
    if proposal is None:
        raise NotFound(NO_SUCH_PROPOSAL)
    return proposal


async def vote(db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, now: datetime) -> None:
    """FR-14: one vote per member per proposal; voting twice changes nothing."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await load(db, event)
        check_can_vote(event, actor, ctx.response_of(actor.id))
        _proposal_in(ctx, proposal_id)
        if await VoteRepository(db).add(actor.household_id, event.id, proposal_id, actor.id, now):
            await _votes_changed(db, event, now)


async def unvote(db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, now: datetime) -> None:
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await load(db, event)
        check_can_vote(event, actor, ctx.response_of(actor.id))
        _proposal_in(ctx, proposal_id)
        if await VoteRepository(db).remove(actor.household_id, proposal_id, actor.id):
            await _votes_changed(db, event, now)


async def toggle_vote(db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, now: datetime) -> bool:
    """Vote for the proposal, or take the vote back, reading the actor's vote under the event's
    lock. For shared buttons, such as on a Slack message. Returns whether the actor now votes
    for it."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await load(db, event)
        check_can_vote(event, actor, ctx.response_of(actor.id))
        _proposal_in(ctx, proposal_id)
        votes = VoteRepository(db)
        voted = actor.id not in ctx.votes.get(proposal_id, [])
        if voted:
            await votes.add(actor.household_id, event.id, proposal_id, actor.id, now)
        else:
            await votes.remove(actor.household_id, proposal_id, actor.id)
        await _votes_changed(db, event, now)
        return voted


async def _votes_changed(db: AsyncSession, event: Event, now: datetime) -> None:
    ctx = await load(db, event)
    await publish(db, ctx, cc.proposals_updated(ctx), now=now)


async def lock(db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, now: datetime) -> None:
    """FR-15: the coordinator, or an admin, decides the call now. Audited (SEC-9)."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_lock(event, actor)
        proposal = _proposal_in(await load(db, event), proposal_id)
        await lifecycle.decide(db, event, now, locked=proposal, actor=actor)


# After the decision (FR-19 to FR-21) ----------------------------------------------------------


async def set_meal_time(db: AsyncSession, actor: Member, event_id: int, at: time, *, now: datetime) -> None:
    """FR-19: the coordinator (or an admin) changes the meal time, on the call's date."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_manage_decision(event, actor)
        if event.state is not EventState.DECIDED:
            raise EventRuleViolation("This meal is already done.")
        ctx = await load(db, event)
        meal = datetime.combine(event.day, at, tzinfo=ctx.zone).astimezone(UTC)
        if event.decided_at is not None and meal < event.decided_at:
            raise EventRuleViolation("Pick a meal time after the decision. Check AM and PM.")
        event = await EventRepository(db).save(replace(event, meal_time=meal), now)
        ctx = await load(db, event)
        await publish(db, ctx, *cc.decision_updated(ctx), now=now)
        payload = {"meal_time": meal.isoformat()}
        await audit(db, actor.household_id, actor, "event.meal_time_changed", _target(event), payload, now)


ROLE_CHANGES = (AssignmentRole.COOK, AssignmentRole.PICKUP, AssignmentRole.ORDER)


async def change_assignment(
    db: AsyncSession, actor: Member, event_id: int, role: AssignmentRole, member_id: int | None, *, now: datetime
) -> None:
    """Who cooks, picks up, or orders: the coordinator picks while decided (two volunteer cooks,
    for example); the admin can also correct it after the meal (spec 5.7). Only the decided
    mode's role, and only someone who's in. Giving a role to whoever has it already changes
    nothing and sends nothing. Audited."""
    if role not in ROLE_CHANGES:
        raise EventRuleViolation("Change the coordinator with hand off instead.")
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_can_manage_decision(event, actor)
        ctx = await load(db, event)
        mode = event.decided_mode
        if mode is None:  # pragma: no cover - a decided or done call always has a mode
            raise EventRuleViolation("There's no decision to change.")
        if role is not MODE_ROLE[mode]:
            raise EventRuleViolation(f"This meal is {mode.label.lower()}, so only {MODE_ROLE[mode].value} is assigned.")
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
                raise EventRuleViolation("Pick someone who's in.")
            assigned = await repo.assign(actor.household_id, event.id, role, member_id, now)
        ctx = await load(db, event)
        decision = cc.decision_info(ctx)
        await publish(db, ctx, *cc.decision_updated(ctx), now=now)
        if assigned is not None and decision is not None and event.state is EventState.DECIDED:
            await publish(db, ctx, cc.assignment_made(ctx, assigned.member_id, assigned.id, decision), now=now)
        payload = {"role": role.value, "member_id": member_id}
        await audit(db, actor.household_id, actor, "event.assignment_changed", _target(event), payload, now)


async def mark_done(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> None:
    """FR-21: any participant, the coordinator, or an admin marks the meal done."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await load(db, event)
        check_can_mark_done(event, actor, ctx.response_of(actor.id), now=now)
        await lifecycle.finish(db, event, now, actor=actor)


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
    sent += [cc.call_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), *cc.decision_updated(ctx)]
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
            sent.append(note.AssignmentCancelled(info, ctx.person(member_id), mine, decision, by, reason))
    return sent


def _audience(ctx: CallContext) -> tuple[note.PersonInfo, ...]:
    """Who a call-off reply mentions: active members who were in or maybe, and the coordinator."""
    who = {r.member_id for r in ctx.responses if r.status is not Status.OUT}
    if ctx.event.coordinator_id is not None:
        who.add(ctx.event.coordinator_id)
    people = [m for m in ctx.active_members if m.id in who]
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
        check_run(event, run)
        window = check_can_close_window(event, actor)
        check_expected_window(event, window, expected_window, now=now)
        current, moved = await lifecycle.move_window(db, event, window, None, now)
        capacity = ActingAs.COORDINATOR if is_coordinator(event, actor) else ActingAs.ADMIN
        payload = {"window": window.value, "from": current.isoformat(), "to": moved.isoformat(), "as": capacity.value}
        await audit(db, actor.household_id, actor, "event.window_closed", _target(event), payload, now)
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
            raise EventRuleViolation(_not_skippable(existing))
        if not schedule.enabled:  # no daily call to skip; checked second, as an ad-hoc call can still exist
            raise EventRuleViolation(f"{slot.value.capitalize()} is off in the schedule.")
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
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not is_unique_violation(exc):
            raise
        # Whoever won the race, the scheduler or a member, just opened or scheduled the call.
        raise EventRuleViolation(f"There's already a {slot.value} call for that day. Call it off instead.") from None
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
