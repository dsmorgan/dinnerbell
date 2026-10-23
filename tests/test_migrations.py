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
M2_TABLES = {"event", "response", "suggestion", "proposal", "vote", "assignment", "outbox"}


def test_migrates_an_empty_database(settings: Settings) -> None:
    assert not settings.database_path.exists()
    run_migrations(settings.sync_database_url, settings.database_path)
    engine = create_engine(settings.sync_database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == M0_TABLES | M1_TABLES | M2_TABLES | {"alembic_version"}
        for table in (M0_TABLES | M1_TABLES | M2_TABLES) - {"household"}:
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


def _upgrade_to(settings: Settings, revision: str) -> None:
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as connection:
            command.upgrade(_alembic_config(connection), revision)
    finally:
        engine.dispose()


def _downgrade_to(settings: Settings, revision: str) -> None:
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as connection:
            command.downgrade(_alembic_config(connection), revision)
    finally:
        engine.dispose()


def test_meal_log_event_has_no_foreign_key_until_m2(settings: Settings) -> None:
    _upgrade_to(settings, "0002_catalog")
    engine = create_engine(settings.sync_database_url)
    try:
        inspector = inspect(engine)
        assert not set(inspector.get_table_names()) & M2_TABLES
        event = next(c for c in inspector.get_columns("meal_log") if c["name"] == "event_id")
        assert event["nullable"]
        assert all("event_id" not in fk["constrained_columns"] for fk in inspector.get_foreign_keys("meal_log"))
    finally:
        engine.dispose()


def test_m2_links_meal_log_to_event_once(migrated: Settings) -> None:
    engine = create_engine(migrated.sync_database_url)
    try:
        inspector = inspect(engine)
        fks = [fk for fk in inspector.get_foreign_keys("meal_log") if fk["constrained_columns"] == ["event_id"]]
        assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in fks] == [("event", "SET NULL")]
        assert {"column_names": ["event_id"], "name": "uq_meal_log_event_id"} in [
            {"column_names": u["column_names"], "name": u["name"]} for u in inspector.get_unique_constraints("meal_log")
        ]
        calls = next(i for i in inspector.get_indexes("event") if i["name"] == "uq_event_call_per_slot")
        assert calls["unique"] and calls["column_names"] == ["household_id", "slot", "date"]
    finally:
        engine.dispose()


EVENT_INSERT = (
    "INSERT INTO event (household_id, kind, slot, date, state, run, version, opens_at, respond_deadline, "
    "reminder_minutes, vote_minutes, created_at, updated_at) VALUES (1, :kind, 'dinner', '2026-10-22', "
    "'collecting', 1, 1, :t, :t, 15, 45, :t, :t)"
)
STAMP = "2026-10-22 00:00:00"


def test_one_call_per_household_slot_and_date(migrated: Settings) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = create_sync_engine(migrated.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO household (name, timezone, settings, created_at) VALUES ('H', 'UTC', '{}', :t)"),
                {"t": STAMP},
            )
            c.execute(text(EVENT_INSERT), {"kind": "call", "t": STAMP})
            c.execute(text(EVENT_INSERT), {"kind": "offer", "t": STAMP})  # offers (M3) don't count
        with pytest.raises(IntegrityError, match="UNIQUE"), engine.begin() as c:
            c.execute(text(EVENT_INSERT), {"kind": "call", "t": STAMP})
    finally:
        engine.dispose()


def test_upgrade_backfills_schedule_and_downgrade_removes_it(settings: Settings) -> None:
    import json

    _upgrade_to(settings, "0002_catalog")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO household (name, timezone, settings, created_at) VALUES "
                    "('Home', 'UTC', '{\"variety_window_days\": 9}', :t), "
                    "('Custom', 'UTC', '{\"proposal_count\": 5}', :t)"
                ),
                {"t": STAMP},
            )
        run_migrations(settings.sync_database_url, settings.database_path)
        with engine.connect() as c:
            stored = [json.loads(row[0]) for row in c.execute(text("SELECT settings FROM household ORDER BY id"))]
        assert stored[0]["variety_window_days"] == 9
        assert stored[0]["slots"]["dinner"]["call_time"] == "15:00"
        assert stored[0]["slots"]["lunch"]["enabled"] is False
        assert stored[0]["ranking_weights"]["cuisine_variety"] == 3.0
        assert stored[0]["proposal_count"] == 3
        assert stored[1]["proposal_count"] == 5  # an existing value is kept
        with engine.begin() as c:
            c.execute(text(EVENT_INSERT), {"kind": "call", "t": STAMP})
            item = c.execute(
                text(
                    "INSERT INTO catalog_item (household_id, kind, name, name_key, modes, active, created_at, "
                    "updated_at) VALUES (1, 'restaurant', 'X', 'x', '[\"pickup\"]', 1, :t, :t) RETURNING id"
                ),
                {"t": STAMP},
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, event_id, "
                    "created_at, updated_at) VALUES (1, '2026-10-22', 'dinner', :i, '[]', '[]', 1, :t, :t)"
                ),
                {"i": item, "t": STAMP},
            )
    finally:
        engine.dispose()
    _downgrade_to(settings, "0002_catalog")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            stored = [json.loads(row[0]) for row in c.execute(text("SELECT settings FROM household ORDER BY id"))]
            meals = c.execute(text("SELECT event_id FROM meal_log")).all()
        assert stored == [{"variety_window_days": 9}, {}]
        assert meals == [(None,)]  # the meal stays; its link to the dropped event doesn't
        assert not set(inspect(engine).get_table_names()) & M2_TABLES
    finally:
        engine.dispose()
    run_migrations(settings.sync_database_url, settings.database_path)


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
            command.upgrade(_alembic_config(connection), "0002_catalog")
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


def test_migrations_run_without_cascades_and_refuse_dangling_references(settings: Settings) -> None:
    """Migrations run with foreign keys off, so a batch rebuild can't cascade-delete rows, then
    check every reference and roll back if any is broken. Regression: a dangling event_id made
    0003's rebuild of meal_log fail with a raw integrity error."""
    from dinnerbell.db.migrate import IntegrityCheckFailed

    _upgrade_to(settings, "0002_catalog")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO household (name, timezone, settings, created_at) VALUES ('H', 'UTC', '{}', :t)"),
                {"t": STAMP},
            )
            c.execute(
                text(
                    "INSERT INTO catalog_item (household_id, kind, name, name_key, modes, active, created_at, "
                    "updated_at) VALUES (1, 'restaurant', 'X', 'x', '[\"pickup\"]', 1, :t, :t)"
                ),
                {"t": STAMP},
            )
            c.execute(
                text(
                    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, event_id, "
                    "created_at, updated_at) VALUES (1, '2026-10-22', 'dinner', 1, '[]', '[]', 999, :t, :t)"
                ),
                {"t": STAMP},
            )
        with pytest.raises(IntegrityCheckFailed, match="meal_log"):
            run_migrations(settings.sync_database_url, settings.database_path)
        with engine.connect() as c:
            assert MigrationContext.configure(c).get_current_heads() == ("0002_catalog",)  # rolled back
            assert c.execute(text("SELECT event_id FROM meal_log")).all() == [(999,)]
            assert c.execute(text("PRAGMA foreign_keys")).scalar() == 1  # app connections keep them on
    finally:
        engine.dispose()
