# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edges of the call lifecycle: races, failures the tick must survive, and display helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from dinnerbell.channels.base import NullChannel
from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.db.models import CatalogItemRow, OutboxRow
from dinnerbell.domain import notifications as n
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import RoleOffer, Status, Step
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.scheduler import runner
from dinnerbell.services import calls, lifecycle, outbox
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import SchedulerState
from dinnerbell.services.preferences import group_preferences, set_my_preference
from dinnerbell.web import filters
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def test_a_lost_race_to_open_the_same_call_is_a_clean_refusal(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two openers both see no call; the unique index stops the second insert."""
    house.clock.now = OPENS
    await house.tick()

    async def nothing_there(*_args: Any) -> None:
        return None

    monkeypatch.setattr(EventRepository, "call_for", nothing_there)
    async with house.sessionmaker() as db:
        with pytest.raises(lifecycle.CallExists):
            await lifecycle.create_call(
                db, house.household, Slot.DINNER, OPENS.date(), opens_at=OPENS, actor=None, now=OPENS
            )
        assert await lifecycle.open_due_calls(db, house.household, OPENS) == []
    assert await house.kinds() == ["call_opened"]


async def test_one_failing_household_or_call_does_not_stop_the_tick(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    real_open, real_advance = lifecycle.open_due_calls, lifecycle.advance

    async def broken_open(*args: Any) -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "open_due_calls", broken_open)
    assert await house.tick() == []
    assert "could not open calls" in caplog.text
    monkeypatch.setattr(lifecycle, "open_due_calls", real_open)
    await house.tick()

    async def broken_advance(*args: Any) -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "advance", broken_advance)
    house.clock.now = RESPOND_BY
    assert await house.tick() == []
    assert "could not advance call" in caplog.text
    monkeypatch.setattr(lifecycle, "advance", real_advance)
    assert await house.tick()  # the next tick catches up


async def test_a_winning_free_text_proposal_that_names_a_new_catalog_item_reuses_it(house: House) -> None:
    """Someone adds the place to the catalog while the vote is open; the decision uses that item."""
    event_id = await dinner_open(house)
    vietnamese = await house.tag("Vietnamese")
    async with house.sessionmaker() as db:
        pho = await calls.add_proposal(
            db, house["Sam"], event_id, Mode.PICKUP, free_text="Pho Place", cuisine_id=vietnamese.id, now=house.clock()
        )
        await calls.vote(db, house["Sam"], event_id, pho.id, now=house.clock())
    await house.add_item("Pho Place", Mode.PICKUP, cuisine="Vietnamese")
    house.clock.now = VOTE_BY
    await house.tick()
    async with house.sessionmaker() as db:
        items = list(await db.scalars(select(CatalogItemRow).where(CatalogItemRow.name == "Pho Place")))
    assert len(items) == 1
    assert (await house.event(event_id)).decided_item_id == items[0].id


async def dinner_open(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    async with house.sessionmaker() as db:
        for name in ("Alex", "Sam"):
            await calls.respond(db, house[name], event_id, Status.IN, now=house.clock())
        await calls.offer_role(db, house["Alex"], event_id, RoleOffer.PICKUP, True, now=house.clock())
    house.clock.now = RESPOND_BY
    await house.tick()
    return event_id


async def test_group_scores_sum_item_stances(house: House) -> None:
    taqueria = house.items["Taqueria"].id
    for name, score in (("Sam", 2), ("Kim", -1)):
        async with house.sessionmaker() as db:
            await set_my_preference(db, house[name], TargetType.ITEM, taqueria, Stance(score=score), now=house.clock())
    async with house.sessionmaker() as db:
        group = await group_preferences(db, house.household.id, frozenset({house["Sam"].id, house["Kim"].id}))
    assert dict(group.item_scores) == {taqueria: 1}


async def test_a_claim_lost_mid_delivery_is_logged(house: House, caplog: pytest.LogCaptureFixture) -> None:
    house.clock.now = OPENS
    await house.tick()

    class Hijacked(RecordingChannel):
        async def deliver(
            self, notification: n.Notification, *, thread: n.MessageRef | None = None
        ) -> n.DeliveryResult:
            async with house.sessionmaker() as db:  # another drainer took the row over
                await db.execute(text("UPDATE outbox SET claim_token = 'other'"))
                await db.commit()
            return await super().deliver(notification, thread=thread)

    await outbox.drain(house.sessionmaker, Hijacked(), house.clock)
    assert "outbox claim expired during delivery" in caplog.text
    async with house.sessionmaker() as db:
        row = await db.scalar(select(OutboxRow))
    assert row is not None and row.delivered_at is None


async def test_scheduler_jobs_tick_drain_and_survive_errors(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, migrated: Settings
) -> None:
    ctx = runner.JobContext(
        state=SchedulerState(),
        clock=house.clock,
        zone=ZoneInfo("America/Chicago"),
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=house.sessionmaker,
        channel=NullChannel(),
    )
    house.clock.now = OPENS
    await runner.call_tick(ctx)
    entries = await house.outbox()
    assert [e.kind for e in entries] == ["call_opened"] and entries[0].attempts == 1  # opened and delivered

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "tick", broken)
    monkeypatch.setattr(outbox, "drain", broken)
    await runner.call_tick(ctx)
    await runner.drain_outbox(ctx)
    assert "call tick failed" in caplog.text and "outbox drain failed" in caplog.text


def test_time_filters() -> None:
    moment = datetime(2026, 10, 22, 21, 30, tzinfo=UTC)
    assert filters.clock(moment, "America/Chicago") == "4:30 PM"
    assert filters.clock(datetime(2026, 10, 22, 5, 5, tzinfo=UTC), "UTC") == "5:05 AM"
    assert filters.clock(None, "UTC") == ""
    assert (filters.wall(time(0, 15)), filters.wall(time(12)), filters.wall(None)) == ("12:15 AM", "12:00 PM", "")
    assert (filters.hhmm(time(7, 5)), filters.hhmm(None)) == ("07:05", "")
    assert (filters.local_hhmm(moment, "America/Chicago"), filters.local_hhmm(None, "UTC")) == ("16:30", "")
    assert filters.day(moment.date()) == "Thu Oct 22"


async def test_a_new_household_stores_the_default_schedule(house: House) -> None:
    from dinnerbell.db.meals import household_settings
    from dinnerbell.domain.schedule import default_settings

    async with house.sessionmaker() as db:
        stored = await household_settings(db, house.household.id)
    assert stored == default_settings()


async def test_a_failed_step_is_rolled_back_not_committed_by_the_next(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the tick logged a failure but kept the session's half-done writes, and the
    next call's step committed them."""
    house.clock.now = OPENS
    await house.tick()  # an open call for the next step to commit through

    async def half_done(db: Any, *args: Any) -> list[Any]:
        await db.execute(text("UPDATE household SET name = 'half-done'"))
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "open_due_calls", half_done)
    house.clock.now = OPENS + timedelta(minutes=75)  # the reminder is due
    assert await house.tick() == [Step.REMIND]
    async with house.sessionmaker() as db:
        assert await db.scalar(text("SELECT name FROM household")) == "Home"


