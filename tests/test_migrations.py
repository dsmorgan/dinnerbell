# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic migrations: fresh databases, reruns, unknown revisions, and model drift."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
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
M3_TABLES = {"feedback", "invitation"}
M4_TABLES = {"contribution", "badge_award", "slack_badge_file", "slack_badge_composite"}
M7_TABLES = {"stance_edit"}


def test_migrates_an_empty_database(settings: Settings) -> None:
    assert not settings.database_path.exists()
    run_migrations(settings.sync_database_url, settings.database_path)
    engine = create_engine(settings.sync_database_url)
    try:
        inspector = inspect(engine)
        everything = M0_TABLES | M1_TABLES | M2_TABLES | M3_TABLES | M4_TABLES | M7_TABLES
        assert set(inspector.get_table_names()) == everything | {"alembic_version"}
        for table in everything - {"household"}:
            assert "household_id" in {c["name"] for c in inspector.get_columns(table)}, table
        member_columns = {c["name"] for c in inspector.get_columns("member")}
        assert {"feedback_reminder", "mute_dm_reminders", "oidc_subject", "slack_user_id"} <= member_columns
        assert {"dietary_needs", "join_refused"} <= member_columns
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
    """Downgrade with foreign keys off, so a batch rebuild can't cascade, and check every
    reference before committing (#36)."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config, migrating, migration_engine

    engine = migration_engine(settings.sync_database_url)
    try:
        with migrating(engine) as connection:
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
OFFER_INSERT = (
    "INSERT INTO event (household_id, kind, slot, date, state, run, version, opens_at, respond_deadline, "
    "reminder_minutes, vote_minutes, capacity, created_at, updated_at) VALUES (1, 'offer', 'dinner', "
    "'2026-10-22', 'collecting', 1, 1, :t, :t, 0, 0, :capacity, :t, :t)"
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
            c.execute(text(OFFER_INSERT), {"capacity": 3, "t": STAMP})  # offers don't count
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
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                    "AND tbl_name NOT IN ('contribution', 'badge_award', 'slack_badge_file', 'slack_badge_composite', "
                    "'stance_edit') "
                    "ORDER BY name"
                )
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
        _upgrade_to(settings, "0008_meal_feedback")  # 0020 replaces food and place with rating
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


# 0010: offers' spare servings --------------------------------------------------------------------


def _event_foreign_keys(settings: Settings) -> list[object]:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            # id and seq number the keys, and a rebuild may number them differently.
            return sorted(tuple(row)[2:] for row in c.execute(text("PRAGMA foreign_key_list(event)")).all())
    finally:
        engine.dispose()


def _without_capacity(snapshot: dict[str, object]) -> dict[str, object]:
    """``snapshot`` with event rows minus their last column, ``capacity``, which must be NULL."""
    rows = dict(snapshot["rows"])  # type: ignore[call-overload]
    assert all(row[-1] is None for row in rows["event"])
    rows["event"] = [tuple(row)[:-1] for row in rows["event"]]
    return {**snapshot, "rows": rows}


CAPACITY_CHECK = (
    "CONSTRAINT ck_event_capacity CHECK ((kind = 'call' AND capacity IS NULL) OR "
    "(kind = 'offer' AND capacity IS NOT NULL AND capacity BETWEEN 1 AND 20))"
)


def test_0010_rebuilds_event_without_losing_a_row(settings: Settings) -> None:
    """Up, down, and up again, every event and child row survives, with the event table's
    indexes, foreign keys, and constraints; the upgrade adds only the empty capacity column and
    its check."""
    _upgrade_to(settings, "0009_meal_log_autoincrement")
    _populate_calls(settings)
    before, fks = _snapshot(settings), _event_foreign_keys(settings)
    assert all(before["rows"][t] for t in ("event", *EVENT_CHILDREN))  # type: ignore[index]

    _upgrade_to(settings, "0010_offers")
    after = _without_capacity(_snapshot(settings))
    assert after["rows"] == before["rows"] and after["indexes"] == before["indexes"]
    assert after["constraints"] == sorted([*before["constraints"], CAPACITY_CHECK])  # type: ignore[misc]
    assert _event_foreign_keys(settings) == fks
    assert "AUTOINCREMENT" in _event_sql(settings)

    _downgrade_to(settings, "0009_meal_log_autoincrement")
    assert _snapshot(settings) == before and _event_foreign_keys(settings) == fks
    _upgrade_to(settings, "0010_offers")
    assert _without_capacity(_snapshot(settings))["rows"] == before["rows"]


def _delete_newest_call(settings: Settings) -> None:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            for table in ("vote", "proposal", "suggestion", "response", "outbox"):
                c.execute(text(f"DELETE FROM {table} WHERE event_id = 2"))  # noqa: S608 - fixed names
            c.execute(text("DELETE FROM event WHERE id = 2"))
    finally:
        engine.dispose()


def _next_event_id(settings: Settings) -> int:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(text(EVENT_INSERT), {"kind": "call", "t": STAMP})
            found: int = c.execute(text("SELECT max(id) FROM event")).scalar_one()
    finally:
        engine.dispose()
    return found


@pytest.mark.parametrize("down_and_up", [False, True])
def test_0010_never_gives_back_an_id_deleted_before_it(settings: Settings, down_and_up: bool) -> None:
    """The newest call was deleted (discarded, say) before the upgrade. The rebuild must keep
    the AUTOINCREMENT high-water mark, not reseed it from the largest ID left, or that call's ID
    comes back (#60). A downgrade rebuilds the table too, and keeps it as well."""
    _upgrade_to(settings, "0009_meal_log_autoincrement")
    _populate_calls(settings)
    _delete_newest_call(settings)
    _upgrade_to(settings, "0010_offers")
    if down_and_up:
        _downgrade_to(settings, "0009_meal_log_autoincrement")
        _upgrade_to(settings, "0010_offers")
    assert _next_event_id(settings) == 3


def test_0010_lets_only_offers_have_a_capacity(settings: Settings) -> None:
    """A call can't have a capacity and an offer must have one from 1 to 20. A downgrade removes
    offers with their rows, leaving everything else as it was."""
    from sqlalchemy.exc import IntegrityError

    _upgrade_to(settings, "0009_meal_log_autoincrement")
    _populate_calls(settings)
    before = _snapshot(settings)
    _upgrade_to(settings, "0010_offers")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        for capacity in (None, 0, 21):
            with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as c:
                c.execute(text(OFFER_INSERT), {"capacity": capacity, "t": STAMP})
        with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as c:
            c.execute(text(OFFER_INSERT.replace("'offer'", "'call'")), {"capacity": 3, "t": STAMP})
        with engine.begin() as c:
            c.execute(text(OFFER_INSERT), {"capacity": 3, "t": STAMP})
            offer = c.execute(text("SELECT max(id) FROM event")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO response (household_id, event_id, member_id, status, guests, guest_needs, "
                    "offers_cook, offers_pickup, offers_order, maybe_at_deadline, responded_at, updated_at) "
                    "VALUES (1, :e, 1, 'in', 0, '[]', 0, 0, 0, 0, :t, :t)"
                ),
                {"e": offer, "t": STAMP},
            )
    finally:
        engine.dispose()
    _downgrade_to(settings, "0009_meal_log_autoincrement")
    assert _snapshot(settings)["rows"] == before["rows"]
    run_migrations(settings.sync_database_url, settings.database_path)


def test_0010_refuses_to_run_with_foreign_keys_on(settings: Settings) -> None:
    """Like 0007, it rebuilds ``event``, so with foreign keys on it refuses rather than cascade."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    _upgrade_to(settings, "0009_meal_log_autoincrement")
    _populate_calls(settings)
    engine = create_sync_engine(settings.sync_database_url)  # the app's engine: foreign keys on
    try:
        with pytest.raises(RuntimeError, match="foreign keys on"), engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "0010_offers")
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM response")).scalar() == 2
    finally:
        engine.dispose()


# 0011: guests (#38) ------------------------------------------------------------------------------

GUEST_INSERT = (
    "INSERT INTO member (household_id, display_name, email, role, can_cook, can_pickup, can_order, "
    "mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
    "VALUES (1, :name, :email, :role, 0, 0, 0, 0, 0, 1, 0, :t, :t)"
)


def _members(settings: Settings) -> list[tuple[object, ...]]:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text("SELECT * FROM member ORDER BY id")).all()]
    finally:
        engine.dispose()


def test_0011_adds_guests_and_invitations_without_losing_a_row(settings: Settings) -> None:
    """The member rebuild keeps every member and every row that refers to one; afterward only a
    guest may have no email, and a downgrade takes guests away with their rows."""
    from sqlalchemy.exc import IntegrityError

    _upgrade_to(settings, "0010_offers")
    _populate_calls(settings)
    before, members = _snapshot(settings), _members(settings)
    _upgrade_to(settings, "0011_guests")
    assert _snapshot(settings)["rows"] == before["rows"]
    # plus dietary_needs, join_refused, slack_email, slack_guest_account
    assert [m[:-4] for m in _members(settings)] == members
    assert all(m[-4:] == ("[]", 0, None, 0) for m in _members(settings))
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as c:
            c.execute(text(GUEST_INSERT), {"name": "Ann", "email": None, "role": "member", "t": STAMP})
        with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as c:  # nor an empty one
            c.execute(text(GUEST_INSERT), {"name": "Cy", "email": "", "role": "member", "t": STAMP})
        with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as c:  # a guest never has a sign-in email
            c.execute(text(GUEST_INSERT), {"name": "Bo", "email": "bo@example.com", "role": "guest", "t": STAMP})
        with engine.begin() as c:
            c.execute(text(GUEST_INSERT), {"name": "Pat", "email": None, "role": "guest", "t": STAMP})
            guest = c.execute(text("SELECT max(id) FROM member")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO invitation (household_id, event_id, guest_id, invited_by, created_at) "
                    "VALUES (1, 2, :g, 1, :t)"
                ),
                {"g": guest, "t": STAMP},
            )
            c.execute(
                text(
                    "INSERT INTO response (household_id, event_id, member_id, status, guests, guest_needs, "
                    "offers_cook, offers_pickup, offers_order, maybe_at_deadline, responded_at, updated_at) "
                    "VALUES (1, 2, :g, 'in', 0, '[]', 0, 0, 0, 0, :t, :t)"
                ),
                {"g": guest, "t": STAMP},
            )
        with pytest.raises(IntegrityError, match="UNIQUE"), engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO invitation (household_id, event_id, guest_id, invited_by, created_at) "
                    "VALUES (1, 2, :g, 1, :t)"
                ),
                {"g": guest, "t": STAMP},
            )
        with engine.begin() as c:
            c.execute(text("PRAGMA foreign_keys=ON"))
            c.execute(text("DELETE FROM event WHERE id = 2"))
            assert c.execute(text("SELECT count(*) FROM invitation")).scalar() == 0  # goes with its call
    finally:
        engine.dispose()
    _downgrade_to(settings, "0010_offers")
    assert _members(settings) == members
    assert "invitation" not in inspect(create_engine(settings.sync_database_url)).get_table_names()
    run_migrations(settings.sync_database_url, settings.database_path)


def test_0011_names_a_member_with_an_empty_email_and_changes_nothing(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The new email check would fail the rebuild with a raw IntegrityError. The upgrade says
    which member instead (by ID, never the email), logs the usual critical line, and rolls back."""
    from dinnerbell import main as cli
    from dinnerbell.db.migrate import UpgradeBlocked

    _upgrade_to(settings, "0010_offers")
    _populate_calls(settings)
    _execute(settings, "UPDATE member SET email = '' WHERE id = 1")
    before = _members(settings)
    with pytest.raises(UpgradeBlocked, match=r"Member 1 has an empty email"):
        run_migrations(settings.sync_database_url, settings.database_path)
    assert _members(settings) == before
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            assert MigrationContext.configure(c).get_current_heads() == ("0010_offers",)
    finally:
        engine.dispose()
    with caplog.at_level(logging.CRITICAL):
        assert cli._migrate(settings) is False
    assert [r.levelno for r in caplog.records] == [logging.CRITICAL]
    assert "Member 1 has an empty email" in caplog.records[0].getMessage()
    _execute(settings, "UPDATE member SET email = 'sam@example.com' WHERE id = 1")
    run_migrations(settings.sync_database_url, settings.database_path)


def test_0011_refuses_to_run_with_foreign_keys_on(settings: Settings) -> None:
    """Rebuilding ``member`` with foreign keys on would cascade to nearly every table."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    _upgrade_to(settings, "0010_offers")
    _populate_calls(settings)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with pytest.raises(RuntimeError, match="foreign keys on"), engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "0011_guests")
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM response")).scalar() == 2
    finally:
        engine.dispose()


# 0012: the meals each catalog item is for (#72) --------------------------------------------------

CATALOG_ROWS = [
    "INSERT INTO catalog_item (id, household_id, kind, name, name_key, modes, active, created_at, updated_at) "
    "VALUES (2, 1, 'home', 'Pancakes', 'pancakes', '[\"cook\"]', 0, :t, :t)",
    "INSERT INTO tag (id, household_id, kind, name, name_key, created_at) VALUES (1, 1, 'cuisine', 'Thai', 'thai', :t)",
    "INSERT INTO catalog_item_tag (household_id, item_id, tag_id) VALUES (1, 1, 1), (1, 2, 1)",
    "INSERT INTO catalog_item_cook (household_id, item_id, member_id) VALUES (1, 2, 1)",
    "INSERT INTO preference (household_id, member_id, item_id, veto, score, updated_at) VALUES (1, 1, 2, 0, 2, :t)",
    "INSERT INTO suggestion (household_id, event_id, member_id, item_id, created_at) VALUES (1, 2, 1, 2, :t)",
]
CATALOG_CHILDREN = ("catalog_item_tag", "catalog_item_cook", "preference")


@pytest.fixture
def migrate_command(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``dinnerbell migrate``'s environment, pointed at ``settings``' database."""
    from tests.test_config import REQUIRED

    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DINNERBELL_INSTANCE_LABEL", raising=False)
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", settings.sync_database_url)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        yield
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


def _populate_catalog(settings: Settings) -> None:
    _populate_calls(settings)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            for statement in CATALOG_ROWS:
                c.execute(text(statement), {"t": STAMP})
    finally:
        engine.dispose()


def _catalog(settings: Settings) -> dict[str, object]:
    """Every catalog item, and every row of the tables that refer to one."""
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            rows = {t: c.execute(text(f"SELECT * FROM {t} ORDER BY rowid")).all() for t in CATALOG_CHILDREN}  # noqa: S608
            items = [tuple(r) for r in c.execute(text("SELECT * FROM catalog_item ORDER BY id")).all()]
            assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
    return {"items": items, "children": rows}


def _catalog_columns(settings: Settings) -> list[str]:
    return [c["name"] for c in inspect(create_engine(settings.sync_database_url)).get_columns("catalog_item")]


def _calls_before_late_join(settings: Settings) -> object:
    """The call rows, without the columns 0014 adds to ``response`` at the end (#76) and 0015
    adds to ``event`` (#87), for comparing with rows read before ``dinnerbell migrate`` took the
    database to the head."""
    rows: dict[str, list[tuple[object, ...]]] = _snapshot(settings)["rows"]  # type: ignore[assignment]
    return rows | {
        "response": [tuple(r)[: -len(LATE_COLUMNS)] for r in rows["response"]],
        "event": [tuple(r)[: -len(COORDINATOR_COLUMNS)] for r in rows["event"]],
    }


def test_0012_gives_every_item_lunch_and_dinner_and_downgrade_drops_the_column(
    settings: Settings, migrate_command: None
) -> None:
    """``dinnerbell migrate`` on a populated 0011 database rebuilds ``catalog_item`` without losing
    an item or a row that refers to one, and every item is for lunch and dinner (6). Afterward
    at least one meal is required. Down and up again gives the same."""
    from dinnerbell import main as cli
    from dinnerbell.db.catalog import meals_from_bits
    from dinnerbell.domain.catalog import DEFAULT_MEALS

    _upgrade_to(settings, "0011_guests")
    _populate_catalog(settings)
    before, calls = _catalog(settings), _snapshot(settings)["rows"]
    assert "meals" not in _catalog_columns(settings)

    assert cli.main(["migrate"]) == 0
    after = _catalog(settings)
    assert after["children"] == before["children"] and _calls_before_late_join(settings) == calls
    assert _catalog_columns(settings)[-1] == "meals"
    assert [i[:-1] for i in after["items"]] == before["items"]  # type: ignore[attr-defined]
    assert {i[-1] for i in after["items"]} == {6}  # type: ignore[attr-defined]
    assert meals_from_bits(6) == DEFAULT_MEALS
    engine = create_sync_engine(settings.sync_database_url)
    try:
        for bits in (0, 8, -1):
            with pytest.raises(IntegrityError, match="CHECK"), engine.begin() as c:
                c.execute(text("UPDATE catalog_item SET meals = :m WHERE id = 1"), {"m": bits})
        with engine.begin() as c:
            c.execute(text("UPDATE catalog_item SET meals = 1 WHERE id = 2"))  # breakfast only
    finally:
        engine.dispose()

    _downgrade_to(settings, "0011_guests")
    assert "meals" not in _catalog_columns(settings)
    assert _catalog(settings) == before and _snapshot(settings)["rows"] == calls

    assert cli.main(["migrate"]) == 0
    assert {i[-1] for i in _catalog(settings)["items"]} == {6}  # type: ignore[attr-defined]


def test_0012_refuses_to_run_with_foreign_keys_on(settings: Settings) -> None:
    """Rebuilding ``catalog_item`` with foreign keys on would cascade to its tags, cooks, and
    preferences, and the proposals and meals that refer to it would refuse the drop."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    _upgrade_to(settings, "0011_guests")
    _populate_catalog(settings)
    before = _catalog(settings)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with pytest.raises(RuntimeError, match="foreign keys on"), engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "0012_catalog_meals")
    finally:
        engine.dispose()
    assert _catalog(settings) == before


# 0013: breakfast calls (#58) ---------------------------------------------------------------------

BREAKFAST = "0013_breakfast"
BEFORE_BREAKFAST = "0012_catalog_meals"
"""0013's revision and the one before it. Renumbering 0013 changes only these two."""
BREAKFAST_CALL = (
    "INSERT INTO event (household_id, kind, slot, date, state, run, version, opens_at, respond_deadline, "
    "reminder_minutes, vote_minutes, created_at, updated_at) VALUES (1, 'call', 'breakfast', '2026-10-24', "
    "'collecting', 1, 1, :t, :t, 10, 15, :t, :t)"
)
BREAKFAST_MEAL = (
    "INSERT INTO meal_log (household_id, date, slot, item_id, cuisine_tags, participants, created_at, updated_at) "
    "VALUES (1, '2026-10-24', 'breakfast', 1, '[]', '[]', :t, :t)"
)


def _execute(settings: Settings, *statements: str) -> None:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            for statement in statements:
                c.execute(text(statement), {"t": STAMP})
    finally:
        engine.dispose()


def _slot_checks(settings: Settings) -> list[str]:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            tables = [
                c.execute(text("SELECT sql FROM sqlite_master WHERE name = :n"), {"n": n}).scalar_one()
                for n in ("event", "meal_log")
            ]
    finally:
        engine.dispose()
    return [line.strip().rstrip(",") for sql in tables for line in sql.splitlines() if "ck_" in line and "slot" in line]


def _sequences(settings: Settings) -> dict[str, int]:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            return {str(n): int(s) for n, s in c.execute(text("SELECT name, seq FROM sqlite_sequence")).all()}
    finally:
        engine.dispose()


def test_0013_lets_calls_and_meals_be_breakfast_and_downgrade_refuses_while_any_are(
    settings: Settings, migrate_command: None
) -> None:
    """``dinnerbell migrate`` on a populated 0012 database rebuilds ``event`` and ``meal_log``
    without losing a row, an index, a foreign key, or an AUTOINCREMENT high-water mark, and both
    take breakfast afterward. A downgrade refuses while a breakfast call or meal exists, and
    changes nothing; without them it goes back to lunch and dinner only, and up again."""
    from alembic.util.exc import CommandError

    from dinnerbell import main as cli

    _upgrade_to(settings, BEFORE_BREAKFAST)
    _populate_calls(settings)
    _delete_newest_call(settings)  # the rebuild must not give its ID back
    before, fks, sequences = _snapshot(settings), _event_foreign_keys(settings), _sequences(settings)
    with pytest.raises(IntegrityError, match="CHECK"):
        _execute(settings, BREAKFAST_CALL)
    with pytest.raises(IntegrityError, match="CHECK"):
        _execute(settings, BREAKFAST_MEAL)

    assert cli.main(["migrate"]) == 0
    after = _snapshot(settings)
    assert _calls_before_late_join(settings) == before["rows"] and after["indexes"] == before["indexes"]
    # 0024 rebuilds badge_award with AUTOINCREMENT, which adds its own (empty) high-water mark.
    assert _event_foreign_keys(settings) == fks and _sequences(settings) == sequences | {"badge_award": 0}
    assert "AUTOINCREMENT" in _event_sql(settings)
    assert _slot_checks(settings) == [
        "CONSTRAINT ck_event_slot CHECK (slot IN ('breakfast', 'lunch', 'dinner'))",
        "CONSTRAINT ck_meal_log_slot CHECK (slot IN ('breakfast', 'lunch', 'dinner'))",
    ]
    with pytest.raises(IntegrityError, match="CHECK"):
        _execute(settings, BREAKFAST_CALL.replace("'breakfast'", "'brunch'"))
    _execute(settings, BREAKFAST_CALL, BREAKFAST_MEAL)
    assert _next_event_id(settings) == 4  # the breakfast call took 3; 2 never comes back
    with_breakfast = _snapshot(settings)

    refusal = rf"1 breakfast call\(s\) or offer\(s\) and 1 breakfast meal\(s\), which {BEFORE_BREAKFAST} can't"
    with pytest.raises((RuntimeError, CommandError), match=refusal):
        _downgrade_to(settings, BEFORE_BREAKFAST)
    assert _snapshot(settings) == with_breakfast
    _execute(
        settings,
        "DELETE FROM event WHERE slot = 'breakfast'",
        "DELETE FROM meal_log WHERE slot = 'breakfast'",
        "DELETE FROM event WHERE id = 4",
    )
    _downgrade_to(settings, BEFORE_BREAKFAST)
    assert _snapshot(settings)["rows"] == before["rows"] and _event_foreign_keys(settings) == fks
    assert "slot IN ('lunch', 'dinner')" in _slot_checks(settings)[0]
    assert cli.main(["migrate"]) == 0
    assert _calls_before_late_join(settings) == before["rows"]
    assert _next_event_id(settings) == 5


def test_0013_refuses_to_run_with_foreign_keys_on(settings: Settings) -> None:
    """Rebuilding ``event`` and ``meal_log`` with foreign keys on would cascade to every call's
    rows and all feedback."""
    from alembic import command

    from dinnerbell.db.migrate import _alembic_config

    _upgrade_to(settings, BEFORE_BREAKFAST)
    _populate_calls(settings)
    before = _snapshot(settings)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with pytest.raises(RuntimeError, match="foreign keys on"), engine.begin() as connection:
            command.upgrade(_alembic_config(connection), BREAKFAST)
    finally:
        engine.dispose()
    assert _snapshot(settings) == before


# 0014: joining a call late (#76) -----------------------------------------------------------------

LATE_COLUMNS = ("late", "vote_counts")


def _response_table(settings: Settings) -> tuple[list[str], str]:
    engine = create_engine(settings.sync_database_url)
    try:
        columns = [c["name"] for c in inspect(engine).get_columns("response")]
        with engine.connect() as c:
            table = c.execute(text("SELECT sql FROM sqlite_master WHERE name = 'response'")).scalar_one()
    finally:
        engine.dispose()
    return columns, table


def test_0014_marks_every_answer_on_time_and_downgrade_drops_the_columns(
    settings: Settings, migrate_command: None
) -> None:
    """``dinnerbell migrate`` on a populated 0013 database adds ``late`` and ``vote_counts`` to
    every answer, both false, in place: no other row or constraint changes. Down drops them and
    gives back the 0013 table exactly; up again gives the same."""
    from dinnerbell import main as cli

    _upgrade_to(settings, BREAKFAST)
    _populate_catalog(settings)
    before, catalog = _snapshot(settings), _catalog(settings)
    columns, table = _response_table(settings)
    assert not set(LATE_COLUMNS) & set(columns)
    assert before["rows"]["response"]  # type: ignore[index]

    assert cli.main(["migrate"]) == 0
    after, _ = _response_table(settings)
    assert after == [*columns, *LATE_COLUMNS]
    assert _calls_before_late_join(settings) == before["rows"] and _catalog(settings) == catalog
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            assert set(c.execute(text("SELECT late, vote_counts FROM response")).all()) == {(0, 0)}
            c.execute(text("UPDATE response SET late = 1, vote_counts = 1 WHERE id = 1"))
            with pytest.raises(IntegrityError, match="CHECK"):  # the 0003 checks are still there
                c.execute(text("UPDATE response SET status = 'late' WHERE id = 1"))
    finally:
        engine.dispose()

    _downgrade_to(settings, BREAKFAST)
    assert _response_table(settings) == (columns, table)
    assert _snapshot(settings) == before and _catalog(settings) == catalog

    assert cli.main(["migrate"]) == 0
    assert _response_table(settings)[0] == [*columns, *LATE_COLUMNS]
    assert _calls_before_late_join(settings) == before["rows"]
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            assert set(c.execute(text("SELECT late, vote_counts FROM response")).all()) == {(0, 0)}
    finally:
        engine.dispose()


# 0015: coordinator powers (#87) ------------------------------------------------------------------

COORDINATOR_COLUMNS = ("shuffles", "shuffle_note", "roles_due_at", "tie_how", "tie_by")
LATE_JOIN = "0014_late_join"


def _event_columns(settings: Settings) -> list[str]:
    engine = create_engine(settings.sync_database_url)
    try:
        return [c["name"] for c in inspect(engine).get_columns("event")]
    finally:
        engine.dispose()


def test_0015_adds_the_coordinator_columns_and_downgrade_drops_them(settings: Settings, migrate_command: None) -> None:
    """``dinnerbell migrate`` on a populated 0014 database adds the five coordinator columns to
    every call in place: ``shuffles`` is 0 and the rest are NULL, and no other row or constraint
    changes. Down drops them and gives back the 0014 table exactly; up again gives the same."""
    from dinnerbell import main as cli

    _upgrade_to(settings, LATE_JOIN)
    _populate_calls(settings)
    before = _snapshot(settings)
    columns, table = _event_columns(settings), _event_sql(settings)
    assert not set(COORDINATOR_COLUMNS) & set(columns)
    assert before["rows"]["event"]  # type: ignore[index]

    assert cli.main(["migrate"]) == 0
    assert _event_columns(settings) == [*columns, *COORDINATOR_COLUMNS]
    after = _snapshot(settings)
    assert [tuple(r)[: -len(COORDINATOR_COLUMNS)] for r in after["rows"]["event"]] == before["rows"]["event"]  # type: ignore[index]
    assert {tuple(r)[-len(COORDINATOR_COLUMNS) :] for r in after["rows"]["event"]} == {(0, None, None, None, None)}  # type: ignore[index]
    assert after["indexes"] == before["indexes"] and after["constraints"] == before["constraints"]
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:  # a call in flight can use them
            c.execute(
                text("UPDATE event SET shuffles = 2, shuffle_note = 'x', tie_how = 'chose', tie_by = 1"), {"t": STAMP}
            )
    finally:
        engine.dispose()

    _downgrade_to(settings, LATE_JOIN)
    assert _event_columns(settings) == columns and _event_sql(settings) == table
    assert _snapshot(settings) == before

    assert cli.main(["migrate"]) == 0
    assert _event_columns(settings) == [*columns, *COORDINATOR_COLUMNS]
    assert {tuple(r)[-len(COORDINATOR_COLUMNS) :] for r in _snapshot(settings)["rows"]["event"]} == {  # type: ignore[index]
        (0, None, None, None, None)
    }


def test_0015_downgrade_refuses_while_a_role_choice_is_pending(settings: Settings, migrate_command: None) -> None:
    """A call waiting on the coordinator has nobody assigned to its role, and an older release would
    never assign one, so the downgrade refuses, naming how many, and changes nothing."""
    from dinnerbell import main as cli

    _upgrade_to(settings, LATE_JOIN)
    _populate_calls(settings)
    assert cli.main(["migrate"]) == 0
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(text("UPDATE event SET roles_due_at = :t WHERE id = 1"), {"t": STAMP})
        columns = _event_columns(settings)
        with pytest.raises(RuntimeError, match=r"1 call\(s\) are waiting"):
            _downgrade_to(settings, LATE_JOIN)
        assert _event_columns(settings) == columns
        with engine.begin() as c:
            c.execute(text("UPDATE event SET roles_due_at = NULL"))
    finally:
        engine.dispose()
    _downgrade_to(settings, LATE_JOIN)
    assert not set(COORDINATOR_COLUMNS) & set(_event_columns(settings))


# 0016: contributions and badges (M4) -------------------------------------------------------------

COORDINATOR = "0015_coordinator"


def _tables(settings: Settings) -> set[str]:
    engine = create_engine(settings.sync_database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _rows_outside_m4(settings: Settings) -> dict[str, list[object]]:
    """Every row of every table that existed at 0015, for comparing across the migration."""
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            names = sorted(inspect(engine).get_table_names())
            return {
                t: list(c.execute(text(f"SELECT * FROM {t} ORDER BY rowid")).all())  # noqa: S608
                for t in names
                if t
                not in {
                    "contribution",
                    "badge_award",
                    "slack_badge_file",
                    "slack_badge_composite",
                    "stance_edit",
                    "alembic_version",
                }
            }
    finally:
        engine.dispose()


def test_0016_adds_contributions_and_badges_without_touching_a_row_and_downgrade_removes_them(
    settings: Settings, migrate_command: None
) -> None:
    """``dinnerbell migrate`` on a populated 0015 database adds two empty tables and changes no
    row. The tables refuse a duplicate count, a count with neither or both sources, a kind or tier
    they don't know, and an award made twice. Down drops them, clears the backfill marker, and
    gives back the 0015 database exactly; up again gives the same."""
    from dinnerbell import main as cli

    _upgrade_to(settings, COORDINATOR)
    _populate_calls(settings)
    _execute(settings, 'UPDATE household SET settings = \'{"contributions_backfilled": "x", "keep": 1}\'')
    before = _rows_outside_m4(settings)
    assert before["event"] and not {"contribution", "badge_award"} & _tables(settings)

    assert cli.main(["migrate"]) == 0
    assert {"contribution", "badge_award"} <= _tables(settings)
    after = _rows_outside_m4(settings)
    ours = {"household", "member"}
    assert {k: v for k, v in after.items() if k not in ours} == {k: v for k, v in before.items() if k not in ours}
    assert [row[:-2] for row in after["member"]] == before["member"]  # 0022 and 0023 add two, last
    (household,) = after["household"]
    assert json.loads(household[3]) == {"contributions_backfilled": "x", "keep": 1, "feedback_cap": 0}  # type: ignore[index]  # 0021
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM contribution")).scalar_one() == 0
            assert c.execute(text("SELECT count(*) FROM badge_award")).scalar_one() == 0
            assert c.execute(text("PRAGMA foreign_key_check")).all() == []
        credit = (
            "INSERT INTO contribution (household_id, member_id, kind, day, {sources}, created_at) "
            "VALUES (1, 1, '{kind}', '2026-10-21', {values}, :t)"
        )

        def count(kind: str, sources: str = "event_id", values: str = "1") -> str:
            return credit.format(kind=kind, sources=sources, values=values)

        with engine.begin() as c:
            c.execute(text(count("picked_up")), {"t": STAMP})
        for bad, why in (
            (count("picked_up"), "UNIQUE"),  # the same call counted twice
            (count("juggled"), "CHECK"),
            (count("cooked", "event_id, meal_id", "1, 1"), "CHECK"),  # from a call and a meal
        ):
            with pytest.raises(IntegrityError, match=why), engine.begin() as c:
                c.execute(text(bad), {"t": STAMP})
        award = (
            "INSERT INTO badge_award (household_id, member_id, badge_key, tier, awarded_at, announcement) "
            "VALUES (1, 1, 'chef', '{tier}', :t, '{state}')"
        )
        with engine.begin() as c:
            c.execute(text(award.format(tier="first", state="pending")), {"t": STAMP})
        for bad, why in (
            (award.format(tier="first", state="skipped"), "UNIQUE"),  # the same tier twice
            (award.format(tier="platinum", state="pending"), "CHECK"),
            (award.format(tier="bronze", state="maybe"), "CHECK"),
        ):
            with pytest.raises(IntegrityError, match=why), engine.begin() as c:
                c.execute(text(bad), {"t": STAMP})
        with engine.begin() as c:  # leave a row of each for the downgrade to drop
            c.execute(text("PRAGMA foreign_keys = ON"))
            c.execute(text("UPDATE badge_award SET event_id = 1"))
            c.execute(text("DELETE FROM contribution"))
    finally:
        engine.dispose()

    _downgrade_to(settings, COORDINATOR)
    assert not {"contribution", "badge_award"} & _tables(settings)
    assert _rows_outside_m4(settings)["event"] == before["event"]
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            assert json.loads(c.execute(text("SELECT settings FROM household")).scalar_one()) == {"keep": 1}
    finally:
        engine.dispose()

    assert cli.main(["migrate"]) == 0
    assert {"contribution", "badge_award"} <= _tables(settings)
    again = _rows_outside_m4(settings)
    assert {k: v for k, v in again.items() if k not in ours} == {k: v for k, v in before.items() if k not in ours}
    assert [row[:-2] for row in again["member"]] == before["member"]


# 0017: Slack badge art (#105) ------------------------------------------------------------------------


def test_0017_adds_the_badge_file_table_and_downgrade_removes_it(settings: Settings) -> None:
    """Up adds one empty table that refuses a second file for the same household, badge, and tier;
    down drops it and leaves the 0016 database as it was; up again gives the same."""
    _upgrade_to(settings, "0016_contributions")
    _populate_calls(settings)
    before = _rows_outside_m4(settings)
    assert "slack_badge_file" not in _tables(settings)

    _upgrade_to(settings, "0017_slack_badge_file")
    assert _rows_outside_m4(settings) == before
    row = (
        "INSERT INTO slack_badge_file (household_id, badge_key, tier, content_hash, file_id, uploaded_at) "
        "VALUES (1, 'chef', 'gold', 'abc', 'F1', :t)"
    )
    _execute(settings, row)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with pytest.raises(IntegrityError, match="UNIQUE"), engine.begin() as c:
            c.execute(text(row), {"t": STAMP})
    finally:
        engine.dispose()

    _downgrade_to(settings, "0016_contributions")
    assert "slack_badge_file" not in _tables(settings)
    assert _rows_outside_m4(settings) == before
    _upgrade_to(settings, "0017_slack_badge_file")
    assert "slack_badge_file" in _tables(settings)


# 0018: Gambler (#108) --------------------------------------------------------------------------------

GAMBLED_ROW = (
    "INSERT INTO contribution (household_id, member_id, kind, day, event_id, created_at) "
    "VALUES (1, 1, :kind, '2026-10-22', 1, :t)"
)


def test_0018_lets_a_contribution_be_gambled_and_downgrade_refuses_while_any_are(settings: Settings) -> None:
    """Up keeps every row and accepts ``gambled``; down refuses while one exists, restores the old
    check once none do, and up again gives the same."""
    _upgrade_to(settings, "0017_slack_badge_file")
    _populate_calls(settings)
    _execute(settings, GAMBLED_ROW.replace(":kind", "'cooked'"))
    with pytest.raises(IntegrityError, match="ck_contribution_kind"):
        _execute(settings, GAMBLED_ROW.replace(":kind", "'gambled'"))

    _upgrade_to(settings, "0018_gambler")
    _execute(settings, GAMBLED_ROW.replace(":kind", "'gambled'"))
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            kinds = sorted(c.execute(text("SELECT kind FROM contribution")).scalars())
    finally:
        engine.dispose()
    assert kinds == ["cooked", "gambled"]

    with pytest.raises(RuntimeError, match="1 gambled"):
        _downgrade_to(settings, "0017_slack_badge_file")
    _execute(settings, "DELETE FROM contribution WHERE kind = 'gambled'")
    _execute(settings, 'UPDATE household SET settings = \'{"gambler_backfilled": "x", "keep": 1}\'')
    _downgrade_to(settings, "0017_slack_badge_file")
    assert _household_settings(settings) == {"keep": 1}  # upgrading again counts Gambler afresh
    with pytest.raises(IntegrityError, match="ck_contribution_kind"):
        _execute(settings, GAMBLED_ROW.replace(":kind", "'gambled'"))
    _upgrade_to(settings, "0018_gambler")
    _execute(settings, GAMBLED_ROW.replace(":kind", "'gambled'"))


def _household_settings(settings: Settings) -> dict[str, object]:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            return dict(json.loads(c.execute(text("SELECT settings FROM household")).scalar_one()))
    finally:
        engine.dispose()


# 0019: composite badge pictures (#114) -----------------------------------------------------------------


def test_0019_adds_the_composite_table_and_downgrade_removes_it(settings: Settings) -> None:
    """Up adds one empty table that refuses a second picture for the same member; down drops it
    and leaves the 0018 database as it was; up again gives the same."""
    _upgrade_to(settings, "0018_gambler")
    _populate_calls(settings)
    before = _rows_outside_m4(settings)
    assert "slack_badge_composite" not in _tables(settings)

    _upgrade_to(settings, "0019_slack_badge_composite")
    assert _rows_outside_m4(settings) == before
    row = (
        "INSERT INTO slack_badge_composite (household_id, member_id, content_hash, file_id, uploaded_at) "
        "VALUES (1, 1, 'abc', 'F1', :t)"
    )
    _execute(settings, row)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with pytest.raises(IntegrityError, match="UNIQUE"), engine.begin() as c:
            c.execute(text(row), {"t": STAMP})
    finally:
        engine.dispose()

    _downgrade_to(settings, "0018_gambler")
    assert "slack_badge_composite" not in _tables(settings)
    _upgrade_to(settings, "0019_slack_badge_composite")
    assert "slack_badge_composite" in _tables(settings)


# 0020: one rating per meal -----------------------------------------------------------------------

OLD_FEEDBACK = (
    "INSERT INTO feedback (household_id, meal_id, member_id, food, place, place_issues, cook_thanks, cook_note, "
    "item_id, item_name, item_kind, participants, created_at, updated_at) "
    "VALUES (1, :meal, :member, {food}, {place}, '[\"other\"]', 0, 'Yum', 1, 'X', 'restaurant', '[]', :t, :t)"
)
# (food, place) before 0020, and the rating it becomes: scores loved +2, fine 0, not_again -2,
# averaged when both are set.
CONVERSIONS = [
    ("loved", None, "loved"),
    ("fine", None, "fine"),
    ("not_again", None, "hated"),
    (None, "loved", "loved"),
    (None, "not_again", "hated"),
    ("loved", "loved", "loved"),
    ("loved", "fine", "liked"),
    ("fine", "loved", "liked"),
    ("fine", "not_again", "disliked"),
    ("not_again", "fine", "disliked"),
    ("loved", "not_again", "fine"),
    ("not_again", "not_again", "hated"),
    ("fine", "fine", "fine"),
    (None, None, None),
]


def _literal(value: str | None) -> str:
    return "NULL" if value is None else f"'{value}'"


def _feedback_rows(settings: Settings, columns: str) -> list[tuple[object, ...]]:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text(f"SELECT {columns} FROM feedback ORDER BY id"))]  # noqa: S608
    finally:
        engine.dispose()


