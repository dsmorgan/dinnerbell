# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calling a call off, closing a window now, and closing responses early (FR-56), against a
migrated database: who may, what each sends and audits, the never-posted call, starting a
called-off call again, skipping a day, and races with the scheduler's tick."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.db.models import OutboxRow
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain import notifications as n
from dinnerbell.domain.events import (
    AssignmentRole,
    Details,
    EndReason,
    EventRuleViolation,
    EventState,
    RoleOffer,
    Status,
    Step,
    Window,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.notifications import DeliveryResult, HeldDelivery, MessageRef, PermanentDeliveryError
from dinnerbell.services import call_admin, calls, event_views, lifecycle
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden
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

LOCAL = ZoneInfo("America/Chicago")
TODAY, TOMORROW = date(2026, 10, 22), date(2026, 10, 23)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=LOCAL).astimezone(UTC)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def open_dinner(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def to_voting(house: House) -> int:
    """Alex (pickup) and Sam are in; Sam coordinates."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.respond, house["Kim"], event_id, Status.MAYBE)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def decided(house: House) -> int:
    """Alex picks up; Sam coordinates; Kim's maybe turned out at the deadline."""
    event_id = await to_voting(house)
    house.clock.now = VOTE_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.DECIDED
    return event_id


async def rows_for(house: House, event_id: int) -> list[n.Notification]:
    return [n.from_payload(e.kind, e.payload) for e in await house.outbox() if e.event_id == event_id]


async def cancel_audits(house: House) -> list[dict[str, Any]]:
    return [dict(a.payload) for a in await house.audits() if a.action == "event.cancelled"]


# Who may call it off ------------------------------------------------------------------------------


async def test_the_starter_calls_off_their_ad_hoc_call(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=2)  # before the standing call
    event = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    await act(house, calls.respond, house["Sam"], event.id, Status.IN)
    await act(house, calls.respond, house["Lee"], event.id, Status.MAYBE)
    await act(house, calls.respond, house["Alex"], event.id, Status.OUT)
    await house.drain(RecordingChannel())  # so the call-off's update gets its own row
    before = len(await house.outbox())
    await act(house, calls.cancel_call, house["Kim"], event.id, note="  Pizza at\nGrandma's ")
    stored = await house.event(event.id)
    assert (stored.state, stored.ended_by, stored.end_note) == (
        EventState.CANCELLED,
        house["Kim"].id,
        "Pizza at Grandma's",
    )
    sent = (await rows_for(house, event.id))[before:]
    assert [s.kind for s in sent] == ["event_cancelled", "call_updated"]
    cancelled = sent[0]
    assert isinstance(cancelled, n.EventCancelled)
    assert (cancelled.note, cancelled.cancelled_by) == ("Pizza at Grandma's", n.PersonInfo(house["Kim"].id, "Kim"))
    assert [p.name for p in cancelled.audience] == ["Lee", "Sam"]  # in or maybe; Alex is out
    assert [p.name for p in cancelled.participants] == ["Sam"]
    assert cancelled.event.ended_by == n.PersonInfo(house["Kim"].id, "Kim")
    assert await cancel_audits(house) == [{"from": "collecting", "as": "starter", "note": "Pizza at Grandma's"}]
    assert await house.meals() == []
    house.clock.now = DONE_AT
    assert await house.tick() == []  # nothing left for the scheduler


async def test_the_coordinator_calls_off_a_decided_call(house: House) -> None:
    event_id = await decided(house)
    channel = RecordingChannel()
    await house.drain(channel)
    await act(house, calls.cancel_call, house["Sam"], event_id, note="Snow day")
    sent = [x for x in await rows_for(house, event_id) if x.kind in {"event_cancelled", "assignment_cancelled"}]
    cancelled, dm = sent
    assert isinstance(cancelled, n.EventCancelled) and isinstance(dm, n.AssignmentCancelled)
    # Kim's maybe turned out at the deadline, so Kim, told then, isn't mentioned again.
    assert [p.name for p in cancelled.audience] == ["Alex", "Sam"]
    assert (dm.member.name, dm.roles, dm.note) == ("Alex", (AssignmentRole.PICKUP,), "Snow day")
    assert dm.decision.proposal.item.name == "Siam Garden"
    assert dm.recipient_id == house["Alex"].id  # Sam coordinates and called it off: no DM to Sam
    assert (await cancel_audits(house))[-1] == {"from": "decided", "as": "coordinator", "note": "Snow day"}
    await house.drain(channel)
    by_kind = {s.kind: s for s in channel.sent[-5:]}
    call_ref = next(s.ref for s in channel.sent if s.kind == "call_opened")
    assert by_kind["event_cancelled"].thread == call_ref
    assert by_kind["assignment_cancelled"].recipient == house["Alex"].id
    decision = next(x for x in reversed(await rows_for(house, event_id)) if isinstance(x, n.DecisionUpdated))
    assert decision.event.state is EventState.CANCELLED  # the decision message is struck through
    assert {"decision_updated", "call_updated", "proposals_updated"} <= {s.kind for s in channel.sent[-5:]}
    assert await house.meals() == []


async def test_an_assignee_who_calls_it_off_is_not_messaged_about_it(house: House) -> None:
    event_id = await decided(house)
    await act(house, calls.cancel_call, house["Alex"], event_id)  # an admin, and the pickup
    assert "assignment_cancelled" not in await house.kinds()
    assert (await cancel_audits(house))[-1] == {"from": "decided", "as": "admin", "note": None}


async def test_a_deactivated_assignee_is_not_messaged(house: House) -> None:
    event_id = await decided(house)
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE member SET active = 0 WHERE id = :id"), {"id": house["Alex"].id})
        await db.commit()
    await act(house, calls.cancel_call, house["Sam"], event_id)
    assert "assignment_cancelled" not in await house.kinds()


async def test_a_call_off_closes_assignment_and_reminder_messages_still_waiting(house: House) -> None:
    """A slow "You're picking up" must never arrive after "You're no longer picking up"."""
    event_id = await decided(house)
    channel = RecordingChannel()
    async with house.sessionmaker() as db:
        # A decided call has no reminder left waiting, so stand one in: closed rows are never decoded.
        await db.execute(text("UPDATE outbox SET kind = 'response_reminder' WHERE kind = 'maybe_resolved'"))
        await db.commit()
        repo = OutboxRepository(db)
        waiting = [e for e in await repo.for_household(house.household.id) if e.kind == "assignment_made"]
        live = waiting[0]  # a drainer holds this one: it's already in Slack's hands
        assert await repo.claim(live.id, "t", now=house.clock(), until=house.clock() + timedelta(minutes=5))
        await db.commit()
    await act(house, calls.cancel_call, house["Sam"], event_id)
    async with house.sessionmaker() as db:
        rows = list(
            await db.scalars(select(OutboxRow).where(OutboxRow.kind.in_(["assignment_made", "response_reminder"])))
        )
    closed = {(r.kind, r.id == live.id, r.last_error) for r in rows if r.delivered_at is not None}
    assert ("response_reminder", False, "superseded by a call-off") in closed
    assert all(r.delivered_at is None for r in rows if r.id == live.id)  # a drainer holds it
    assert {r.last_error for r in rows if r.id != live.id} == {"superseded by a call-off"}
    house.clock.now += timedelta(minutes=10)  # the held claim lapses, and the drainer checks the call
    await house.drain(channel)
    assert "assignment_made" not in channel.kinds and "response_reminder" not in channel.kinds
    async with house.sessionmaker() as db:
        held = await db.scalar(select(OutboxRow).where(OutboxRow.id == live.id))
    assert held is not None and held.last_error == "superseded by a call-off"


async def test_who_is_refused(house: House) -> None:
    event_id = await to_voting(house)
    with pytest.raises(EventRuleViolation, match="whoever started this call, or an admin"):
        await act(house, calls.cancel_call, house["Kim"], event_id)
    house.clock.now = OPENS - timedelta(hours=1)
    later = await act(house, calls.start_call, house["Kim"], Slot.LUNCH, TOMORROW)
    with pytest.raises(EventRuleViolation, match="whoever started"):
        await act(house, calls.cancel_call, house["Lee"], later.id)
    assert await cancel_audits(house) == []


async def test_once_decided_only_the_coordinator_or_an_admin_can(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=2)
    event = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    for name in ("Kim", "Alex"):
        await act(house, calls.respond, house[name], event.id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event.id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Alex"], event.id)
    house.clock.now = event.respond_deadline
    await house.tick()
    house.clock.now = event.respond_deadline + timedelta(minutes=45)
    await house.tick()
    assert (await house.event(event.id)).state is EventState.DECIDED
    with pytest.raises(EventRuleViolation, match="It's decided now"):
        await act(house, calls.cancel_call, house["Kim"], event.id)


async def test_a_member_who_claims_coordinating_can_then_call_it_off(house: House) -> None:
    """Members aren't stuck: anyone who's in can say "I'll coordinate", which the channel sees."""
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    with pytest.raises(EventRuleViolation):
        await act(house, calls.cancel_call, house["Kim"], event_id)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, calls.cancel_call, house["Kim"], event_id)
    assert (await cancel_audits(house))[-1]["as"] == "coordinator"


async def test_permissions_match_the_rules(house: House) -> None:
    event_id = await to_voting(house)
    views = {name: await act(house, event_views.event_view, house[name], event_id) for name in ("Alex", "Sam", "Kim")}
    assert {name: (v.can.cancel, v.can.close_window) for name, v in views.items()} == {
        "Alex": (True, Window.VOTE),
        "Sam": (True, Window.VOTE),
        "Kim": (False, None),
    }
    await act(house, calls.cancel_call, house["Sam"], event_id)
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    assert (view.can.cancel, view.can.close_window) == (False, None)


# A call that was never posted ----------------------------------------------------------------------


async def test_a_call_that_was_never_posted_posts_nothing_and_tells_its_starter(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=3)
    tomorrow = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TOMORROW)
    assert tomorrow.state is EventState.SCHEDULED
    await act(house, calls.cancel_call, house["Alex"], tomorrow.id, note="Away all weekend")
    sent = await rows_for(house, tomorrow.id)
    assert [s.kind for s in sent] == ["started_call_cancelled"]
    dm = sent[0]
    assert isinstance(dm, n.StartedCallCancelled)
    assert (dm.member.name, dm.cancelled_by.name, dm.note) == ("Kim", "Alex", "Away all weekend")
    assert (await cancel_audits(house))[-1] == {"from": "scheduled", "as": "admin", "note": "Away all weekend"}
    # The row stays, so the scheduler doesn't open tomorrow's standing call over it.
    house.clock.now = local(23, 15)
    assert await house.tick() == []
    assert (await house.event(tomorrow.id)).state is EventState.CANCELLED
    assert [e.kind for e in await house.outbox() if e.event_id == tomorrow.id] == ["started_call_cancelled"]


