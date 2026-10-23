# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell scrub-test`` (ADR 0006): a restored production backup, made safe for test."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell import main as cli
from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.models import AuditRow, EventRow, HouseholdRow, MemberRow, OutboxRow, SessionRow
from dinnerbell.db.repositories import SessionRepository
from dinnerbell.domain.events import EndReason, EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.schedule import slot_schedules
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.scrub import ScrubRefused, ScrubSummary, scrub_test
from tests.support.calls import OPENS, REMIND, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.slack import SLACK_IDS, link
from tests.test_config import REQUIRED

pytestmark = pytest.mark.anyio

LABEL = "TEST"
KEEP = ("admin@example.com", " SAM@Example.com ")
"""Alex (the admin, bootstrapped as admin@example.com) and Sam, written the way people type."""


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    """Production-like data: everyone linked and signed in, an open dinner call with delivered
    and pending notifications, a decided lunch tomorrow, and a finished dinner in the meal log."""
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    for name, slack_id in SLACK_IDS.items():
        await link(house, name, slack_id)
    async with house.sessionmaker() as db:
        sessions = SessionRepository(db)
        for name in ("Alex", "Kim"):
            await sessions.add(token_hash=name * 8, member=house[name], now=clock(), expires_at=clock() + timedelta(1))
        events = EventRepository(db)
        common = {"reminder_minutes": 15, "vote_minutes": 45, "created_by": None, "now": clock()}
        await events.add(
            household_id=house.household.id,
            slot=Slot.LUNCH,
            day=date(2026, 10, 23),
            state=EventState.DECIDED,
            opens_at=OPENS,
            respond_deadline=OPENS,
            **common,
        )
        done = await events.add(
            household_id=house.household.id,
            slot=Slot.DINNER,
            day=date(2026, 10, 21),
            state=EventState.DONE,
            opens_at=OPENS - timedelta(days=1),
            respond_deadline=OPENS - timedelta(days=1),
            **common,
        )
        await MealLogRepository(db).add(
            household_id=house.household.id,
            day=date(2026, 10, 21),
            slot=Slot.DINNER,
            item_id=house.items["Taqueria"].id,
            cuisines=(),
            participants=(),
            logged_by=None,
            now=clock(),
            event_id=done.id,
        )
        await db.commit()
    clock.now = OPENS
    await house.tick()
    await house.drain(RecordingChannel())  # the call message went out: its ref is in the outbox
    clock.now = REMIND
    await house.tick()  # reminders wait to go out
    return house


async def scrub(house: House, *keep: str, label: str = LABEL) -> ScrubSummary:
    async with house.sessionmaker() as db:
        return await scrub_test(db, instance_label=label, keep=keep or KEEP, now=house.clock())


async def snapshot(house: House) -> dict[str, Any]:
    """Everything the scrub may change, plus what it must never change."""
    async with house.sessionmaker() as db:
        members = [
            (m.id, m.active, m.slack_user_id, m.slack_link_locked, m.updated_at)
            for m in await db.scalars(select(MemberRow).order_by(MemberRow.id))
        ]
        events = [(e.id, e.state, e.end_reason, e.updated_at) for e in await db.scalars(select(EventRow))]
        return {
            "members": members,
            "events": events,
            "settings": await household_settings(db, house.household.id),
            "sessions": sorted(s.member_id for s in await db.scalars(select(SessionRow))),
            "outbox": [(o.id, o.kind) for o in await db.scalars(select(OutboxRow))],
            "meals": [(m.id, m.event_id, m.date) for m in await house.meals()],
        }


async def test_scrub_leaves_only_kept_members_and_sends_nothing(house: House) -> None:
    before = await snapshot(house)
    assert any(kind == "response_reminder" for _, kind in before["outbox"])  # pending rows exist

    summary = await scrub(house)

    assert summary == ScrubSummary(
        members_kept=2,
        members_deactivated=2,
        slack_links_removed=2,
        slots_turned_off=1,
        events_abandoned=1,
        events_cancelled=1,
        outbox_pending_deleted=summary.outbox_pending_deleted,
        outbox_rows_deleted=len(before["outbox"]),
    )
    assert 0 < summary.outbox_pending_deleted < summary.outbox_rows_deleted
    for name in ("Alex", "Sam"):
        member = await house.refresh(name)
        assert (member.active, member.slack_user_id) == (True, SLACK_IDS[name])
    for name in ("Kim", "Lee"):
        member = await house.refresh(name)
        assert (member.active, member.slack_user_id, member.slack_link_locked) == (False, None, True)

    after = await snapshot(house)
    assert after["sessions"] == [house["Alex"].id]  # Kim was signed out
    assert not any(s.enabled for s in slot_schedules(after["settings"]).values())
    assert after["outbox"] == []  # emptied, and abandoning wrote no notifications
    assert after["meals"] == before["meals"]
    dinner = await house.event(await dinner_id(house))
    assert (dinner.state, dinner.end_reason) == (EventState.ABANDONED, EndReason.MISSED)
    states = {e.day: e.state for e in [await house.event(i) for i, *_ in after["events"]]}
    assert states == {
        date(2026, 10, 21): EventState.DONE,  # history stays as it was
        date(2026, 10, 22): EventState.ABANDONED,
        date(2026, 10, 23): EventState.CANCELLED,
    }
    done = next(e for e in before["events"] if e[1] == "done")
    assert done in after["events"]  # not even touched

    (row,) = await house.audits("instance.")
    target = f"household:{house.household.id}"
    assert (row.action, row.actor_member_id, row.target) == ("instance.scrubbed", None, target)
    assert row.payload["kept_member_ids"] == sorted([house["Alex"].id, house["Sam"].id])
    assert row.payload["members_deactivated"] == 2 and row.payload["instance_label"] == LABEL
    assert "@" not in str(row.payload)  # no emails in the audit log


