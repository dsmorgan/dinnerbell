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

from dinnerbell.db.engine import create_sync_engine

SCRIPT_LOCATION = Path(__file__).parent / "alembic"
log = logging.getLogger(__name__)


class SchemaTooNewError(Exception):
    """The database records a schema revision this build does not know."""


class IntegrityCheckFailed(Exception):
    """After migrating up or down, some rows refer to rows that don't exist. Nothing was changed."""


class UpgradeBlocked(Exception):
    """A migration found data it can't carry over, and says which row. Nothing was changed."""


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
    """Raise ``IntegrityCheckFailed``, naming the tables, if any row refers to a row that doesn't exist."""
    broken = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if broken:
        tables = ", ".join(sorted({str(row[0]) for row in broken}))
        raise IntegrityCheckFailed(
            f"The schema change would leave rows in {tables} referring to rows that don't exist, so "
            "nothing was changed. Restore a backup from /data/backups/ or report this as a bug."
        )


@contextmanager
def migrating(engine: Engine) -> Iterator[Connection]:
    """One transaction for a migration, up or down. ``engine`` is a ``migration_engine`` (foreign
    keys off). Every reference is checked before the commit, and a broken one rolls everything
    back. The app, the Alembic CLI (``env.py``), and the tests all migrate through this (#36)."""
    with engine.begin() as connection:
        yield connection
        check_references(connection)


def run_migrations(sync_url: str, database_path: Path) -> None:
    """Upgrade the database at ``sync_url`` to the newest revision, all in one transaction.

    Foreign keys are off while migrating: SQLite rebuilds a table to change it (Alembic's batch
    mode), and dropping the old copy with foreign keys on would cascade-delete or null out the
    rows that refer to it. Every reference is checked before committing instead, and any broken
    one rolls the whole upgrade back, schema changes included (see ``migration_engine`` and
    ``migrating``).
    Connections the app opens keep foreign keys on.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = migration_engine(sync_url)
    try:
        with migrating(engine) as connection:
            config = _alembic_config(connection)
            _check_known(connection, ScriptDirectory.from_config(config))
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    log.info("database schema is current", extra={"revision": head_revision()})


def migration_engine(sync_url: str) -> Engine:
    """An engine whose transactions really contain DDL.

    pysqlite's default (legacy) transaction handling emits BEGIN only before INSERT, UPDATE, and
    DELETE, so each CREATE TABLE commits as it runs and a failed upgrade would leave half its
    tables behind. Here pysqlite is in autocommit mode and SQLAlchemy's ``begin`` emits a real
    BEGIN, so a rollback undoes everything. Foreign keys are switched off on connect, outside any
    transaction, where SQLite honors the PRAGMA.
    """
    engine = create_sync_engine(sync_url)

    @event.listens_for(engine, "connect")
    def _autocommit_without_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    return engine