async def test_a_starter_calling_off_their_own_unposted_call_sends_nothing(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=3)
    tomorrow = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TOMORROW)
    await act(house, calls.cancel_call, house["Kim"], tomorrow.id)
    assert await rows_for(house, tomorrow.id) == []


class LostCallChannel(RecordingChannel):
    """A channel where the call post fails for good, so nothing can thread under it."""

    async def deliver(self, notification: n.Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
        if notification.kind == "call_opened":
            raise PermanentDeliveryError("channel_not_found")
        return await super().deliver(notification, thread=thread)


async def test_the_outbox_drops_a_call_off_whose_call_message_never_landed(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    channel = LostCallChannel()
    await house.drain(channel)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    await house.drain(channel)
    assert "event_cancelled" not in channel.kinds  # never posted at the top level
    async with house.sessionmaker() as db:
        row = await db.scalar(select(OutboxRow).where(OutboxRow.kind == "event_cancelled"))
    assert row is not None and row.delivered_at is not None and row.last_error == "no message to thread under"


# Starting it again ----------------------------------------------------------------------------------


async def test_starting_a_called_off_call_again_reuses_its_row(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.MAYBE)
    await act(house, calls.respond, house["Lee"], event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Lee"], event_id)
    house.clock.now = OPENS + timedelta(minutes=10)
    await act(house, calls.cancel_call, house["Alex"], event_id, note="Oops")
    house.clock.now = OPENS + timedelta(minutes=20)
    restarted = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    assert restarted.id == event_id
    ctx = await house.context(event_id)
    event = ctx.event
    assert (event.state, event.run, event.created_by) == (EventState.COLLECTING, 2, house["Kim"].id)
    assert (event.ended_by, event.end_note, event.opens_at) == (None, None, house.clock())
    assert event.coordinator_id is None  # whoever coordinated the called-off call may not be around
    assert ctx.response_of(house["Sam"].id).status is Status.MAYBE  # type: ignore[union-attr]
    entries = await house.outbox()
    assert (entries[-1].kind, entries[-1].idempotency_key) == ("call_opened", f"event:{event_id}:run2:call_opened")
    restart = next(a for a in await house.audits() if a.action == "event.restarted")
    assert (restart.actor_member_id, restart.payload["from"], restart.payload["run"]) == (
        house["Kim"].id,
        "cancelled",
        2,
    )
    # Kim started it this time, so Kim may call it off as its starter.
    await act(house, calls.cancel_call, house["Kim"], event_id)
    assert (await cancel_audits(house))[-1]["as"] == "starter"


async def test_starting_again_keeps_the_too_late_rule_and_a_later_days_timing(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    house.clock.now = local(22, 20, 31)  # past the usual 18:30 meal time plus two hours
    with pytest.raises(EventRuleViolation, match="too late to start today's dinner call again"):
        await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    assert (await house.event(event_id)).state is EventState.CANCELLED
    tomorrow = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TOMORROW)
    await act(house, calls.cancel_call, house["Kim"], tomorrow.id)
    again = await act(house, calls.start_call, house["Lee"], Slot.DINNER, TOMORROW)
    assert (again.id, again.state, again.run, again.opens_at) == (tomorrow.id, EventState.SCHEDULED, 2, local(23, 15))
    assert [e.kind for e in await house.outbox() if e.event_id == tomorrow.id] == []  # posted when it opens
    house.clock.now = local(23, 15)
    assert await house.tick() == [Step.OPEN]


async def test_only_a_called_off_call_starts_again(house: House) -> None:
    event_id = await open_dinner(house)
    with pytest.raises(lifecycle.CallExists):
        await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).end_reason is EndReason.TOO_FEW
    with pytest.raises(lifecycle.CallExists):  # re-running an abandoned call is still the admin's
        await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)


async def test_a_restart_that_finds_the_call_reopened_under_the_lock_is_refused(house: House) -> None:
    """Two members start the same called-off call at once: the second finds it open."""
    event_id = await open_dinner(house)
    async with house.sessionmaker() as db:
        with pytest.raises(lifecycle.CallExists):
            await lifecycle.restart(db, await house.event(event_id), actor=house["Kim"], now=house.clock())
        await db.rollback()
    assert (await house.event(event_id)).run == 1


async def test_an_admin_rerun_clears_who_called_it_off_and_keeps_the_coordinator(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    await act(house, calls.cancel_call, house["Alex"], event_id, note="Oops")
    await act(house, call_admin.rerun, house["Alex"], event_id)
    event = await house.event(event_id)
    assert (event.state, event.ended_by, event.end_note, event.created_by) == (EventState.COLLECTING, None, None, None)
    assert event.coordinator_id == house["Sam"].id


async def test_answers_kept_from_before_a_restart_do_not_close_it_early(house: House) -> None:
    """Regression: the next tick closed a restarted call on answers from its called-off run."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = OPENS + timedelta(minutes=5)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    house.clock.now = OPENS + timedelta(minutes=10)
    await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    house.clock.now += timedelta(minutes=30)  # past the quiet spell, before the reminder
    assert await house.tick() == []
    # A partly answered run waits too: three answer again, Lee's answer is the old one.
    for name in ("Alex", "Sam", "Kim"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    house.clock.now += timedelta(minutes=15)
    assert await house.tick() == []
    await act(house, calls.respond, house["Lee"], event_id, Status.IN)
    house.clock.now += timedelta(minutes=10)
    assert await house.tick() == [Step.CLOSE_RESPONSES]


async def test_an_admin_rerun_of_an_everyone_out_call_gets_a_real_window(house: House) -> None:
    """Regression: re-running a call everyone was out of abandoned it again at the next tick."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=1)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    assert (await house.event(event_id)).state is EventState.ABANDONED
    house.clock.now = OPENS + timedelta(minutes=5)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    house.clock.now += timedelta(seconds=30)
    assert await house.tick() == []
    assert (await house.event(event_id)).state is EventState.COLLECTING


# Skipping a day ---------------------------------------------------------------------------------------


async def test_an_admin_skips_a_later_days_call(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=1)
    skipped = await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TOMORROW, note="Out of town")
    assert (skipped.state, skipped.ended_by, skipped.end_note) == (
        EventState.CANCELLED,
        house["Alex"].id,
        "Out of town",
    )
    assert await rows_for(house, skipped.id) == []
    audit = next(a for a in await house.audits() if a.action == "event.skipped")
    assert audit.payload == {"slot": "dinner", "date": "2026-10-23", "note": "Out of town"}
    house.clock.now = local(23, 15)
    assert await house.tick() == []  # tomorrow's standing call doesn't open
    assert await house.outbox() == [e for e in await house.outbox() if e.event_id != skipped.id]
    # Any member can still start it, as with any called-off call.
    again = await act(house, calls.start_call, house["Kim"], Slot.DINNER, TOMORROW)
    assert (again.id, again.state) == (skipped.id, EventState.COLLECTING)


async def test_skipping_today_before_the_call_opens(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=1)
    await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TODAY)
    house.clock.now = OPENS
    assert await house.tick() == []
    assert await house.kinds() == []