def test_0020_turns_food_and_place_into_one_rating_and_downgrade_turns_it_back(settings: Settings) -> None:
    """Up converts every row's food and place into one rating and keeps everything else; down
    turns the rating back into food, with place empty and the note gone; up again gives the
    same."""
    _upgrade_to(settings, "0019_slack_badge_composite")
    _populate_calls(settings)  # a household, members, and a restaurant
    meals = [MEAL_ROW.replace(":day", f"'2026-10-{n + 1:02d}'") for n in range(len(CONVERSIONS))]
    _execute(settings, *meals)
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            meal_ids = list(c.execute(text("SELECT id FROM meal_log ORDER BY id DESC")).scalars())[: len(CONVERSIONS)]
            for meal, (food, place, _) in zip(sorted(meal_ids), CONVERSIONS, strict=True):
                insert = OLD_FEEDBACK.format(food=_literal(food), place=_literal(place))
                c.execute(text(insert), {"meal": meal, "member": 1, "t": STAMP})
    finally:
        engine.dispose()
    kept = "id, meal_id, member_id, place_issues, cook_thanks, cook_note, item_id, created_at"
    before = _feedback_rows(settings, kept)

    _upgrade_to(settings, "0020_one_rating")
    assert [r[0] for r in _feedback_rows(settings, "rating")] == [want for _, _, want in CONVERSIONS]
    assert _feedback_rows(settings, kept) == before
    assert {r[0] for r in _feedback_rows(settings, "issue_note")} == {None}
    with pytest.raises(IntegrityError, match="ck_feedback_rating"):
        _execute(settings, "UPDATE feedback SET rating = 'not_again'")
    _execute(settings, "UPDATE feedback SET issue_note = 'No forks' WHERE rating = 'liked'")

    _downgrade_to(settings, "0019_slack_badge_composite")
    downgraded = {"hated": "not_again", "disliked": "not_again", "fine": "fine", "liked": "loved", "loved": "loved"}
    assert _feedback_rows(settings, "food, place") == [
        (None if want is None else downgraded[want], None) for _, _, want in CONVERSIONS
    ]
    assert _feedback_rows(settings, kept) == before
    with pytest.raises(IntegrityError, match="ck_feedback_food"):
        _execute(settings, "UPDATE feedback SET food = 'liked'")
    with pytest.raises(IntegrityError, match="ck_feedback_place"):
        _execute(settings, "UPDATE feedback SET place = 'liked'")

    _upgrade_to(settings, "0020_one_rating")
    want_again = {"not_again": "hated", "fine": "fine", "loved": "loved"}
    assert [r[0] for r in _feedback_rows(settings, "rating")] == [
        None if want is None else want_again[downgraded[want]] for _, _, want in CONVERSIONS
    ]


