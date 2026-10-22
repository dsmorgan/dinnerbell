# SPDX-License-Identifier: AGPL-3.0-or-later
"""Health probes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = timedelta(seconds=30)
STALE_AFTER = HEARTBEAT_INTERVAL * 3
"""A tick older than this makes /healthz report "degraded"."""
STOPPED_AFTER = timedelta(minutes=5)
"""A tick older than this means the scheduler is dead: /healthz returns 503."""


@dataclass
class SchedulerState:
    """What the scheduler's jobs record and /healthz reports. The composition root shares one
    instance between them, so neither layer imports the other."""

    started_at: datetime | None = None
    last_tick: datetime | None = None
    last_backup: datetime | None = None

    def tick_is_fresh(self, now: datetime) -> bool:
        return self.last_tick is not None and now - self.last_tick <= STALE_AFTER

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


async def database_reachable(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with sessionmaker() as db:
            await db.execute(text("SELECT 1 FROM household LIMIT 1"))
    except (SQLAlchemyError, OSError):
        log.warning("database health check failed", exc_info=True)
        return False
    return True
