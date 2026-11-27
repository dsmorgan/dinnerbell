# SPDX-License-Identifier: AGPL-3.0-or-later
"""Health probes."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.migrate import head_revision
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.push import PushRepository
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.services.web_app import web_app_enabled

if TYPE_CHECKING:
    from dinnerbell.services.stuck import StuckCall

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = timedelta(seconds=30)
STALE_AFTER = HEARTBEAT_INTERVAL * 3
"""A tick older than this makes /healthz report "degraded"."""
STOPPED_AFTER = timedelta(minutes=5)
"""A tick older than this means the scheduler is dead: /healthz returns 503."""
STUCK_CHECK_STALE_AFTER = timedelta(seconds=90)
"""Three call ticks (``scheduler.runner.CALL_TICK_INTERVAL``). A stuck call check older than this
means the call tick has hung or stopped while the heartbeat goes on: /healthz reports
"degraded" with ``stuck_calls`` null, and Admin shows no banner."""


@dataclass
class SchedulerState:
    """What the scheduler's jobs record and /healthz reports. The composition root shares one
    instance between them, so neither layer imports the other."""

    started_at: datetime | None = None
    last_tick: datetime | None = None
    last_backup: datetime | None = None
    last_backup_file: str | None = None
    """The newest nightly backup's file name, beside ``last_backup``: Admin > Health shows it."""
    backup_failing: bool = False
    """The newest nightly backup failed, or failed its check and was deleted. /healthz reports
    "degraded" until one succeeds."""
    stuck: list[StuckCall] | None = field(default_factory=list)
    """The stuck calls the latest call tick found (#142), or None if its check failed. Read it
    with ``current_stuck``."""
    stuck_checked_at: datetime | None = None
    """When the call tick last finished looking for stuck calls."""

    def tick_is_fresh(self, now: datetime) -> bool:
        return self.last_tick is not None and now - self.last_tick <= STALE_AFTER

    def current_stuck(self, now: datetime) -> list[StuckCall] | None:
        """The latest stuck call check's result, or None if it failed or is older than
        ``STUCK_CHECK_STALE_AFTER``. Before the first check, the scheduler's start stands in for
        it, so a fresh start reports none."""
        checked = self.stuck_checked_at or self.started_at
        if checked is None or now - checked > STUCK_CHECK_STALE_AFTER:
            return None
        return self.stuck

    def scheduler_status(self, now: datetime) -> str:
        """ "starting" before the first tick, then "ok", "stale", or "stopped" by tick age.

        A scheduler that never ticks counts as stopped once it has had longer than the stale
        threshold to start, so /healthz can't sit at "starting" forever.
        """
        if self.last_tick is None:
            if self.started_at is not None and now - self.started_at > STALE_AFTER:
                return "stopped"
            return "starting"
        age = now - self.last_tick
        if age <= STALE_AFTER:
            return "ok"
        return "stale" if age <= STOPPED_AFTER else "stopped"


@dataclass
class SlackState:
    """The Slack connection as /healthz reports it. The Slack adapter updates it; the web app
    reads it. Like ``SchedulerState``, the composition root shares one instance.

    ``problem`` is set when a startup check fails (bad token, wrong workspace, bot not in the
    channel). ``connected`` asks the live socket. The status is one of ``not_configured``,
    ``connected``, ``disconnected`` (reconnecting, or Slack unreachable), or ``error``.

    ``dms_dropped`` counts direct messages a labeled instance dropped because the recipient
    isn't in the channel (ADR 0006). It is None while that guard is off.
    """

    configured: bool = False
    problem: str | None = None
    connected: Callable[[], Awaitable[bool]] | None = None
    dms_dropped: int | None = None
    dms_dropped_inactive: int = 0
    """Queued direct messages dropped because the recipient was deactivated. Counted since startup, on
    every instance; not in /healthz."""

    async def status(self) -> str:
        if not self.configured:
            return "not_configured"
        if self.problem is not None:
            return "error"
        if self.connected is not None and await self.connected():
            return "connected"
        return "disconnected"


