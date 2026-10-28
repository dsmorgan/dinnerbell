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
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import AssignmentRepository, EventRepository, ProposalRepository, ResponseRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import DEFAULT_MEALS, CatalogItem, ItemKind, Mode
from dinnerbell.domain.events import (
    MAX_PROPOSALS,
    MIN_HEAD_COUNT,
    MODE_ROLE,
    AssignmentRole,
    EndReason,
    Event,
    EventRuleViolation,
    EventState,
    Proposal,
    ProposalSource,
    ProposalTally,
    Step,
    TieHow,
    Window,
    assign_roles,
    check_restart,
    check_run,
    check_window_change,
    clean_cancel_note,
    counted_votes,
    decide_winner,
    head_count,
    meal_time,
    needs_reminder,
    posted_before_its_day,
    resolve_maybes,
    roles_unclear,
    shifted_deadline,
    should_open_today,
    transition,
)
from dinnerbell.domain.guests import tie_breaker
from dinnerbell.domain.household import Household
from dinnerbell.domain.meals import MemberRef, Slot, TagRef
from dinnerbell.domain.members import Member
from dinnerbell.domain.offers import replaces_call
from dinnerbell.domain.ranking import GroupPreferences, RankingSnapshot, RankItem, rank, ranking_seed
from dinnerbell.domain.schedule import (
    proposal_count,
    roles_choice_minutes,
    slot_schedule,
    slot_schedules,
    temperature,
    weights,
)
from dinnerbell.services import activity, feedback
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import CallContext, load, locked_event, publish
from dinnerbell.services.common import audit, is_unique_violation
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import group_preferences
from dinnerbell.services.variety import household_today, last_eaten, variety_window

log = logging.getLogger(__name__)
MAX_STEPS = 10
"""More steps than any event can owe at once; a guard against a rule bug looping forever."""


class CallExists(EventRuleViolation):
    code = "call_exists"


class OfferedInstead(EventRuleViolation):
    """A cooking offer stands in for that slot's call on that day (FR-5)."""

    code = "offered_instead"


async def cooking_offer(db: AsyncSession, household_id: int, slot: Slot, day: date) -> Event | None:
    """The live cooking offer that replaces ``slot``'s call on ``day``, if there is one (FR-5)."""
    offers = await EventRepository(db).offers_for(household_id, slot, day)
    return next((o for o in offers if replaces_call(o)), None)


async def check_no_cooking_offer(db: AsyncSession, household_id: int, slot: Slot, day: date) -> None:
    """Refuse a call for ``slot`` on ``day`` while a live cooking offer stands in for it (FR-5):
    starting one, starting a called-off one again, or an admin's re-run."""
    offer = await cooking_offer(db, household_id, slot, day)
    if offer is not None:
        cook = await MemberRepository(db).get(household_id, offer.created_by) if offer.created_by else None
        who = cook.display_name if cook is not None else "Someone"
        raise OfferedInstead(
            f"{who} is cooking {slot.value} that day, so there's no call. Join the offer instead, "
            "or ask them to call it off first."
        )


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
    if actor is not None and existing is None:  # ``restart`` checks a called-off one
        await check_no_cooking_offer(db, household.id, slot, day)
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
    await publish(db, ctx, cc.call_opened(ctx), *cc.guest_invitations(ctx), now=now)
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
        # Check and create under the write lock, so a cooking offer posted at the same moment
        # (``offers.post_offer`` takes it too) either sees the call or keeps it from opening.
        async with write_transaction(db):
            exists = await EventRepository(db).call_for(household.id, slot, today) is not None
            # A cooking offer stands in for the call (FR-5). Called off before the response
            # deadline, it frees the slot, and the next tick posts the call after all.
            exists = exists or await cooking_offer(db, household.id, slot, today) is not None
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
                event = await create_call(db, household, slot, today, opens_at=opens_at, actor=None, now=now)
                activity.action(db, "opened", event, None, started=True)
                await db.commit()
                opened.append(event)
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
    await check_no_cooking_offer(db, event.household_id, event.slot, event.day)
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
        shuffles=0,
        shuffle_note=None,
        roles_due_at=None,
        tie_how=None,
        tie_by=None,
    )
    fresh = await EventRepository(db).save(fresh, now)
    if not scheduled:  # a scheduled call is posted when the tick opens it
        opened = await load(db, fresh)
        await publish(db, opened, cc.call_opened(opened), *cc.guest_invitations(opened), now=now)
    return fresh


