# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell set-timezone`` and ``set-household-name`` (#334): change what the settings only
seeded on first start, and the running scheduler following a changed zone."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from dinnerbell import main as cli
from dinnerbell.channels.base import NullChannel
from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.db.models import AuditRow, HouseholdRow
from dinnerbell.domain.events import EventState
from dinnerbell.domain.household import NAME_MAX_LENGTH, InvalidHouseholdInput, check_timezone, is_timezone
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.schedule import default_settings, slot_fingerprint, slot_schedule
from dinnerbell.scheduler import runner
from dinnerbell.services import backups, calls, household, lifecycle
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import SchedulerState
from tests.support.calls import OPENS, ZONE, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.test_reset import open_call, with_house

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def stored(house: House) -> HouseholdRow:
    async with house.sessionmaker() as db:
        row = await db.scalar(select(HouseholdRow))
    assert row is not None
    return row


async def audits(house: House, action: str) -> list[AuditRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(AuditRow).where(AuditRow.action == action)))


async def set_zone(house: House, zone: str) -> household.Changed:
    async with house.sessionmaker() as db:
        return await household.set_timezone(db, zone, now=house.clock())


async def rename(house: House, name: str) -> household.Changed:
    async with house.sessionmaker() as db:
        return await household.set_name(db, name, now=house.clock())


# The time zone -----------------------------------------------------------------------------------


