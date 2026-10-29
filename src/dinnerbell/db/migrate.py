# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run Alembic migrations, refusing databases written by a newer Dinnerbell."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection, Engine, event
from sqlalchemy.exc import OperationalError

from dinnerbell.db.engine import BUSY_TIMEOUT_MS, create_sync_engine

SCRIPT_LOCATION = Path(__file__).parent / "alembic"
log = logging.getLogger(__name__)
LOCK_ATTEMPTS = 3
"""How many times a migration waits ``BUSY_TIMEOUT_MS`` for another process that is migrating
the same database, so the longest wait is about 15 seconds."""


class SchemaTooNewError(Exception):
    """The database records a schema revision this build does not know."""


class IntegrityCheckFailed(Exception):
    """After migrating up or down, some rows refer to rows that don't exist. Nothing was changed."""


class UpgradeBlocked(Exception):
    """A migration found data it can't carry over, and says which row. Nothing was changed."""


class MigrationLocked(Exception):
    """Another process held the database's write lock for the whole wait, most likely because it
    is migrating the same database. Nothing was changed."""


def _alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    return ScriptDirectory.from_config(config).get_current_head() or "base"


def _check_known(connection: Connection, script: ScriptDirectory) -> None:
    for revision in MigrationContext.configure(connection).get_current_heads():
        try:
            known = script.get_revision(revision)
        except CommandError:
            known = None
        if known is None:
            raise SchemaTooNewError(
                f"The database is at schema revision {revision!r}, which this version of Dinnerbell "
                "does not know. It was probably written by a newer release. Run the newer image, "
                "or restore a backup from /data/backups/ taken before the upgrade."
            )


def check_references(connection: Connection) -> None:
    """Raise ``IntegrityCheckFailed``, naming the tables, if any row refers to a row that doesn't
    exist. The log gets the first few (table, rowid, parent table), never a row's contents."""
    broken = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if broken:
        tables = ", ".join(sorted({str(row[0]) for row in broken}))
        for table, rowid, parent, _ in broken[:5]:
            log.error("dangling reference", extra={"table": table, "rowid": rowid, "parent": parent})
        raise IntegrityCheckFailed(
            f"Rows in {tables} refer to rows that don't exist: this schema change left them that way, "
            "or they were already broken before it. Nothing was changed. Restore a backup from "
            "/data/backups/; if the database was fine before, report this as a bug."
        )


@contextmanager
def migrating(engine: Engine) -> Iterator[Connection]:
    """One transaction for a migration, up or down. ``engine`` is a ``migration_engine`` (foreign
    keys off). Every reference is checked before the commit, and a broken one rolls everything
    back. The app, the Alembic CLI (``env.py``), and the tests all migrate through this (#36)."""
    with engine.begin() as connection:
        yield connection
        check_references(connection)


def _is_locked(error: OperationalError) -> bool:
    message = str(error.orig).lower()
    return "locked" in message or "busy" in message


def run_migrations(
    sync_url: str, database_path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS, attempts: int = LOCK_ATTEMPTS
) -> None:
    """Upgrade the database at ``sync_url`` to the newest revision, all in one transaction.

    Foreign keys are off while migrating: SQLite rebuilds a table to change it (Alembic's batch
    mode), and dropping the old copy with foreign keys on would cascade-delete or null out the
    rows that refer to it. Every reference is checked before committing instead, and any broken
    one rolls the whole upgrade back, schema changes included (see ``migration_engine`` and
    ``migrating``).
    Connections the app opens keep foreign keys on.

    Two processes migrating at once take turns: the transaction starts with ``BEGIN IMMEDIATE``,
    so the second waits (``busy_timeout_ms``, ``attempts`` times) and then finds the schema
    current. If the first doesn't finish in that time, raises ``MigrationLocked``.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        engine = migration_engine(sync_url, busy_timeout_ms=busy_timeout_ms)
        try:
            with migrating(engine) as connection:
                config = _alembic_config(connection)
                _check_known(connection, ScriptDirectory.from_config(config))
                command.upgrade(config, "head")
            break
        except OperationalError as exc:
            if not _is_locked(exc):
                raise
            if attempt == attempts:
                raise MigrationLocked(
                    "The database is locked: another Dinnerbell process is probably migrating it "
                    "right now (a second `migrate`, `serve`, or `reset-history`). Wait for it to "
                    "finish, then run this again. Nothing was changed."
                ) from None
            log.warning("database is locked by another process; waiting to migrate", extra={"attempt": attempt})
        finally:
            engine.dispose()
    log.info("database schema is current", extra={"revision": head_revision()})


def migration_engine(sync_url: str, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> Engine:
    """An engine whose transactions really contain DDL.

    pysqlite's default (legacy) transaction handling emits BEGIN only before INSERT, UPDATE, and
    DELETE, so each CREATE TABLE commits as it runs and a failed upgrade would leave half its
    tables behind. Here pysqlite is in autocommit mode and SQLAlchemy's ``begin`` emits a real
    BEGIN IMMEDIATE, so a rollback undoes everything and a second migrating process waits for the
    first (up to ``busy_timeout_ms``) instead of failing on a stale read snapshot. Foreign keys are
    switched off on connect, outside any transaction, where SQLite honors the PRAGMA.
    """
    engine = create_sync_engine(sync_url)

    @event.listens_for(engine, "connect")
    def _autocommit_without_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")
        dbapi_connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