async def _open_scheduled(db: AsyncSession, event: Event, now: datetime) -> None:
    """The tick's ``Step.OPEN``. A call scheduled for a slot and day that a live cooking offer
    now stands in for (FR-5) is called off quietly instead of posted next to it: it was never
    posted, so nobody is told. ``offers.post_offer`` replaces a scheduled call itself; this is
    the backstop for any other way one got scheduled. Audited as ``event.replaced_by_offer``."""
    offer = await cooking_offer(db, event.household_id, event.slot, event.day)
    if offer is None:
        await open_call(db, event, now)
        return
    cook = await MemberRepository(db).get(event.household_id, offer.created_by) if offer.created_by else None
    note_text = clean_cancel_note(f"{cook.display_name} is cooking instead.") if cook is not None else None
    ended = replace(
        event, state=transition(event.state, EventState.CANCELLED), ended_by=offer.created_by, end_note=note_text
    )
    await EventRepository(db).save(ended, now)
    payload = {"offer_id": offer.id, "from": event.state.value}
    await audit(db, event.household_id, None, "event.replaced_by_offer", _target(event), payload, now)


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
    # Members, and registered guests invited to the call (#38); guests' reminders have In and Out only.
    people = [*ctx.active_members, *(g for g in ctx.guests if g.is_registered_guest)]
    members = sorted(people, key=lambda m: m.display_name.casefold())
    reminders = [
        note.ResponseReminder(info, ctx.person(m.id), m.is_guest)
        for m in members
        if needs_reminder(m, ctx.response_of(m.id))
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
        meals=item.meals,
    )


def guest_needs(ctx: CallContext) -> frozenset[int]:
    """``CallContext.guest_needs``: what every pick must meet for the guests who might eat."""
    return ctx.guest_needs


async def ranking_snapshot(
    db: AsyncSession,
    ctx: CallContext,
    group: GroupPreferences,
    *,
    taken: frozenset[int] = frozenset(),
    shuffle: int = 0,
) -> RankingSnapshot:
    """Spec section 7's inputs for this call, as of the call's date. ``taken`` holds the items
    that are picks already, which ranking leaves out (#77). The seed is this run's, so ranking
    the run again gives the same picks and a restart draws again (#78). ``shuffle`` is the
    coordinator's shuffle count (#87): each shuffle draws with the next seed."""
    household_id, day = ctx.event.household_id, ctx.event.day
    window = await variety_window(db, household_id, day)
    who = ctx.veto_people
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
        meal=ctx.event.slot.meal,
        guest_needs=guest_needs(ctx),
        default_modes=ctx.schedule.modes,
        recent_items={i.item_id: i.days_since for i in window.items},
        recent_cuisines={c.tag_id: c.days_since for c in window.cuisines},
        last_eaten_days={i: max((day - d).days, 0) for i, d in (await last_eaten(db, household_id)).items()},
        suggesters={i: tuple(by_member.values()) for i, by_member in suggesters.items()},
        member_names={m.id: m.display_name for m in ctx.members.values()},
        cuisine_names={t.id: t.name for t in ctx.tags.values()},
        limit=proposal_count(ctx.settings),
        taken=taken,
        temperature=temperature(ctx.settings),
        seed=ranking_seed(household_id, ctx.event.id, ctx.event.run, shuffle),
    )


