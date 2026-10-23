# SPDX-License-Identifier: AGPL-3.0-or-later
"""The call lifecycle: open, remind, close responses and rank, decide, and mark done
(spec 5.1, 5.4, 7, 8).

The scheduler's tick calls ``tick`` every 30 seconds. ``tick`` opens each household's standing
calls once their time comes and asks every open event for the step it owes (``domain.events.
due_step``), applying steps until none is due. A restart or downtime past several deadlines is
caught up on the next tick, one transaction per step. Members' actions reuse ``decide`` (lock)
and ``finish`` (mark done).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.catalog import CatalogRepository, ItemValues
from dinnerbell.db.events import AssignmentRepository, EventRepository, ProposalRepository, ResponseRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Mode
from dinnerbell.domain.events import (
    MIN_HEAD_COUNT,
    MODE_ROLE,
    EndReason,
    Event,
    EventRuleViolation,
    EventState,
    Proposal,
    ProposalSource,
    ProposalTally,
    Step,
    assign_roles,
    choose_winner,
    due_step,
    head_count,
    meal_time,
    needs_reminder,
    resolve_maybes,
    should_open_today,
    transition,
    veto_participants,
)
from dinnerbell.domain.household import Household
from dinnerbell.domain.meals import MemberRef, Slot, TagRef
from dinnerbell.domain.members import Member
from dinnerbell.domain.ranking import GroupPreferences, RankingSnapshot, RankItem, rank
from dinnerbell.domain.schedule import proposal_count, slot_schedule, slot_schedules, weights
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import CallContext, load, locked_event, publish
from dinnerbell.services.common import audit, is_unique_violation
from dinnerbell.services.preferences import group_preferences
from dinnerbell.services.variety import household_today, last_eaten, variety_window

log = logging.getLogger(__name__)
MAX_STEPS = 10
"""More steps than any event can owe at once; a guard against a rule bug looping forever."""


class CallExists(EventRuleViolation):
    pass


def _target(event: Event) -> str:
    return f"event:{event.id}"


# Opening ----------------------------------------------------------------------------------------


async def create_call(
    db: AsyncSession,
    household: Household,
    slot: Slot,
    day: date,
    *,
    opens_at: datetime,
    actor: Member | None,
    now: datetime,
) -> Event:
    """Add a call for ``slot`` on ``day`` and open it if its time has come. The caller commits.
    Raises CallExists if the household already has that call."""
    schedule = slot_schedule(await household_settings(db, household.id), slot)
    events = EventRepository(db)
    if await events.call_for(household.id, slot, day) is not None:
        raise CallExists(f"There's already a {slot.value} call for that day.")
    try:
        event = await events.add(
            household_id=household.id,
            slot=slot,
            day=day,
            state=EventState.SCHEDULED,
            opens_at=opens_at,
            respond_deadline=opens_at + schedule.response_window,
            reminder_minutes=schedule.reminder_minutes,
            vote_minutes=schedule.vote_minutes,
            created_by=actor.id if actor else None,
            now=now,
        )
    except IntegrityError as exc:
        await db.rollback()
        if not is_unique_violation(exc):
            raise
        raise CallExists(f"There's already a {slot.value} call for that day.") from None
    if now >= opens_at:
        return await open_call(db, event, now, actor=actor)
    payload = {"slot": slot.value, "date": day.isoformat(), "opens_at": opens_at.isoformat()}
    await audit(db, household.id, actor, "event.scheduled", _target(event), payload, now)
    return event


async def open_call(db: AsyncSession, event: Event, now: datetime, *, actor: Member | None = None) -> Event:
    event = await EventRepository(db).save(replace(event, state=transition(event.state, EventState.COLLECTING)), now)
    ctx = await load(db, event)
    await publish(db, ctx, cc.call_opened(ctx), now=now)
    payload = {
        "slot": event.slot.value,
        "date": event.day.isoformat(),
        "respond_by": event.respond_deadline.isoformat(),
    }
    await audit(db, event.household_id, actor, "event.opened", _target(event), payload, now)
    return event


async def open_due_calls(db: AsyncSession, household: Household, now: datetime) -> list[Event]:
    """FR-2: post each enabled slot's standing call once its time has come (household time).

    Yesterday is checked too, so a call whose window crosses midnight still opens after a late
    restart. The unique index on (household, slot, date) makes a double post impossible even
    if two ticks race.
    """
    zone = ZoneInfo(household.timezone)
    today = household_today(household, now)
    opened: list[Event] = []
    for slot, schedule in slot_schedules(await household_settings(db, household.id)).items():
        for day in (today - timedelta(days=1), today):
            opens_at = schedule.opens_at(day, zone)
            exists = await EventRepository(db).call_for(household.id, slot, day) is not None
            due = should_open_today(
                enabled=schedule.enabled,
                opens_at=opens_at,
                respond_deadline=opens_at + schedule.response_window,
                now=now,
                exists=exists,
            )
            if not due:
                continue
            try:
                opened.append(await create_call(db, household, slot, day, opens_at=opens_at, actor=None, now=now))
                await db.commit()
            except CallExists:
                await db.rollback()
    return opened


# Reminders --------------------------------------------------------------------------------------


async def _remind(db: AsyncSession, event: Event, now: datetime) -> None:
    """FR-11: one direct message to each active member who hasn't answered, once per call run."""
    ctx = await load(db, event)
    info = cc.event_info(ctx)
    members = sorted(ctx.active_members, key=lambda m: m.display_name.casefold())
    reminders = [
        note.ResponseReminder(info, ctx.person(m.id)) for m in members if needs_reminder(m, ctx.response_of(m.id))
    ]
    await publish(db, ctx, *reminders, now=now)
    await EventRepository(db).save(replace(event, reminder_sent_at=now), now)