async def test_a_second_run_changes_nothing(house: House) -> None:
    await scrub(house)
    house.clock.now += timedelta(minutes=5)
    before = await snapshot(house)
    summary = await scrub(house)
    assert summary == ScrubSummary(2, 0, 0, 0, 0, 0, 0, 0)
    assert await snapshot(house) == before
    assert len(await house.audits("instance.")) == 2  # every run is audited


async def test_a_scheduled_call_is_abandoned_quietly(house: House) -> None:
    """A call that hasn't opened yet ends too, so test never posts it."""
    async with house.sessionmaker() as db:
        await EventRepository(db).add(
            household_id=house.household.id,
            slot=Slot.DINNER,
            day=date(2026, 10, 24),
            state=EventState.SCHEDULED,
            opens_at=OPENS + timedelta(days=2),
            respond_deadline=OPENS + timedelta(days=2),
            reminder_minutes=15,
            vote_minutes=45,
            created_by=None,
            now=house.clock(),
        )
        await db.commit()
    assert (await scrub(house)).events_abandoned == 2
    async with house.sessionmaker() as db:
        assert await EventRepository(db).open_ids(house.household.id) == []


@pytest.mark.parametrize(
    ("label", "keep", "message"),
    [
        ("", KEEP, "only on a labeled instance"),
        (LABEL, ("admin@example.com", "sma@example.com"), "--keep number 2"),
        (LABEL, ("admin@example.com", "not an email"), "--keep number 2 isn't a valid email"),
        (LABEL, ("sam@example.com",), "at least one active admin"),
        (LABEL, (), "at least one --keep"),
    ],
)
async def test_refusals_change_nothing(house: House, label: str, keep: tuple[str, ...], message: str) -> None:
    before = await snapshot(house)
    async with house.sessionmaker() as db:
        with pytest.raises(ScrubRefused, match=message) as refused:
            await scrub_test(db, instance_label=label, keep=keep, now=house.clock())
    assert "@" not in str(refused.value)  # never echoes an email
    assert await snapshot(house) == before
    assert await house.audits("instance.") == []


async def test_an_empty_database_is_refused(database: Database, clock: FakeClock) -> None:
    async with database.sessionmaker() as db:
        with pytest.raises(ScrubRefused, match="no household"):
            await scrub_test(db, instance_label=LABEL, keep=KEEP, now=clock())


# The command -------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    database = tmp_path / "data" / "dinnerbell.db"
    monkeypatch.setenv("DINNERBELL_DATABASE_URL", f"sqlite:///{database}")
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        yield database
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


def test_the_command_refuses_without_a_label_before_touching_the_database(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["scrub-test", "--keep", "admin@example.com"]) == 1
    assert "DINNERBELL_INSTANCE_LABEL" in capsys.readouterr().err
    assert not cli_env.exists()  # not even migrated


def test_the_command_needs_a_keep(cli_env: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["scrub-test"])


def test_the_command_scrubs_and_prints_a_summary(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DINNERBELL_INSTANCE_LABEL", LABEL)
    assert cli.main(["migrate"]) == 0

    async def seed() -> None:
        database = open_database(f"sqlite+aiosqlite:///{cli_env}")
        async with database.sessionmaker() as db:
            await bootstrap(db, household_name="Home", timezone="UTC", admin_email="admin@example.com", now=OPENS)
            await db.commit()
        await database.close()

    asyncio.run(seed())
    assert cli.main(["scrub-test", "--keep", "Admin@Example.com"]) == 0
    out = capsys.readouterr().out
    assert "Scrubbed for test." in out and "Slots turned off: 1" in out and "Members kept: 1" in out

    assert cli.main(["scrub-test", "--keep", "admim@example.com"]) == 1
    err = capsys.readouterr().err
    assert "No member has the email given as --keep number 1" in err and "admim" not in err

    async def audits() -> list[str]:
        database = open_database(f"sqlite+aiosqlite:///{cli_env}")
        async with database.sessionmaker() as db:
            rows = list(await db.scalars(select(AuditRow.action).where(AuditRow.action == "instance.scrubbed")))
            assert await db.scalar(select(HouseholdRow.id)) is not None
        await database.close()
        return rows

    assert asyncio.run(audits()) == ["instance.scrubbed"]
