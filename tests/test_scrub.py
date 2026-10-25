# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell scrub-test`` (ADR 0006): a restored production backup, made safe for test."""

from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell import main as cli
from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.models import AuditRow, EventRow, FeedbackRow, MemberRow, OutboxRow, SessionRow
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import MemberRepository, SessionRepository
from dinnerbell.domain.events import EndReason, EventState
from dinnerbell.domain.feedback import FeedbackInput, MealFacts, Rating
from dinnerbell.domain.meals import MemberRef, Slot
from dinnerbell.domain.schedule import slot_schedules
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.scrub import ScrubPreview, ScrubRefused, ScrubSummary, scrub_test
from dinnerbell.services.scrub import preview as scrub_preview
from tests.support.calls import OPENS, REMIND, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.slack import SLACK_IDS, link
from tests.test_config import REQUIRED

pytestmark = pytest.mark.anyio

LABEL = "TEST"
DAYS = {"open": date(2026, 10, 22), "decided": date(2026, 10, 23)}
KEEP = ("admin@example.com", " SAM@Example.com ")
"""Alex (the admin, bootstrapped as admin@example.com) and Sam, written the way people type."""


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    """Production-like data: everyone linked, Alex and Kim signed in, Lee deactivated earlier but
    with a session left over, an open dinner call with delivered and pending notifications, a
    decided lunch tomorrow, and a finished dinner in the meal log."""
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    for name, slack_id in SLACK_IDS.items():
        await link(house, name, slack_id)
    async with house.sessionmaker() as db:
        sessions = SessionRepository(db)
        for name in ("Alex", "Kim", "Lee"):
            await sessions.add(token_hash=name * 8, member=house[name], now=clock(), expires_at=clock() + timedelta(1))
        await MemberRepository(db).update(house.household.id, house["Lee"].id, clock(), active=False)
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
    async with house.sessionmaker() as db:
        pending, _ = await OutboxRepository(db).backlog()
    assert 0 < pending < len(before["outbox"])  # some sent (with refs), some waiting
    async with house.sessionmaker() as db:
        event_ids = {
            name: await db.scalar(select(EventRow.id).where(EventRow.date == day)) for name, day in DAYS.items()
        }

    summary = await scrub(house)

    assert summary == ScrubSummary(
        members_kept=2,
        members_deactivated=1,  # Lee was already inactive
        slack_links_removed=2,
        slots_turned_off=1,
        events_abandoned=1,
        events_cancelled=1,
        outbox_pending_deleted=pending,
        outbox_rows_deleted=len(before["outbox"]),
    )
    for name in ("Alex", "Sam"):
        member = await house.refresh(name)
        assert (member.active, member.slack_user_id) == (True, SLACK_IDS[name])
    for name in ("Kim", "Lee"):
        member = await house.refresh(name)
        assert (member.active, member.slack_user_id, member.slack_link_locked) == (False, None, True)

    after = await snapshot(house)
    assert after["sessions"] == [house["Alex"].id]  # Kim signed out, and Lee's stale session gone
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

    ended = [(r.action, r.target, r.actor_member_id, r.payload) for r in await house.audits("event.")]
    assert sorted(ended[-2:], key=lambda row: row[0]) == [
        ("event.abandoned", f"event:{event_ids['open']}", None, {"reason": "scrub", "from": "collecting"}),
        ("event.cancelled", f"event:{event_ids['decided']}", None, {"reason": "scrub", "from": "decided"}),
    ]
    (row,) = await house.audits("instance.")
    target = f"household:{house.household.id}"
    assert (row.action, row.actor_member_id, row.target) == ("instance.scrubbed", None, target)
    assert row.payload["kept_member_ids"] == sorted([house["Alex"].id, house["Sam"].id])
    assert row.payload["members_deactivated"] == 1 and row.payload["instance_label"] == LABEL
    assert "@" not in str(row.payload)  # no emails in the audit log


