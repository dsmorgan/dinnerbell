# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin overrides for calls (spec 5.7, FR-6): move a deadline, re-run, and assign the
coordinator. Calling a call off, closing a window now, locking, and correcting assignments are
in ``services.calls``, where the coordinator can do them too. Every override is audited (SEC-9).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import (
    ActingAs,
    Event,
    EventState,
    Window,
    check_assign_coordinator,
    check_can_coordinate,
    check_deadline_ahead,
    check_rerun,
    check_window_change,
)
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import CALL_OFF_SUPERSEDES, SUPERSEDED_BY_RERUN
from dinnerbell.services import activity, lifecycle
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import CallContext, load, locked_event, publish
from dinnerbell.services.calls import roles_called_off, set_coordinator
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
    that lands in the past closes the window at once. A deadline that has passed already doesn't
    move (``check_deadline_ahead``). The coordinator closes a window with ``calls.close_window``,
    and moves the decision time with ``coordinator.move_decision_time``. Audited with ``"as":
    "admin"``, as the coordinator's move names its capacity."""
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        if minutes is not None:  # closing it now is harmless: the scheduler would close it anyway
            check_window_change(event, window)
            check_deadline_ahead(event, window, now=now)
        current, moved = await lifecycle.move_window(db, event, window, minutes, now)
        capacity = ActingAs.ADMIN.value
        payload = {"window": window.value, "from": current.isoformat(), "to": moved.isoformat(), "as": capacity}
        await audit(db, actor.household_id, actor, "event.deadline_moved", _target(event), payload, now)
        activity.action(db, "move_deadline", event, actor, window=window.value, to=moved.isoformat())
    await lifecycle.advance(db, actor.household_id, event_id, now)


async def rerun(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> None:
    """Start the call again with fresh windows (``lifecycle.reopen``): now, for today's call,
    or at the slot's call time for a later day (FR-3 timing), unless it was posted early.
    Refused for today's call once it's past the usual meal time plus two hours. Any member can
    start a called-off call again with ``calls.start_call``; re-running a voting, decided, or
    abandoned one is the admin's.

    A re-run from voting or decided tells people what a call-off from there would (FR-6, FR-56):
    the old run's call, picks, and decision messages say it was replaced, with no buttons, and
    each active cook, pickup, and orderer but the admin gets ``AssignmentCancelled``. The old
    run's direct messages not yet sent are closed unsent. All of it goes before the new run's post.
    """
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await load(db, event)
        today = household_today(ctx.household, now)
        check_rerun(event, today, now=now, too_late_after=ctx.too_late_for(today))
        await lifecycle.check_no_cooking_offer(db, event.household_id, event.slot, event.day)  # FR-5
        if event.state in RERUN_TELLS:
            await OutboxRepository(db).close_unsent(
                event.household_id, event.id, CALL_OFF_SUPERSEDES, note=SUPERSEDED_BY_RERUN, now=now
            )
            await publish(db, ctx, *_replaced(ctx, actor), now=now)
        fresh = await lifecycle.reopen(db, ctx, today=today, now=now)
        payload = {"from": event.state.value, "run": fresh.run, "respond_by": fresh.respond_deadline.isoformat()}
        await audit(db, actor.household_id, actor, "event.rerun", _target(event), payload, now)
        activity.action(db, "rerun", fresh, actor, before=event.state)


RERUN_TELLS = frozenset({EventState.VOTING, EventState.DECIDED})
"""States a re-run tells people about leaving. An abandoned call's messages have nothing left to
act on, and a called-off one's said so already."""


def _replaced(before: CallContext, actor: Member) -> list[note.Notification]:
    """The old run's messages as replaced (``EventInfo.replaced``), and its role holders' notes."""
    info = replace(cc.event_info(before), replaced=True)
    sent: list[note.Notification] = [replace(cc.call_updated(before), event=info)]
    if before.proposals:
        sent.append(replace(cc.proposals_updated(before), event=info))
    decision = cc.decision_info(before)
    if decision is not None:
        sent.append(note.DecisionUpdated(info, decision, cc.locked_by(before)))
    sent += roles_called_off(before, before, info, actor, None)
    return sent


async def assign_coordinator(
    db: AsyncSession, actor: Member, event_id: int, member_id: int | None, *, now: datetime
) -> None:
    """FR-10: the admin assigns or replaces the coordinator, or clears it with None. A guest is
    refused (#227)."""
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_assign_coordinator(event)
        if member_id is not None:
            ctx = await load(db, event)
            target = ctx.members.get(member_id)
            if target is None or not target.active:
                raise NotFound("No such member.")
            check_can_coordinate(target)
        await set_coordinator(db, event, member_id, by=actor, now=now)
        payload = {"from": event.coordinator_id, "to": member_id}
        await audit(db, actor.household_id, actor, "event.coordinator_assigned", _target(event), payload, now)
        activity.action(db, "assign_coordinator", event, actor, to_member_id=member_id)