async def test_skip_day_losing_a_race_with_the_scheduler_is_a_clean_refusal(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler opens today's call between the skip's check and its insert."""
    await open_dinner(house)

    async def nothing_there(*_args: Any) -> None:
        return None

    monkeypatch.setattr(EventRepository, "call_for", nothing_there)
    with pytest.raises(EventRuleViolation, match="Call it off instead"):
        await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TODAY)
    assert [a.action for a in await house.audits() if a.action == "event.skipped"] == []


async def test_skip_day_rules(house: House) -> None:
    house.clock.now = OPENS - timedelta(hours=1)
    with pytest.raises(Forbidden):
        await act(house, calls.skip_day, house["Sam"], Slot.DINNER, TOMORROW)
    with pytest.raises(EventRuleViolation, match="today's call or a later"):
        await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TODAY - timedelta(days=1))
    with pytest.raises(EventRuleViolation, match="7 days ahead"):
        await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TODAY + timedelta(days=8))
    await act(house, calls.start_call, house["Kim"], Slot.LUNCH, TOMORROW)
    with pytest.raises(EventRuleViolation, match="Call it off instead"):
        await act(house, calls.skip_day, house["Alex"], Slot.LUNCH, TOMORROW)
    await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TOMORROW)
    with pytest.raises(EventRuleViolation, match=r"^The dinner call for that day is already off\.$"):
        await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TOMORROW)
    today = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], today, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()  # too few: on-your-own
    with pytest.raises(EventRuleViolation, match=r"^The dinner call for that day already finished\.$"):
        await act(house, calls.skip_day, house["Alex"], Slot.DINNER, TODAY)
    assert [a.action for a in await house.audits() if a.action == "event.skipped"] == ["event.skipped"]


# Close now -------------------------------------------------------------------------------------------


async def test_the_coordinator_starts_the_vote_now_and_an_admin_decides_now(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    with pytest.raises(EventRuleViolation, match="coordinator or an admin"):
        await act(house, calls.close_window, house["Sam"], event_id)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = OPENS + timedelta(minutes=20)
    await act(house, calls.close_window, house["Sam"], event_id)
    event = await house.event(event_id)
    assert event.state is EventState.VOTING  # at once, not at the next tick
    assert (event.respond_deadline, event.vote_deadline) == (house.clock(), house.clock() + timedelta(minutes=45))
    with pytest.raises(EventRuleViolation, match="coordinator or an admin"):
        await act(house, calls.close_window, house["Kim"], event_id)
    house.clock.now += timedelta(minutes=5)
    await act(house, calls.close_window, house["Alex"], event_id)
    assert (await house.event(event_id)).state is EventState.DECIDED
    closes = [a.payload for a in await house.audits() if a.action == "event.window_closed"]
    assert [(c["window"], c["as"]) for c in closes] == [("respond", "coordinator"), ("vote", "admin")]
    with pytest.raises(EventRuleViolation, match="no open window"):
        await act(house, calls.close_window, house["Alex"], event_id)


async def test_close_now_on_a_finished_call(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    with pytest.raises(EventRuleViolation, match="already finished"):
        await act(house, calls.close_window, house["Alex"], event_id)


# Closing early once everyone answered -----------------------------------------------------------------


async def test_everyone_answering_starts_the_vote_after_a_quiet_spell(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.MAYBE)
    await act(house, calls.respond, house["Lee"], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=20)
    assert await house.tick() == []  # Kim is a maybe
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    house.clock.now += timedelta(minutes=15)
    assert await house.tick() == []  # quiet, but nobody can cook, pick up, or order yet
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now += timedelta(minutes=9)
    assert await house.tick() == []  # the offer restarted the quiet spell
    house.clock.now += timedelta(minutes=1)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert event.state is EventState.VOTING
    assert (event.respond_deadline, event.vote_deadline) == (house.clock(), house.clock() + timedelta(minutes=45))
    early = next(a for a in await house.audits() if a.action == "event.everyone_answered")
    assert (early.actor_member_id, early.payload["from"]) == (None, RESPOND_BY.isoformat())


async def test_in_first_and_details_next_is_never_cut_short(house: House) -> None:
    """Regression: everyone tapped In, and the next tick ended the call as nothing covered
    before anyone could offer to pick up."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    house.clock.now = OPENS + timedelta(seconds=30)
    assert await house.tick() == []
    house.clock.now += timedelta(minutes=2)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.save_details, house["Sam"], event_id, Details(guests=1))
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_everyone_out_ends_the_call_early(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=5)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.TOO_FEW)
    assert "call_abandoned" in await house.kinds()