# 0021: feedback-informed ranking (FR-55) ------------------------------------------------------------

FEEDBACK_RANKING_ROWS = [
    "INSERT INTO tag (id, household_id, kind, name, name_key, created_at) VALUES (1, 1, 'cuisine', 'Thai', 'thai', :t)",
    "INSERT INTO catalog_item (id, household_id, kind, name, name_key, modes, active, created_at, updated_at) "
    "VALUES (2, 1, 'restaurant', 'Y', 'y', '[\"pickup\"]', 1, :t, :t)",
    "INSERT INTO preference (household_id, member_id, item_id, veto, score, updated_at) "
    "VALUES (1, 1, 1, 0, 2, '2026-10-20 12:00:00'), (1, 1, 2, 1, 0, '2026-10-21 08:30:00')",
    "INSERT INTO preference (household_id, member_id, tag_id, veto, score, updated_at) VALUES (1, 1, 1, 0, -1, :t)",
    "INSERT INTO household (id, name, timezone, settings, created_at) "
    "VALUES (2, 'Other', 'UTC', '{\"keep\": 1, \"ranking_temperature\": 0.5}', :t)",
    "INSERT INTO feedback (household_id, meal_id, member_id, rating, place_issues, cook_thanks, item_id, item_name, "
    "item_kind, participants, created_at, updated_at) "
    "VALUES (1, 1, 1, 'hated', '[]', 0, 1, 'X', 'restaurant', '[]', :t, '2026-10-23 01:30:00')",
]