async def test_scrub_deletes_all_feedback_and_counts_it(house: House) -> None:
    """Feedback says how each member rated a meal (FR-49). Test, where testers act as other
    people, never keeps it; the meals themselves stay."""
    (meal,) = await house.meals()
    eaters = (MemberRef(house["Kim"].id, "Kim"),)
    item = house.items["Taqueria"]
    facts = MealFacts(meal.id, meal.date, Slot.DINNER, item.id, item.name, item.kind, None, None, eaters)
    async with house.sessionmaker() as db:
        await FeedbackRepository(db).save(
            house.household.id, house["Kim"].id, FeedbackInput(Rating.NOT_AGAIN), facts, house.clock()
        )
        await db.commit()
        shown = await scrub_preview(db)
    assert shown is not None and shown.feedback == 1 and "Feedback on meals: 1" in shown.lines(house.clock())

    summary = await scrub(house)

    assert summary.feedback_deleted == 1 and "Feedback rows deleted: 1" in summary.lines()
    async with house.sessionmaker() as db:
        assert list(await db.scalars(select(FeedbackRow))) == []
    assert [m.id for m in await house.meals()] == [meal.id]
    (row,) = await house.audits("instance.")
    assert row.payload["feedback_deleted"] == 1 and "not_again" not in str(row.payload)


async def test_a_second_run_changes_nothing(house: House) -> None:
    await scrub(house)
    house.clock.now += timedelta(minutes=5)
    before = await snapshot(house)
    summary = await scrub(house)
    assert summary == ScrubSummary(2, 0, 0, 0, 0, 0, 0, 0)
    assert await snapshot(house) == before
    assert len(await house.audits("instance.")) == 2  # every run is audited
    assert len([r for r in await house.audits("event.") if r.payload.get("reason") == "scrub"]) == 2


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


class Terminal(io.StringIO):
    """Stands in for an interactive stdin, answering ``input()`` with what it was given."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def labeled(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A labeled instance whose database holds a household with one admin."""
    monkeypatch.setenv("DINNERBELL_INSTANCE_LABEL", LABEL)
    assert cli.main(["migrate"]) == 0

    async def seed() -> None:
        database = open_database(f"sqlite+aiosqlite:///{cli_env}")
        async with database.sessionmaker() as db:
            await bootstrap(db, household_name="Home", timezone="UTC", admin_email="admin@example.com", now=OPENS)
            await db.commit()
        await database.close()

    asyncio.run(seed())
    return cli_env


def scrub_audits(path: Path) -> list[str]:
    async def read() -> list[str]:
        database = open_database(f"sqlite+aiosqlite:///{path}")
        async with database.sessionmaker() as db:
            rows = list(await db.scalars(select(AuditRow.action).where(AuditRow.action == "instance.scrubbed")))
        await database.close()
        return rows

    return asyncio.run(read())


