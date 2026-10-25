# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic migrations: fresh databases, reruns, unknown revisions, and model drift."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from dinnerbell.config import Settings
from dinnerbell.db.engine import create_sync_engine
from dinnerbell.db.migrate import SchemaTooNewError, head_revision, run_migrations
from dinnerbell.db.models import Base

M0_TABLES = {"household", "member", "session", "audit"}
M1_TABLES = {"tag", "catalog_item", "catalog_item_tag", "catalog_item_cook", "preference", "meal_log"}
M2_TABLES = {"event", "response", "suggestion", "proposal", "vote", "assignment", "outbox"}
M3_TABLES = {"feedback"}


def test_migrates_an_empty_database(settings: Settings) -> None:
    assert not settings.database_path.exists()
    run_migrations(settings.sync_database_url, settings.database_path)
    engine = create_engine(settings.sync_database_url)
    try:
        inspector = inspect(engine)
        everything = M0_TABLES | M1_TABLES | M2_TABLES | M3_TABLES
        assert set(inspector.get_table_names()) == everything | {"alembic_version"}
        for table in everything - {"household"}:
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

    from dinnerbell.db.migrate import _alembic_config, migration_engine

    engine = migration_engine(migrated.sync_database_url)
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
    """Upgrade as ``run_migrations`` does, with foreign keys off, but only to ``revision``."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config, migration_engine

    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = migration_engine(settings.sync_database_url)
    try:
        with engine.begin() as connection:
            command.upgrade(_alembic_config(connection), revision)
    finally:
        engine.dispose()


def _downgrade_to(settings: Settings, revision: str) -> None:
    """Downgrade with foreign keys off, so a batch rebuild can't cascade (#36)."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config, migration_engine

    engine = migration_engine(settings.sync_database_url)
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
            # Regression: pysqlite committed each CREATE TABLE as it ran, so the refused upgrade
            # left 0003's tables behind and the next start failed with "table event already exists".
            tables = {row[0] for row in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
            assert tables == M0_TABLES | M1_TABLES | {"alembic_version"}
        with engine.begin() as c:
            c.execute(text("UPDATE meal_log SET event_id = NULL"))
        run_migrations(settings.sync_database_url, settings.database_path)  # fixed: now it upgrades
        with engine.connect() as c:
            assert MigrationContext.configure(c).get_current_heads() == (head_revision(),)
    finally:
        engine.dispose()


def test_0006_adds_who_called_a_call_off_and_downgrade_removes_it(settings: Settings) -> None:
    _upgrade_to(settings, "0005_event_locked_by")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO household (name, timezone, settings, created_at) VALUES ('H', 'UTC', '{}', :t)"),
                {"t": STAMP},
            )
            c.execute(text(EVENT_INSERT), {"kind": "call", "t": STAMP})
        run_migrations(settings.sync_database_url, settings.database_path)
        columns = {c["name"]: c for c in inspect(engine).get_columns("event")}
        assert columns["ended_by"]["nullable"] and columns["end_note"]["nullable"]
        fks = [fk for fk in inspect(engine).get_foreign_keys("event") if fk["constrained_columns"] == ["ended_by"]]
        assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in fks] == [("member", "SET NULL")]
        with engine.begin() as c:
            assert c.execute(text("SELECT ended_by, end_note FROM event")).all() == [(None, None)]  # existing rows
            c.execute(
                text(
                    "INSERT INTO member (household_id, display_name, email, role, can_cook, can_pickup, can_order, "
                    "mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
                    "VALUES (1, 'Sam', 'sam@example.com', 'member', 0, 0, 0, 0, 0, 1, 0, :t, :t)"
                ),
                {"t": STAMP},
            )
            c.execute(text("UPDATE event SET state = 'cancelled', ended_by = 1, end_note = 'Snow day'"))
    finally:
        engine.dispose()
    _downgrade_to(settings, "0005_event_locked_by")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        assert not {"ended_by", "end_note"} & {c["name"] for c in inspect(engine).get_columns("event")}
        with engine.connect() as c:
            assert c.execute(text("SELECT state FROM event")).all() == [("cancelled",)]  # the call itself stays
    finally:
        engine.dispose()
    run_migrations(settings.sync_database_url, settings.database_path)


# 0007: event IDs are never reused (#60) -------------------------------------------------------