def _stance_edits(settings: Settings) -> list[tuple[object, ...]]:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            query = "SELECT household_id, member_id, item_id, edited_at FROM stance_edit ORDER BY item_id"
            return [tuple(r) for r in c.execute(text(query))]
    finally:
        engine.dispose()


def _every_household_settings(settings: Settings) -> list[dict[str, object]]:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            return [json.loads(r[0]) for r in c.execute(text("SELECT settings FROM household ORDER BY id"))]
    finally:
        engine.dispose()


def test_0021_records_stance_edits_and_turns_feedback_off_and_downgrade_undoes_both(settings: Settings) -> None:
    """Up adds ``stance_edit``, filled from each item stance with the time it was saved (tag
    stances have no ratings to override), adds ``feedback.rating_saved_at`` from each row's
    ``updated_at``, and sets Feedback to 0 in every household's settings, keeping the rest. Down
    drops the table, the column, and the setting and changes no other row; up again gives the
    same."""
    _upgrade_to(settings, "0020_one_rating")
    _populate_calls(settings)
    _execute(settings, *FEEDBACK_RANKING_ROWS)
    before = _rows_outside_m4(settings)
    old_settings = _every_household_settings(settings)

    _upgrade_to(settings, "0021_feedback_ranking")
    assert "stance_edit" in _tables(settings)
    assert _stance_edits(settings) == [(1, 1, 1, "2026-10-20 12:00:00"), (1, 1, 2, "2026-10-21 08:30:00")]
    assert _every_household_settings(settings) == [
        {"feedback_cap": 0},
        {"keep": 1, "ranking_temperature": 0.5, "feedback_cap": 0},
    ]
    after = _rows_outside_m4(settings)
    ours = {"household", "feedback"}
    assert {k: v for k, v in after.items() if k not in ours} == {k: v for k, v in before.items() if k not in ours}
    assert _feedback_rows(settings, "rating, updated_at, rating_saved_at") == [
        ("hated", "2026-10-23 01:30:00", "2026-10-23 01:30:00")
    ]
    with pytest.raises(IntegrityError, match="UNIQUE"):  # one edit time per member per item
        _execute(settings, "INSERT INTO stance_edit (household_id, member_id, item_id, edited_at) VALUES (1, 1, 1, :t)")

    _downgrade_to(settings, "0020_one_rating")
    assert "stance_edit" not in _tables(settings)
    assert _every_household_settings(settings) == old_settings  # json_remove rewrites the text, so compare values
    down = _rows_outside_m4(settings)
    assert {k: v for k, v in down.items() if k != "household"} == {k: v for k, v in before.items() if k != "household"}
    engine = create_engine(settings.sync_database_url)
    try:
        assert "rating_saved_at" not in {c["name"] for c in inspect(engine).get_columns("feedback")}
    finally:
        engine.dispose()

    _upgrade_to(settings, "0021_feedback_ranking")
    assert len(_stance_edits(settings)) == 2
    assert all(s["feedback_cap"] == 0 for s in _every_household_settings(settings))


