# SPDX-License-Identifier: AGPL-3.0-or-later
"""The call lifecycle: open, remind, close responses and rank, decide, and mark done
(spec 5.1, 5.4, 7, 8).

The scheduler's tick calls ``tick`` every 30 seconds. ``tick`` opens each household's standing
calls once their time comes and asks every open event for the step it owes (``domain.events.
due_step``), applying steps until none is due. A restart or downtime past several deadlines is
caught up on the next tick, one transaction per step. Members' actions reuse ``decide`` (lock),
``finish`` (mark done), ``move_window`` (close now), and ``reopen`` (re-run and restart).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
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
    Window,
    assign_roles,
    check_restart,
    check_run,
    check_window_change,
    choose_winner,
    head_count,
    meal_time,
    needs_reminder,
    posted_before_its_day,
    resolve_maybes,
    shifted_deadline,
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
from dinnerbell.services import feedback
from dinnerbell.services.call_context import CallContext, load, locked_event, publish
from dinnerbell.services.common import audit, is_unique_violation
from dinnerbell.services.errors import NotFound
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
    restart_any_time: bool = False,
) -> Event:
    """Add a call for ``slot`` on ``day`` and open it if its time has come. The caller commits.

    A member (``actor``) starting a call that was called off for that slot and day starts it
    again instead (``restart``, with ``restart_any_time`` as its ``any_time``). Raises
    CallExists if the household already has that call."""
    schedule = slot_schedule(await household_settings(db, household.id), slot)
    events = EventRepository(db)
    existing = await events.call_for(household.id, slot, day)
    if existing is not None:
        if existing.state is EventState.CANCELLED and actor is not None:
            return await restart(db, existing, actor=actor, now=now, any_time=restart_any_time)
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
    """Post ``event`` and open responses: the tick's ``Step.OPEN`` at the call's time, or a
    member posting it early (``calls.post_now``), whose payload keeps when it was due. Either
    way responses open now (``opens_at``), and the deadlines stay as scheduled."""
    early = event.opens_at if now < event.opens_at else None
    opened = replace(event, state=transition(event.state, EventState.COLLECTING), opens_at=min(event.opens_at, now))
    event = await EventRepository(db).save(opened, now)
    ctx = await load(db, event)
    await publish(db, ctx, cc.call_opened(ctx), now=now)
    payload = {
        "slot": event.slot.value,
        "date": event.day.isoformat(),
        "respond_by": event.respond_deadline.isoformat(),
    }
    if early is not None:
        payload["scheduled_for"] = early.isoformat()
    await audit(db, event.household_id, actor, "event.opened", _target(event), payload, now)
    return event


async def open_due_calls(db: AsyncSession, household: Household, now: datetime) -> list[Event]:
    """FR-2: post each enabled slot's standing call for today once its time has come
    (household time). Only today: a call is stale at midnight after its day, so opening
    yesterday's would post it only to expire it. The unique index on (household, slot, date)
    makes a double post impossible even if two ticks race.
    """
    zone = ZoneInfo(household.timezone)
    today = household_today(household, now)
    opened: list[Event] = []
    for slot, schedule in slot_schedules(await household_settings(db, household.id)).items():
        opens_at = schedule.opens_at(today, zone)
        exists = await EventRepository(db).call_for(household.id, slot, today) is not None
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
            opened.append(await create_call(db, household, slot, today, opens_at=opens_at, actor=None, now=now))
            await db.commit()
        except CallExists:
            await db.rollback()
    return opened


async def restart(
    db: AsyncSession, event: Event, *, actor: Member, run: int | None = None, now: datetime, any_time: bool = False
) -> Event:
    """FR-3 and FR-56: any member starts a called-off call again, as a fresh run of the same
    row (a slot has one call per date). They become its starter, and it starts with no
    coordinator. The re-run's too-late rule applies, unless ``any_time`` (a test instance, #60).
    ``run``, when given, is the run the actor saw (``check_run``). Takes the event's lock; the
    caller commits. Audited."""
    if not await EventRepository(db).lock(event.household_id, event.id, now):  # pragma: no cover - just read
        raise NotFound("No such call.")
    ctx = await load(db, event)  # re-read under the lock
    check_run(ctx.event, run)
    if ctx.event.state is not EventState.CANCELLED:
        raise CallExists(f"There's already a {event.slot.value} call for that day.")
    today = household_today(ctx.household, now)
    check_restart(ctx.event, today, now=now, too_late_after=ctx.too_late_for(today), any_time=any_time)
    fresh = await reopen(db, ctx, today=today, now=now, started_by=actor)
    payload = {"from": EventState.CANCELLED.value, "run": fresh.run, "respond_by": fresh.respond_deadline.isoformat()}
    await audit(db, event.household_id, actor, "event.restarted", _target(event), payload, now)
    return fresh


async def reopen(
    db: AsyncSession, ctx: CallContext, *, today: date, now: datetime, started_by: Member | None = None
) -> Event:
    """Start ``ctx.event`` again with fresh windows from the current schedule: now, for today's
    call, or at the slot's call time for a later day (FR-3 timing). A later day's call that was
    posted early (``posted_before_its_day``) stays posted: it opens now, with that day's
    response deadline, as ``calls.post_now`` leaves it (issue #59). The caller holds the
    event's lock, checks ``check_rerun``, and audits.

    Responses stay; maybes the deadline turned out are maybes again. Proposals, votes, the
    decision, assignments, and who called it off are cleared. ``started_by``, when given (a
    member starting a called-off call again), becomes the starter, and the coordinator is
    cleared: whoever coordinated the call that was called off may not be around for this one.
    An admin re-run keeps both. The run number goes up, so the channel posts a new call message and
    the reminder can go out once more.
    """
    event, household_id = ctx.event, ctx.event.household_id
    await ProposalRepository(db).clear(household_id, event.id)
    await AssignmentRepository(db).clear(household_id, event.id)
    await ResponseRepository(db).restore_maybes(household_id, event.id, now)
    schedule = ctx.schedule
    due = schedule.opens_at(event.day, ctx.zone) if event.day > today else now
    scheduled = event.day > today and not posted_before_its_day(event, ctx.zone)
    opens_at = due if scheduled else now
    fresh = replace(
        event,
        state=transition(event.state, EventState.SCHEDULED if scheduled else EventState.COLLECTING),
        run=event.run + 1,
        created_by=started_by.id if started_by is not None else event.created_by,
        coordinator_id=None if started_by is not None else event.coordinator_id,
        opens_at=opens_at,
        respond_deadline=due + schedule.response_window,
        reminder_minutes=schedule.reminder_minutes,
        vote_minutes=schedule.vote_minutes,
        vote_deadline=None,
        reminder_sent_at=None,
        decided_item_id=None,
        decided_mode=None,
        meal_time=None,
        decided_at=None,
        end_reason=None,
        locked_by=None,
        ended_by=None,
        end_note=None,
    )
    fresh = await EventRepository(db).save(fresh, now)
    if not scheduled:  # a scheduled call is posted when the tick opens it
        opened = await load(db, fresh)
        await publish(db, opened, cc.call_opened(opened), now=now)
    return fresh


# Deadlines --------------------------------------------------------------------------------------


async def move_window(
    db: AsyncSession, event: Event, window: Window, minutes: int | None, now: datetime
) -> tuple[datetime, datetime]:
    """Move the open ``window``'s deadline by ``minutes``, or to now when None, and update the
    messages that show it. Returns the old and new deadlines. The caller holds the event's
    lock, checks who may, audits, and calls ``advance`` after its commit, so a deadline moved
    into the past closes the window at once."""
    current = check_window_change(event, window)
    zone = (await load(db, event)).zone
    midnight = datetime.combine(event.day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
    moved = shifted_deadline(current, minutes, now, latest=midnight)
    changes = {"respond_deadline": moved} if window is Window.RESPOND else {"vote_deadline": moved}
    event = await EventRepository(db).save(replace(event, **changes), now)  # type: ignore[arg-type]
    ctx = await load(db, event)
    await publish(db, ctx, cc.call_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), now=now)
    return current, moved


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
    """Dietary needs of guests of everyone who might be eating (in, or maybe). Needs left on a
    response with no guests don't count."""
    who = veto_participants(ctx.responses)
    return frozenset(tag for r in ctx.responses if r.member_id in who and r.guests for tag in r.guest_needs)


async def ranking_snapshot(db: AsyncSession, ctx: CallContext, group: GroupPreferences) -> RankingSnapshot:
    """Spec section 7's inputs for this call, as of the call's date."""
    household_id, day = ctx.event.household_id, ctx.event.day
    window = await variety_window(db, household_id, day)
    who = veto_participants(ctx.responses)
    suggesters: dict[int, dict[int, str]] = {}  # item -> {member ID: name}, credited once per member
    for suggestion in ctx.suggestions:
        if suggestion.item_id is not None and suggestion.member_id in who:
            suggesters.setdefault(suggestion.item_id, {})[suggestion.member_id] = ctx.name(suggestion.member_id)
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
        suggesters={i: tuple(by_member.values()) for i, by_member in suggesters.items()},
        member_names={m.id: m.display_name for m in ctx.members.values()},
        cuisine_names={t.id: t.name for t in ctx.tags.values()},
        limit=proposal_count(ctx.settings),
    )