async def _close_responses(db: AsyncSession, event: Event, now: datetime) -> None:
    """The response deadline: rank with everyone who might eat, turn maybes out and tell them,
    then abandon (fewer than two, or nothing fits) or open voting. Picks that offers added while
    responses were open (FR-5) stay if they still fit, after the ranked ones; ranking leaves their
    places out and still proposes its usual number (#77). Before the deadline,
    because everyone answered (FR-56), the deadline moves to now first, so voting gets its
    usual length from now, as when the coordinator closes the window."""
    if now < event.respond_deadline:
        early = {"window": Window.RESPOND.value, "from": event.respond_deadline.isoformat(), "to": now.isoformat()}
        event = await EventRepository(db).save(replace(event, respond_deadline=now), now)
        await audit(db, event.household_id, None, "event.everyone_answered", _target(event), early, now)
    ctx = await load(db, event)
    group = await group_preferences(db, event.household_id, ctx.veto_people)
    attached = await _keep_attached(db, ctx, group, now)
    taken = frozenset(p.item_id for p in attached if p.item_id is not None)
    snapshot = await ranking_snapshot(db, ctx, group, taken=taken)
    ranked = rank(snapshot)[: max(MAX_PROPOSALS - len(attached), 0)]  # FR-13: at most 10 picks in all
    resolved = resolve_maybes(ctx.responses)
    for response in resolved:
        await ResponseRepository(db).save(event.household_id, event.id, response, now)
    info = cc.event_info(ctx)
    await publish(db, ctx, *(note.MaybeResolved(info, ctx.person(r.member_id)) for r in resolved), now=now)
    if head_count(ctx.responses) < MIN_HEAD_COUNT:  # maybes never count toward the head count
        await abandon(db, event, EndReason.TOO_FEW, now)
        return
    if not ranked and not attached:
        # Would anything have fit without the dietary needs? Then they're why nothing did.
        unmet = bool(snapshot.guest_needs) and bool(rank(replace(snapshot, guest_needs=frozenset())))
        await abandon(db, event, EndReason.NO_COVERAGE, now, needs_unmet=unmet)
        return
    proposals = ProposalRepository(db)
    for position, offered in enumerate(attached, start=len(ranked) + 1):
        await proposals.set_rank(event.household_id, offered.id, position)  # after the ranked picks
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
    await publish(db, ctx, cc.proposals_ready(ctx), now=now)
    await publish(db, ctx, cc.call_updated(ctx), now=now)
    payload = {"proposals": [{"item_id": r.item_id, "mode": r.mode.value, "score": r.score} for r in ranked]}
    await audit(db, event.household_id, None, "event.voting", _target(event), payload, now)


async def _keep_attached(db: AsyncSession, ctx: CallContext, group: GroupPreferences, now: datetime) -> list[Proposal]:
    """Picks offers added while responses were open (FR-5) that can still be decided. Each passed
    every check when it was offered, but since then people who answered may veto it or bring a
    guest it doesn't suit, its offerer may have said out, or its item may have left the catalog.
    Those go, as ranking would never propose them (FR-29). Audited as ``event.pick_dropped``,
    without saying why."""
    kept: list[Proposal] = []
    for proposal in ctx.proposals:
        offerer = ctx.response_of(proposal.proposed_by) if proposal.proposed_by is not None else None
        if offerer is None or not offerer.is_in or misfit(ctx, proposal, group) is not None:
            await ProposalRepository(db).delete(ctx.event.household_id, proposal.id)
            payload = {"proposal_id": proposal.id, "item_id": proposal.item_id, "mode": proposal.mode.value}
            await audit(db, ctx.event.household_id, None, "event.pick_dropped", _target(ctx.event), payload, now)
        else:
            kept.append(proposal)
    return kept


async def _close_offer(db: AsyncSession, event: Event, now: datetime) -> None:
    """An offer's time: it stops taking people and it's on (FR-4). Nobody else joining doesn't
    end it: whoever made it still eats."""
    closed = replace(event, state=transition(event.state, EventState.DECIDED), decided_at=now)
    closed = await EventRepository(db).save(closed, now)
    ctx = await load(db, closed)
    await publish(db, ctx, cc.offer_updated(ctx), now=now)
    payload = {"head_count": ctx.head_count}
    await audit(db, event.household_id, None, "event.offer_closed", _target(event), payload, now)


async def _close(db: AsyncSession, event: Event, now: datetime) -> None:
    if event.is_offer:
        await _close_offer(db, event, now)
    else:
        await _close_responses(db, event, now)


async def expire(db: AsyncSession, event: Event, now: datetime) -> None:
    """End a call it's too late to finish (``EndReason.MISSED``), quietly: no new channel post
    and no direct messages. A call that was posted gets its messages updated to show it ended;
    a scheduled call that never opened sends nothing (FR-2)."""
    posted = event.state is not EventState.SCHEDULED
    ended = replace(event, state=transition(event.state, EventState.ABANDONED), end_reason=EndReason.MISSED)
    ended = await EventRepository(db).save(ended, now)
    if posted:
        ctx = await load(db, ended)
        await publish(
            db, ctx, cc.message_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), now=now
        )
    payload = {"reason": EndReason.MISSED.value, "from": event.state.value}
    await audit(db, event.household_id, None, "event.abandoned", _target(event), payload, now)