# 0022: fun mode (#153) ------------------------------------------------------------------------------


def _fun_modes(settings: Settings) -> list[tuple[object, ...]]:
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text("SELECT id, fun_mode FROM member ORDER BY id"))]
    finally:
        engine.dispose()


def test_0022_turns_fun_mode_on_for_everyone_and_downgrade_drops_it(settings: Settings) -> None:
    """Up adds ``member.fun_mode``, on for every member and guest already there and for anyone
    added later. Down drops the column and changes no other row; up again gives the same."""
    _upgrade_to(settings, "0021_feedback_ranking")
    _populate_calls(settings)
    _execute(
        settings,
        "INSERT INTO member (id, household_id, display_name, email, role, can_cook, can_pickup, can_order, "
        "mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
        "VALUES (2, 1, 'Pat', NULL, 'guest', 0, 0, 0, 0, 0, 1, 0, :t, :t)",
    )
    before = _rows_outside_m4(settings)

    _upgrade_to(settings, "0022_fun_mode")
    assert _fun_modes(settings) == [(1, 1), (2, 1)]
    after = _rows_outside_m4(settings)
    assert [row[:-1] for row in after["member"]] == before["member"]  # the new column is last
    assert {k: v for k, v in after.items() if k != "member"} == {k: v for k, v in before.items() if k != "member"}
    _execute(
        settings,
        "INSERT INTO member (id, household_id, display_name, email, role, can_cook, can_pickup, can_order, "
        "mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
        "VALUES (3, 1, 'Lee', 'lee@example.com', 'member', 0, 0, 0, 0, 0, 1, 0, :t, :t)",
    )
    assert _fun_modes(settings)[-1] == (3, 1)
    _execute(settings, "DELETE FROM member WHERE id = 3")

    _downgrade_to(settings, "0021_feedback_ranking")
    assert _rows_outside_m4(settings) == before
    _upgrade_to(settings, "0022_fun_mode")
    assert _fun_modes(settings) == [(1, 1), (2, 1)]


