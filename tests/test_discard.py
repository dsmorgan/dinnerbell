# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discarding a call and starting a called-off call again at any time, on a test instance only
(#60): what a discard deletes and keeps, the Slack edits it queues, who may, races with the
tick, the drainer, and a tap, and that production refuses all of it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text

from dinnerbell.config import Settings, is_test_instance
from dinnerbell.db.events import EVENT_ROWS
from dinnerbell.db.models import Base, EventRow, OutboxRow
from dinnerbell.domain import notifications as n
from dinnerbell.domain.events import EventRuleViolation, EventState, RoleOffer, Status, Step
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.notifications import DeliveryResult, MessageRef
from dinnerbell.services import calls, discard, event_views, lifecycle
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
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

LABEL = "TEST"
LOCAL = ZoneInfo("America/Chicago")
TODAY = date(2026, 10, 22)
TOO_LATE = datetime(2026, 10, 22, 20, 31, tzinfo=LOCAL).astimezone(UTC)
"""Past the usual 18:30 dinner plus two hours."""


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


async def throw_away(house: House, event_id: int, *, by: str = "Alex", label: str = LABEL) -> discard.DiscardSummary:
    summary: discard.DiscardSummary = await act(
        house, discard.discard_call, house.household.id, event_id, actor=house[by], instance_label=label
    )
    return summary


async def done_dinner(house: House, channel: RecordingChannel) -> int:
    """Today's dinner, posted, voted, decided, and done, with every message delivered."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.save_details, house["Sam"], event_id, _details_with_suggestion())
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


def _details_with_suggestion() -> Any:
    from dinnerbell.domain.events import Details

    return Details(guests=1, suggestion_texts=("Soup",))


def event_linked_tables() -> set[str]:
    """Every table with a foreign key to ``event``, or an ``event_id`` column without one."""
    return {
        table.name
        for table in Base.metadata.sorted_tables
        if any(fk.column.table.name == "event" for fk in table.foreign_keys) or "event_id" in table.columns
    } - {"event"}


async def leftovers(house: House, event_id: int) -> dict[str, int]:
    """Rows in every event-linked table that still name ``event_id``."""
    found: dict[str, int] = {}
    async with house.sessionmaker() as db:
        for table in sorted(event_linked_tables()):
            count = await db.scalar(text(f"SELECT count(*) FROM {table} WHERE event_id = :e"), {"e": event_id})  # noqa: S608
            found[table] = int(count or 0)
    return found


# What a discard handles -----------------------------------------------------------------------


def test_discard_handles_every_table_that_refers_to_an_event() -> None:
    """A new table that refers to events must be added to ``EVENT_ROWS`` (or be the outbox), so
    discarding a call can't leave its rows behind. TODO(m3a-feedback): the feedback tables."""
    handled = {model.__tablename__ for model in EVENT_ROWS} | {"outbox"}
    assert event_linked_tables() == handled


def test_the_audit_outlives_the_event() -> None:
    """The audit row of a discard names the call by ID; nothing cascades it away."""
    audit = Base.metadata.tables["audit"]
    assert all(fk.column.table.name != "event" for fk in audit.foreign_keys)
    assert "event_id" not in audit.columns


def test_the_gate_is_the_instance_label() -> None:
    assert is_test_instance("TEST") and not is_test_instance("")


# Discarding -------------------------------------------------------------------------------------


async def test_discarding_a_done_call_deletes_everything_and_a_fresh_call_can_start(house: House) -> None:
    channel = RecordingChannel()
    event_id = await done_dinner(house, channel)
    assert len(await house.meals()) == 1
    before = await leftovers(house, event_id)
    assert all(before[t] for t in ("response", "suggestion", "proposal", "vote", "assignment", "meal_log", "outbox"))
    message = {"call_opened": "call", "proposals_ready": "proposals", "decision_made": "decision"}
    posts = {message[s.kind]: s.ref for s in channel.sent if s.kind in message}
    assert len(posts) == 3

    summary = await throw_away(house, event_id)

    assert await leftovers(house, event_id) == dict.fromkeys(before, 0)
    async with house.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(EventRow)) == 0
    assert summary.rows["meal_log"] == 1 and summary.rows["vote"] == 1 and summary.outbox_pending_deleted == 0
    assert await house.meals() == []
    # The posts that made the three channel messages stay, detached, so the edits can find them.
    rows = await house.outbox()
    kept = [r for r in rows if r.kind in {"call_opened", "proposals_ready", "decision_made"}]
    assert {r.event_id for r in kept} == {None} and len(kept) == 3
    queued = [n.from_payload(r.kind, r.payload) for r in rows if r.kind == "call_discarded"]
    assert {r.event_id for r in rows if r.kind == "call_discarded"} == {None}
    assert sorted(q.message_key or "" for q in queued) == sorted(f"event:{event_id}:run1:{m}" for m in posts)
    assert summary.messages_marked == 3
    audit = next(a for a in await house.audits() if a.action == "event.discarded")
    assert (audit.actor_member_id, audit.target) == (house["Alex"].id, f"event:{event_id}")
    assert audit.payload["state"] == "done" and audit.payload["instance_label"] == LABEL

    # Only after the delete committed can the drainer send the edits, to each message.
    channel.sent.clear()
    await house.drain(channel)
    edits = [(s.op, s.kind, s.ref) for s in channel.sent]
    assert sorted(edits, key=str) == sorted((("update", "call_discarded", ref) for ref in posts.values()), key=str)

    # A fresh dinner for today: a new ID (never the discarded one), posted as a new message.
    fresh = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    assert fresh.id > event_id and fresh.state is EventState.COLLECTING and fresh.run == 1
    channel.sent.clear()
    await house.drain(channel)
    assert [(s.op, s.kind) for s in channel.sent] == [("deliver", "call_opened")]