async def test_the_time_zone_changes_and_is_audited(house: House, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dinnerbell.services.household")
    result = await set_zone(house, " Europe/Berlin ")
    assert (result.before, result.after, result.changed) == (ZONE, "Europe/Berlin", True)
    assert (await stored(house)).timezone == "Europe/Berlin"
    (row,) = await audits(house, "household.timezone_changed")
    assert (row.actor_member_id, row.target) == (None, f"household:{house.household.id}")
    assert row.payload == {"from": ZONE, "to": "Europe/Berlin"}
    assert "household time zone changed" in caplog.text


async def test_the_same_zone_changes_nothing(house: House) -> None:
    result = await set_zone(house, ZONE)
    assert not result.changed
    assert await audits(house, "household.timezone_changed") == []


@pytest.mark.parametrize("zone", ["", "  ", "Region/City", "Mars/Olympus", "../etc/passwd", "/etc/localtime"])
async def test_a_name_that_is_not_a_zone_is_refused(house: House, zone: str) -> None:
    with pytest.raises(InvalidHouseholdInput):
        await set_zone(house, zone)
    assert (await stored(house)).timezone == ZONE


def test_the_zone_rule_is_the_settings_rule() -> None:
    assert is_timezone("America/Chicago") and is_timezone("UTC")
    assert not is_timezone("Region/City")
    with pytest.raises(InvalidHouseholdInput, match="Enter an IANA time zone name"):
        check_timezone("")
    with pytest.raises(InvalidHouseholdInput, match="Region/City isn't an IANA time zone name"):
        check_timezone("Region/City")


@pytest.mark.parametrize("state", [EventState.SCHEDULED, EventState.COLLECTING, EventState.VOTING, EventState.DECIDED])
async def test_an_open_call_refuses_until_it_is_called_off(house: House, state: EventState) -> None:
    event_id = await open_call(house, state)
    with pytest.raises(household.TimezoneRefused) as refused:
        await set_zone(house, "Europe/Berlin")
    assert str(refused.value).startswith(household.OPEN_CALLS)
    assert f"(ID {event_id}): {state.label}" in str(refused.value)
    assert (await stored(house)).timezone == ZONE
    assert await audits(house, "household.timezone_changed") == []

    async with house.sessionmaker() as db:
        await calls.cancel_call(db, house["Alex"], event_id, now=house.clock())
    assert (await set_zone(house, "Europe/Berlin")).changed


async def test_no_household_is_refused(database: Database, clock: FakeClock) -> None:
    async with database.sessionmaker() as db:
        with pytest.raises(household.NoHousehold):
            await household.set_timezone(db, "Europe/Berlin", now=clock())
        with pytest.raises(household.NoHousehold):
            await household.set_name(db, "Elm Street", now=clock())


# The name ----------------------------------------------------------------------------------------


async def test_the_name_changes_and_is_audited(house: House, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dinnerbell.services.household")
    await open_call(house, EventState.COLLECTING)  # an open call doesn't stop a rename
    result = await rename(house, "  Elm Street ")
    assert (result.before, result.after) == ("Home", "Elm Street")
    assert (await stored(house)).name == "Elm Street"
    (row,) = await audits(house, "household.renamed")
    assert (row.actor_member_id, row.payload) == (None, {"from": "Home", "to": "Elm Street"})
    assert "household renamed" in caplog.text and "Elm Street" not in caplog.text  # never the name in a log
    assert not (await rename(house, "Elm Street")).changed
    assert len(await audits(house, "household.renamed")) == 1


@pytest.mark.parametrize("name", ["", "   ", "x" * (NAME_MAX_LENGTH + 1)])
async def test_an_empty_or_long_name_is_refused(house: House, name: str) -> None:
    with pytest.raises(InvalidHouseholdInput):
        await rename(house, name)
    assert (await stored(house)).name == "Home"


# The scheduler follows -----------------------------------------------------------------------------


@pytest.fixture
def job_context(house: House, migrated: Settings) -> runner.JobContext:
    return runner.JobContext(
        state=SchedulerState(),
        clock=house.clock,
        zone=ZoneInfo(ZONE),
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=house.sessionmaker,
        channel=NullChannel(),
    )


def backup_job(scheduler: Any) -> Any:
    return scheduler.get_job("nightly_backup")


async def test_the_nightly_backup_follows_a_changed_zone(
    house: House, job_context: runner.JobContext, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="dinnerbell.scheduler.runner")
    scheduler = runner.build_scheduler(job_context)
    scheduler.start(paused=True)
    try:
        assert str(scheduler.timezone) == "UTC"  # only the backup and the outbox prune keep a zone
        assert scheduler.get_job("follow_zone").trigger.interval == runner.ZONE_CHECK_INTERVAL
        await runner.follow_zone(job_context, scheduler)  # nothing changed
        assert str(backup_job(scheduler).trigger.timezone) == ZONE
        assert str(scheduler.get_job("prune_outbox").trigger.timezone) == ZONE
        assert "new time zone" not in caplog.text

        await set_zone(house, "Asia/Tokyo")
        await runner.follow_zone(job_context, scheduler)
        assert job_context.zone == ZoneInfo("Asia/Tokyo")
        job = backup_job(scheduler)
        assert str(job.trigger.timezone) == "Asia/Tokyo"
        nightly = job.next_run_time.astimezone(ZoneInfo("Asia/Tokyo"))
        assert (nightly.hour, nightly.minute) == (runner.BACKUP_HOUR, runner.BACKUP_MINUTE)
        assert nightly - datetime.now(UTC) < timedelta(days=1)
        prune = scheduler.get_job("prune_outbox")  # #340: the prune moves with it
        assert str(prune.trigger.timezone) == "Asia/Tokyo"
        nightly = prune.next_run_time.astimezone(ZoneInfo("Asia/Tokyo"))
        assert (nightly.hour, nightly.minute) == (runner.PRUNE_HOUR, runner.PRUNE_MINUTE)
        assert "the nightly backup and outbox prune now run in the household's new time zone" in caplog.text
    finally:
        scheduler.shutdown(wait=False)


async def test_the_backup_names_its_day_in_the_new_zone(house: House, job_context: runner.JobContext) -> None:
    # 16:00 UTC on 2026-10-22 is 11:00 that day in Chicago, and 01:00 on the 23rd in Tokyo.
    house.clock.now = datetime(2026, 10, 22, 16, tzinfo=UTC)
    scheduler = runner.build_scheduler(job_context)
    scheduler.start(paused=True)
    try:
        await set_zone(house, "Asia/Tokyo")
        await runner.follow_zone(job_context, scheduler)
        await runner.nightly_backup(job_context)
    finally:
        scheduler.shutdown(wait=False)
    assert [p.name for p in job_context.backup_dir.glob("dinnerbell-*.db")] == ["dinnerbell-2026-10-23.db"]


async def test_following_stops_quietly_once_the_jobs_are_gone(house: House, job_context: runner.JobContext) -> None:
    scheduler = runner.build_scheduler(job_context)
    scheduler.start(paused=True)
    try:
        scheduler.remove_all_jobs()  # as stop_scheduler does first
        await set_zone(house, "Asia/Tokyo")
        await runner.follow_zone(job_context, scheduler)
    finally:
        scheduler.shutdown(wait=False)
    assert job_context.zone == ZoneInfo(ZONE)  # it tries again next time


async def test_a_failed_check_is_logged(
    job_context: runner.JobContext, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode(_db: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(household, "current_household", explode)
    scheduler = runner.build_scheduler(job_context)
    scheduler.start(paused=True)
    try:
        await runner.follow_zone(job_context, scheduler)
    finally:
        scheduler.shutdown(wait=False)
    assert "time zone check failed" in caplog.text
    assert job_context.zone == ZoneInfo(ZONE)


async def test_after_a_move_west_the_next_copy_comes_within_a_day_plus_the_zones_difference(
    house: House, job_context: runner.JobContext
) -> None:
    """Copies are one per date in the household's zone. Tokyo's copy for 10-23 is taken at
    18:30 UTC on 10-22; moved to Los Angeles at 20:00 UTC, the next run, 03:30 on 10-23 there,
    finds that date's copy and writes nothing. The next copy comes the night after, a day plus
    the zones' difference after the last: 40 hours in October, 41 once Los Angeles is on PST."""
    tokyo, los_angeles = ZoneInfo("Asia/Tokyo"), ZoneInfo("America/Los_Angeles")
    await set_zone(house, "Asia/Tokyo")
    job_context.zone = tokyo
    last = datetime(2026, 10, 22, 18, 30, tzinfo=UTC)  # 03:30 on 10-23 in Tokyo
    for at in (last - timedelta(days=1), last):
        house.clock.now = at
        await runner.nightly_backup(job_context)
    assert sorted(backups.existing_backups(job_context.backup_dir)) == [date(2026, 10, 22), date(2026, 10, 23)]

    house.clock.now = changed = datetime(2026, 10, 22, 20, tzinfo=UTC)
    await set_zone(house, "America/Los_Angeles")
    scheduler = runner.build_scheduler(job_context)
    scheduler.start(paused=True)
    try:
        await runner.follow_zone(job_context, scheduler)
        trigger = backup_job(scheduler).trigger
    finally:
        scheduler.shutdown(wait=False)
    first = trigger.get_next_fire_time(None, changed)
    second = trigger.get_next_fire_time(first, first + timedelta(seconds=1))
    assert first.astimezone(los_angeles).date() == date(2026, 10, 23)
    house.clock.now = first.astimezone(UTC)
    await runner.nightly_backup(job_context)
    assert len(backups.existing_backups(job_context.backup_dir)) == 2  # that date has its copy
    house.clock.now = second.astimezone(UTC)
    await runner.nightly_backup(job_context)
    assert date(2026, 10, 24) in backups.existing_backups(job_context.backup_dir)
    difference = tokyo.utcoffset(last) - los_angeles.utcoffset(last)
    assert second.astimezone(UTC) - last == timedelta(days=1) + difference == timedelta(hours=40)


# Calls read the zone under the lock ----------------------------------------------------------------


def change_zone_before_the_lock(monkeypatch: pytest.MonkeyPatch, module: Any, house: House, zone: str) -> None:
    """Commit ``zone`` from another session just before ``module`` takes the write lock, as
    ``set-timezone`` could in the gap the reviewer of #381 found."""
    real = module.write_transaction
    done: list[bool] = []

    @asynccontextmanager
    async def racing(db: Any) -> AsyncIterator[None]:
        if not done:
            done.append(True)
            await set_zone(house, zone)
        async with real(db):
            yield

    monkeypatch.setattr(module, "write_transaction", racing)


async def test_a_call_started_as_the_zone_changes_uses_the_new_zone(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    tomorrow = date(2026, 10, 23)
    change_zone_before_the_lock(monkeypatch, calls, house, "Europe/Berlin")
    async with house.sessionmaker() as db:
        event = await calls.start_call(db, house["Kim"], Slot.DINNER, tomorrow, now=house.clock())
    dinner = slot_schedule(default_settings(), Slot.DINNER)
    assert event.state is EventState.SCHEDULED
    assert event.opens_at == dinner.opens_at(tomorrow, ZoneInfo("Europe/Berlin"))
    assert event.opens_at != dinner.opens_at(tomorrow, ZoneInfo(ZONE))


async def test_the_tick_opens_no_call_on_a_zone_changed_as_it_ran(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 20:00 UTC, dinner is due in Chicago but two hours off in Los Angeles."""
    house.clock.now = OPENS
    change_zone_before_the_lock(monkeypatch, lifecycle, house, "America/Los_Angeles")
    async with house.sessionmaker() as db:
        opened = await lifecycle.open_due_calls(db, house.household, house.clock())
    assert opened == []
    monkeypatch.undo()
    house.clock.now = OPENS + timedelta(hours=2)  # dinner's time in Los Angeles
    await house.tick()
    event = await house.event(await dinner_id(house))
    assert event.state is EventState.COLLECTING


async def test_a_schedule_change_as_the_zone_changes_uses_the_new_zone(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 19:00 UTC, dinner moves from 15:00 to 12:00. In Chicago, 14:00, the new time's window
    has passed, so the old schedule keeps today's call at 15:00. In Los Angeles, 12:00, the new
    schedule can still open today's call, so none is kept."""
    house.clock.now = datetime(2026, 10, 22, 19, tzinfo=UTC)
    before = slot_schedule(default_settings(), Slot.DINNER)
    change_zone_before_the_lock(monkeypatch, schedule_service, house, "America/Los_Angeles")
    async with house.sessionmaker() as db:
        changed = await schedule_service.update_slot(
            db,
            house["Alex"],
            Slot.DINNER,
            replace(before, call_time=time(12)),
            seen=slot_fingerprint(before),
            now=house.clock(),
        )
    assert changed
    async with house.sessionmaker() as db:
        assert await EventRepository(db).call_for(house.household.id, Slot.DINNER, date(2026, 10, 22)) is None


# The commands --------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    from tests.test_config import REQUIRED

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


def row(path: Path) -> tuple[str, str]:
    with sqlite3.connect(path) as db:
        name, zone = db.execute("SELECT name, timezone FROM household").fetchone()
    return str(name), str(zone)


async def nothing(_house: House) -> None:
    return None


def test_set_timezone_prints_what_changed(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    with_house(cli_env, nothing)
    capsys.readouterr()
    assert cli.main(["set-timezone", "Europe/Berlin"]) == 0
    out = capsys.readouterr().out
    assert f"Changed the household's time zone from {ZONE} to Europe/Berlin." in out
    assert "within a minute" in out and "DINNERBELL_TIMEZONE=Europe/Berlin" in out
    assert row(cli_env)[1] == "Europe/Berlin"
    with sqlite3.connect(cli_env) as db:
        query = "SELECT actor_member_id FROM audit WHERE action = 'household.timezone_changed'"
        (actor,) = db.execute(query).fetchone()
    assert actor is None
    assert cli.main(["set-timezone", "Europe/Berlin"]) == 0
    assert "already Europe/Berlin. Nothing changed." in capsys.readouterr().out


def test_set_timezone_refuses_while_a_call_is_open(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    event_id = with_house(cli_env, lambda house: open_call(house, EventState.VOTING))
    capsys.readouterr()
    assert cli.main(["set-timezone", "Europe/Berlin"]) == 1
    err = capsys.readouterr().err
    assert err.startswith(household.OPEN_CALLS) and f"(ID {event_id}): Voting" in err
    assert row(cli_env)[1] == ZONE


def test_the_commands_refuse_without_a_database_a_household_or_a_valid_value(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in (["set-timezone", "Europe/Berlin"], ["set-household-name", "Elm Street"]):
        assert cli.main(command) == 1
        assert f"No database at {cli_env}. Nothing changed." in capsys.readouterr().err
    assert not cli_env.exists()
    assert cli.main(["migrate"]) == 0
    for command in (["set-timezone", "Europe/Berlin"], ["set-household-name", "Elm Street"]):
        assert cli.main(command) == 1
        assert "no household yet" in capsys.readouterr().err
    with_house(cli_env, nothing)
    assert cli.main(["set-timezone", "Region/City"]) == 2
    assert "Region/City isn't an IANA time zone name" in capsys.readouterr().err
    assert cli.main(["set-household-name", " "]) == 2
    assert "Enter a household name. Nothing changed." in capsys.readouterr().err
    assert row(cli_env) == ("Home", ZONE)


def test_set_household_name_prints_what_changed(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate"]) == 0
    with_house(cli_env, nothing)
    capsys.readouterr()
    assert cli.main(["set-household-name", "Elm Street"]) == 0
    out = capsys.readouterr().out
    assert "Renamed the household from 'Home' to 'Elm Street'." in out and "DINNERBELL_HOUSEHOLD_NAME" in out
    assert row(cli_env)[0] == "Elm Street"
    assert cli.main(["set-household-name", "Elm Street"]) == 0
    assert "already named 'Elm Street'. Nothing changed." in capsys.readouterr().out