# 0023: the den's badge marker (#153) ----------------------------------------------------------------


def test_0023_marks_every_badge_so_far_seen_and_downgrade_drops_it(settings: Settings) -> None:
    """Up adds ``member.fun_seen_award`` at each member's newest award, or 0 without one, so no
    badge earned before it flips. Down drops the column and changes no other row."""
    _upgrade_to(settings, "0022_fun_mode")
    _populate_calls(settings)
    _execute(
        settings,
        "INSERT INTO member (id, household_id, display_name, email, role, can_cook, can_pickup, can_order, "
        "mute_dm_reminders, feedback_reminder, active, slack_link_locked, created_at, updated_at) "
        "VALUES (2, 1, 'Pat', NULL, 'guest', 0, 0, 0, 0, 0, 1, 0, :t, :t)",
        "INSERT INTO badge_award (id, household_id, member_id, badge_key, tier, awarded_at, announcement) "
        "VALUES (4, 1, 1, 'chef', 'first', :t, 'announced'), (9, 1, 1, 'chef', 'bronze', :t, 'pending')",
    )
    before = _rows_outside_m4(settings)
    _upgrade_to(settings, "0023_fun_seen_award")
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            assert c.execute(text("SELECT id, fun_seen_award FROM member ORDER BY id")).all() == [(1, 9), (2, 0)]
    finally:
        engine.dispose()
    after = _rows_outside_m4(settings)
    assert [row[:-1] for row in after["member"]] == before["member"]
    assert {k: v for k, v in after.items() if k != "member"} == {k: v for k, v in before.items() if k != "member"}
    _downgrade_to(settings, "0022_fun_mode")
    assert _rows_outside_m4(settings) == before


