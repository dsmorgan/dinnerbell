# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin overrides for calls (spec 5.7, FR-6): move a deadline, cancel, re-run, and assign the
coordinator. Locking and correcting assignments are in ``services.calls``, where the coordinator
can do them too. Every override is audited (SEC-9).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import AssignmentRepository, EventRepository, ProposalRepository, ResponseRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import (
    OPEN_STATES,
    Event,
    EventRuleViolation,
    EventState,
    Window,
    check_rerun,
    check_window_change,
    shifted_deadline,
    transition,
)
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services import lifecycle
from dinnerbell.services.call_context import load, locked_event, publish
from dinnerbell.services.calls import set_coordinator
from dinnerbell.services.common import audit, require_admin
from dinnerbell.services.errors import NotFound
from dinnerbell.services.variety import household_today

ADMINS_ONLY = "Only an admin can do that."


def _target(event: Event) -> str:
    return f"event:{event.id}"


async def move_deadline(
    db: AsyncSession, actor: Member, event_id: int, window: Window, minutes: int | None, *, now: datetime
) -> None:
    """Extend or shorten the open window by ``minutes``, or close it now when None. A deadline
    that lands in the past closes the window at once."""
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        current = check_window_change(event, window)
        moved = shifted_deadline(current, minutes, now)
        changes = {"respond_deadline": moved} if window is Window.RESPOND else {"vote_deadline": moved}
        event = await EventRepository(db).save(replace(event, **changes), now)  # type: ignore[arg-type]
        ctx = await load(db, event)
        await publish(db, ctx, cc.call_updated(ctx), *([cc.proposals_updated(ctx)] if ctx.proposals else []), now=now)
        payload = {"window": window.value, "from": current.isoformat(), "to": moved.isoformat()}
        await audit(db, actor.household_id, actor, "event.deadline_moved", _target(event), payload, now)
    await lifecycle.advance(db, actor.household_id, event_id, now)


async def cancel(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> None:
    """Cancel an event that hasn't finished. Nothing is logged or awarded (spec 8)."""
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        before = await load(db, event)
        event = await EventRepository(db).save(replace(event, state=transition(event.state, EventState.CANCELLED)), now)
        ctx = await load(db, event)
        participants = tuple(ctx.person(r.member_id) for r in before.participants)
        cancelled = note.EventCancelled(cc.event_info(ctx), participants, ctx.person(actor.id))
        updates: list[note.Notification] = [cc.call_updated(ctx)]
        updates += [cc.proposals_updated(ctx)] if ctx.proposals else []
        await publish(db, ctx, cancelled, *updates, *cc.decision_updated(ctx), now=now)
        payload = {"from": before.event.state.value}
        await audit(db, actor.household_id, actor, "event.cancelled", _target(event), payload, now)


async def rerun(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> None:
    """Start the call again with fresh windows from the current schedule: now, for today's
    call, or at the slot's call time for a later day (FR-3 timing). Refused for today's call
    once it's past the usual meal time plus two hours.

    Responses stay; maybes the deadline turned out are maybes again. Proposals, votes, the
    decision, and assignments are cleared. The run number goes up, so the channel posts a new
    call message and the reminder can go out once more.
    """
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await load(db, event)
        today = household_today(ctx.household, now)
        check_rerun(event, today, now=now, too_late_after=ctx.too_late_for(today))
        household_id = actor.household_id
        await ProposalRepository(db).clear(household_id, event.id)
        await AssignmentRepository(db).clear(household_id, event.id)
        await ResponseRepository(db).restore_maybes(household_id, event.id, now)
        schedule = ctx.schedule
        later = event.day > today
        opens_at = schedule.opens_at(event.day, ctx.zone) if later else now
        fresh = replace(
            event,
            state=transition(event.state, EventState.SCHEDULED if later else EventState.COLLECTING),
            run=event.run + 1,
            opens_at=opens_at,
            respond_deadline=opens_at + schedule.response_window,
            reminder_minutes=schedule.reminder_minutes,
            vote_minutes=schedule.vote_minutes,
            vote_deadline=None,
            reminder_sent_at=None,
            decided_item_id=None,
            decided_mode=None,
            meal_time=None,
            decided_at=None,
            end_reason=None,
        )
        fresh = await EventRepository(db).save(fresh, now)
        if not later:  # a later day's call is posted when the tick opens it
            ctx = await load(db, fresh)
            await publish(db, ctx, cc.call_opened(ctx), now=now)
        payload = {"from": event.state.value, "run": fresh.run, "respond_by": fresh.respond_deadline.isoformat()}
        await audit(db, household_id, actor, "event.rerun", _target(event), payload, now)


async def assign_coordinator(
    db: AsyncSession, actor: Member, event_id: int, member_id: int | None, *, now: datetime
) -> None:
    """FR-10: the admin assigns or replaces the coordinator, or clears it with None."""
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        if event.state not in OPEN_STATES - {EventState.SCHEDULED}:
            raise EventRuleViolation("This call is over.")
        if member_id is not None:
            target = (await load(db, event)).members.get(member_id)
            if target is None or not target.active:
                raise NotFound("No such member.")
        await set_coordinator(db, event, member_id, now=now)
        payload = {"from": event.coordinator_id, "to": member_id}
        await audit(db, actor.household_id, actor, "event.coordinator_assigned", _target(event), payload, now)
