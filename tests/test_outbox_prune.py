# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nightly outbox prune (#340): which rows go 90 days on, which stay for good, that an old
call's messages can still be edited after it, and that it works in short batches."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text, update

from dinnerbell.config import Settings
from dinnerbell.db.models import EventRow, OutboxRow
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain.events import EventState, RoleOffer, Status
from dinnerbell.domain.notifications import NULL_CHANNEL_ID
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls, discard, outbox
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.health import outbox_backlog
from tests.support.calls import (
    DONE_AT,
    OPENS,
    RESPOND_BY,
    VOTE_BY,
    House,
    RecordingChannel,
    dinner_id,
    furnish,
    make_house,
)
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio

POSTS = {"call_opened": "call", "proposals_ready": "proposals", "decision_made": "decision"}
"""The posts that create a call's channel messages, and their message keys' suffixes."""


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


async def done_dinner(house: House, channel: RecordingChannel) -> int:
    """Today's dinner, answered, voted, decided, and done, with every message delivered."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    await house.drain(channel)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    await act(house, calls.vote, house["Sam"], event_id, ctx.proposals[0].id)
    house.clock.now = VOTE_BY
    await house.tick()
    await house.drain(channel)
    house.clock.now = DONE_AT
    await house.tick()
    await house.drain(channel)
    assert (await house.event(event_id)).state is EventState.DONE
    return event_id


async def prune(house: House, **batches: int) -> int:
    return await outbox.prune(house.sessionmaker, house.clock(), **batches)


async def add_row(house: House, kind: str, key: str, *, event_id: int | None = None, **values: Any) -> None:
    """An outbox row as the drainer would have left it, written straight to the table."""
    created = values.pop("created_at", DONE_AT)
    async with house.sessionmaker() as db:
        db.add(
            OutboxRow(
                household_id=house.household.id,
                event_id=event_id,
                kind=kind,
                payload={},
                idempotency_key=key,
                created_at=created,
                updated_at=created,
                available_at=created,
                attempts=1,
                **values,
            )
        )
        await db.commit()


async def keys(house: House) -> set[str]:
    return {e.idempotency_key for e in await house.outbox()}


async def test_a_finished_call_keeps_only_its_posts_90_days_on(house: House) -> None:
    channel = RecordingChannel()
    event_id = await done_dinner(house, channel)
    before = await house.kinds()
    assert {"call_updated", "assignment_made"} <= set(before)  # updates and direct messages to prune

    house.clock.now = DONE_AT + outbox.KEEP_FOR - timedelta(minutes=1)
    assert await prune(house) == 0
    assert await house.kinds() == before

    house.clock.now = DONE_AT + outbox.KEEP_FOR + timedelta(minutes=1)
    assert await prune(house) == len(before) - len(POSTS)
    kept = await house.outbox()
    assert sorted(e.kind for e in kept) == sorted(POSTS)
    assert {e.event_id for e in kept} == {event_id}
    assert await prune(house) == 0  # idempotent


async def test_an_old_calls_messages_can_still_be_edited_after_a_prune(house: House) -> None:
    """A discard on a test instance edits every message an old call posted, and so would any
    later update: the posts that hold each message's ref stay, and with every update gone, a new
    one's key starts again at 1 without meeting a stored key."""
    channel = RecordingChannel()
    event_id = await done_dinner(house, channel)
    refs = {POSTS[s.kind]: s.ref for s in channel.sent if s.kind in POSTS}
    house.clock.now = DONE_AT + outbox.KEEP_FOR + timedelta(days=1)
    assert await prune(house) > 0

    async with house.sessionmaker() as db:
        ctx = await house.context(event_id)
        await cc.publish(db, ctx, cc.call_updated(ctx), now=house.clock())
        await db.commit()
    assert f"event:{event_id}:run1:call_updated:1" in await keys(house)
    channel.sent.clear()
    await house.drain(channel)
    assert [(s.op, s.kind, s.ref) for s in channel.sent] == [("update", "call_updated", refs["call"])]

    await act(house, discard.discard_call, house.household.id, event_id, actor=house["Alex"], instance_label="TEST")
    channel.sent.clear()
    await house.drain(channel)
    edits = sorted((s.op, s.kind, str(s.ref)) for s in channel.sent)
    assert edits == sorted(("update", "call_discarded", str(ref)) for ref in refs.values())


async def test_a_call_still_open_or_with_a_row_waiting_keeps_every_row(house: House) -> None:
    event_id = await done_dinner(house, RecordingChannel())
    before = await keys(house)
    house.clock.now = DONE_AT + outbox.KEEP_FOR + timedelta(days=1)
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE event SET state = 'decided' WHERE id = :id"), {"id": event_id})
        await db.commit()
    assert await prune(house) == 0  # an open call, however old its last change

    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE event SET state = 'done' WHERE id = :id"), {"id": event_id})
        await db.commit()
    await add_row(house, "call_updated", "waiting", event_id=event_id)
    assert await prune(house) == 0  # a row still waiting holds back the call's prune
    assert await keys(house) == before | {"waiting"}