# 0024: badge award IDs are never reused (#162) ------------------------------------------------------


def _badge_awards(settings: Settings) -> tuple[str, list[object]]:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            sql: str = c.execute(text("SELECT sql FROM sqlite_master WHERE name = 'badge_award'")).scalar_one()
            return sql, list(c.execute(text("SELECT * FROM badge_award ORDER BY id")).all())
    finally:
        engine.dispose()


def test_0024_rebuilds_badge_award_without_losing_a_row_and_never_reuses_an_id(settings: Settings) -> None:
    """Up rebuilds ``badge_award`` with AUTOINCREMENT, keeping every award; then taking back the
    newest award doesn't free its ID. Down rebuilds it without, and changes no other row."""
    _upgrade_to(settings, "0023_fun_seen_award")
    _populate_calls(settings)
    _execute(
        settings,
        "INSERT INTO badge_award (id, household_id, member_id, badge_key, tier, awarded_at, announcement) "
        "VALUES (4, 1, 1, 'chef', 'first', :t, 'announced'), (9, 1, 1, 'chef', 'bronze', :t, 'pending')",
    )
    sql, awards = _badge_awards(settings)
    others = _rows_outside_m4(settings)
    assert "AUTOINCREMENT" not in sql and len(awards) == 2

    _upgrade_to(settings, "0024_badge_award_autoincrement")
    sql, after = _badge_awards(settings)
    assert "AUTOINCREMENT" in sql and after == awards
    assert _rows_outside_m4(settings) == others
    _execute(
        settings,
        "DELETE FROM badge_award WHERE id = 9",
        "INSERT INTO badge_award (household_id, member_id, badge_key, tier, awarded_at, announcement) "
        "VALUES (1, 1, 'chef', 'bronze', :t, 'pending')",
    )
    assert [row[0] for row in _badge_awards(settings)[1]] == [4, 10]

    _downgrade_to(settings, "0023_fun_seen_award")
    sql, down = _badge_awards(settings)
    assert "AUTOINCREMENT" not in sql and [row[0] for row in down] == [4, 10]
    assert _rows_outside_m4(settings) == others