# Closing responses ------------------------------------------------------------------------------


def rank_item(ctx: CallContext, item: CatalogItem) -> RankItem:
    return RankItem(
        id=item.id,
        name=item.name,
        modes=item.modes,
        tag_ids=item.tag_ids,
        cuisine_ids=frozenset(ctx.cuisine_ids(item.tag_ids)),
        cook_ids=item.cook_ids,
    )


def guest_needs(ctx: CallContext) -> frozenset[int]:
    """Dietary needs of guests of everyone who might be eating (in, or maybe)."""
    who = veto_participants(ctx.responses)
    return frozenset(tag for r in ctx.responses if r.member_id in who for tag in r.guest_needs)


async def ranking_snapshot(db: AsyncSession, ctx: CallContext, group: GroupPreferences) -> RankingSnapshot:
    """Spec section 7's inputs for this call, as of the call's date."""
    household_id, day = ctx.event.household_id, ctx.event.day
    window = await variety_window(db, household_id, day)
    who = veto_participants(ctx.responses)
    suggesters: dict[int, list[str]] = {}
    for suggestion in ctx.suggestions:
        if suggestion.item_id is not None and suggestion.member_id in who:
            names = suggesters.setdefault(suggestion.item_id, [])
            if ctx.name(suggestion.member_id) not in names:
                names.append(ctx.name(suggestion.member_id))
    return RankingSnapshot(
        items=tuple(rank_item(ctx, item) for item in ctx.items.values() if item.active),
        preferences=group,
        coverage=ctx.coverage,
        weights=weights(ctx.settings),
        window_days=window.days,
        guest_needs=guest_needs(ctx),
        default_modes=ctx.schedule.modes,
        recent_items={i.item_id: i.days_since for i in window.items},
        recent_cuisines={c.tag_id: c.days_since for c in window.cuisines},
        last_eaten_days={i: max((day - d).days, 0) for i, d in (await last_eaten(db, household_id)).items()},
        suggesters={i: tuple(names) for i, names in suggesters.items()},
        member_names={m.id: m.display_name for m in ctx.members.values()},
        cuisine_names={t.id: t.name for t in ctx.tags.values()},
        limit=proposal_count(ctx.settings),
    )


