# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell reset-history`` (#56): delete calls, meals, and feedback before going live, and
keep the setup. Every table is classified; the reset empties the CLEAR ones, leaves the KEEP ones
exactly as they were, refuses while a call is open, and the scheduler carries on afterwards."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from dinnerbell import __version__
from dinnerbell import main as cli
from dinnerbell.config import Settings
from dinnerbell.db.events import EVENT_ROWS
from dinnerbell.db.models import Base, PreferenceRow, SessionRow
from dinnerbell.domain.events import EventState, RoleOffer, Status
from dinnerbell.domain.feedback import FeedbackInput, Rating
from dinnerbell.domain.meals import Slot
from dinnerbell.services import backups, calls, event_views, feedback, reset
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.test_config import REQUIRED
from tests.test_discard import done_dinner

pytestmark = pytest.mark.anyio

TODAY = date(2026, 10, 22)
TOMORROW = TODAY + timedelta(days=1)
BACKUP = "pre-reset-0.3.0-20261023T120000Z.db"


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def act(house: House, use_case: Any, *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def lived_in(house: House) -> int:
    """A household with a row in every table: a done dinner with feedback, Slack messages,
    preferences, and a signed-in member. Returns the dinner's ID."""
    event_id = await done_dinner(house, RecordingChannel())
    (meal,) = await house.meals()
    await act(house, feedback.give, house["Sam"], meal.id, FeedbackInput(food=Rating.LOVED))
    async with house.sessionmaker() as db:
        now = house.clock()
        kim, household_id = house["Kim"].id, house.household.id
        thai = house.tags["Thai"].id
        db.add(PreferenceRow(household_id=household_id, member_id=kim, tag_id=thai, veto=True, score=0, updated_at=now))
        expires = now + timedelta(days=30)
        db.add(
            SessionRow(
                id="a" * 64, household_id=household_id, member_id=kim, created_at=now, expires_at=expires, last_seen=now
            )
        )
        await db.commit()
    return event_id


async def table_rows(house: House, name: str) -> list[tuple[Any, ...]]:
    async with house.sessionmaker() as db:
        return [tuple(row) for row in await db.execute(text(f"SELECT * FROM {name} ORDER BY rowid"))]  # noqa: S608


async def everything(house: House) -> dict[str, list[tuple[Any, ...]]]:
    return {name: await table_rows(house, name) for name in sorted(Base.metadata.tables)}


async def reset_now(house: House) -> reset.ResetSummary:
    summary: reset.ResetSummary = await act(house, reset.reset_history, backup=BACKUP)
    return summary


# The classification ------------------------------------------------------------------------------


def test_every_table_is_kept_or_cleared() -> None:
    """A new table must be added to ``reset.KEEP`` or ``reset.CLEAR``, so going live can't leave
    test history behind in it, or delete setup with it. Classify it in services/reset.py."""
    tables = set(Base.metadata.tables)
    assert not reset.KEEP & reset.CLEAR, "a table can't be both kept and cleared"
    assert tables - reset.KEEP - reset.CLEAR == set(), "classify these tables in services/reset.py"
    assert (reset.KEEP | reset.CLEAR) - tables == set(), "these tables no longer exist"


def test_everything_in_a_call_is_cleared() -> None:
    """``EVENT_ROWS`` lists every table that belongs to a call (a test in test_discard.py makes
    sure); the reset clears them all, with the meal log's feedback and the outbox."""
    assert {model.__tablename__ for model in EVENT_ROWS} | {"event", "feedback", "outbox"} <= reset.CLEAR


def test_no_kept_table_refers_to_a_cleared_one() -> None:
    """A kept row that referred to a cleared one would be deleted by a cascade, or block the
    delete. Neither may happen: kept tables don't refer to history."""
    for name in reset.KEEP:
        referred = {fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys}
        assert not referred & reset.CLEAR, f"{name} refers to {referred & reset.CLEAR}"


# Resetting ---------------------------------------------------------------------------------------


async def test_the_reset_empties_every_cleared_table_and_keeps_every_other_row(house: House) -> None:
    await lived_in(house)
    before = await everything(house)
    assert [name for name, rows in before.items() if not rows] == [], "every table needs a row to be tested"

    summary = await reset_now(house)

    after = await everything(house)
    for name in sorted(reset.CLEAR):
        assert after[name] == [], name
        assert summary.deleted[name] == len(before[name]), name
    for name in sorted(reset.KEEP - {"audit"}):
        assert after[name] == before[name], name
        assert summary.kept[name] == len(before[name]), name
    assert after["audit"][: len(before["audit"])] == before["audit"]
    assert len(after["audit"]) == len(before["audit"]) + 1


async def test_one_audit_row_records_the_counts_and_the_backup(house: House) -> None:
    await lived_in(house)
    summary = await reset_now(house)
    (row,) = await house.audits("history.")
    assert (row.action, row.actor_member_id, row.target) == ("history.reset", None, f"household:{house.household.id}")
    assert row.payload == {"rows": summary.deleted, "backup": BACKUP}
    assert row.payload["rows"]["event"] == 1 and row.payload["rows"]["feedback"] == 1
    assert "loved" not in str(row.payload)
    assert summary.lines()[0] == f"Backup: {BACKUP}"


async def test_a_second_reset_deletes_nothing(house: House) -> None:
    await lived_in(house)
    await reset_now(house)
    again = await reset_now(house)
    assert set(again.deleted.values()) == {0}
    assert len(await house.audits("history.")) == 2


async def test_history_home_and_old_slack_buttons_show_nothing_afterwards(house: House) -> None:
    channel = RecordingChannel()
    event_id = await done_dinner(house, channel)
    await reset_now(house)

    async with house.sessionmaker() as db:
        today = await event_views.today_view(db, house["Sam"], now=house.clock())
        awaiting = await feedback.awaiting(db, house["Sam"], now=house.clock())
    assert today.events == [] and awaiting == []
    assert await house.meals() == []
    # A tap on a button of a message posted before finds no call, and nothing edits the messages.
    with pytest.raises(NotFound):
        await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    with pytest.raises(NotFound):
        await act(house, feedback.form_for_event, house["Sam"], event_id, run=1)
    channel.sent.clear()
    await house.drain(channel)
    assert channel.sent == []


async def test_the_next_tick_opens_a_call_as_usual(house: House) -> None:
    old = await done_dinner(house, RecordingChannel())
    await reset_now(house)

    house.clock.now = OPENS + timedelta(days=1)
    await house.tick()
    fresh = await house.event(await dinner_id(house, TOMORROW))
    assert fresh.state is EventState.COLLECTING and fresh.run == 1
    assert fresh.id > old  # never the ID of a call from before, which old Slack buttons name
    channel = RecordingChannel()
    await house.drain(channel)
    assert [(s.op, s.kind) for s in channel.sent] == [("deliver", "call_opened")]


# Refusing ----------------------------------------------------------------------------------------


async def open_call(house: House, state: EventState) -> int:
    if state is EventState.SCHEDULED:
        event = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TOMORROW)
        return int(event.id)
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    if state is EventState.COLLECTING:
        return event_id
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    if state is EventState.DECIDED:
        house.clock.now = VOTE_BY
        await house.tick()
    return event_id


