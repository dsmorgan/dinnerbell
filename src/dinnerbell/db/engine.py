# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine construction with the SQLite pragmas Dinnerbell depends on."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import Engine, create_engine, event, text
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


def create_readonly_engine(path: Path) -> AsyncEngine:
    """A read-only engine on the SQLite file at ``path`` (``mode=ro``). It sets no pragmas, so
    it never changes the journal mode, and SQLite can't create the file or write to it."""
    uri = f"file:{quote(str(path.resolve()))}?mode=ro"
    return create_async_engine(f"sqlite+aiosqlite:///{uri}&uri=true", hide_parameters=True)


@asynccontextmanager
async def read_snapshot(session: AsyncSession) -> AsyncIterator[None]:
    """Make every read inside the block see one snapshot of the database, then end it.

    pysqlite's default (legacy) transaction handling emits BEGIN only before INSERT, UPDATE, and
    DELETE, so each SELECT otherwise sees whatever was committed when it ran, and a write that
    commits between two queries shows up in one but not the other. An explicit BEGIN opens a
    read transaction: under WAL, later writers don't block and aren't seen. The block ends
    with a rollback, so it can only read.
    """
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("read_snapshot needs a session with no unsaved changes")
    await session.rollback()  # end the session's own (read-only) transaction, so BEGIN can start one
    await session.execute(text("BEGIN"))
    try:
        yield
    finally:
        await session.rollback()


@asynccontextmanager
async def write_transaction(session: AsyncSession) -> AsyncIterator[None]:
    """Run a read-modify-write as one transaction that holds SQLite's write lock from its first
    read. Commit inside the block; anything left uncommitted is rolled back at the end.

    pysqlite's legacy transaction handling begins a transaction only at the first INSERT,
    UPDATE, or DELETE, so two sessions could otherwise both read a value, then each write their
    own change over it, and one change would be lost. ``BEGIN IMMEDIATE`` takes the write lock
    first: a second writer waits (``busy_timeout``) and then reads what the first committed.
    """
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("write_transaction needs a session with no unsaved changes")
    await session.rollback()  # end the session's own transaction, so BEGIN can start one
    await session.execute(text("BEGIN IMMEDIATE"))
    try:
        yield
    finally:
        await session.rollback()  # a no-op after a commit; otherwise it releases the lock


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