# Races with the scheduler ---------------------------------------------------------------------------


async def test_a_call_off_after_the_scheduler_decided_still_goes_through(house: House) -> None:
    event_id = await to_voting(house)
    house.clock.now = VOTE_BY
    assert await house.tick() == [Step.CLOSE_VOTING]  # the scheduler takes the lock first
    await act(house, calls.cancel_call, house["Sam"], event_id)  # decided -> cancelled is allowed
    assert (await house.event(event_id)).state is EventState.CANCELLED
    kinds = await house.kinds()
    assert kinds.index("decision_made") < kinds.index("event_cancelled")


async def test_a_call_off_after_the_scheduler_marked_it_done_is_refused_kindly(house: House) -> None:
    event_id = await decided(house)
    house.clock.now = DONE_AT
    assert await house.tick() == [Step.MARK_DONE]
    before = (await house.kinds(), len(await house.audits()))
    with pytest.raises(EventRuleViolation, match=r"^This call already finished\.$"):
        await act(house, calls.cancel_call, house["Sam"], event_id)
    assert (await house.kinds(), len(await house.audits())) == before  # rolled back: nothing written
    assert len(await house.meals()) == 1


async def test_the_scheduler_after_a_call_off_does_nothing(house: House) -> None:
    event_id = await to_voting(house)
    house.clock.now = VOTE_BY - timedelta(seconds=1)
    await act(house, calls.cancel_call, house["Sam"], event_id)
    house.clock.now = VOTE_BY
    assert await house.tick() == []
    assert "decision_made" not in await house.kinds()
    assert (await house.event(event_id)).state is EventState.CANCELLED


