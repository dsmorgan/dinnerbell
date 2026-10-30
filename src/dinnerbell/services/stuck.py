# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stuck calls (#142): open calls that have owed the scheduler a step for more than an hour,
because the step keeps failing. ``/healthz`` counts them, and Admin lists them.

Nothing is stored: a call's owed step and when it fell due follow from the call itself
(``domain.events.owed_since``), and the scheduler's start time stands in for the due time of a
step that fell due while Dinnerbell was down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.events import EventRepository
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.events import STUCK_AFTER, Event, stuck_for
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services.common import require_admin

log = logging.getLogger(__name__)
ADMINS_ONLY = "Only an admin can see stuck calls."


@dataclass(frozen=True)
class StuckCall:
    event: Event
    stuck_for: timedelta


async def _stuck(db: AsyncSession, household_id: int, now: datetime, running_since: datetime) -> list[StuckCall]:
    if now - running_since <= STUCK_AFTER:
        return []  # nothing can be stuck in the scheduler's first hour, so skip the reads
    found: list[StuckCall] = []
    events = EventRepository(db)
    for event_id in await events.open_ids(household_id):
        event = await events.get(household_id, event_id)
        if event is None:  # pragma: no cover - discarded since the list was read
            continue
        waited = stuck_for(await cc.owed(db, event), now, running_since=running_since)
        if waited is not None:
            found.append(StuckCall(event, waited))
    return found


async def stuck_calls(db: AsyncSession, actor: Member, now: datetime, *, running_since: datetime) -> list[StuckCall]:
    """The actor's household's stuck calls, oldest call first. ``running_since`` is when the
    scheduler started (``SchedulerState.started_at``). Admins only."""
    require_admin(actor, ADMINS_ONLY)
    return await _stuck(db, actor.household_id, now, running_since)


async def count_stuck(
    sessionmaker: async_sessionmaker[AsyncSession], now: datetime, *, running_since: datetime
) -> int | None:
    """Stuck calls in every household, for ``/healthz``, or None if the check failed.

    Any error counts as a failed check, not only a database error: whatever makes a call's step
    fail may trip this check too, and ``/healthz`` must answer rather than fail with a 500,
    which would make the container health check restart Dinnerbell.
    """
    try:
        async with sessionmaker() as db:
            households = await HouseholdRepository(db).all()
            return sum([len(await _stuck(db, h.id, now, running_since)) for h in households])
    except Exception:
        log.warning("stuck call check failed", exc_info=True)
        return None