@pytest.mark.parametrize(("awards", "marker", "next_id"), [((4, 9), 15, 16), ((), 12, 13), ((4, 9), 7, 10)])
def test_0024_starts_new_award_ids_above_every_den_marker(
    settings: Settings, awards: tuple[int, ...], marker: int, next_id: int
) -> None:
    """A den marker can name an award already deleted (``scrub-test``), even with none left. After
    the upgrade, the next award's ID is above both the largest award and the largest marker, so it
    flips the den."""
    _upgrade_to(settings, "0023_fun_seen_award")
    _populate_calls(settings)
    insert = (
        "INSERT INTO badge_award (id, household_id, member_id, badge_key, tier, awarded_at, announcement) "
        "VALUES (:id, 1, 1, 'chef', :tier, :t, 'announced')"
    )
    engine = create_sync_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            for award_id, tier in zip(awards, ("first", "bronze"), strict=False):
                c.execute(text(insert), {"id": award_id, "tier": tier, "t": STAMP})
            c.execute(text("UPDATE member SET fun_seen_award = :m WHERE id = 1"), {"m": marker})
    finally:
        engine.dispose()
    _upgrade_to(settings, "0024_badge_award_autoincrement")
    _execute(
        settings,
        "INSERT INTO badge_award (household_id, member_id, badge_key, tier, awarded_at, announcement) "
        "VALUES (1, 1, 'chef', 'silver', :t, 'pending')",
    )
    assert _badge_awards(settings)[1][-1][0] == next_id


def test_a_second_migrator_waits_then_reports_a_lock(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    """Another process holding the write lock: the loser waits, then raises MigrationLocked
    instead of a raw "database is locked" (audit #14)."""
    import sqlite3

    from dinnerbell.db.migrate import MigrationLocked

    run_migrations(settings.sync_database_url, settings.database_path)
    holder = sqlite3.connect(settings.database_path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationLocked) as caught:
            run_migrations(settings.sync_database_url, settings.database_path, busy_timeout_ms=50, attempts=2)
        assert "another Dinnerbell process" in str(caught.value)
        holder.execute("ROLLBACK")
        run_migrations(settings.sync_database_url, settings.database_path, busy_timeout_ms=50, attempts=2)
    finally:
        holder.close()


def test_migrate_command_exits_nonzero_when_locked(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from dinnerbell import main as main_module
    from dinnerbell.db.migrate import MigrationLocked

    def locked(*_args: object, **_kwargs: object) -> None:
        raise MigrationLocked("The database is locked: another Dinnerbell process")

    monkeypatch.setattr(main_module, "run_migrations", locked)
    assert main_module._migrate(settings) is False


def test_the_alembic_cli_path_reports_a_lock_readably(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import functools
    import sqlite3

    from alembic import command
    from alembic.config import Config

    from dinnerbell.db import migrate
    from dinnerbell.db.migrate import SCRIPT_LOCATION, MigrationLocked

    run_migrations(settings.sync_database_url, settings.database_path)
    # env.py imports migration_engine each time alembic runs it: shorten the wait.
    monkeypatch.setattr(migrate, "migration_engine", functools.partial(migrate.migration_engine, busy_timeout_ms=50))
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", settings.sync_database_url)
    holder = sqlite3.connect(settings.database_path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationLocked, match="another Dinnerbell process"):
            command.downgrade(config, "-1")
    finally:
        holder.close()