@pytest.mark.parametrize("state", [EventState.SCHEDULED, EventState.COLLECTING, EventState.VOTING, EventState.DECIDED])
async def test_an_open_call_refuses_the_reset_until_it_is_called_off(house: House, state: EventState) -> None:
    event_id = await open_call(house, state)
    assert (await house.event(event_id)).state is state
    before = await everything(house)

    with pytest.raises(reset.ResetRefused) as refused:
        await reset_now(house)

    day = TOMORROW if state is EventState.SCHEDULED else TODAY
    assert str(refused.value).startswith(reset.OPEN_CALLS)
    assert f"dinner on {day.isoformat()} (ID {event_id}): {state.label}" in str(refused.value)
    assert await everything(house) == before

    await act(house, calls.cancel_call, house["Alex"], event_id)
    summary = await reset_now(house)
    assert summary.deleted["event"] == 1


async def test_the_preview_counts_by_table_and_says_what_posts_again(house: House) -> None:
    """A dinner called off while its answers are still open is posted again after the reset."""
    event_id = await open_call(house, EventState.COLLECTING)
    async with house.sessionmaker() as db:
        found = await reset.preview(db, now=house.clock())
    assert found is not None
    assert found.open_calls == (reset.OpenCall(event_id, Slot.DINNER, TODAY, EventState.COLLECTING),)
    assert found.clear["event"] == 1 and found.clear["meal_log"] == 0
    assert found.keep["catalog_item"] == 3 and found.keep["member"] == 4
    assert found.posts_again == (Slot.DINNER,)

    await act(house, calls.cancel_call, house["Alex"], event_id)
    async with house.sessionmaker() as db:
        found = await reset.preview(db, now=house.clock())
    assert found is not None and found.open_calls == () and found.pending_outbox > 0
    lines = found.lines()
    assert lines[0] == "Household: Home"
    assert lines[1].startswith("Deletes: assignment 0, event 1, feedback 0,")
    assert lines[2].startswith("Keeps: audit ")
    assert lines[4] == "Today's dinner call is still taking answers, so the scheduler posts it again after the reset."

    house.clock.now = OPENS + timedelta(minutes=10)
    await reset_now(house)
    await house.tick()
    fresh = await house.event(await dinner_id(house))
    assert fresh.id > event_id and fresh.state is EventState.COLLECTING