async def abandon(
    db: AsyncSession, event: Event, reason: EndReason, now: datetime, *, needs_unmet: bool = False
) -> None:
    """Spec 5.4: the on-your-own message. Nothing is assigned or logged. ``needs_unmet`` says
    the dietary needs of who was eating ruled out everything else that would have fit."""
    ended = replace(event, state=transition(event.state, EventState.ABANDONED), end_reason=reason)
    ended = await EventRepository(db).save(ended, now)
    ctx = await load(db, ended)
    await publish(db, ctx, note.CallAbandoned(cc.event_info(ctx), reason, ctx.head_count, needs_unmet), now=now)
    await publish(db, ctx, cc.call_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), now=now)
    await audit(db, event.household_id, None, "event.abandoned", _target(event), {"reason": reason.value}, now)


# Deciding ---------------------------------------------------------------------------------------


DOESNT_FIT = "That doesn't work for everyone who's in tonight."
"""The one message for any veto or guest dietary need. It never names the tag, the item's
vetoed attribute, or how many people object, so a proposer can't probe proposal by proposal to
learn who vetoed what (FR-29)."""


NO_COVERAGE = "no_coverage"
"""``misfit``'s code when nobody who's in can take the pick's role (FR-17)."""


def misfit(
    ctx: CallContext, proposal: Proposal, group: GroupPreferences, *, settled: bool = False
) -> EventRuleViolation | None:
    """Why ``proposal`` can't be decided for this call (FR-17, FR-28), as the refusal to raise,
    or None if it can.

    Private reasons (vetoes and guests' dietary needs) all give ``DOESNT_FIT``, coded
    ``doesnt_fit``. Coverage and availability reasons stay specific, because they reveal
    nothing private.

    ``settled`` checks a pick that exists already, as a decision does: the needs of guests that
    late joiners bring warn on it instead of ruling it out (``call_context.late_warnings``, #76).
    A pick being added or drawn is held to them, as to any guest's.
    """
    needs = ctx.settled_guest_needs if settled else guest_needs(ctx)
    if proposal.item_id is not None:
        item = ctx.items.get(proposal.item_id)
        if item is None or not item.active:
            return EventRuleViolation("That isn't in the catalog anymore.", code="not_in_catalog")
        if group.vetoes(rank_item(ctx, item)) or not needs <= item.tag_ids:
            return EventRuleViolation(DOESNT_FIT, code="doesnt_fit")
        if proposal.mode not in item.modes:
            return EventRuleViolation(
                f"{item.name} isn't available by {proposal.mode.label.lower()}.", code="unavailable"
            )
        cooks = item.cook_ids
    else:
        # A new item's only known tag is its cuisine, so it can't show it meets a dietary need.
        if proposal.free_cuisine_id in group.vetoed_tags or needs:
            return EventRuleViolation(DOESNT_FIT, code="doesnt_fit")
        cooks = frozenset()
    if not ctx.coverage.covers(proposal.mode, cooks):
        return EventRuleViolation(f"Nobody who's in offered to {proposal.mode.verb} for that.", code=NO_COVERAGE)
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
    """FR-13: a free-text proposal becomes a catalog item when it wins. It's for the call's meal
    and the default meals (#72), since it was proposed for this one."""
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
            meals=DEFAULT_MEALS | {ctx.event.slot.meal},
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
    # One pick per place (#77). Defensive: a call can hold a pick for this item besides the
    # free text only from 0.5.0 or earlier, since adding either one now refuses the other.
    if not any(p.item_id == item.id for p in ctx.proposals):
        await ProposalRepository(db).link_item(household_id, proposal.id, item.id)
    return item