async def test_a_call_that_changed_since_keeps_its_rows(house: House) -> None:
    """The clock starts at the call's last change, not when it was done."""
    event_id = await done_dinner(house, RecordingChannel())
    changed = DONE_AT + timedelta(days=30)
    async with house.sessionmaker() as db:
        await db.execute(update(EventRow).where(EventRow.id == event_id).values(updated_at=changed))
        await db.commit()
    house.clock.now = DONE_AT + outbox.KEEP_FOR + timedelta(days=1)
    assert await prune(house) == 0
    house.clock.now = changed + outbox.KEEP_FOR + timedelta(days=1)
    assert await prune(house) > 0


async def test_rows_about_no_call_go_90_days_after_they_end(house: House) -> None:
    now = datetime(2027, 6, 1, tzinfo=DONE_AT.tzinfo)
    old, recent = now - outbox.KEEP_FOR - timedelta(days=1), now - outbox.KEEP_FOR + timedelta(days=1)
    landed = {"message_key": "event:99:run1:call", "ref_channel": "C1", "ref_message": "1.0"}
    await add_row(house, "schedule_changed", "old-sent", delivered_at=old)
    await add_row(house, "schedule_changed", "recent-sent", delivered_at=recent)
    await add_row(house, "monthly_recap", "old-failed", failed_at=old)
    await add_row(house, "guest_joined", "old-closed", delivered_at=old, last_error="superseded")
    await add_row(house, "badges_earned", "waiting", created_at=old)
    await add_row(house, "link_needed", "link-told", delivered_at=old)
    await add_row(house, "link_needed", "link-failed", failed_at=old)
    await add_row(house, "call_opened", "detached-post", delivered_at=old, **landed)
    await add_row(house, "call_discarded", "detached-edit", delivered_at=old, message_key=landed["message_key"])
    pretend = landed | {"ref_channel": NULL_CHANNEL_ID, "message_key": "event:98:run1:call"}
    await add_row(house, "call_opened", "pretend-post", delivered_at=old, **pretend)
    house.clock.now = now

    assert await prune(house) == 6
    # Kept: newer than 90 days, still waiting, a delivered link_needed (the admins were told
    # once; its key keeps them from being asked again), and a post with a real message.
    assert await keys(house) == {"recent-sent", "waiting", "link-told", "detached-post"}


async def test_the_prune_goes_in_batches_until_nothing_is_left(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    calls_seen: list[int] = []
    loose_seen: list[int] = []
    prune_calls, prune_loose = OutboxRepository.prune_calls, OutboxRepository.prune_loose

    async def counted_calls(self: OutboxRepository, before: datetime, limit: int) -> tuple[int, int]:
        found = await prune_calls(self, before, limit)
        calls_seen.append(found[0])
        return found

    async def counted_loose(self: OutboxRepository, before: datetime, limit: int) -> int:
        found = await prune_loose(self, before, limit)
        loose_seen.append(found)
        return found

    monkeypatch.setattr(OutboxRepository, "prune_calls", counted_calls)
    monkeypatch.setattr(OutboxRepository, "prune_loose", counted_loose)

    first = await done_dinner(house, RecordingChannel())
    async with house.sessionmaker() as db:
        for copy in (2, 3):  # two more done calls, each with an old direct message
            await db.execute(
                text(
                    "INSERT INTO event (id, household_id, kind, slot, date, state, run, version, opens_at, "
                    "respond_deadline, reminder_minutes, vote_minutes, created_at, updated_at) "
                    "SELECT :id, household_id, kind, slot, date(date, :days), state, run, version, opens_at, "
                    "respond_deadline, reminder_minutes, vote_minutes, created_at, updated_at "
                    "FROM event WHERE id = :first"
                ),
                {"id": first + copy, "days": f"+{copy} days", "first": first},
            )
        await db.commit()
    for copy in (2, 3):
        await add_row(house, "assignment_made", f"dm{copy}", event_id=first + copy, delivered_at=DONE_AT)
    for loose in range(3):
        await add_row(house, "schedule_changed", f"loose{loose}", delivered_at=DONE_AT)
    house.clock.now = DONE_AT + outbox.KEEP_FOR + timedelta(days=1)

    removed = await prune(house, calls_per_batch=1, rows_per_batch=2)
    assert calls_seen == [1, 1, 1, 0]  # one call per batch, then one that finds none
    assert loose_seen == [2, 1]
    assert sorted(e.kind for e in await house.outbox()) == sorted(POSTS)
    assert removed > 5

    calls_seen.clear()
    assert await prune(house) == 0
    assert calls_seen == []  # nothing to prune: a read, and no write transaction


async def test_healthz_failures_fall_as_old_ones_are_pruned(house: House) -> None:
    await add_row(house, "schedule_changed", "gave-up", failed_at=DONE_AT)
    assert await outbox_backlog(house.sessionmaker) == (0, 1)
    house.clock.now = DONE_AT + outbox.KEEP_FOR + timedelta(seconds=1)
    assert await prune(house) == 1
    assert await outbox_backlog(house.sessionmaker) == (0, 0)