EVENT_CHILDREN = ("response", "suggestion", "proposal", "vote", "assignment", "outbox", "meal_log")
CALL_ROWS = [
    "INSERT INTO household (id, name, timezone, settings, created_at) VALUES (1, 'H', 'UTC', '{}', :t)",
    "INSERT INTO member (id, household_id, display_name, email, role, can_cook, can_pickup, can_order, "
    "mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
    "VALUES (1, 1, 'Sam', 'sam@example.com', 'admin', 1, 0, 0, 0, 0, 1, 0, :t, :t)",
    "INSERT INTO catalog_item (id, household_id, kind, name, name_key, modes, active, created_at, updated_at) "
    "VALUES (1, 1, 'restaurant', 'X', 'x', '[\"pickup\"]', 1, :t, :t)",
    "INSERT INTO event (id, household_id, kind, slot, date, state, run, version, opens_at, respond_deadline, "
    "reminder_minutes, vote_minutes, created_by, coordinator_id, decided_item_id, decided_mode, created_at, "
    "updated_at) VALUES (1, 1, 'call', 'dinner', '2026-10-21', 'done', 1, 3, :t, :t, 15, 45, 1, 1, 1, 'pickup', "
    ":t, :t)",
    "INSERT INTO event (id, household_id, kind, slot, date, state, run, version, opens_at, respond_deadline, "
    "reminder_minutes, vote_minutes, created_at, updated_at) "
    "VALUES (2, 1, 'call', 'dinner', '2026-10-22', 'voting', 2, 1, :t, :t, 15, 45, :t, :t)",
    "INSERT INTO response (household_id, event_id, member_id, status, guests, guest_needs, offers_cook, "
    "offers_pickup, offers_order, maybe_at_deadline, responded_at, updated_at) "
    "VALUES (1, 2, 1, 'in', 1, '[]', 0, 1, 0, 0, :t, :t), (1, 1, 1, 'in', 0, '[]', 0, 0, 0, 0, :t, :t)",
    "INSERT INTO suggestion (household_id, event_id, member_id, free_text, created_at) VALUES (1, 2, 1, 'Soup', :t)",
    "INSERT INTO proposal (id, household_id, event_id, item_id, mode, rank, reasons, source, created_at) "
    "VALUES (1, 1, 2, 1, 'pickup', 1, '[]', 'system', :t)",
    "INSERT INTO vote (household_id, event_id, proposal_id, member_id, created_at) VALUES (1, 2, 1, 1, :t)",
    "INSERT INTO assignment (household_id, event_id, member_id, role, created_at) VALUES (1, 1, 1, 'pickup', :t)",
    "INSERT INTO outbox (household_id, event_id, kind, payload, idempotency_key, message_key, created_at, "
    "updated_at, available_at, attempts, delivered_at, ref_channel, ref_message) VALUES "
    "(1, 2, 'call_opened', '{}', 'event:2:run2:call_opened', 'event:2:run2:call', :t, :t, :t, 1, :t, 'C1', '1.0'), "
    "(1, 2, 'call_updated', '{}', 'event:2:run2:call_updated:1', 'event:2:run2:call', :t, :t, :t, 0, NULL, NULL, "
    "NULL)",
    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, event_id, created_at, "
    "updated_at) VALUES (1, '2026-10-21', 'dinner', 1, '[]', '[]', 1, :t, :t)",
]


def _populate_calls(settings: Settings) -> None:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            for statement in CALL_ROWS:
                c.execute(text(statement), {"t": STAMP})
    finally:
        engine.dispose()


def _snapshot(settings: Settings) -> dict[str, object]:
    """Every row of ``event`` and the tables that refer to it, and the event table's indexes
    and constraints, for comparing across a rebuild."""
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            rows = {t: c.execute(text(f"SELECT * FROM {t} ORDER BY rowid")).all() for t in ("event", *EVENT_CHILDREN)}  # noqa: S608
            indexes = c.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name")
            ).all()
            table = c.execute(text("SELECT sql FROM sqlite_master WHERE name = 'event'")).scalar_one()
            assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
    # The primary key moves inline (``id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT``); the rest stay.
    lines = (line.strip().rstrip(",") for line in table.splitlines())
    constraints = sorted(line for line in lines if "CONSTRAINT" in line and "PRIMARY KEY" not in line)
    return {"rows": rows, "indexes": indexes, "constraints": constraints}


def _event_sql(settings: Settings) -> str:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            sql: str = c.execute(text("SELECT sql FROM sqlite_master WHERE name = 'event'")).scalar_one()
    finally:
        engine.dispose()
    return sql


