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


async def database_reachable(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with sessionmaker() as db:
            await db.execute(text("SELECT 1 FROM household LIMIT 1"))
    except (SQLAlchemyError, OSError):
        log.warning("database health check failed", exc_info=True)
        return False
    return True


def schema_revision() -> str:
    """The schema revision this build migrates the database to, such as ``0006_event_ended_by``.

    ``dinnerbell serve`` upgrades the database to it before it starts, and refuses a database at
    a revision it doesn't know, so while the app runs this is the database's revision too. It
    reads the migration scripts from disk, so call it once at startup.
    """
    return head_revision()