@pytest.mark.parametrize("when", [VOTE_BY, DONE_AT])
async def test_a_call_off_racing_the_tick_ends_consistently(house: House, when: datetime) -> None:
    """Both take the event's lock, so one runs after the other, in either order."""
    event_id = await (to_voting(house) if when == VOTE_BY else decided(house))
    house.clock.now = when
    async with house.sessionmaker() as db:
        tick, cancel = await asyncio.gather(
            house.tick(), calls.cancel_call(db, house["Sam"], event_id, now=when), return_exceptions=True
        )
    assert not isinstance(tick, BaseException)
    event = await house.event(event_id)
    if when == VOTE_BY:  # decided -> cancelled is allowed, so the call-off always wins
        assert cancel is None and event.state is EventState.CANCELLED
        assert await house.meals() == []
    elif isinstance(cancel, EventRuleViolation):  # the tick marked it done first
        assert str(cancel) == "This call already finished."
        assert event.state is EventState.DONE and len(await house.meals()) == 1
    else:  # the call-off came first, and the tick found nothing to do
        assert cancel is None and event.state is EventState.CANCELLED and await house.meals() == []
    assert len(await cancel_audits(house)) == (event.state is EventState.CANCELLED)


@pytest.mark.parametrize(("when", "step"), [(VOTE_BY, Step.CLOSE_VOTING), (DONE_AT, Step.MARK_DONE)])
async def test_a_call_off_waits_while_the_tick_holds_the_lock(
    house: House, monkeypatch: pytest.MonkeyPatch, when: datetime, step: Step
) -> None:
    """The call-off arrives while the tick's step is mid-transaction: it waits for the commit,
    then checks its rules against what the tick left."""
    event_id = await (to_voting(house) if step is Step.CLOSE_VOTING else decided(house))
    real = lifecycle.HANDLERS[step]
    racing: list[asyncio.Task[Any]] = []
    waited: list[bool] = []

    async def step_then_call_off(db: Any, event: Any, now: datetime) -> None:
        await real(db, event, now)
        racing.append(asyncio.create_task(act(house, calls.cancel_call, house["Sam"], event_id)))
        await asyncio.sleep(0.05)  # the call-off is now waiting on this transaction's lock
        waited.append(not racing[0].done())

    monkeypatch.setitem(lifecycle.HANDLERS, step, step_then_call_off)
    house.clock.now = when
    assert await house.tick() == [step]
    [outcome] = await asyncio.gather(*racing, return_exceptions=True)
    assert waited == [True]  # it really did wait for the tick's commit
    event = await house.event(event_id)
    if step is Step.CLOSE_VOTING:
        assert outcome is None and event.state is EventState.CANCELLED
        kinds = await house.kinds()
        assert kinds.index("decision_made") < kinds.index("event_cancelled")
    else:
        assert isinstance(outcome, EventRuleViolation) and str(outcome) == "This call already finished."
        assert event.state is EventState.DONE and len(await house.meals()) == 1
        assert await cancel_audits(house) == []