async def _close_responses(db: AsyncSession, event: Event, now: datetime) -> None:
    """The response deadline: rank with everyone who might eat, turn maybes out and tell them,
    then abandon (fewer than two, or nothing fits) or open voting."""
    ctx = await load(db, event)
    group = await group_preferences(db, event.household_id, veto_participants(ctx.responses))
    ranked = rank(await ranking_snapshot(db, ctx, group))
    resolved = resolve_maybes(ctx.responses)
    for response in resolved:
        await ResponseRepository(db).save(event.household_id, event.id, response, now)
    info = cc.event_info(ctx)
    await publish(db, ctx, *(note.MaybeResolved(info, ctx.person(r.member_id)) for r in resolved), now=now)
    if head_count(ctx.responses) < MIN_HEAD_COUNT:  # maybes never count toward the head count
        await abandon(db, event, EndReason.TOO_FEW, now)
        return
    if not ranked:
        await abandon(db, event, EndReason.NO_COVERAGE, now)
        return
    proposals = ProposalRepository(db)
    for position, choice in enumerate(ranked, start=1):
        await proposals.add(
            household_id=event.household_id,
            event_id=event.id,
            item_id=choice.item_id,
            mode=choice.mode,
            rank=position,
            score=choice.score,
            reasons=choice.reasons,
            source=ProposalSource.SYSTEM,
            now=now,
        )
    vote_deadline = event.respond_deadline + timedelta(minutes=event.vote_minutes)
    voting = replace(event, state=transition(event.state, EventState.VOTING), vote_deadline=vote_deadline)
    voting = await EventRepository(db).save(voting, now)
    ctx = await load(db, voting)
    await publish(db, ctx, note.ProposalsReady(cc.event_info(ctx), cc.proposals(ctx), ctx.head_count), now=now)
    await publish(db, ctx, cc.call_updated(ctx), now=now)
    payload = {"proposals": [{"item_id": r.item_id, "mode": r.mode.value, "score": r.score} for r in ranked]}
    await audit(db, event.household_id, None, "event.voting", _target(event), payload, now)


async def abandon(db: AsyncSession, event: Event, reason: EndReason, now: datetime) -> None:
    """Spec 5.4: the on-your-own message. Nothing is assigned or logged."""
    ended = replace(event, state=transition(event.state, EventState.ABANDONED), end_reason=reason)
    ended = await EventRepository(db).save(ended, now)
    ctx = await load(db, ended)
    await publish(db, ctx, note.CallAbandoned(cc.event_info(ctx), reason, ctx.head_count), now=now)
    await publish(db, ctx, cc.call_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), now=now)
    await audit(db, event.household_id, None, "event.abandoned", _target(event), {"reason": reason.value}, now)


# Deciding ---------------------------------------------------------------------------------------


def misfit(ctx: CallContext, proposal: Proposal, group: GroupPreferences) -> str | None:
    """Why ``proposal`` can't be decided for this call (FR-17, FR-28), or None if it can.

    The veto message never says whose veto it is.
    """
    needs = guest_needs(ctx)
    coverage = ctx.coverage
    if proposal.item_id is not None:
        item = ctx.items.get(proposal.item_id)
        if item is None:
            return "That isn't in the catalog anymore."
        if group.vetoes(rank_item(ctx, item)):
            return "That won't work for someone who's in."
        if not needs <= item.tag_ids:
            return "That doesn't meet a guest's dietary needs."
        if proposal.mode not in item.modes:
            return f"{item.name} isn't available by {proposal.mode.label.lower()}."
        cooks = item.cook_ids
    else:
        if proposal.free_cuisine_id in group.vetoed_tags:
            return "That won't work for someone who's in."
        if needs:
            return "A guest has dietary needs, so pick something from the catalog whose tags show them."
        cooks = frozenset()
    if not coverage.covers(proposal.mode, cooks):
        return f"Nobody who's in offered to {MODE_ROLE[proposal.mode].value} for that."
    return None


