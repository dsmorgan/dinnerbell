# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run Alembic migrations, refusing databases written by a newer Dinnerbell."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection

from dinnerbell.db.engine import create_sync_engine

SCRIPT_LOCATION = Path(__file__).parent / "alembic"
log = logging.getLogger(__name__)


class SchemaTooNewError(Exception):
    """The database records a schema revision this build does not know."""


class IntegrityCheckFailed(Exception):
    """After migrating, some rows refer to rows that don't exist. Nothing was changed."""


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


def _check_references(connection: Connection) -> None:
    broken = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if broken:
        tables = ", ".join(sorted({str(row[0]) for row in broken}))
        raise IntegrityCheckFailed(
            f"The schema upgrade would leave rows in {tables} referring to rows that don't exist, so "
            "nothing was changed. Restore a backup from /data/backups/ or report this as a bug."
        )


def run_migrations(sync_url: str, database_path: Path) -> None:
    """Upgrade the database at ``sync_url`` to the newest revision.

    Foreign keys are off while migrating: SQLite rebuilds a table to change it (Alembic's batch
    mode), and dropping the old copy with foreign keys on would cascade-delete or null out the
    rows that refer to it. Every reference is checked before committing instead, and any broken
    one rolls the whole upgrade back. Connections the app opens keep foreign keys on.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_sync_engine(sync_url)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")  # takes effect only outside a transaction
            connection.commit()
            with connection.begin():
                config = _alembic_config(connection)
                _check_known(connection, ScriptDirectory.from_config(config))
                command.upgrade(config, "head")
                _check_references(connection)
    finally:
        engine.dispose()
    log.info("database schema is current", extra={"revision": head_revision()})