def test_the_command_shows_the_database_then_scrubs_with_yes(labeled: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scrub-test", "--keep", "Admin@Example.com", "--yes"]) == 0
    out = capsys.readouterr().out
    assert f"Database: {labeled}" in out and "Household: Home" in out
    assert "Last activity: 2026-10-22 20:00 UTC (" in out  # bootstrap's audit row
    assert "Active members: 1" in out and "Enabled slots: 1" in out and "Open events: 0" in out
    assert "Notifications not yet sent: 0" in out
    assert "Scrubbed for test." in out and "Slots turned off: 1" in out and "Members kept: 1" in out
    assert scrub_audits(labeled) == ["instance.scrubbed"]

    assert cli.main(["scrub-test", "--keep", "admim@example.com", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "No member has the email given as --keep number 1" in err and "admim" not in err
    assert scrub_audits(labeled) == ["instance.scrubbed"]


def test_a_typed_confirmation_at_a_terminal_scrubs(
    labeled: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal("scrub\n"))
    assert cli.main(["scrub-test", "--keep", "admin@example.com"]) == 0
    assert 'Type "scrub" to scrub it' in capsys.readouterr().out
    assert scrub_audits(labeled) == ["instance.scrubbed"]


@pytest.mark.parametrize(
    ("stdin", "message"),
    [
        (Terminal("yes\n"), "Not scrubbed"),
        (Terminal(""), "Not scrubbed"),
        (io.StringIO("scrub\n"), "No terminal to confirm on"),
    ],
)
def test_no_confirmation_changes_nothing(
    labeled: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: io.StringIO, message: str
) -> None:
    """A wrong word, end of input, or no terminal (and no --yes) refuses before migrating."""
    migrations: list[object] = []
    monkeypatch.setattr(cli, "run_migrations", lambda *args: migrations.append(args))
    monkeypatch.setattr("sys.stdin", stdin)
    assert cli.main(["scrub-test", "--keep", "admin@example.com"]) == 1
    assert message in capsys.readouterr().err
    assert migrations == [] and scrub_audits(labeled) == []


def test_the_command_refuses_a_missing_or_unreadable_database(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DINNERBELL_INSTANCE_LABEL", LABEL)
    assert cli.main(["scrub-test", "--keep", "admin@example.com", "--yes"]) == 1
    assert "No database at" in capsys.readouterr().err
    assert not cli_env.exists()  # not created by asking

    cli_env.parent.mkdir(parents=True)
    cli_env.write_bytes(b"not a database, just some bytes " * 64)
    assert cli.main(["scrub-test", "--keep", "admin@example.com", "--yes"]) == 1
    assert "Can't read the database" in capsys.readouterr().err

    cli_env.unlink()
    assert cli.main(["migrate"]) == 0  # a schema, but no household yet
    assert cli.main(["scrub-test", "--keep", "admin@example.com", "--yes"]) == 1
    assert "no household yet" in capsys.readouterr().err


def test_the_preview_reads_without_writing(
    labeled: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Opened read-only: no journal-mode change and no -wal or -shm files, even on refusal."""
    with sqlite3.connect(labeled) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    before = sorted(p.name for p in labeled.parent.iterdir())
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["scrub-test", "--keep", "admin@example.com"]) == 1
    assert "Household: Home" in capsys.readouterr().out
    assert sorted(p.name for p in labeled.parent.iterdir()) == before
    with sqlite3.connect(labeled) as db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_a_pre_m2_backup_is_previewed_migrated_and_scrubbed(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backup at 0002_catalog has no event or outbox table yet; they count as 0."""
    from tests.conftest import make_settings
    from tests.test_migrations import _upgrade_to

    monkeypatch.setenv("DINNERBELL_INSTANCE_LABEL", LABEL)
    _upgrade_to(make_settings(cli_env.parent.parent), "0002_catalog")
    stamp = "2026-10-01 12:00:00.000000"
    with sqlite3.connect(cli_env) as db:
        db.execute(
            "INSERT INTO household (id, name, timezone, settings, created_at) VALUES (1, 'Home', 'UTC', '{}', ?)",
            (stamp,),
        )
        for member_id, email, role in ((1, "admin@example.com", "admin"), (2, "kim@example.com", "member")):
            db.execute(
                "INSERT INTO member (id, household_id, display_name, email, slack_user_id, role, can_cook, can_pickup,"
                " can_order, mute_dm_reminders, feedback_reminder, active, created_at, updated_at)"
                " VALUES (?, 1, ?, ?, ?, ?, 0, 0, 0, 0, 0, 1, ?, ?)",
                (member_id, email.split("@")[0], email, f"U0{member_id}", role, stamp, stamp),
            )
        db.execute(
            "INSERT INTO audit (household_id, action, target, payload, created_at)"
            " VALUES (1, 'member.invited', 'member:2', '{}', ?)",
            (stamp,),
        )
        assert {"event", "outbox"}.isdisjoint(r[0] for r in db.execute("SELECT name FROM sqlite_master"))

    assert cli.main(["scrub-test", "--keep", "admin@example.com", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Last activity: 2026-10-01 12:00 UTC (" in out
    assert "Active members: 2" in out and "Open events: 0" in out and "Notifications not yet sent: 0" in out
    assert "Members deactivated: 1" in out and "Slack links removed: 1" in out
    with sqlite3.connect(cli_env) as db:
        assert db.execute("SELECT active, slack_user_id FROM member WHERE id = 2").fetchone() == (0, None)


def test_last_activity_reads_as_an_age() -> None:
    at = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    preview = ScrubPreview("Home", 1, 1, 0, 0, at)

    def activity(now: datetime) -> str:
        return preview.lines(now)[1]

    assert activity(at + timedelta(minutes=1)) == "Last activity: 2026-10-23 12:00 UTC (1 minute ago)"
    assert activity(at + timedelta(minutes=7)).endswith("(7 minutes ago)")
    assert activity(at + timedelta(hours=5)).endswith("(5 hours ago)")
    assert activity(at + timedelta(days=9)).endswith("(9 days ago)")
    assert ScrubPreview("Home", 1, 1, 0, 0, None).lines(at)[1] == "Last activity: none recorded"
