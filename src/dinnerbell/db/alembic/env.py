# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic environment.

At runtime ``dinnerbell.db.migrate`` passes an open connection. For authoring revisions,
``uv run alembic revision --autogenerate`` uses ``sqlalchemy.url`` from alembic.ini.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from dinnerbell.db.engine import create_sync_engine
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
    engine = create_sync_engine(url)
    try:
        with engine.begin() as new_connection:
            _run(new_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")
run_migrations_online()