def test_0007_rebuilds_event_without_losing_a_row(settings: Settings) -> None:
    """Rebuilding ``event``, the parent of every call table, keeps every child row and every
    reference, up and down and up again, along with the event table's indexes (the one-call-per-
    slot partial index too) and constraints."""
    _upgrade_to(settings, "0006_event_ended_by")
    _populate_calls(settings)
    before = _snapshot(settings)
    assert all(before["rows"][t] for t in ("event", *EVENT_CHILDREN))  # type: ignore[index]
    assert "AUTOINCREMENT" not in _event_sql(settings)

    _upgrade_to(settings, "0007_event_autoincrement")  # later revisions add their own indexes
    assert "AUTOINCREMENT" in _event_sql(settings)
    assert _snapshot(settings) == before

    _downgrade_to(settings, "0006_event_ended_by")
    assert "AUTOINCREMENT" not in _event_sql(settings)
    assert _snapshot(settings) == before

    _upgrade_to(settings, "0007_event_autoincrement")  # later revisions add their own indexes
    assert _snapshot(settings) == before
    index = next(i for i in before["indexes"] if i[0] == "uq_event_call_per_slot")  # type: ignore[attr-defined]
    assert index[1].endswith("WHERE kind = 'call'")


def test_0007_never_reuses_an_event_id(settings: Settings, tmp_path: Path) -> None:
    """After the upgrade, deleting the newest event doesn't free its ID, on a database that
    already had events before migrating."""
    _upgrade_to(settings, "0006_event_ended_by")
    _populate_calls(settings)
    run_migrations(settings.sync_database_url, settings.database_path)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(text("DELETE FROM event WHERE id = 2"))
            c.execute(text(EVENT_INSERT), {"kind": "call", "t": STAMP})
            assert c.execute(text("SELECT max(id) FROM event")).scalar() == 3
            assert c.execute(text("SELECT count(*) FROM response WHERE event_id = 3")).scalar() == 0
    finally:
        engine.dispose()


def test_0007_refuses_to_run_with_foreign_keys_on(settings: Settings) -> None:
    """With foreign keys on, dropping the old event table would cascade, so 0007 refuses."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    _upgrade_to(settings, "0006_event_ended_by")
    _populate_calls(settings)
    engine = create_sync_engine(settings.sync_database_url)  # the app's engine: foreign keys on
    try:
        with pytest.raises(RuntimeError, match="foreign keys on"), engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "0007_event_autoincrement")
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM response")).scalar() == 2
    finally:
        engine.dispose()


def test_0008_adds_feedback_that_goes_with_its_meal_and_downgrade_removes_it(settings: Settings) -> None:
    _upgrade_to(settings, "0007_event_autoincrement")
    engine = create_sync_engine(settings.sync_database_url)
    member = (
        "INSERT INTO member (household_id, display_name, email, role, can_cook, can_pickup, can_order, "
        "mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
        "VALUES (1, 'Sam', 'sam@example.com', 'member', 0, 0, 0, 0, 0, 1, 0, :t, :t)"
    )
    try:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO household (name, timezone, settings, created_at) VALUES ('H', 'UTC', '{}', :t)"),
                {"t": STAMP},
            )
            c.execute(text(member), {"t": STAMP})
            c.execute(
                text(
                    "INSERT INTO catalog_item (household_id, kind, name, name_key, modes, active, created_at, "
                    "updated_at) VALUES (1, 'restaurant', 'X', 'x', '[\"pickup\"]', 1, :t, :t)"
                ),
                {"t": STAMP},
            )
            c.execute(
                text(
                    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, "
                    "created_at, updated_at) VALUES (1, '2026-10-22', 'dinner', 1, '[]', '[]', :t, :t)"
                ),
                {"t": STAMP},
            )
        run_migrations(settings.sync_database_url, settings.database_path)
        engine.dispose()  # a connection from before the table existed reads stale foreign key lists
        fks = {
            (fk["constrained_columns"][0], fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspect(engine).get_foreign_keys("feedback")
        }
        assert fks == {
            ("household_id", "household", "CASCADE"),
            ("meal_id", "meal_log", "CASCADE"),  # feedback goes with its meal
            ("member_id", "member", "CASCADE"),
            ("item_id", "catalog_item", "RESTRICT"),
            ("cook_member_id", "member", "SET NULL"),
        }
        insert = text(
            "INSERT INTO feedback (household_id, meal_id, member_id, food, place_issues, cook_thanks, item_id, "
            "item_name, item_kind, participants, created_at, updated_at) "
            "VALUES (1, 1, 1, :food, '[]', 0, 1, 'X', 'restaurant', '[]', :t, :t)"
        )
        with engine.begin() as c:
            c.execute(text("PRAGMA foreign_keys=ON"))
            c.execute(insert, {"food": "not_again", "t": STAMP})
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(insert, {"food": "loved", "t": STAMP})  # one per member per meal
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text("UPDATE feedback SET food = 'meh'"))
        with engine.begin() as c:
            c.execute(text("PRAGMA foreign_keys=ON"))
            c.execute(text("DELETE FROM meal_log"))
            assert c.execute(text("SELECT count(*) FROM feedback")).scalar() == 0
    finally:
        engine.dispose()
    _downgrade_to(settings, "0007_event_autoincrement")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        assert "feedback" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    run_migrations(settings.sync_database_url, settings.database_path)


# 0009: meal IDs are never reused (#56) ---------------------------------------------------------

FEEDBACK_ROW = (
    "INSERT INTO feedback (household_id, meal_id, member_id, food, place_issues, cook_thanks, item_id, item_name, "
    "item_kind, participants, created_at, updated_at) VALUES (1, :meal, 1, 'loved', '[]', 0, 1, 'X', 'restaurant', "
    "'[]', :t, :t)"
)
MEAL_ROW = (
    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, created_at, updated_at) "
    "VALUES (1, :day, 'lunch', 1, '[]', '[]', :t, :t)"
)


def _populate_meals(settings: Settings) -> None:
    """The calls of 0007's test, a second meal with no call, and feedback on both meals."""
    _populate_calls(settings)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(text(MEAL_ROW), {"day": "2026-10-22", "t": STAMP})
            for meal in (1, 2):
                c.execute(text(FEEDBACK_ROW), {"meal": meal, "t": STAMP})
    finally:
        engine.dispose()


