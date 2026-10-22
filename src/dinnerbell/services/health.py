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


@dataclass
class SchedulerState:
    """What the scheduler's jobs record and /healthz reports. The composition root shares one
    instance between them, so neither layer imports the other."""

    last_tick: datetime | None = None
    last_backup: datetime | None = None

    def tick_is_fresh(self, now: datetime) -> bool:
        return self.last_tick is not None and now - self.last_tick <= HEARTBEAT_INTERVAL * 3


async def database_reachable(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with sessionmaker() as db:
            await db.execute(text("SELECT 1 FROM household LIMIT 1"))
    except (SQLAlchemyError, OSError):
        log.warning("database health check failed", exc_info=True)
        return False
    return True