async def _close_responses(db: AsyncSession, event: Event, now: datetime) -> None:
    """The response deadline: rank with everyone who might eat, turn maybes out and tell them,
    then abandon (fewer than two, or nothing fits) or open voting. Before the deadline,
    because everyone answered (FR-56), the deadline moves to now first, so voting gets its
    usual length from now, as when the coordinator closes the window."""
    if now < event.respond_deadline:
        early = {"window": Window.RESPOND.value, "from": event.respond_deadline.isoformat(), "to": now.isoformat()}
        event = await EventRepository(db).save(replace(event, respond_deadline=now), now)
        await audit(db, event.household_id, None, "event.everyone_answered", _target(event), early, now)
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


async def expire(db: AsyncSession, event: Event, now: datetime) -> None:
    """End a call it's too late to finish (``EndReason.MISSED``), quietly: no new channel post
    and no direct messages. A call that was posted gets its messages updated to show it ended;
    a scheduled call that never opened sends nothing (FR-2)."""
    posted = event.state is not EventState.SCHEDULED
    ended = replace(event, state=transition(event.state, EventState.ABANDONED), end_reason=EndReason.MISSED)
    ended = await EventRepository(db).save(ended, now)
    if posted:
        ctx = await load(db, ended)
        await publish(db, ctx, cc.call_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), now=now)
    payload = {"reason": EndReason.MISSED.value, "from": event.state.value}
    await audit(db, event.household_id, None, "event.abandoned", _target(event), payload, now)