def _meal_snapshot(settings: Settings) -> dict[str, object]:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            rows = {t: c.execute(text(f"SELECT * FROM {t} ORDER BY rowid")).all() for t in ("meal_log", "feedback")}  # noqa: S608
            indexes = c.execute(
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'meal_log' "
                    "AND sql IS NOT NULL ORDER BY name"
                )
            ).all()
            assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
    return {"rows": rows, "indexes": indexes}


def _meal_log_sql(settings: Settings) -> str:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            sql: str = c.execute(text("SELECT sql FROM sqlite_master WHERE name = 'meal_log'")).scalar_one()
    finally:
        engine.dispose()
    return sql


def test_0009_rebuilds_meal_log_without_losing_a_row_or_its_feedback(settings: Settings) -> None:
    """Feedback cascades with its meal, so a rebuild with foreign keys on would empty it. Every
    meal and every feedback row survive, up, down, and up again, with meal_log's indexes."""
    _upgrade_to(settings, "0008_meal_feedback")
    _populate_meals(settings)
    before = _meal_snapshot(settings)
    assert [len(before["rows"][t]) for t in ("meal_log", "feedback")] == [2, 2]  # type: ignore[index]
    assert "AUTOINCREMENT" not in _meal_log_sql(settings)

    _upgrade_to(settings, "0009_meal_log_autoincrement")
    assert "AUTOINCREMENT" in _meal_log_sql(settings)
    assert _meal_snapshot(settings) == before

    _downgrade_to(settings, "0008_meal_feedback")
    assert "AUTOINCREMENT" not in _meal_log_sql(settings)
    assert _meal_snapshot(settings) == before

    _upgrade_to(settings, "0009_meal_log_autoincrement")
    assert _meal_snapshot(settings) == before
    assert _snapshot(settings)["rows"]["meal_log"] == before["rows"]["meal_log"]  # type: ignore[index]


def test_0009_never_reuses_a_meal_id(settings: Settings) -> None:
    """After the upgrade, deleting the newest meal doesn't free its ID, and deleting a meal
    still deletes its feedback."""
    _upgrade_to(settings, "0008_meal_feedback")
    _populate_meals(settings)
    run_migrations(settings.sync_database_url, settings.database_path)
    engine = create_sync_engine(settings.sync_database_url)  # foreign keys on, as the app runs
    try:
        with engine.begin() as c:
            c.execute(text("DELETE FROM meal_log WHERE id = 2"))
            assert c.execute(text("SELECT count(*) FROM feedback WHERE meal_id = 2")).scalar() == 0
            c.execute(text(MEAL_ROW), {"day": "2026-10-23", "t": STAMP})
            assert c.execute(text("SELECT max(id) FROM meal_log")).scalar() == 3
            c.execute(text("DELETE FROM meal_log"))
            c.execute(text(MEAL_ROW), {"day": "2026-10-24", "t": STAMP})
            assert c.execute(text("SELECT id FROM meal_log")).scalar() == 4
    finally:
        engine.dispose()


def test_0009_refuses_to_run_with_foreign_keys_on(settings: Settings) -> None:
    """With foreign keys on, dropping the old meal_log would delete all feedback, so 0009 refuses."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    _upgrade_to(settings, "0008_meal_feedback")
    _populate_meals(settings)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with pytest.raises(RuntimeError, match="foreign keys on"), engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "0009_meal_log_autoincrement")
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM feedback")).scalar() == 2
    finally:
        engine.dispose()
