# SPDX-License-Identifier: AGPL-3.0-or-later
"""Health probes."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)


async def database_reachable(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    try:
        async with sessionmaker() as db:
            await db.execute(text("SELECT 1 FROM household LIMIT 1"))
    except (SQLAlchemyError, OSError):
        log.warning("database health check failed", exc_info=True)
        return False
    return True