async def outbox_backlog(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[int, int] | None:
    """(notifications waiting, notifications that failed for good), or None if unreadable."""
    try:
        async with sessionmaker() as db:
            return await OutboxRepository(db).backlog()
    except (SQLAlchemyError, OSError):
        log.warning("outbox backlog check failed", exc_info=True)
        return None


STALE_OUTBOX = timedelta(hours=1)
"""A notification waiting longer than this makes /healthz report "degraded"."""


async def outbox_oldest_age(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> timedelta | None:
    """How long the oldest waiting notification has waited, or None if none waits or the outbox
    is unreadable."""
    try:
        async with sessionmaker() as db:
            queued = await OutboxRepository(db).oldest_pending()
    except (SQLAlchemyError, OSError):
        log.warning("outbox age check failed", exc_info=True)
        return None
    return None if queued is None else max(now - queued, timedelta(0))


STALE_PUSH = STALE_OUTBOX
"""A push waiting longer than this makes /healthz report "degraded": the push services are
unreachable or refusing, and every push retries until it closes unsent after 24 hours."""


@dataclass(frozen=True, slots=True)
class PushBacklog:
    waiting: int
    oldest: timedelta | None
    """How long the oldest waiting push has waited, or None if none waits."""


async def push_backlog(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> PushBacklog | None:
    """The push queue's backlog while the install's household has the web app on (spec 6.15),
    or None while it's off, before bootstrap, or if the queue is unreadable."""
    try:
        async with sessionmaker() as db:
            household = await HouseholdRepository(db).first()
            if household is None or not await web_app_enabled(db, household.id):
                return None
            repo = PushRepository(db)
            _, waiting = await repo.counts(household.id)
            queued = await repo.oldest_pending(household.id)
    except (SQLAlchemyError, OSError):
        log.warning("push backlog check failed", exc_info=True)
        return None
    return PushBacklog(waiting, None if queued is None else max(now - queued, timedelta(0)))


async def database_reachable(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with sessionmaker() as db:
            await db.execute(text("SELECT 1 FROM household LIMIT 1"))
    except (SQLAlchemyError, OSError):
        log.warning("database health check failed", exc_info=True)
        return False
    return True


BACKUP_STALE_AFTER = timedelta(hours=36)
"""A nightly backup older than this means at least one night's was missed: Admin > Health warns.
/healthz reports only ``failing``, so its status doesn't change for this."""


@dataclass(frozen=True, slots=True)
class OutboxHealth:
    waiting: int
    failed: int
    """Notifications that gave up for good."""
    oldest: timedelta | None
    """How long the oldest waiting notification has waited, or None if none waits."""


@dataclass(frozen=True, slots=True)
class HealthReport:
    """What /healthz reports, and Admin > Health shows in words (#339). Both read it from
    ``check_health``, so they can't disagree."""

    database_ok: bool
    scheduler: str
    """``SchedulerState.scheduler_status``."""
    last_tick: datetime | None
    slack: str
    """``SlackState.status``."""
    outbox: OutboxHealth | None
    """None while the database or the outbox is unreadable."""
    push: PushBacklog | None
    """None while the web app is off, or the push queue is unreadable."""
    last_backup: datetime | None
    last_backup_file: str | None
    backup_failing: bool
    stuck_calls: int | None
    """How many calls are stuck, or None if the check failed or is out of date."""

    @property
    def causes(self) -> frozenset[str]:
        """What makes the report "degraded": ``scheduler`` (not ticking on time), ``slack``
        (disconnected, or a startup check failed), ``outbox`` (a notification waiting over
        ``STALE_OUTBOX``), ``push`` (a push waiting over ``STALE_PUSH``), ``backup`` (the last
        backup failed), and ``stuck_calls`` (a stuck call, or a check that failed or is out of
        date). Admin > Health shows a warning for each."""
        found = set()
        if self.scheduler != "ok":
            found.add("scheduler")
        if self.slack not in {"connected", "not_configured"}:
            found.add("slack")
        if self.outbox is not None and (self.outbox.oldest or timedelta(0)) > STALE_OUTBOX:
            found.add("outbox")
        if self.push is not None and (self.push.oldest or timedelta(0)) > STALE_PUSH:
            found.add("push")
        if self.backup_failing:
            found.add("backup")
        if self.stuck_calls != 0:
            found.add("stuck_calls")
        return frozenset(found)

    @property
    def status(self) -> str:
        """``error`` (/healthz answers 503) when the database is unreachable or the scheduler
        has stopped; otherwise ``degraded`` with any of ``causes``, or ``ok``."""
        if not self.database_ok or self.scheduler == "stopped":
            return "error"
        return "degraded" if self.causes else "ok"

    @property
    def healthy(self) -> bool:
        return self.status != "error"

    def backup_stale(self, now: datetime) -> bool:
        """No nightly backup, or none for over ``BACKUP_STALE_AFTER``."""
        return self.last_backup is None or now - self.last_backup > BACKUP_STALE_AFTER


async def check_health(
    sessionmaker: async_sessionmaker[AsyncSession], scheduler: SchedulerState, slack: SlackState, now: datetime
) -> HealthReport:
    """Check everything /healthz reports; ``HealthReport.status`` and ``causes`` say what's wrong.
    ``error`` when the database is unreachable or the scheduler has stopped ticking, so the
    container health check and Uptime Kuma both catch a dead scheduler.

    A notification waiting over an hour also reports "degraded": the outbox is stuck. So does a
    push waiting over an hour while the web app is on: the push services can't be reached or
    refuse it. So does a stuck call (#142), one that has owed the scheduler a step for over an
    hour, or a check for them that failed or is out of date (the call tick hung): restarting
    can't fix a failing step, and the admin calls the call off from its page.
    Slack problems alone report "degraded", not "error". Restarting the container can't invite
    the bot or fix a token, the outbox keeps every notification until Slack is back, and the
    web app must stay up so the admin can sign in and fix things."""
    database_ok = await database_reachable(sessionmaker)
    backlog = await outbox_backlog(sessionmaker) if database_ok else None
    oldest = await outbox_oldest_age(sessionmaker, now) if backlog is not None else None
    pushes = await push_backlog(sessionmaker, now) if database_ok else None
    scheduler_status = scheduler.scheduler_status(now)
    current = scheduler.current_stuck(now)  # the latest tick's check, never a query here
    stuck = None if current is None else len(current)
    slack_status = await slack.status()
    return HealthReport(
        database_ok=database_ok,
        scheduler=scheduler_status,
        last_tick=scheduler.last_tick,
        slack=slack_status,
        outbox=None if backlog is None else OutboxHealth(backlog[0], backlog[1], oldest),
        push=pushes,
        last_backup=scheduler.last_backup,
        last_backup_file=scheduler.last_backup_file,
        backup_failing=scheduler.backup_failing,
        stuck_calls=stuck,
    )


def schema_revision() -> str:
    """The schema revision this build migrates the database to, such as ``0006_event_ended_by``.

    ``dinnerbell serve`` upgrades the database to it before it starts, and refuses a database at
    a revision it doesn't know, so while the app runs this is the database's revision too. It
    reads the migration scripts from disk, so call it once at startup.
    """
    return head_revision()
