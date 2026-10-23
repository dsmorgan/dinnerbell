# SPDX-License-Identifier: AGPL-3.0-or-later
"""The notification outbox: idempotency, coalescing, ordered and concurrent delivery, retries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from dinnerbell.config import Settings
from dinnerbell.db.outbox import OutboxEntry, OutboxRepository
from dinnerbell.domain import notifications as n
from dinnerbell.domain.events import EndReason, EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.services import outbox
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import outbox_backlog
from tests.support.calls import RecordingChannel
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)


def info(event_id: int, run: int = 1) -> n.EventInfo:
    return n.EventInfo(
        event_id, 1, Slot.DINNER, date(2026, 10, 22), EventState.COLLECTING, run, f"/events/{event_id}", "UTC", NOW
    )


def opened(event_id: int) -> n.CallOpened:
    return n.CallOpened(info(event_id), ())


def updated(event_id: int, head_count: int) -> n.CallUpdated:
    return n.CallUpdated(info(event_id), head_count=head_count)


@pytest.fixture
async def database(migrated: Settings, clock: FakeClock) -> AsyncIterator[Database]:
    clock.now = NOW
    database = open_database(migrated.async_database_url)
    async with database.sessionmaker() as db:
        await bootstrap(db, household_name="Home", timezone="UTC", admin_email="a@example.com", now=clock())
        for event_id in (1, 2):
            await db.execute(
                text(
                    "INSERT INTO event (id, household_id, kind, slot, date, state, run, version, opens_at, "
                    "respond_deadline, reminder_minutes, vote_minutes, created_at, updated_at) VALUES "
                    "(:id, 1, 'call', :slot, '2026-10-22', 'collecting', 1, 1, :t, :t, 15, 45, :t, :t)"
                ),
                {"id": event_id, "slot": "dinner" if event_id == 1 else "lunch", "t": "2026-10-22 20:00:00"},
            )
        await db.commit()
    yield database
    await database.close()


async def enqueue(database: Database, *notes: n.Notification, now: datetime = NOW) -> None:
    async with database.sessionmaker() as db:
        for note in notes:
            await OutboxRepository(db).enqueue(1, note.event.id, note, now)
        await db.commit()


async def entries(database: Database) -> list[OutboxEntry]:
    async with database.sessionmaker() as db:
        return await OutboxRepository(db).for_household(1)


async def test_the_same_key_is_stored_once(database: Database) -> None:
    await enqueue(database, opened(1), opened(1))
    await enqueue(database, opened(1))
    assert [e.idempotency_key for e in await entries(database)] == ["event:1:run1:call_opened"]


async def test_pending_updates_coalesce_and_claimed_ones_do_not(database: Database, clock: FakeClock) -> None:
    await enqueue(database, opened(1), updated(1, 1), updated(1, 2), updated(1, 3))
    rows = await entries(database)
    assert [(e.kind, e.idempotency_key, e.payload["head_count"]) for e in rows] == [
        ("call_opened", "event:1:run1:call_opened", 0),
        ("call_updated", "event:1:run1:call_updated:1", 3),  # the latest state won
    ]
    async with database.sessionmaker() as db:
        assert await OutboxRepository(db).claim(rows[1].id, "t", now=NOW, until=NOW + timedelta(minutes=5))
        await db.commit()
    await enqueue(database, updated(1, 4))  # the first update is out for delivery: queue another
    assert [e.idempotency_key for e in await entries(database)][-1] == "event:1:run1:call_updated:2"


async def test_delivery_threads_and_updates_use_the_posts_ref(database: Database, clock: FakeClock) -> None:
    ready = n.ProposalsReady(info(1), ())
    await enqueue(database, opened(1), updated(1, 2), ready, n.ProposalsUpdated(info(1), ()))
    channel = RecordingChannel()
    assert (await outbox.drain(database.sessionmaker, channel, clock)).delivered == 4
    call_ref, ready_ref = channel.sent[0].ref, channel.sent[2].ref
    assert [(s.op, s.ref if s.op == "update" else s.thread) for s in channel.sent] == [
        ("deliver", None),
        ("update", call_ref),
        ("deliver", call_ref),
        ("update", ready_ref),
    ]
    assert await outbox.drain(database.sessionmaker, channel, clock) == outbox.DrainResult()


async def test_a_failure_holds_back_its_events_later_rows_only(database: Database, clock: FakeClock) -> None:
    await enqueue(database, opened(1), opened(2), updated(1, 2), updated(2, 2))
    channel = RecordingChannel(fail=1)  # the first call fails: event 1's post
    result = await outbox.drain(database.sessionmaker, channel, clock)
    assert (result.delivered, result.retrying) == (2, 1)
    assert [(s.kind, s.key) for s in channel.sent] == [
        ("call_opened", "event:2:run1:call_opened"),
        ("call_updated", "event:2:run1:call_updated"),
    ]
    first = (await entries(database))[0]
    assert (first.attempts, first.available_at) == (1, clock() + timedelta(seconds=30))
    async with database.sessionmaker() as db:
        row = (
            await db.execute(text("SELECT last_error, claim_token FROM outbox WHERE id = :id"), {"id": first.id})
        ).one()
    assert row == ("ConnectionError: slack is down", None)
    assert (await outbox.drain(database.sessionmaker, channel, clock)).delivered == 0  # not due yet
    clock.advance(timedelta(seconds=30))
    assert (await outbox.drain(database.sessionmaker, channel, clock)).delivered == 2
    assert [s.kind for s in channel.sent][-2:] == ["call_opened", "call_updated"]


async def test_retries_back_off_then_give_up_and_unblock(database: Database, clock: FakeClock) -> None:
    await enqueue(database, opened(1), updated(1, 2))
    channel = RecordingChannel(fail=outbox.MAX_ATTEMPTS)
    waits = []
    for _ in range(outbox.MAX_ATTEMPTS):
        result = await outbox.drain(database.sessionmaker, channel, clock)
        row = (await entries(database))[0]
        waits.append(row.available_at - clock())
        clock.now = row.available_at
    assert waits[: outbox.MAX_ATTEMPTS - 1] == [timedelta(seconds=30 * 2**i) for i in range(outbox.MAX_ATTEMPTS - 1)]
    # The post failed for good, so it stops holding back its update, which has no message to
    # edit and is closed out in the same drain.
    assert (result.failed, result.delivered) == (1, 1)
    async with database.sessionmaker() as db:
        rows = (
            await db.execute(text("SELECT failed_at IS NOT NULL, delivered_at IS NOT NULL, last_error FROM outbox"))
        ).all()
        assert await OutboxRepository(db).backlog() == (0, 1)
    assert rows == [(1, 0, "ConnectionError: slack is down"), (0, 1, outbox.NO_MESSAGE)]
    assert outbox.retry_delay(20) == timedelta(hours=1)


async def test_a_corrupt_payload_fails_for_good(database: Database, clock: FakeClock) -> None:
    await enqueue(database, opened(1))
    async with database.sessionmaker() as db:
        await db.execute(text("UPDATE outbox SET payload = '{}'"))
        await db.commit()
    result = await outbox.drain(database.sessionmaker, RecordingChannel(), clock)
    assert result.failed == 1
    assert await outbox_backlog(database.sessionmaker) == (0, 1)


@dataclass
class SlowChannel(RecordingChannel):
    """Yields to the event loop mid-delivery, so concurrent drainers interleave."""

    started: list[str] = field(default_factory=list)

    async def deliver(self, notification: n.Notification, *, thread: n.MessageRef | None = None) -> n.DeliveryResult:
        self.started.append(notification.idempotency_key)
        await asyncio.sleep(0.01)
        return await super().deliver(notification, thread=thread)


async def test_concurrent_drainers_never_deliver_a_row_twice(database: Database, clock: FakeClock) -> None:
    notes: list[n.Notification] = []
    for event_id in (1, 2):
        notes += [opened(event_id), n.CallAbandoned(info(event_id), EndReason.TOO_FEW)]
        notes += [n.ResponseReminder(info(event_id), n.PersonInfo(m, "M")) for m in range(1, 6)]
    await enqueue(database, *notes)
    channel = SlowChannel()
    results = await asyncio.gather(*(outbox.drain(database.sessionmaker, channel, clock) for _ in range(4)))
    keys = [s.key for s in channel.sent]
    assert len(keys) == len(set(keys)) == len(notes)
    assert sum(r.delivered for r in results) == len(notes)
    for event_id in (1, 2):  # and in order within each event
        mine = [k for k in keys if k.startswith(f"event:{event_id}:")]
        assert mine == [note.idempotency_key for note in notes if note.event.id == event_id]


async def test_an_expired_claim_can_be_taken_over(
    database: Database, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    await enqueue(database, opened(1))
    (row,) = await entries(database)
    async with database.sessionmaker() as db:
        repo = OutboxRepository(db)
        assert await repo.claim(row.id, "dead", now=NOW, until=NOW + outbox.LEASE)
        assert not await repo.claim(row.id, "other", now=NOW + timedelta(minutes=1), until=NOW + outbox.LEASE)
        await db.commit()
    clock.now = NOW + outbox.LEASE  # the first drainer died mid-delivery
    channel = RecordingChannel()
    assert (await outbox.drain(database.sessionmaker, channel, clock)).delivered == 1
    async with database.sessionmaker() as db:  # the dead drainer's late acknowledgment is refused
        assert not await OutboxRepository(db).delivered(row.id, "dead", now=clock(), ref=None)
        assert not await OutboxRepository(db).failed(row.id, "dead", error="x", retry_at=None, now=clock())


async def test_health_reports_the_backlog(database: Database, migrated: Settings) -> None:
    await enqueue(database, opened(1), opened(2))
    assert await outbox_backlog(database.sessionmaker) == (2, 0)
    broken = open_database(f"sqlite+aiosqlite:///{migrated.database_path.parent}/missing/nope.db")
    try:
        assert await outbox_backlog(broken.sessionmaker) is None
    finally:
        await broken.close()


async def test_an_update_coalesced_after_listing_is_delivered_as_latest(
    database: Database, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the drainer delivered the payload it listed, not the one it claimed, so an
    update coalesced in between was lost."""
    await enqueue(database, opened(1))
    await outbox.drain(database.sessionmaker, RecordingChannel(), clock)
    await enqueue(database, updated(1, 2))
    listed = OutboxRepository.due

    async def list_then_vote(self: OutboxRepository, limit: int, now: datetime) -> list[OutboxEntry]:
        rows = await listed(self, limit, now)
        await enqueue(database, updated(1, 3))  # a vote lands after the listing, before the claim
        return rows

    monkeypatch.setattr(OutboxRepository, "due", list_then_vote)
    seen: list[n.Notification] = []

    class Capturing(RecordingChannel):
        async def update(self, ref: n.MessageRef, notification: n.Notification) -> None:
            seen.append(notification)

    await outbox.drain(database.sessionmaker, Capturing(), clock)
    assert [getattr(s, "head_count", None) for s in seen] == [3]


