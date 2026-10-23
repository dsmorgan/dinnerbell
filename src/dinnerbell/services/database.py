# SPDX-License-Identifier: AGPL-3.0-or-later
"""Open the application database for the web app and scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from dinnerbell.db.engine import create_app_engine, create_readonly_engine, create_sessionmaker


@dataclass(frozen=True)
class Database:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    async def close(self) -> None:
        await self.engine.dispose()


def open_database(async_url: str) -> Database:
    engine = create_app_engine(async_url)
    return Database(engine=engine, sessionmaker=create_sessionmaker(engine))


def open_readonly_database(path: Path) -> Database:
    """Open the SQLite file at ``path`` read-only, such as to inspect a database before deciding
    to change it."""
    engine = create_readonly_engine(path)
    return Database(engine=engine, sessionmaker=create_sessionmaker(engine))
