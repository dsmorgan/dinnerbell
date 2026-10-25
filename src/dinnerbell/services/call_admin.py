# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin overrides for calls (spec 5.7, FR-6): move a deadline, re-run, and assign the
coordinator. Calling a call off, closing a window now, locking, and correcting assignments are
in ``services.calls``, where the coordinator can do them too. Every override is audited (SEC-9).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.domain.events import Event, Window, check_assign_coordinator, check_rerun
from dinnerbell.domain.members import Member
from dinnerbell.services import lifecycle
from dinnerbell.services.call_context import load, locked_event
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
    that lands in the past closes the window at once. The coordinator closes a window with
    ``calls.close_window``."""
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        current, moved = await lifecycle.move_window(db, event, window, minutes, now)
        payload = {"window": window.value, "from": current.isoformat(), "to": moved.isoformat()}
        await audit(db, actor.household_id, actor, "event.deadline_moved", _target(event), payload, now)
    await lifecycle.advance(db, actor.household_id, event_id, now)


async def rerun(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> None:
    """Start the call again with fresh windows (``lifecycle.reopen``): now, for today's call,
    or at the slot's call time for a later day (FR-3 timing), unless it was posted early.
    Refused for today's call once it's past the usual meal time plus two hours. Any member can
    start a called-off call again with ``calls.start_call``; re-running a voting, decided, or
    abandoned one is the admin's.
    """
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await load(db, event)
        today = household_today(ctx.household, now)
        check_rerun(event, today, now=now, too_late_after=ctx.too_late_for(today))
        await lifecycle.check_no_cooking_offer(db, event.household_id, event.slot, event.day)  # FR-5
        fresh = await lifecycle.reopen(db, ctx, today=today, now=now)
        payload = {"from": event.state.value, "run": fresh.run, "respond_by": fresh.respond_deadline.isoformat()}
        await audit(db, actor.household_id, actor, "event.rerun", _target(event), payload, now)


async def assign_coordinator(
    db: AsyncSession, actor: Member, event_id: int, member_id: int | None, *, now: datetime
) -> None:
    """FR-10: the admin assigns or replaces the coordinator, or clears it with None."""
    require_admin(actor, ADMINS_ONLY)
    async with locked_event(db, actor.household_id, event_id, now) as event:
        check_assign_coordinator(event)
        if member_id is not None:
            ctx = await load(db, event)
            target = ctx.members.get(member_id)
            if target is None or not target.active or (target.is_guest and not ctx.invited(target.id)):
                raise NotFound("No such member.")
        await set_coordinator(db, event, member_id, now=now)
        payload = {"from": event.coordinator_id, "to": member_id}
        await audit(db, actor.household_id, actor, "event.coordinator_assigned", _target(event), payload, now)