async def decide(
    db: AsyncSession,
    event: Event,
    now: datetime,
    *,
    locked: Proposal | None = None,
    actor: Member | None = None,
    tie_chosen: bool = False,
) -> None:
    """FR-15 and FR-16: decide the call, by ``locked`` or by the vote, and assign roles.
    ``tie_chosen`` says ``locked`` is the coordinator's choice among tied picks (#87), not a lock.
    Abandons the call when no proposal can be covered (spec 5.4).

    When the winning pick's role has no clear owner and someone coordinates, the coordinator is
    asked to choose (FR-61): the automatic assignment waits until they do, or until the household's
    time to choose runs out (``assign_due_roles``)."""
    ctx = await load(db, event)
    # First, before anything is written: a meal that would land after the call's day can't be
    # decided. A lock says so; the vote deadline ends the call quietly instead.
    meal = meal_time(ctx.responses, day=event.day, zone=ctx.zone, default=ctx.schedule.meal_time, not_before=now)
    if meal.astimezone(ctx.zone).date() != event.day:
        if locked is not None:
            raise EventRuleViolation("It's too late to decide: the meal would be after midnight.", code="too_late")
        await expire(db, event, now)
        return
    group = await group_preferences(db, event.household_id, ctx.veto_people)
    # Check free text as the catalog item it will become, if one by that name exists by now.
    checked = {p.id: await _as_catalog_item(db, ctx, p) for p in ctx.proposals}
    tie: TieHow | None = None
    tie_by: int | None = None
    if locked is not None:
        problem = misfit(ctx, checked.get(locked.id, locked), group, settled=True)
        if problem is not None:
            raise problem
        winner = locked
        if tie_chosen and actor is not None:
            tie, tie_by = TieHow.CHOSE, actor.id
    else:
        counted = counted_votes(ctx.votes, ctx.responses)  # a late joiner's only if the coordinator says (#76)
        eaten = await last_eaten(db, event.household_id)
        tallies = [
            ProposalTally(
                p.id,
                p.rank,
                misfit(ctx, checked[p.id], group, settled=True) is None,
                counted.get(p.id, frozenset()),
                days_since_eaten=_days_since(event, eaten, checked[p.id]),
            )
            for p in ctx.proposals
        ]
        admins = frozenset(m.id for m in ctx.active_members if m.is_admin)
        # A guest coordinating doesn't break a tie: whoever invited them does, then an admin (#38).
        coordinator = ctx.members.get(event.coordinator_id) if event.coordinator_id is not None else None
        breaker = tie_breaker(coordinator, ctx.invitations.get(coordinator.id) if coordinator else None)
        guest_coordinating = coordinator is not None and coordinator.is_guest
        chosen = decide_winner(tallies, coordinator_id=breaker, admin_ids=admins, admins_next=guest_coordinating)
        found = ctx.proposal(chosen.tally.id) if chosen is not None else None
        if chosen is None or found is None:
            await abandon(db, event, EndReason.NO_COVERAGE, now)
            return
        winner, tie, tie_by = found, chosen.tie, chosen.by
    item = ctx.items[winner.item_id] if winner.item_id is not None else await _catalog_free_text(db, ctx, winner, now)
    volunteers = ctx.coverage.volunteers(winner.mode, item.cook_ids)
    roles = assign_roles(winner.mode, volunteers, event.coordinator_id)
    coordinator_member = ctx.members.get(event.coordinator_id) if event.coordinator_id is not None else None
    asking = coordinator_member is not None and coordinator_member.active and roles_unclear(volunteers)
    due = now + timedelta(minutes=roles_choice_minutes(ctx.settings)) if asking else None
    if asking:  # the role waits for the coordinator's choice
        roles = {role: who for role, who in roles.items() if role is AssignmentRole.COORDINATOR}
    decided = replace(
        event,
        state=transition(event.state, EventState.DECIDED),
        decided_item_id=item.id,
        decided_mode=winner.mode,
        meal_time=meal,
        decided_at=now,
        locked_by=actor.id if locked is not None and actor is not None and not tie_chosen else None,
        roles_due_at=due,
        tie_how=tie,
        tie_by=tie_by,
    )
    decided = await EventRepository(db).save(decided, now)
    repo = AssignmentRepository(db)
    assigned = [await repo.assign(event.household_id, event.id, role, who, now) for role, who in roles.items()]
    await _announce_decision(db, decided, assigned_ids=[a.id for a in assigned], now=now, asking=asking)
    payload = {
        "how": "tie_break" if tie_chosen else "locked" if locked else "vote",
        "proposal_id": winner.id,
        "item_id": item.id,
        "mode": winner.mode.value,
        "meal_time": meal.isoformat(),
        "assignments": {role.value: who for role, who in roles.items()},
        "tie": tie.value if tie is not None else None,
        "roles_pending": asking,
    }
    await audit(db, event.household_id, actor if locked else None, "event.decided", _target(event), payload, now)


def _days_since(event: Event, eaten: dict[int, date], proposal: Proposal) -> int | None:
    """Whole days from when the household last ate the pick's place to the call's day, or None
    if it never has (FR-16's least recently eaten rule)."""
    last = eaten.get(proposal.item_id) if proposal.item_id is not None else None
    return None if last is None else max((event.day - last).days, 0)


