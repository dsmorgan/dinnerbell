# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic environment.

At runtime ``dinnerbell.db.migrate`` passes an open connection. For authoring revisions,
``uv run alembic revision --autogenerate`` uses ``sqlalchemy.url`` from alembic.ini, through the
same engine and transaction ``run_migrations`` uses: foreign keys off, so a batch rebuild can't
cascade, and a reference check before the commit, for ``upgrade`` and ``downgrade`` alike (#36).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.exc import OperationalError

from dinnerbell.db.migrate import LOCKED_MESSAGE, MigrationLocked, is_locked, migrating, migration_engine
from dinnerbell.db.models import Base

config = context.config
target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("set sqlalchemy.url in alembic.ini to author migrations")
    engine = migration_engine(url)
    try:
        with migrating(engine) as new_connection:
            _run(new_connection)
    except OperationalError as exc:
        if is_locked(exc):
            raise MigrationLocked(LOCKED_MESSAGE) from None
        raise
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")
run_migrations_online()