async def test_a_call_whose_answers_closed_does_not_post_again(house: House) -> None:
    await done_dinner(house, RecordingChannel())
    async with house.sessionmaker() as db:
        found = await reset.preview(db, now=house.clock())
    assert found is not None and found.posts_again == () and len(found.lines()) == 4


async def test_no_household_is_refused(database: Database, clock: FakeClock) -> None:
    async with database.sessionmaker() as db:
        assert await reset.preview(db, now=clock()) is None
        with pytest.raises(reset.ResetRefused, match="no household"):
            await reset.reset_history(db, backup=BACKUP, now=clock())


# The command -------------------------------------------------------------------------------------


class Terminal(io.StringIO):
    """Stands in for an interactive stdin, answering ``input()`` with what it was given."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Production: no instance label."""
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DINNERBELL_INSTANCE_LABEL", raising=False)
    database = tmp_path / "data" / "dinnerbell.db"
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", f"sqlite:///{database}")
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        yield database
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


def with_house(path: Path, work: Any) -> Any:
    async def run() -> Any:
        database = open_database(f"sqlite+aiosqlite:///{path}")
        try:
            house = await furnish(await make_house(database.sessionmaker, FakeClock(), "admin@example.com"))
            return await work(house)
        finally:
            await database.close()

    return asyncio.run(run())


@pytest.fixture
def production(cli_env: Path) -> Path:
    """Production's database, with a done dinner in it."""
    assert cli.main(["migrate"]) == 0
    with_house(cli_env, lambda house: done_dinner(house, RecordingChannel()))
    return cli_env


def count(path: Path, sql: str) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute(sql).fetchone()[0])


def backups_in(path: Path) -> list[Path]:
    folder = path.parent / "backups"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def test_the_command_backs_up_then_resets_production(production: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["reset-history", "--yes"]) == 0
    out = capsys.readouterr().out
    assert f"Database: {production}" in out and "Household: Home" in out
    assert re.search(r"Deletes: assignment \d+, event 1, feedback 0, meal_log 1,", out)
    assert "Keeps: audit " in out and "catalog_item 3," in out and "member 4," in out
    assert "Reset the history." in out

    (backup,) = backups_in(production)
    assert re.fullmatch(rf"pre-reset-{re.escape(__version__)}-\d{{8}}T\d{{6}}Z\.db", backup.name)
    assert f"Backed up the database to {backup}" in out and f"Backup: {backup.name}" in out
    assert count(backup, "SELECT count(*) FROM event") == 1 and count(backup, "SELECT count(*) FROM meal_log") == 1
    assert backup.stat().st_mode & 0o077 == 0  # it holds member emails
    assert count(production, "SELECT count(*) FROM event") == 0
    assert count(production, "SELECT count(*) FROM catalog_item") == 3
    assert count(production, "SELECT count(*) FROM audit WHERE action = 'history.reset'") == 1