@dataclass
class FailingUpdates(RecordingChannel):
    """Fails every update; optionally fails one post for good."""

    permanent_for: str | None = None

    async def update(self, ref: n.MessageRef, notification: n.Notification) -> None:
        raise ConnectionError("message is locked")

    async def deliver(self, notification: n.Notification, *, thread: n.MessageRef | None = None) -> n.DeliveryResult:
        if notification.kind == self.permanent_for:
            raise n.PermanentDeliveryError("channel_not_found")
        return await super().deliver(notification, thread=thread)


async def test_a_failed_update_does_not_hold_back_its_event(database: Database, clock: FakeClock) -> None:
    """Regression: a failing message edit blocked the event's later posts while it backed off;
    the next update carries the latest state anyway."""
    await enqueue(database, opened(1), updated(1, 2), n.CallAbandoned(info(1), EndReason.TOO_FEW))
    channel = FailingUpdates()
    result = await outbox.drain(database.sessionmaker, channel, clock)
    assert (result.delivered, result.retrying) == (2, 1)
    assert [s.kind for s in channel.sent] == ["call_opened", "call_abandoned"]
    update = next(e for e in await entries(database) if e.kind == "call_updated")
    assert update.available_at == clock() + timedelta(seconds=30)  # still retried, just not blocking