async def abandon(db: AsyncSession, event: Event, reason: EndReason, now: datetime) -> None:
    """Spec 5.4: the on-your-own message. Nothing is assigned or logged."""
    ended = replace(event, state=transition(event.state, EventState.ABANDONED), end_reason=reason)
    ended = await EventRepository(db).save(ended, now)
    ctx = await load(db, ended)
    await publish(db, ctx, note.CallAbandoned(cc.event_info(ctx), reason, ctx.head_count), now=now)
    await publish(db, ctx, cc.call_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), now=now)
    await audit(db, event.household_id, None, "event.abandoned", _target(event), {"reason": reason.value}, now)


# Deciding ---------------------------------------------------------------------------------------


DOESNT_FIT = "That doesn't work for everyone who's in tonight."
"""The one message for any veto or guest dietary need. It never names the tag, the item's
vetoed attribute, or how many people object, so a proposer can't probe proposal by proposal to
learn who vetoed what (FR-29)."""


def misfit(ctx: CallContext, proposal: Proposal, group: GroupPreferences) -> str | None:
    """Why ``proposal`` can't be decided for this call (FR-17, FR-28), or None if it can.

    Private reasons (vetoes and guests' dietary needs) all give ``DOESNT_FIT``. Coverage and
    availability reasons stay specific, because they reveal nothing private.
    """
    needs = guest_needs(ctx)
    if proposal.item_id is not None:
        item = ctx.items.get(proposal.item_id)
        if item is None or not item.active:
            return "That isn't in the catalog anymore."
        if group.vetoes(rank_item(ctx, item)) or not needs <= item.tag_ids:
            return DOESNT_FIT
        if proposal.mode not in item.modes:
            return f"{item.name} isn't available by {proposal.mode.label.lower()}."
        cooks = item.cook_ids
    else:
        # A new item's only known tag is its cuisine, so it can't show it meets a dietary need.
        if proposal.free_cuisine_id in group.vetoed_tags or needs:
            return DOESNT_FIT
        cooks = frozenset()
    if not ctx.coverage.covers(proposal.mode, cooks):
        return f"Nobody who's in offered to {MODE_ROLE[proposal.mode].value} for that."
    return None


