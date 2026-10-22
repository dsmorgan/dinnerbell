# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine construction with the SQLite pragmas Dinnerbell depends on."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

BUSY_TIMEOUT_MS = 5000


def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_sync_engine(url: str) -> Engine:
    # hide_parameters keeps bound values (emails, tokens) out of error messages and logs (SEC-5).
    engine = create_engine(url, hide_parameters=True)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def create_app_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url, hide_parameters=True)
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
