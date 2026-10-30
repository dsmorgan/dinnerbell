# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stuck calls (#142): open calls that have owed the scheduler a step for more than an hour,
because the step keeps failing.

The scheduler's call tick looks for them right after it advances the calls and keeps the result
in ``SchedulerState.stuck``; ``/healthz`` counts it, and every Admin tab lists the household's.
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
from dinnerbell.domain.events import Event, stuck_for
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services.common import require_admin
from dinnerbell.services.lifecycle import FailureLog

CHECK_FAILED = "stuck call check failed"

log = logging.getLogger(__name__)
ADMINS_ONLY = "Only an admin can see stuck calls."


@dataclass(frozen=True)
class StuckCall:
    event: Event
    stuck_for: timedelta | None
    """How long it has been stuck, as of the check. None when the check failed for this call,
    which counts as stuck: the tick reads the same things to find the call's step."""


async def _household(
    db: AsyncSession, household_id: int, now: datetime, running_since: datetime, failures: FailureLog
) -> list[StuckCall]:
    found: list[StuckCall] = []
    events = EventRepository(db)
    for event_id in await events.open_ids(household_id):
        event = await events.get(household_id, event_id)
        if event is None:  # pragma: no cover - discarded since the list was read
            continue
        try:
            waited = stuck_for(await cc.owed(db, event), now, running_since=running_since)
        except Exception:
            await db.rollback()
            failures.failed(event_id, now)
            found.append(StuckCall(event, None))
            continue
        failures.recovered(event_id)
        if waited is not None:
            found.append(StuckCall(event, waited))
    return found


async def find_stuck(
    sessionmaker: async_sessionmaker[AsyncSession],
    now: datetime,
    *,
    running_since: datetime,
    failures: FailureLog | None = None,
    check_failures: FailureLog | None = None,
) -> list[StuckCall] | None:
    """Every household's stuck calls, oldest call first, or None if the check failed.

    ``running_since`` is when the scheduler started (``SchedulerState.started_at``). A call
    whose own check fails counts as stuck, for how long unknown, and the others are still
    checked; ``failures`` throttles that call's log line as it does the tick's. Any other error
    fails the whole check, which never raises: whatever makes a step fail may trip this check
    too. ``check_failures`` throttles that warning (``whole_check_log``).
    """
    failures = failures if failures is not None else call_check_log()
    check_failures = check_failures if check_failures is not None else whole_check_log()
    try:
        async with sessionmaker() as db:
            found: list[StuckCall] = []
            for household in await HouseholdRepository(db).all():
                found += await _household(db, household.id, now, running_since, failures)
    except Exception:
        check_failures.failed(0, now)
        return None
    check_failures.recovered(0)
    failures.keep_only({s.event.id for s in found})
    return found


def call_check_log() -> FailureLog:
    """The throttled log line for a call whose own stuck check fails."""
    return FailureLog("could not check a call for being stuck")


def whole_check_log() -> FailureLog:
    """The throttled warning for a stuck check that failed as a whole."""
    return FailureLog(CHECK_FAILED, level=logging.WARNING, key=None)


def for_admin(actor: Member, found: list[StuckCall] | None) -> list[StuckCall]:
    """The actor's household's calls among the scheduler's latest ``find_stuck``. Admins only."""
    require_admin(actor, ADMINS_ONLY)
    return [s for s in found or [] if s.event.household_id == actor.household_id]