async def test_the_scheduler_never_drains_twice_at_once(house: House, migrated: Settings) -> None:
    """The tick's drain and the 5-second drain job share one lock, so in one process a channel
    is only ever handling one notification at a time; the database claim covers other processes."""
    import asyncio

    in_flight, peak = 0, 0

    class Counting(RecordingChannel):
        async def deliver(
            self, notification: n.Notification, *, thread: n.MessageRef | None = None
        ) -> n.DeliveryResult:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.005)
            in_flight -= 1
            return await super().deliver(notification, thread=thread)

    ctx = runner.JobContext(
        state=SchedulerState(),
        clock=house.clock,
        zone=ZoneInfo("America/Chicago"),
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=house.sessionmaker,
        channel=Counting(),
    )
    house.clock.now = OPENS
    await house.tick()
    await house.drain(RecordingChannel())  # the call post is out, so the reminders are free to race
    async with house.sessionmaker() as db:
        from dinnerbell.db.outbox import OutboxRepository

        event = await EventRepository(db).get(house.household.id, await dinner_id(house))
        assert event is not None
        info = n.EventInfo(
            event.id, 1, Slot.DINNER, event.day, event.state, 1, event.path, "UTC", event.respond_deadline
        )
        for member in range(1, 9):
            await OutboxRepository(db).enqueue(1, event.id, n.ResponseReminder(info, n.PersonInfo(member, "M")), OPENS)
        await db.commit()
    await asyncio.gather(runner.drain_outbox(ctx), runner.drain_outbox(ctx), runner.call_tick(ctx))
    assert peak == 1
    assert all(e.attempts == 1 for e in await house.outbox())


async def test_one_households_failing_lookup_does_not_stop_the_tick(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: when opening calls failed, the fallback lookup of open calls was unguarded,
    so a database error there stopped the tick for every later household."""
    from dinnerbell.db.repositories import HouseholdRepository
    from dinnerbell.domain.schedule import default_settings

    async with house.sessionmaker() as db:
        other = await HouseholdRepository(db).create("Other", "America/Chicago", OPENS, default_settings())
        await db.commit()
    real_open, real_ids = lifecycle.open_due_calls, EventRepository.open_ids

    async def failing_open(db: Any, household: Any, now: Any) -> list[Any]:
        if household.id == house.household.id:
            raise RuntimeError("open failed")
        return await real_open(db, household, now)

    async def failing_ids(self: EventRepository, household_id: int) -> list[int]:
        if household_id == house.household.id:
            raise RuntimeError("lookup failed")
        return await real_ids(self, household_id)

    monkeypatch.setattr(lifecycle, "open_due_calls", failing_open)
    monkeypatch.setattr(EventRepository, "open_ids", failing_ids)
    house.clock.now = OPENS
    await house.tick()
    async with house.sessionmaker() as db:
        assert await EventRepository(db).call_for(other.id, Slot.DINNER, OPENS.date()) is not None
    assert "could not list calls" in caplog.text


async def test_the_tick_does_not_wait_behind_a_running_drain(house: House, migrated: Settings) -> None:
    """A hung channel call holds the drain lock; the tick must still finish its lifecycle work
    instead of queuing behind it, or every later tick would be skipped."""
    import asyncio

    ctx = runner.JobContext(
        state=SchedulerState(),
        clock=house.clock,
        zone=ZoneInfo("America/Chicago"),
        database_path=migrated.database_path,
        backup_dir=migrated.backup_dir,
        sessionmaker=house.sessionmaker,
        channel=RecordingChannel(),
    )
    house.clock.now = OPENS
    async with ctx.drain_lock:  # stands in for a drain stuck in a channel call
        await asyncio.wait_for(runner.call_tick(ctx), timeout=2)
    async with house.sessionmaker() as db:
        event = await EventRepository(db).get(house.household.id, await dinner_id(house))
        assert event is not None  # the tick still opened the call