async def test_a_permanent_delivery_error_gives_up_at_once(database: Database, clock: FakeClock) -> None:
    await enqueue(database, n.ProposalsReady(info(1), ()), n.CallAbandoned(info(1), EndReason.TOO_FEW))
    channel = FailingUpdates(permanent_for="proposals_ready")
    result = await outbox.drain(database.sessionmaker, channel, clock)
    assert (result.failed, result.retrying, result.delivered) == (1, 0, 1)
    first = (await entries(database))[0]
    assert first.attempts == 1
    async with database.sessionmaker() as db:
        failed = (
            await db.execute(
                text("SELECT failed_at IS NOT NULL, last_error FROM outbox WHERE id = :i"), {"i": first.id}
            )
        ).one()
    assert failed == (1, "PermanentDeliveryError: channel_not_found")


async def test_null_channel_refs_are_not_real_messages(database: Database, clock: FakeClock) -> None:
    """Regression: a call posted before Slack was configured left a made-up ref, and Slack was
    then asked to edit and thread under a message that doesn't exist."""
    from dinnerbell.channels.base import NullChannel

    await enqueue(database, opened(1))
    await outbox.drain(database.sessionmaker, NullChannel(), clock)
    await enqueue(database, updated(1, 2), n.ProposalsReady(info(1), ()))
    slack = RecordingChannel()
    assert (await outbox.drain(database.sessionmaker, slack, clock)).delivered == 2
    assert [(s.op, s.kind, s.thread) for s in slack.sent] == [("deliver", "proposals_ready", None)]
    async with database.sessionmaker() as db:
        note = await db.scalar(text("SELECT last_error FROM outbox WHERE kind = 'call_updated'"))
    assert note == outbox.NO_MESSAGE