class CallOffMidDelivery(RecordingChannel):
    """While a drainer holds the first ``assignment_made``, someone calls the call off; then
    the channel fails the delivery (retryable) or holds it, so the row goes back to pending."""

    def __init__(self, house: House, event_id: int, failure: Exception) -> None:
        super().__init__()
        self.house, self.event_id, self.failure = house, event_id, failure
        self.called_off = False

    async def deliver(self, notification: n.Notification, *, thread: MessageRef | None = None) -> DeliveryResult:
        if notification.kind == "assignment_made" and not self.called_off:
            self.called_off = True
            await act(self.house, calls.cancel_call, self.house["Sam"], self.event_id)
            raise self.failure
        return await super().deliver(notification, thread=thread)


@pytest.mark.parametrize(
    "failure", [ConnectionError("slack is down"), HeldDelivery("setup broken", retry_after=5)], ids=["retry", "held"]
)
async def test_an_assignment_message_a_drainer_held_during_the_call_off_never_follows_it(
    house: House, failure: Exception
) -> None:
    """Regression: the held row went back to pending and arrived after "You're no longer…"."""
    event_id = await decided(house)
    channel = CallOffMidDelivery(house, event_id, failure)
    await house.drain(channel)
    assert channel.called_off
    house.clock.now += timedelta(minutes=5)  # past any retry delay or hold
    await house.drain(channel)
    assert "assignment_cancelled" in channel.kinds and "assignment_made" not in channel.kinds
    async with house.sessionmaker() as db:
        rows = list(await db.scalars(select(OutboxRow).where(OutboxRow.kind == "assignment_made")))
    assert rows and all(r.delivered_at is not None for r in rows)
    assert {r.last_error for r in rows} == {"superseded by a call-off"}


async def test_a_reminder_from_an_earlier_run_is_not_sent_after_a_restart(house: House) -> None:
    event_id = await open_dinner(house)
    house.clock.now = OPENS + timedelta(minutes=75)
    assert await house.tick() == [Step.REMIND]
    await act(house, calls.cancel_call, house["Alex"], event_id)
    async with house.sessionmaker() as db:  # as if a drainer had held the reminders through the call-off
        await db.execute(
            text("UPDATE outbox SET delivered_at = NULL, last_error = NULL WHERE kind = 'response_reminder'")
        )
        await db.commit()
    await act(house, calls.start_call, house["Kim"], Slot.DINNER, TODAY)
    channel = RecordingChannel()
    await house.drain(channel)
    assert "response_reminder" not in channel.kinds and "call_opened" in channel.kinds


async def test_a_lone_in_gets_time_to_add_a_guest(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    for name in ("Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=1)
    assert await house.tick() == []
    house.clock.now += timedelta(minutes=3)
    await act(house, calls.save_details, house["Alex"], event_id, Details(guests=1))
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now += timedelta(minutes=10)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_a_lone_in_without_a_guest_ends_after_the_quiet_spell(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    for name in ("Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=10)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.TOO_FEW)
