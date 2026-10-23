# SPDX-License-Identifier: AGPL-3.0-or-later
"""End to end (spec 13.10): the scheduler's tick drives a dinner call from open through the
decision to done with a frozen clock, and every notification is checked in order. Then the same
call survives a restart and downtime past its deadlines."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from dinnerbell.channels.base import NullChannel
from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import AssignmentRole, Details, EventState, RoleOffer, Status, Step
from dinnerbell.domain.meals import Slot
from dinnerbell.services import calls, outbox
from dinnerbell.services.database import Database, open_database
from tests.support.calls import (
    DONE_AT,
    MEAL,
    OPENS,
    REMIND,
    RESPOND_BY,
    VOTE_BY,
    ZONE,
    House,
    RecordingChannel,
    dinner_id,
    furnish,
    make_house,
)
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


async def answer(house: House, event_id: int) -> None:
    """Alex is in and can pick up; Sam is in, cooks, brings a guest, and suggests tacos; Kim
    says maybe; Lee never answers."""
    now = house.clock()
    async with house.sessionmaker() as db:
        await calls.respond(db, house["Alex"], event_id, Status.IN, now=now)
        await calls.offer_role(db, house["Alex"], event_id, RoleOffer.PICKUP, True, now=now)
        await calls.offer_role(db, house["Sam"], event_id, RoleOffer.COOK, True, now=now)
        details = Details(guests=1, eat_from=time(18, 40), suggestion_item_ids=(house.items["Sam's Tacos"].id,))
        await calls.save_details(db, house["Sam"], event_id, details, now=now)
        await calls.respond(db, house["Kim"], event_id, Status.MAYBE, now=now)


async def test_a_full_call_from_open_to_done(house: House, clock: FakeClock) -> None:
    channel = RecordingChannel()
    clock.now = OPENS - timedelta(seconds=1)
    assert await house.tick() == []
    assert await house.outbox() == []

    clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    assert (await house.event(event_id)).state is EventState.COLLECTING
    await house.drain(channel)

    clock.now = OPENS + timedelta(minutes=10)
    await answer(house, event_id)
    clock.now = REMIND - timedelta(seconds=1)
    assert await house.tick() == []
    clock.now = REMIND
    assert await house.tick() == [Step.REMIND]
    await house.drain(channel)

    clock.now = RESPOND_BY
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.VOTING and ctx.event.vote_deadline == VOTE_BY
    assert [(ctx.items[p.item_id or 0].name, p.mode) for p in ctx.proposals] == [
        ("Sam's Tacos", Mode.COOK),
        ("Siam Garden", Mode.PICKUP),
        ("Taqueria", Mode.PICKUP),
    ]
    assert ctx.proposals[0].reasons == ("Suggested by Sam.", "New to the household.")
    assert ctx.response_of(house["Kim"].id).status is Status.OUT  # type: ignore[union-attr]
    await house.drain(channel)

    clock.now = RESPOND_BY + timedelta(minutes=5)
    tacos = ctx.proposals[0].id
    async with house.sessionmaker() as db:
        await calls.vote(db, house["Alex"], event_id, tacos, now=clock())
        await calls.vote(db, house["Sam"], event_id, tacos, now=clock())
    await house.drain(channel)

    clock.now = VOTE_BY
    assert await house.tick() == [Step.CLOSE_VOTING]
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED
    assert (ctx.event.decided_item_id, ctx.event.decided_mode) == (house.items["Sam's Tacos"].id, Mode.COOK)
    # FR-19: Sam's earliest time, 18:40, rounded up to 18:45 in Chicago.
    assert ctx.event.meal_time == MEAL + timedelta(minutes=15)
    assert [(a.role, a.member_id) for a in ctx.assignments] == [(AssignmentRole.COOK, house["Sam"].id)]
    await house.drain(channel)

    clock.now = MEAL + timedelta(minutes=15) + timedelta(hours=2) - timedelta(seconds=1)
    assert await house.tick() == []
    clock.now = DONE_AT + timedelta(minutes=15)
    assert await house.tick() == [Step.MARK_DONE]
    assert await house.tick() == []
    await house.drain(channel)

    (meal,) = await house.meals()
    assert (meal.event_id, meal.item_id, meal.slot, meal.date) == (
        event_id,
        house.items["Sam's Tacos"].id,
        "dinner",
        date(2026, 10, 22),
    )
    assert [p["name"] for p in meal.participants] == ["Alex", "Sam"]
    assert [c["name"] for c in meal.cuisine_tags] == ["Mexican"]
    assert meal.logged_by is None

    entries = await house.outbox()
    key = f"event:{event_id}:run1"
    assert [(e.kind, e.idempotency_key) for e in entries] == [
        ("call_opened", f"{key}:call_opened"),
        ("call_updated", f"{key}:call_updated:1"),  # five roster changes, coalesced
        ("response_reminder", f"{key}:response_reminder:member{house['Lee'].id}"),  # not Kim: a maybe
        ("maybe_resolved", f"{key}:maybe_resolved:member{house['Kim'].id}"),
        ("proposals_ready", f"{key}:proposals_ready"),
        ("call_updated", f"{key}:call_updated:2"),
        ("proposals_updated", f"{key}:proposals_updated:1"),  # two votes, coalesced
        ("decision_made", f"{key}:decision_made"),
        ("assignment_made", f"{key}:assignment_made:{ctx.assignments[0].id}"),
        ("proposals_updated", f"{key}:proposals_updated:2"),
        ("call_updated", f"{key}:call_updated:3"),
        ("decision_updated", f"{key}:decision_updated:1"),
        ("call_updated", f"{key}:call_updated:4"),
    ]
    assert all(e.attempts == 1 for e in entries)

    # The channel saw posts, then updates to them, and threads under the call.
    call_ref = channel.sent[0].ref
    assert [(s.op, s.kind) for s in channel.sent] == [
        ("deliver", "call_opened"),
        ("update", "call_updated"),
        ("deliver", "response_reminder"),
        ("deliver", "maybe_resolved"),
        ("deliver", "proposals_ready"),
        ("update", "call_updated"),
        ("update", "proposals_updated"),
        ("deliver", "decision_made"),
        ("deliver", "assignment_made"),
        ("update", "proposals_updated"),
        ("update", "call_updated"),
        ("update", "decision_updated"),
        ("update", "call_updated"),
    ]
    by_kind = {s.kind: s for s in channel.sent if s.op == "deliver"}
    assert by_kind["proposals_ready"].thread == call_ref
    assert by_kind["decision_made"].thread == call_ref
    assert by_kind["response_reminder"].recipient == house["Lee"].id
    assert by_kind["assignment_made"].recipient == house["Sam"].id
    assert {s.ref for s in channel.sent if s.kind == "call_updated"} == {call_ref}
    assert {s.ref for s in channel.sent if s.kind == "proposals_updated"} == {by_kind["proposals_ready"].ref}
    assert {s.ref for s in channel.sent if s.kind == "decision_updated"} == {by_kind["decision_made"].ref}

    actions = [a.action for a in await house.audits()]
    assert actions == ["event.opened", "event.voting", "event.decided", "event.done"]


async def test_restart_mid_call_and_downtime_past_every_deadline(
    migrated: Settings, clock: FakeClock, database: Database
) -> None:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await answer(house, event_id)
    await database.close()  # the server stops before the reminder

    clock.now = VOTE_BY + timedelta(minutes=15)  # and comes back after the vote deadline
    restarted = open_database(migrated.async_database_url)
    try:
        house.sessionmaker = restarted.sessionmaker
        assert await house.tick() == [Step.CLOSE_RESPONSES, Step.CLOSE_VOTING]
        assert await house.tick() == []  # caught up: nothing more is owed
        ctx = await house.context(event_id)
        assert ctx.event.state is EventState.DECIDED
        assert ctx.event.vote_deadline == VOTE_BY  # from the deadline, not from the late tick
        assert ctx.event.meal_time == MEAL + timedelta(minutes=15)
        kinds = await house.kinds()
        # No late reminder; one call message update carries the latest state.
        assert kinds == [
            "call_opened",
            "call_updated",
            "maybe_resolved",
            "proposals_ready",
            "decision_made",
            "assignment_made",
            "proposals_updated",
        ]
        channel = RecordingChannel()
        assert (await house.drain(channel)).delivered == len(kinds)
        assert [s.kind for s in channel.sent] == kinds
    finally:
        await restarted.close()

    clock.now = DONE_AT + timedelta(hours=10)  # down again, past meal time + 2 hours
    again = open_database(migrated.async_database_url)
    try:
        house.sessionmaker = again.sessionmaker
        assert await house.tick() == [Step.MARK_DONE]
        assert await house.tick() == []
        assert len(await house.meals()) == 1
        assert (await house.event(event_id)).state is EventState.DONE
    finally:
        await again.close()


async def test_a_call_opens_late_after_a_restart_but_not_after_its_window(house: House, clock: FakeClock) -> None:
    clock.now = RESPOND_BY - timedelta(minutes=1)  # down from before 15:00 until 16:29
    await house.tick()
    event = await house.event(await dinner_id(house))
    assert event.state is EventState.COLLECTING
    assert event.respond_deadline == RESPOND_BY  # the window keeps its scheduled end
    clock.now = RESPOND_BY + timedelta(days=1)  # the next day, down through the whole window
    await house.tick()
    async with house.sessionmaker() as db:
        missed = await EventRepository(db).call_for(house.household.id, Slot.DINNER, date(2026, 10, 23))
    assert missed is None


async def test_concurrent_ticks_open_one_call(house: House, clock: FakeClock) -> None:
    clock.now = OPENS
    await asyncio.gather(house.tick(), house.tick(), house.tick())
    assert await house.kinds() == ["call_opened"]
    assert [a.action for a in await house.audits()] == ["event.opened"]


async def test_a_disabled_slot_never_opens_and_lunch_is_off_by_default(house: House, clock: FakeClock) -> None:
    clock.now = datetime_at(10, 31)
    await house.tick()
    assert await house.outbox() == []


def datetime_at(hour: int, minute: int) -> datetime:
    """A wall-clock time on the test day in the household's time zone, as UTC."""
    return datetime(2026, 10, 22, hour, minute, tzinfo=ZoneInfo(ZONE)).astimezone(UTC)


async def test_the_null_channel_drains_everything(house: House, clock: FakeClock) -> None:
    clock.now = OPENS
    await house.tick()
    result = await outbox.drain(house.sessionmaker, NullChannel(), clock)
    assert result.delivered == 1
    assert all(e.attempts == 1 for e in await house.outbox())