async def _as_catalog_item(db: AsyncSession, ctx: CallContext, proposal: Proposal) -> Proposal:
    """A free-text proposal as the catalog item of the same name, when one exists, so its tags
    and item vetoes are checked before it can win; otherwise the proposal unchanged."""
    if proposal.item_id is not None:
        return proposal
    kind = ItemKind.HOME if proposal.mode is Mode.COOK else ItemKind.RESTAURANT
    item = await CatalogRepository(db).item_by_name(ctx.event.household_id, kind, proposal.free_text or "")
    return proposal if item is None else replace(proposal, item_id=item.id)


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
    # First, before anything is written: a meal that would land after the call's day can't be
    # decided. A lock says so; the vote deadline ends the call quietly instead.
    meal = meal_time(ctx.responses, day=event.day, zone=ctx.zone, default=ctx.schedule.meal_time, not_before=now)
    if meal.astimezone(ctx.zone).date() != event.day:
        if locked is not None:
            raise EventRuleViolation("It's too late to decide: the meal would be after midnight.")
        await expire(db, event, now)
        return
    group = await group_preferences(db, event.household_id, veto_participants(ctx.responses))
    # Check free text as the catalog item it will become, if one by that name exists by now.
    checked = {p.id: await _as_catalog_item(db, ctx, p) for p in ctx.proposals}
    if locked is not None:
        problem = misfit(ctx, checked.get(locked.id, locked), group)
        if problem is not None:
            raise EventRuleViolation(problem)
        winner = locked
    else:
        tallies = [
            ProposalTally(p.id, p.rank, misfit(ctx, checked[p.id], group) is None, frozenset(ctx.votes.get(p.id, [])))
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
    decided = replace(
        event,
        state=transition(event.state, EventState.DECIDED),
        decided_item_id=item.id,
        decided_mode=winner.mode,
        meal_time=meal,
        decided_at=now,
        locked_by=actor.id if locked is not None and actor is not None else None,
    )
    decided = await EventRepository(db).save(decided, now)
    roles = assign_roles(winner.mode, ctx.coverage.volunteers(winner.mode, item.cook_ids), event.coordinator_id)
    repo = AssignmentRepository(db)
    assigned = [await repo.assign(event.household_id, event.id, role, who, now) for role, who in roles.items()]
    await _announce_decision(db, decided, assigned_ids=[a.id for a in assigned], now=now)
    payload = {
        "how": "locked" if locked else "vote",
        "proposal_id": winner.id,
        "item_id": item.id,
        "mode": winner.mode.value,
        "meal_time": meal.isoformat(),
        "assignments": {role.value: who for role, who in roles.items()},
    }
    await audit(db, event.household_id, actor if locked else None, "event.decided", _target(event), payload, now)


async def _announce_decision(db: AsyncSession, event: Event, *, assigned_ids: list[int], now: datetime) -> None:
    ctx = await load(db, event)
    decision = cc.decision_info(ctx)
    if decision is None:  # pragma: no cover - decide() always stores a winner and a meal time
        return
    first: dict[int, int] = {}  # one direct message per member, keyed by their first assignment
    for a in sorted(ctx.assignments, key=lambda a: a.id):
        if a.id in assigned_ids:
            first.setdefault(a.member_id, a.id)
    dms = [cc.assignment_made(ctx, member_id, assignment_id, decision) for member_id, assignment_id in first.items()]
    await publish(db, ctx, note.DecisionMade(cc.event_info(ctx), decision, cc.locked_by(ctx)), *dms, now=now)
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
    reminders = feedback.reminders(ctx, item.name) if item is not None else []  # FR-48: only those who asked
    await publish(db, ctx, *cc.decision_updated(ctx), cc.call_updated(ctx), *reminders, now=now)
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
    Step.EXPIRE: expire,
    Step.OPEN: open_call,
    Step.REMIND: _remind,
    Step.CLOSE_RESPONSES: _close_responses,
    Step.CLOSE_VOTING: _decide_by_vote,
    Step.MARK_DONE: _mark_done,
}


async def advance(db: AsyncSession, household_id: int, event_id: int, now: datetime) -> list[Step]:
    """Apply every step ``event_id`` owes at ``now``, each in its own transaction.

    The cheap read first means an event with nothing due costs no write. A call that's gone
    (discarded) owes nothing.
    """
    applied: list[Step] = []
    for _ in range(MAX_STEPS):
        current = await EventRepository(db).get(household_id, event_id)
        if current is None or await cc.due(db, current, now) is None:
            break
        try:
            async with locked_event(db, household_id, event_id, now) as event:
                step = await cc.due(db, event, now)
                if step is not None:
                    await HANDLERS[step](db, event, now)
        except NotFound:
            if await EventRepository(db).get(household_id, event_id) is None:
                break  # discarded since the read, on a test instance (#60): nothing is owed
            raise
        if step is None:  # pragma: no cover - another worker took the step between the read and the lock
            break
        applied.append(step)
    return applied


async def tick(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> list[Step]:
    """One scheduler tick for every household. Safe to run twice, late, or concurrently.

    A failure is logged and rolled back, so the next step's commit can't carry its half-done
    writes, and one household or call failing never stops the others.
    """
    async with sessionmaker() as db:
        households = await HouseholdRepository(db).all()
    applied: list[Step] = []
    for household in households:
        async with sessionmaker() as db:
            try:
                await open_due_calls(db, household, now)
            except Exception:
                await db.rollback()
                log.exception("could not open calls", extra={"household_id": household.id})
            try:
                event_ids = await EventRepository(db).open_ids(household.id)
            except Exception:
                await db.rollback()
                log.exception("could not list calls", extra={"household_id": household.id})
                continue
            for event_id in event_ids:
                try:
                    applied += await advance(db, household.id, event_id, now)
                except Exception:
                    await db.rollback()
                    log.exception("could not advance call", extra={"event_id": event_id})
    return applied