async def test_discarding_an_open_call_deletes_its_pending_and_held_notifications(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    async with house.sessionmaker() as db:  # one held row, as a channel's HeldDelivery leaves it
        await db.execute(
            OutboxRow.__table__.update()
            .where(OutboxRow.event_id == event_id, OutboxRow.kind == "call_updated")
            .values(available_at=DONE_AT, last_error="HeldDelivery: setup problem")
        )
        await db.commit()
    summary = await throw_away(house, event_id)
    assert summary.outbox_pending_deleted == 2 and summary.outbox_rows_deleted == 2
    assert summary.messages_marked == 0  # nothing was ever posted, so there's nothing to edit
    assert await house.outbox() == []
    channel = RecordingChannel()
    await house.drain(channel)
    assert channel.sent == []


async def test_a_message_null_channel_only_pretended_to_post_is_not_kept(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    async with house.sessionmaker() as db:
        await db.execute(
            OutboxRow.__table__.update()
            .where(OutboxRow.event_id == event_id)
            .values(delivered_at=OPENS, ref_channel=n.NULL_CHANNEL_ID, ref_message="1")
        )
        await db.commit()
    assert (await throw_away(house, event_id)).messages_marked == 0
    assert await house.outbox() == []


async def test_discarding_a_called_off_call_edits_every_runs_messages(house: House) -> None:
    """A restarted call posted a new call message for each run; each says it was discarded."""
    channel = RecordingChannel()
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await house.drain(channel)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    await house.drain(channel)
    await throw_away(house, event_id)
    queued = [r.message_key for r in await house.outbox() if r.kind == "call_discarded"]
    assert queued == [f"event:{event_id}:run1:call", f"event:{event_id}:run2:call"]


async def test_preview_counts_what_a_discard_would_delete(house: House) -> None:
    channel = RecordingChannel()
    event_id = await done_dinner(house, channel)
    async with house.sessionmaker() as db:
        found = await discard.preview(db, house.household.id, event_id)
    assert (found.event_id, found.slot, found.day, found.state) == (event_id, Slot.DINNER, TODAY, EventState.DONE)
    assert found.rows["meal_log"] == 1 and found.pending_outbox == 0
    assert found.lines()[0] == f"Call: dinner on 2026-10-22 (ID {event_id}, run 1)"
    summary = await throw_away(house, event_id)
    assert summary.rows == found.rows
    assert summary.lines()[0] == f"Discarded the dinner call for 2026-10-22 (ID {event_id})."
    async with house.sessionmaker() as db:
        with pytest.raises(NotFound):
            await discard.preview(db, house.household.id, event_id)


# Who may -----------------------------------------------------------------------------------------


async def test_production_never_discards(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    with pytest.raises(discard.NotTestInstance, match="only on a labeled instance"):
        await throw_away(house, event_id, label="")
    assert (await house.event(event_id)).state is EventState.COLLECTING
    assert [a for a in await house.audits() if a.action == "event.discarded"] == []


async def test_only_an_active_admin_discards(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    with pytest.raises(Forbidden, match="Only an admin"):
        await throw_away(house, event_id, by="Sam")
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_a_call_that_is_gone_or_another_households_is_not_found(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await throw_away(house, event_id)
    with pytest.raises(NotFound):
        await throw_away(house, event_id)  # a second discard, as from a double tap
    with pytest.raises(NotFound):
        await act(house, discard.discard_call, house.household.id + 1, event_id + 1, actor=None, instance_label=LABEL)
    assert len([a for a in await house.audits() if a.action == "event.discarded"]) == 1


async def test_the_operator_discards_without_a_member(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await act(house, discard.discard_call, house.household.id, event_id, actor=None, instance_label=LABEL)
    audit = next(a for a in await house.audits() if a.action == "event.discarded")
    assert audit.actor_member_id is None


# Races ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("when", [VOTE_BY, DONE_AT])
async def test_a_discard_racing_the_tick_leaves_nothing_behind(house: House, when: datetime) -> None:
    """Both take the event's lock, so one runs after the other, in either order, and the tick
    never fails on a call that's gone."""
    channel = RecordingChannel()
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    if when == DONE_AT:
        house.clock.now = VOTE_BY
        await house.tick()
    await house.drain(channel)
    house.clock.now = when
    async with house.sessionmaker() as db:
        tick, thrown = await asyncio.gather(
            house.tick(),
            discard.discard_call(db, house.household.id, event_id, actor=house["Alex"], instance_label=LABEL, now=when),
            return_exceptions=True,
        )
    assert not isinstance(tick, BaseException) and not isinstance(thrown, BaseException)
    assert set((await leftovers(house, event_id)).values()) == {0}
    assert await house.meals() == []


async def test_the_tick_skips_a_call_discarded_between_its_read_and_its_lock(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    real_due = lifecycle.cc.due
    calls_made: list[int] = []

    async def due_then_discard(db: Any, event: Any, now: datetime) -> Step | None:
        step = await real_due(db, event, now)
        if not calls_made:  # the cheap read, outside the lock: the admin discards right after it
            calls_made.append(event.id)
            await throw_away(house, event.id)
        return step

    monkeypatch.setattr(lifecycle.cc, "due", due_then_discard)
    house.clock.now = RESPOND_BY
    with caplog.at_level(logging.ERROR, logger="dinnerbell"):
        assert await house.tick() == []
    assert calls_made == [event_id]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_tap_on_a_discarded_call_is_not_found(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await throw_away(house, event_id)
    with pytest.raises(NotFound):
        await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    with pytest.raises(NotFound):
        await act(house, calls.toggle_role, house["Sam"], event_id, RoleOffer.COOK)
    with pytest.raises(NotFound):
        await act(house, event_views.event_view, house["Sam"], event_id)


class DiscardMidDelivery(RecordingChannel):
    """While a drainer holds the call message's post, the admin discards the call."""

    def __init__(self, house: House, event_id: int) -> None:
        super().__init__()
        self.house, self.event_id = house, event_id

    async def deliver(self, notification: n.Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
        if notification.kind == "call_opened":
            await throw_away(self.house, self.event_id)
        return await super().deliver(notification, thread=thread)


async def test_a_discard_while_the_drainer_holds_a_post_is_clean(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    """The row the drainer holds is gone when it comes to record the delivery: it logs a
    warning, delivers nothing else of the call's, and the next drain has nothing to send."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    channel = DiscardMidDelivery(house, event_id)
    with caplog.at_level(logging.WARNING, logger="dinnerbell"):
        result = await house.drain(channel)
    assert (result.delivered, result.failed, result.retrying) == (1, 0, 0)
    assert any(r.getMessage() == "outbox claim expired during delivery" for r in caplog.records)
    assert await house.outbox() == []
    channel.sent.clear()
    await house.drain(channel)
    assert channel.sent == []


# Starting a called-off call again at any time ------------------------------------------------------


async def test_a_called_off_call_starts_again_at_any_time_on_test_only(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    house.clock.now = TOO_LATE
    view = await act(house, event_views.event_view, house["Kim"], event_id)
    assert not view.can.restart  # production keeps the too-late rule
    with pytest.raises(EventRuleViolation, match="too late"):
        await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    with pytest.raises(EventRuleViolation, match="too late"):
        await act(house, calls.restart_call, house["Kim"], event_id)
    view = await act(house, event_views.event_view, house["Kim"], event_id, any_time=True)
    today = await act(house, event_views.today_view, house["Kim"], any_time=True)
    assert view.can.restart and [v.can.restart for v in today.events] == [True]
    again = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY, restart_any_time=True)
    assert (again.id, again.run, again.state) == (event_id, 2, EventState.COLLECTING)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    third = await act(house, calls.restart_call, house["Kim"], event_id, any_time=True)
    assert (third.run, third.state) == (3, EventState.COLLECTING)