async def test_rows_backing_off_never_starve_newer_events(database: Database, clock: FakeClock) -> None:
    """Regression: the drainer listed the oldest 100 pending rows whether due or not, so more
    than 100 rows waiting to retry hid every newer event's notifications."""
    reminders = [n.ResponseReminder(info(1), n.PersonInfo(m, "M")) for m in range(1, outbox.BATCH + 21)]
    await enqueue(database, *reminders)
    async with database.sessionmaker() as db:
        await db.execute(text("UPDATE outbox SET available_at = '2099-01-01 00:00:00', attempts = 3"))
        await db.commit()
    await enqueue(database, opened(2))
    channel = RecordingChannel()
    assert (await outbox.drain(database.sessionmaker, channel, clock)).delivered == 1
    assert [s.key for s in channel.sent] == ["event:2:run1:call_opened"]


async def test_a_failing_direct_message_does_not_hold_back_channel_posts(database: Database, clock: FakeClock) -> None:
    """Regression: a reminder DM that kept failing (say, a member who left Slack) delayed the
    event's proposals post until it gave up. Only posts that create a message hold others back."""
    await enqueue(database, opened(1))
    await outbox.drain(database.sessionmaker, RecordingChannel(), clock)
    await enqueue(database, n.ResponseReminder(info(1), n.PersonInfo(4, "Lee")), n.ProposalsReady(info(1), ()))
    channel = RecordingChannel(fail=1)  # the DM fails
    result = await outbox.drain(database.sessionmaker, channel, clock)
    assert (result.retrying, result.delivered) == (1, 1)
    assert [s.kind for s in channel.sent] == ["proposals_ready"]
    clock.advance(timedelta(seconds=30))
    await enqueue(database, n.MaybeResolved(info(1), n.PersonInfo(4, "Lee")))
    assert (await outbox.drain(database.sessionmaker, channel, clock)).delivered == 2  # the retry, then the next DM


