# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic migrations: fresh databases, reruns, unknown revisions, and model drift."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from dinnerbell.config import Settings
from dinnerbell.db.engine import create_sync_engine
from dinnerbell.db.migrate import SchemaTooNewError, head_revision, run_migrations
from dinnerbell.db.models import Base

M0_TABLES = {"household", "member", "session", "audit"}
M1_TABLES = {"tag", "catalog_item", "catalog_item_tag", "catalog_item_cook", "preference", "meal_log"}
M2_TABLES = {"event", "response", "suggestion", "proposal", "vote", "assignment"}


def test_migrates_an_empty_database(settings: Settings) -> None:
    assert not settings.database_path.exists()
    run_migrations(settings.sync_database_url, settings.database_path)
    engine = create_engine(settings.sync_database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == M0_TABLES | M1_TABLES | {"alembic_version"}
        for table in (M0_TABLES | M1_TABLES) - {"household"}:
            assert "household_id" in {c["name"] for c in inspector.get_columns(table)}, table
        member_columns = {c["name"] for c in inspector.get_columns("member")}
        assert {"feedback_reminder", "mute_dm_reminders", "oidc_subject", "slack_user_id"} <= member_columns
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_heads() == (head_revision(),)
    finally:
        engine.dispose()


def test_rerun_is_a_no_op(migrated: Settings) -> None:
    run_migrations(migrated.sync_database_url, migrated.database_path)


def test_pragmas_are_applied(migrated: Settings) -> None:
    engine = create_sync_engine(migrated.sync_database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_refuses_unknown_newer_revision(migrated: Settings) -> None:
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '9999_from_the_future'"))
    finally:
        engine.dispose()
    with pytest.raises(SchemaTooNewError, match="9999_from_the_future"):
        run_migrations(migrated.sync_database_url, migrated.database_path)


def test_models_match_migrations(migrated: Settings) -> None:
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()


def test_creates_missing_data_directory(tmp_path: Path) -> None:
    from tests.conftest import make_settings

    settings = make_settings(tmp_path, database_url=f"sqlite:///{tmp_path}/nested/dir/db.sqlite")
    run_migrations(settings.sync_database_url, settings.database_path)
    assert settings.database_path.exists()


def test_downgrade_then_upgrade(migrated: Settings) -> None:
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    engine = create_sync_engine(migrated.sync_database_url)
    try:
        with engine.begin() as connection:
            command.downgrade(_alembic_config(connection), "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()
    run_migrations(migrated.sync_database_url, migrated.database_path)


def test_database_errors_hide_bound_parameters(migrated: Settings) -> None:
    """SEC-5: an SQL error message must not carry the email or token it was called with."""
    import asyncio

    from sqlalchemy.exc import OperationalError

    from dinnerbell.db.engine import create_app_engine

    statement = text("SELECT * FROM no_such_table WHERE email = :email")
    engine = create_sync_engine(migrated.sync_database_url)
    try:
        with engine.connect() as connection, pytest.raises(OperationalError) as caught:
            connection.execute(statement, {"email": "leak@example.com"})
    finally:
        engine.dispose()
    assert "leak@example.com" not in str(caught.value)

    async def run_async() -> str:
        async_engine = create_app_engine(migrated.async_database_url)
        try:
            async with async_engine.connect() as connection:
                await connection.execute(statement, {"email": "leak@example.com"})
        except OperationalError as exc:
            return str(exc)
        finally:
            await async_engine.dispose()
        return ""

    message = asyncio.run(run_async())
    assert "no_such_table" in message
    assert "leak@example.com" not in message


def test_meal_log_event_has_no_foreign_key_until_m2(migrated: Settings) -> None:
    engine = create_engine(migrated.sync_database_url)
    try:
        inspector = inspect(engine)
        assert not set(inspector.get_table_names()) & M2_TABLES
        event = next(c for c in inspector.get_columns("meal_log") if c["name"] == "event_id")
        assert event["nullable"]
        assert all("event_id" not in fk["constrained_columns"] for fk in inspector.get_foreign_keys("meal_log"))
    finally:
        engine.dispose()


def test_upgrade_backfills_variety_window(settings: Settings) -> None:
    import json

    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "0001_initial")
            connection.execute(
                text(
                    "INSERT INTO household (name, timezone, settings, created_at) "
                    "VALUES ('Home', 'UTC', '{\"slack\": 1}', '2026-10-22 00:00:00'),"
                    " ('Other', 'UTC', '{\"variety_window_days\": 10}', '2026-10-22 00:00:00')"
                )
            )
        run_migrations(settings.sync_database_url, settings.database_path)
        with engine.connect() as connection:
            stored = [
                json.loads(row[0]) for row in connection.execute(text("SELECT settings FROM household ORDER BY id"))
            ]
        assert stored == [{"slack": 1, "variety_window_days": 7}, {"variety_window_days": 10}]
        with engine.begin() as connection:
            command.downgrade(_alembic_config(connection), "0001_initial")
        with engine.connect() as connection:
            stored = [
                json.loads(row[0]) for row in connection.execute(text("SELECT settings FROM household ORDER BY id"))
            ]
        assert stored == [{"slack": 1}, {}]
    finally:
        engine.dispose()