async def _announce_decision(
    db: AsyncSession, event: Event, *, assigned_ids: list[int], now: datetime, asking: bool = False
) -> None:
    ctx = await load(db, event)
    decision = cc.decision_info(ctx)
    if decision is None:  # pragma: no cover - decide() always stores a winner and a meal time
        return
    first: dict[int, int] = {}  # one direct message per member, keyed by their first assignment
    for a in sorted(ctx.assignments, key=lambda a: a.id):
        if a.id in assigned_ids:
            first.setdefault(a.member_id, a.id)
    if asking:  # the coordinator's message below says they coordinate
        first.pop(event.coordinator_id or 0, None)
    dms = [cc.assignment_made(ctx, member_id, assignment_id, decision) for member_id, assignment_id in first.items()]
    ask = cc.roles_to_choose(ctx, decision, event.roles_due_at) if asking and event.roles_due_at else None
    await publish(db, ctx, note.DecisionMade(cc.event_info(ctx), decision, cc.locked_by(ctx)), *dms, now=now)
    if ask is not None:
        await publish(db, ctx, ask, now=now)
    await publish(db, ctx, cc.proposals_updated(ctx), cc.call_updated(ctx), now=now)


async def assign_due_roles(db: AsyncSession, event: Event, now: datetime) -> None:
    """The coordinator's time to choose roles ran out (FR-61, ``Step.ASSIGN_ROLES``): the automatic
    assignment stands, as it would have at the decision. The first volunteer takes the role, and
    is told. Audited as ``event.roles_assigned`` with ``by`` None."""
    ctx = await load(db, event)
    mode, item = event.decided_mode, ctx.items.get(event.decided_item_id) if event.decided_item_id else None
    cleared = await EventRepository(db).save(replace(event, roles_due_at=None), now)
    assigned = None
    if mode is not None and item is not None:
        volunteers = ctx.coverage.volunteers(mode, item.cook_ids)
        if volunteers:
            assigned = await AssignmentRepository(db).assign(
                event.household_id, event.id, MODE_ROLE[mode], volunteers[0], now
            )
    ctx = await load(db, cleared)
    decision = cc.decision_info(ctx)
    messages: list[note.Notification] = [*cc.decision_updated(ctx), cc.call_updated(ctx)]
    if assigned is not None and decision is not None:
        messages.append(cc.assignment_made(ctx, assigned.member_id, assigned.id, decision))
    await publish(db, ctx, *messages, now=now)
    payload = {"role": MODE_ROLE[mode].value if mode else None, "member_id": assigned.member_id if assigned else None}
    await audit(db, event.household_id, None, "event.roles_assigned", _target(event), payload | {"by": None}, now)


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
    await publish(db, ctx, *cc.decision_updated(ctx), cc.message_updated(ctx), *reminders, now=now)
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
    Step.OPEN: _open_scheduled,
    Step.REMIND: _remind,
    Step.CLOSE_RESPONSES: _close,
    Step.CLOSE_VOTING: _decide_by_vote,
    Step.ASSIGN_ROLES: assign_due_roles,
    Step.MARK_DONE: _mark_done,
}


STEP_LINES = {
    EventState.COLLECTING: "opened",
    EventState.VOTING: "voting",
    EventState.DECIDED: "decided",
    EventState.DONE: "done",
    EventState.ABANDONED: "abandoned",
    EventState.CANCELLED: "replaced_by_offer",
}
"""Each step's log line, by the state it leaves the call in (#75)."""


async def _log_step(db: AsyncSession, step: Step, before: Event) -> None:
    """One line for the step the scheduler (or a deadline moved to now) just took: the
    transition, with why a call ended and what it decided."""
    after = await EventRepository(db).get(before.household_id, before.id)
    if after is None:  # pragma: no cover - the step holds the event's lock
        return
    if step is Step.EXPIRE:
        name = "stale_closed"
    elif before.is_offer and after.state is EventState.DECIDED:
        name = "offer_closed"
    elif after.state is before.state:
        name = "reminded" if step is Step.REMIND else step.value
    else:
        name = STEP_LINES.get(after.state, step.value)
    item_id = after.decided_item_id if after.state is EventState.DECIDED else None
    item = await CatalogRepository(db).item(after.household_id, item_id) if item_id is not None else None
    end_reason = after.end_reason.value if after.end_reason is not None else None
    activity.action(
        db,
        name,
        before,
        None,
        after=after.state,
        item=item,
        item_id=item_id,
        mode=after.decided_mode if item_id is not None else None,
        step=step.value,
        end_reason=end_reason,
    )


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
                    await _log_step(db, step, event)
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