async def _catalog_free_text(db: AsyncSession, ctx: CallContext, proposal: Proposal, now: datetime) -> CatalogItem:
    """FR-13: a free-text proposal becomes a catalog item when it wins."""
    household_id = ctx.event.household_id
    kind = ItemKind.HOME if proposal.mode is Mode.COOK else ItemKind.RESTAURANT
    repo = CatalogRepository(db)
    name = proposal.free_text or "Untitled"
    item = await repo.item_by_name(household_id, kind, name)
    if item is None:
        tags = frozenset({proposal.free_cuisine_id}) if proposal.free_cuisine_id is not None else frozenset()
        values = ItemValues(
            kind=kind,
            name=name,
            modes=frozenset({proposal.mode}),
            tag_ids=tags,
            cook_ids=frozenset(),
            url=None,
            order_url=None,
            notes=None,
            default_servings=None,
            active=True,
        )
        item = await repo.add_item(household_id, values, created_by=proposal.proposed_by, now=now)
        payload = {"name": name, "kind": kind.value, "from_proposal": proposal.id}
        await audit(db, household_id, None, "catalog.item_created", f"item:{item.id}", payload, now)
    if not any(p.item_id == item.id and p.mode is proposal.mode for p in ctx.proposals):
        await ProposalRepository(db).link_item(household_id, proposal.id, item.id)
    return item


async def decide(
    db: AsyncSession, event: Event, now: datetime, *, locked: Proposal | None = None, actor: Member | None = None
) -> None:
    """FR-15 and FR-16: decide the call, by ``locked`` or by the vote, and assign roles.
    Abandons the call when no proposal can be covered (spec 5.4)."""
    ctx = await load(db, event)
    group = await group_preferences(db, event.household_id, veto_participants(ctx.responses))
    if locked is not None:
        problem = misfit(ctx, locked, group)
        if problem is not None:
            raise EventRuleViolation(problem)
        winner = locked
    else:
        tallies = [
            ProposalTally(p.id, p.rank, misfit(ctx, p, group) is None, frozenset(ctx.votes.get(p.id, [])))
            for p in ctx.proposals
        ]
        admins = frozenset(m.id for m in ctx.active_members if m.is_admin)
        chosen = choose_winner(tallies, coordinator_id=event.coordinator_id, admin_ids=admins)
        found = ctx.proposal(chosen.id) if chosen is not None else None
        if found is None:
            await abandon(db, event, EndReason.NO_COVERAGE, now)
            return
        winner = found
    item = ctx.items[winner.item_id] if winner.item_id is not None else await _catalog_free_text(db, ctx, winner, now)
    meal = meal_time(ctx.responses, day=event.day, zone=ctx.zone, default=ctx.schedule.meal_time, not_before=now)
    decided = replace(
        event,
        state=transition(event.state, EventState.DECIDED),
        decided_item_id=item.id,
        decided_mode=winner.mode,
        meal_time=meal,
        decided_at=now,
    )
    decided = await EventRepository(db).save(decided, now)
    roles = assign_roles(winner.mode, ctx.coverage.volunteers(winner.mode, item.cook_ids), event.coordinator_id)
    repo = AssignmentRepository(db)
    assigned = [await repo.assign(event.household_id, event.id, role, who, now) for role, who in roles.items()]
    await _announce_decision(
        db, decided, assigned_ids=[a.id for a in assigned], actor=actor if locked else None, now=now
    )
    payload = {
        "how": "locked" if locked else "vote",
        "proposal_id": winner.id,
        "item_id": item.id,
        "mode": winner.mode.value,
        "meal_time": meal.isoformat(),
        "assignments": {role.value: who for role, who in roles.items()},
    }
    await audit(db, event.household_id, actor if locked else None, "event.decided", _target(event), payload, now)