@dataclass
class Edits(RecordingChannel):
    """Records each edit's head count; can hold an edit in flight or fail the next ones."""

    edits: list[int] = field(default_factory=list)
    hold: asyncio.Event | None = None
    holding: asyncio.Event = field(default_factory=asyncio.Event)
    fail_updates: int = 0

    async def update(self, ref: n.MessageRef, notification: n.Notification) -> None:
        if self.hold is not None:
            hold, self.hold = self.hold, None
            self.holding.set()
            await hold.wait()
        if self.fail_updates:
            self.fail_updates -= 1
            raise ConnectionError("rate limited")
        self.edits.append(getattr(notification, "head_count", -1))


async def posted_call(database: Database, clock: FakeClock) -> None:
    """Event 1's call message, delivered, so updates have a message to edit."""
    await enqueue(database, opened(1))
    await outbox.drain(database.sessionmaker, RecordingChannel(), clock)


async def test_an_update_in_flight_is_never_overtaken_by_a_newer_one(database: Database, clock: FakeClock) -> None:
    """Regression: a newer update queued while an older one was being sent could go out
    first, and the older one then landed on top, leaving the message showing stale state."""
    await posted_call(database, clock)
    await enqueue(database, updated(1, 2))
    hold = asyncio.Event()
    channel = Edits(hold=hold)
    first = asyncio.create_task(outbox.drain(database.sessionmaker, channel, clock))
    await channel.holding.wait()  # the edit showing 2 is in flight
    await enqueue(database, updated(1, 3))
    await outbox.drain(database.sessionmaker, channel, clock)  # another drainer meanwhile
    assert channel.edits == []  # it waited rather than send 3 beneath the edit in flight
    hold.set()
    await first
    await outbox.drain(database.sessionmaker, channel, clock)
    assert channel.edits == [2, 3]


async def test_a_failed_update_never_lands_after_a_newer_one(database: Database, clock: FakeClock) -> None:
    """Regression: the reviewer's case. The older edit failed after the newer one was queued,
    the newer one went out, and the older one's retry overwrote it: edits [3, 2]."""
    await posted_call(database, clock)
    await enqueue(database, updated(1, 2))
    hold = asyncio.Event()
    channel = Edits(hold=hold, fail_updates=1)
    first = asyncio.create_task(outbox.drain(database.sessionmaker, channel, clock))
    await channel.holding.wait()
    await enqueue(database, updated(1, 3))
    hold.set()
    await first  # the edit showing 2 failed and waits to retry
    await outbox.drain(database.sessionmaker, channel, clock)
    clock.advance(timedelta(minutes=5))
    await outbox.drain(database.sessionmaker, channel, clock)
    assert channel.edits == [3]
    async with database.sessionmaker() as db:
        notes = (await db.execute(text("SELECT last_error FROM outbox WHERE kind = 'call_updated' ORDER BY id"))).all()
    assert notes == [(outbox.SUPERSEDED,), (None,)]


async def test_a_tried_update_never_absorbs_newer_state(database: Database, clock: FakeClock) -> None:
    await posted_call(database, clock)
    await enqueue(database, updated(1, 2))
    await outbox.drain(database.sessionmaker, Edits(fail_updates=1), clock)
    await enqueue(database, updated(1, 3))
    keys = [e.idempotency_key for e in await entries(database) if e.kind == "call_updated"]
    assert keys == ["event:1:run1:call_updated:1", "event:1:run1:call_updated:2"]


@dataclass
class SlowEdits(Edits):
    async def update(self, ref: n.MessageRef, notification: n.Notification) -> None:
        await asyncio.sleep(0.002)
        await super().update(ref, notification)


async def test_concurrent_drains_end_on_the_latest_state(database: Database, clock: FakeClock) -> None:
    await posted_call(database, clock)
    channel = SlowEdits(fail_updates=3)
    for count in range(1, 13):
        await enqueue(database, updated(1, count))
        await asyncio.gather(*(outbox.drain(database.sessionmaker, channel, clock) for _ in range(3)))
        clock.advance(timedelta(seconds=7))
    for _ in range(6):  # let every retry come due
        clock.advance(timedelta(minutes=10))
        await outbox.drain(database.sessionmaker, channel, clock)
    assert channel.edits[-1] == 12
    assert channel.edits == sorted(channel.edits)  # never an older state after a newer one
