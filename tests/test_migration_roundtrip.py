# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every migration, down one revision and up again, on a database full of child rows (#36).

The walk runs through the Alembic CLI path (``env.py``), the way ``uv run alembic downgrade``
does. With foreign keys on, a batch rebuild of ``event`` or ``meal_log`` would cascade-delete
every response, outbox, assignment, vote, and feedback row. The test fails if any row a
revision's schema keeps goes missing or changes, in either direction.

Not covered by the walk, because each one refuses or loses data by design (and has its own test
in test_migrations.py):

- 0001 downgrades to an empty database.
- 0013 refuses to downgrade while a breakfast call or meal exists, 0015 while a role choice is
  pending, and 0018 while a gambled contribution exists. The seed has none, so they pass here.
- 0011 downgrades by deleting every guest, with their invitations, responses, and assignments. The
  seed's guest has only an invitation, so the walk expects that one member row to go and nothing else.
- 0016 and 0018 downgrades clear their ``*_backfilled`` markers from the household settings (and
  SQLite's ``json_remove`` rewrites the JSON), so the walk compares the settings without them.
- 0020 converts ``food`` and ``place`` to ``rating`` and back, so the walk skips those two columns
  across it; test_migrations.py checks the conversion both ways.
- 0021 sets ``feedback_cap`` to 0 in the household settings and its downgrade removes it, so the
  walk compares the settings without it; test_migrations.py checks both ways.
- A downgrade drops the tables and columns its upgrade added, so the walk compares only what the
  older revision has.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from dinnerbell.config import Settings
from dinnerbell.db.migrate import SCRIPT_LOCATION, IntegrityCheckFailed
from dinnerbell.services.database import open_database
from tests.support.calls import furnish, make_house
from tests.support.clock import FakeClock
from tests.test_reset import lived_in

Rows = dict[str, tuple[list[str], list[tuple[Any, ...]]]]


def _cli_config(settings: Settings) -> Config:
    """What ``alembic`` builds from alembic.ini: no connection, so env.py opens its own."""
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", settings.sync_database_url.replace("%", "%%"))
    return config


def _populate(settings: Settings, clock: FakeClock) -> None:
    async def run() -> None:
        database = open_database(settings.async_database_url)
        try:
            await lived_in(await furnish(await make_house(database.sessionmaker, clock, settings.admin_email)))
        finally:
            await database.close()

    asyncio.run(run())


def _without_markers(columns: list[str], row: tuple[Any, ...]) -> tuple[Any, ...]:
    values = list(row)
    index = columns.index("settings")
    kept = {k: v for k, v in json.loads(values[index]).items() if not k.endswith("_backfilled")}
    values[index] = json.dumps(kept, sort_keys=True)
    return tuple(values)


def _rows(settings: Settings) -> Rows:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as c:
            names = [r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))]
            found: Rows = {}
            for name in sorted(n for n in names if not n.startswith("sqlite_") and n != "alembic_version"):
                result = c.execute(text(f'SELECT * FROM "{name}"'))  # noqa: S608
                columns = list(result.keys())
                rows = [tuple(r) for r in result]
                if name == "household":
                    rows = [_without_markers(columns, row) for row in rows]
                found[name] = (columns, rows)
            return found
    finally:
        engine.dispose()


Ignored = frozenset[tuple[str, str]]
# A downgrade's own documented changes to rows the older schema keeps.
DOWNGRADE_CHANGES: dict[str, Ignored] = {
    # 0002 backfills the variety window into the household settings, and its downgrade removes it.
    "0002_catalog": frozenset({("household", "settings")}),
    # Before 0003 no household had the schedule and ranking keys, and no meal had a call.
    "0003_dinner_calls": frozenset({("household", "settings"), ("meal_log", "event_id")}),
    # 0020 turns food and place into one rating, and its downgrade turns the rating back into food.
    "0020_one_rating": frozenset({("feedback", "food"), ("feedback", "place")}),
    # 0021 turns Feedback off for the households there already, and its downgrade removes the setting.
    "0021_feedback_ranking": frozenset({("household", "settings")}),
}


def _view(
    rows: Rows, columns_of: Rows, ignore: Ignored = frozenset(), *, guests_go: bool = False
) -> dict[str, list[tuple[Any, ...]]]:
    """The rows of ``rows`` limited to the tables and columns ``columns_of`` has, less ``ignore``."""
    view: dict[str, list[tuple[Any, ...]]] = {}
    for table, (columns, _) in columns_of.items():
        have, found = rows[table]
        if guests_go and table == "member":
            found = [row for row in found if row[have.index("role")] != "guest"]
        indexes = [have.index(c) for c in columns if (table, c) not in ignore]
        view[table] = sorted((tuple(row[i] for i in indexes) for row in found), key=repr)
    return view


def test_every_migration_goes_down_and_up_without_losing_a_row(migrated: Settings, clock: FakeClock) -> None:
    _populate(migrated, clock)
    seeded = _rows(migrated)
    assert [t for t, (_, found) in seeded.items() if not found] == [], "every table needs a row to be tested"

    config = _cli_config(migrated)
    script = ScriptDirectory.from_config(config)
    steps = [(r.revision, r.down_revision) for r in script.walk_revisions() if r.down_revision]
    assert len(steps) >= 18  # 0002 to head: a revision added without a step here would be missed
    for revision, older in steps:
        assert isinstance(older, str)
        before = _rows(migrated)
        command.downgrade(config, older)
        down = _rows(migrated)
        ignore = DOWNGRADE_CHANGES.get(revision, frozenset())
        guests_go = revision == "0011_guests"
        assert _view(down, down, ignore) == _view(before, down, ignore, guests_go=guests_go), (
            f"downgrading {revision} changed or lost rows"
        )
        command.upgrade(config, revision)
        up = _rows(migrated)
        assert _view(up, down, ignore) == _view(down, down, ignore), f"upgrading {revision} lost rows"
        command.downgrade(config, older)  # go on from the older revision


def test_the_cli_path_refuses_a_downgrade_that_finds_a_dangling_reference(
    migrated: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The check after a CLI migration names the table and rolls the change back."""
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(text("PRAGMA foreign_keys=OFF"))
            c.execute(
                text(
                    "INSERT INTO household (name, timezone, settings, created_at) "
                    "VALUES ('H', 'UTC', '{}', '2026-10-22 00:00:00')"
                )
            )
            c.execute(
                text(
                    "INSERT INTO tag (household_id, kind, name, name_key, created_at) "
                    "VALUES (99, 'cuisine', 'T', 't', '2026-10-22 00:00:00')"
                )
            )
    finally:
        engine.dispose()
    config = _cli_config(migrated)
    schema = "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.connect() as c:
            schema_before = c.execute(text(schema)).all()
    finally:
        engine.dispose()
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(IntegrityCheckFailed, match=r"Rows in tag refer to rows that don't exist.*already broken"),
    ):
        command.downgrade(config, "-1")
    assert [(r.table, r.rowid, r.parent) for r in caplog.records if r.message == "dangling reference"] == [
        ("tag", 1, "household")
    ]
    assert "'T'" not in caplog.text
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.connect() as c:
            assert c.execute(text(schema)).all() == schema_before  # the rollback undid the DDL
            assert c.execute(text("SELECT name FROM tag")).all() == [("T",)]
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one().startswith("0022")
    finally:
        engine.dispose()