async def _announce_decision(
    db: AsyncSession, event: Event, *, assigned_ids: list[int], actor: Member | None, now: datetime
) -> None:
    ctx = await load(db, event)
    decision = cc.decision_info(ctx)
    if decision is None:  # pragma: no cover - decide() always stores a winner and a meal time
        return
    info = cc.event_info(ctx)
    locked_by = ctx.person(actor.id) if actor else None
    dms = [
        note.AssignmentMade(info, a.id, a.role, ctx.person(a.member_id), decision)
        for a in ctx.assignments
        if a.id in assigned_ids
    ]
    await publish(db, ctx, note.DecisionMade(info, decision, locked_by), *dms, now=now)
    await publish(db, ctx, cc.proposals_updated(ctx), cc.call_updated(ctx), now=now)


# Done -------------------------------------------------------------------------------------------


async def finish(db: AsyncSession, event: Event, now: datetime, *, actor: Member | None = None) -> None:
    """FR-21: mark the call done and log the meal, linked by ``event_id``, at most once."""
    ctx = await load(db, event)
    done = await EventRepository(db).save(
        replace(event, state=transition(event.state, EventState.DONE), done_at=now), now
    )
    meals = MealLogRepository(db)
    item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
    if item is not None and await meals.for_event(event.household_id, event.id) is None:
        cuisines = tuple(TagRef(t, ctx.tags[t].name) for t in sorted(ctx.cuisine_ids(item.tag_ids)))
        people = sorted((MemberRef(r.member_id, ctx.name(r.member_id)) for r in ctx.participants), key=_by_name)
        await meals.add(
            household_id=event.household_id,
            day=event.day,
            slot=event.slot,
            item_id=item.id,
            cuisines=cuisines,
            participants=tuple(people),
            logged_by=None,
            now=now,
            event_id=event.id,
        )
    ctx = await load(db, done)
    await publish(db, ctx, *cc.decision_updated(ctx), cc.call_updated(ctx), now=now)
    await audit(db, event.household_id, actor, "event.done", _target(event), {"item_id": event.decided_item_id}, now)


def _by_name(ref: MemberRef) -> str:
    return ref.name.casefold()


# The tick ---------------------------------------------------------------------------------------

Handler = Callable[[AsyncSession, Event, datetime], Awaitable[object]]


async def _decide_by_vote(db: AsyncSession, event: Event, now: datetime) -> None:
    await decide(db, event, now)


async def _mark_done(db: AsyncSession, event: Event, now: datetime) -> None:
    await finish(db, event, now)


HANDLERS: dict[Step, Handler] = {
    Step.OPEN: open_call,
    Step.REMIND: _remind,
    Step.CLOSE_RESPONSES: _close_responses,
    Step.CLOSE_VOTING: _decide_by_vote,
    Step.MARK_DONE: _mark_done,
}


async def advance(db: AsyncSession, household_id: int, event_id: int, now: datetime) -> list[Step]:
    """Apply every step ``event_id`` owes at ``now``, each in its own transaction.

    The cheap read first means an event with nothing due costs no write.
    """
    applied: list[Step] = []
    for _ in range(MAX_STEPS):
        current = await EventRepository(db).get(household_id, event_id)
        if current is None or due_step(current, now) is None:
            break
        async with locked_event(db, household_id, event_id, now) as event:
            step = due_step(event, now)
            if step is not None:
                await HANDLERS[step](db, event, now)
        if step is None:  # pragma: no cover - another worker took the step between the read and the lock
            break
        applied.append(step)
    return applied


async def tick(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> list[Step]:
    """One scheduler tick for every household. Safe to run twice, late, or concurrently."""
    async with sessionmaker() as db:
        households = await HouseholdRepository(db).all()
    applied: list[Step] = []
    for household in households:
        async with sessionmaker() as db:
            try:
                await open_due_calls(db, household, now)
            except Exception:
                log.exception("could not open calls", extra={"household_id": household.id})
            for event_id in await EventRepository(db).open_ids(household.id):
                try:
                    applied += await advance(db, household.id, event_id, now)
                except Exception:
                    log.exception("could not advance call", extra={"event_id": event_id})
    return applied