def test_a_typed_confirmation_at_a_terminal_resets(
    production: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal("reset\n"))
    assert cli.main(["reset-history"]) == 0
    assert 'Type "reset" to delete the history' in capsys.readouterr().out
    assert count(production, "SELECT count(*) FROM event") == 0


@pytest.mark.parametrize(
    ("stdin", "message"),
    [
        (Terminal("yes\n"), "Not reset"),
        (Terminal(""), "Not reset"),
        (io.StringIO("reset\n"), "No terminal to confirm on"),
    ],
)
def test_no_confirmation_changes_nothing(
    production: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: io.StringIO,
    message: str,
) -> None:
    """A wrong word, end of input, or no terminal (and no --yes) refuses before the backup and
    before migrating."""
    migrations: list[object] = []
    monkeypatch.setattr(cli, "run_migrations", lambda *args: migrations.append(args))
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["reset-history"]) == 1
    assert message in capsys.readouterr().err
    assert migrations == [] and backups_in(production) == []
    assert count(production, "SELECT count(*) FROM event") == 1


def test_an_open_call_is_listed_and_refused_before_the_backup(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["migrate"]) == 0
    event_id = with_house(cli_env, lambda house: open_call(house, EventState.VOTING))
    assert cli.main(["reset-history", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "Deletes: " in captured.out
    assert reset.OPEN_CALLS in captured.err and f"dinner on 2026-10-22 (ID {event_id}): Voting" in captured.err
    assert backups_in(cli_env) == []
    assert count(cli_env, "SELECT count(*) FROM event") == 1


def test_a_failed_backup_changes_nothing(
    production: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def full(*args: object) -> Path:
        raise OSError("No space left on device")

    monkeypatch.setattr(backups, "run_one_off_backup", full)
    assert cli.main(["reset-history", "--yes"]) == 1
    assert "Can't back up the database (OSError). Nothing changed." in capsys.readouterr().err
    assert count(production, "SELECT count(*) FROM event") == 1


def test_the_command_refuses_a_missing_or_empty_database(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["reset-history", "--yes"]) == 1
    assert "No database at" in capsys.readouterr().err
    assert not cli_env.exists()
    assert cli.main(["migrate"]) == 0
    assert cli.main(["reset-history", "--yes"]) == 1
    assert "no household yet" in capsys.readouterr().err
    assert backups_in(cli_env) == []


def test_a_one_off_backup_never_replaces_one_or_is_pruned(tmp_path: Path, production: Path) -> None:
    folder = tmp_path / "copies"
    first = backups.run_one_off_backup(production, folder, "pre-reset-0.3.0-20261023T120000Z.db")
    with pytest.raises(FileExistsError):
        backups.run_one_off_backup(production, folder, first.name)
    assert backups.prune(folder) == () and first.exists()
    assert sorted(p.name for p in folder.iterdir()) == [first.name]


def test_a_one_off_backup_is_named_for_its_purpose_version_and_utc_time() -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    at = datetime(2026, 10, 23, 7, 30, 5, tzinfo=ZoneInfo("America/Chicago"))
    assert backups.one_off_name("reset", "0.3.0", at) == "pre-reset-0.3.0-20261023T123005Z.db"
    assert backups.one_off_name("reset", "0.3.0", at.astimezone(UTC)) == "pre-reset-0.3.0-20261023T123005Z.db"


def test_a_partial_copy_is_removed_when_the_backup_fails(
    tmp_path: Path, production: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(source: Path, target: Path) -> None:
        target.with_suffix(backups.PARTIAL).write_bytes(b"half")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backups, "_copy_online", broken)
    folder = tmp_path / "copies"
    with pytest.raises(sqlite3.OperationalError):
        backups.run_one_off_backup(production, folder, "pre-reset-0.3.0-20261023T120000Z.db")
    assert list(folder.iterdir()) == []
