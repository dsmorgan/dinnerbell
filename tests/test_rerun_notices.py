# SPDX-License-Identifier: AGPL-3.0-or-later
"""An admin's re-run of a voting or decided call (FR-6, issue 331): the run it replaces says
so, and whoever had a role in it is told the role is off, as a call-off from the same state
tells them (FR-56)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.models import OutboxRow
from dinnerbell.domain import notifications as n
from dinnerbell.domain.events import AssignmentRole, EventState, RoleOffer, Status
from dinnerbell.services import call_admin, calls
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio

WORK_ROLES = {AssignmentRole.COOK, AssignmentRole.PICKUP, AssignmentRole.ORDER}


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


async def to_voting(house: House) -> int:
    """Alex (the admin), Sam, and Lee are in; Lee picks up, and Sam coordinates."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Lee"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def decided(house: House) -> int:
    event_id = await to_voting(house)
    await house.settle_ways(event_id)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED
    assert {a.member_id for a in ctx.assignments if a.role in WORK_ROLES} == {house["Lee"].id}
    return event_id


async def rows_after(house: House, before: int) -> list[tuple[OutboxRow, n.Notification]]:
    async with house.sessionmaker() as db:
        rows = list(await db.scalars(select(OutboxRow).where(OutboxRow.id > before).order_by(OutboxRow.id)))
    return [(r, n.from_payload(r.kind, r.payload)) for r in rows]


async def last_row_id(house: House) -> int:
    async with house.sessionmaker() as db:
        return int(await db.scalar(select(OutboxRow.id).order_by(OutboxRow.id.desc()).limit(1)) or 0)


async def test_a_rerun_of_a_decided_call_replaces_its_messages_and_tells_whoever_had_a_role(house: House) -> None:
    event_id = await decided(house)
    channel = RecordingChannel()
    await house.drain(channel)
    before = await last_row_id(house)

    await act(house, call_admin.rerun, house["Alex"], event_id)

    sent = await rows_after(house, before)
    run1 = f"event:{event_id}:run1"
    edits = {row.message_key: x for row, x in sent if x.is_update}
    assert set(edits) == {f"{run1}:call", f"{run1}:proposals", f"{run1}:decision"}
    assert all(x.event.replaced and x.event.run == 1 for x in edits.values())
    dms = [x for _, x in sent if isinstance(x, n.AssignmentCancelled)]
    assert len(dms) == 1
    dm = dms[0]
    assert (dm.member.name, dm.cancelled_by, dm.note) == ("Lee", n.PersonInfo(house["Alex"].id, "Alex"), None)
    assert set(dm.roles) <= WORK_ROLES and AssignmentRole.PICKUP in dm.roles
    assert dm.event.replaced and dm.idempotency_key == f"{run1}:assignment_cancelled:member{house['Lee'].id}"
    assert sent[-1][1].kind == "call_opened" and sent[-1][1].event.run == 2  # the new run's post comes last

    await house.drain(channel)
    refs = {s.key: s.ref for s in channel.sent if s.op == "deliver"}
    updated = {s.ref for s in channel.sent[-5:] if s.op == "update"}
    assert {refs[f"{run1}:call_opened"], refs[f"{run1}:proposals_ready"], refs[f"{run1}:decision_made"]} <= updated
    told = [s for s in channel.sent if s.kind == "assignment_cancelled"]
    assert [s.recipient for s in told] == [house["Lee"].id]


async def test_a_rerun_tells_neither_the_admin_nor_the_coordinator_about_their_roles(house: House) -> None:
    event_id = await decided(house)
    ctx = await house.context(event_id)
    role = next(a.role for a in ctx.assignments if a.role in WORK_ROLES)
    await act(house, calls.change_assignment, house["Alex"], event_id, role, house["Alex"].id)  # the admin has it now
    assert ctx.event.coordinator_id == house["Sam"].id
    before = await last_row_id(house)

    await act(house, call_admin.rerun, house["Alex"], event_id)

    told = {x.member.name for _, x in await rows_after(house, before) if isinstance(x, n.AssignmentCancelled)}
    assert "Alex" not in told and "Sam" not in told  # nobody is told about their own re-run, or coordinating


async def test_a_rerun_of_a_voting_call_replaces_the_call_and_the_picks(house: House) -> None:
    event_id = await to_voting(house)
    await house.drain(RecordingChannel())
    before = await last_row_id(house)

    await act(house, call_admin.rerun, house["Alex"], event_id)

    sent = [x for _, x in await rows_after(house, before)]
    run1 = f"event:{event_id}:run1"
    assert [(x.kind, x.message_key) for x in sent if x.is_update] == [
        ("call_updated", f"{run1}:call"),
        ("proposals_updated", f"{run1}:proposals"),
    ]
    assert all(x.event.replaced for x in sent if x.is_update)
    assert not any(isinstance(x, n.AssignmentCancelled) for x in sent)
    assert [x.kind for x in sent if not x.is_update] == ["call_opened"]


async def test_a_rerun_merges_into_a_waiting_edit_and_closes_the_old_runs_direct_messages(house: House) -> None:
    """A slow "You're picking up" must never arrive after "You're no longer picking up"."""
    event_id = await decided(house)  # nothing drained: the old run's rows are all still waiting
    await act(house, call_admin.rerun, house["Alex"], event_id)
    async with house.sessionmaker() as db:
        rows = list(await db.scalars(select(OutboxRow).where(OutboxRow.event_id == event_id)))
    made = [r for r in rows if r.kind == "assignment_made"]
    assert made and {(r.delivered_at is not None, r.last_error) for r in made} == {(True, "superseded by a re-run")}
    calls_edits = [r for r in rows if r.kind == "call_updated" and r.message_key == f"event:{event_id}:run1:call"]
    assert calls_edits[-1].delivered_at is None and calls_edits[-1].payload["event"]["replaced"] is True
    channel = RecordingChannel()
    house.clock.now += timedelta(seconds=5)
    await house.drain(channel)
    assert "assignment_made" not in channel.kinds and channel.kinds.count("assignment_cancelled") == 1


async def test_a_rerun_of_an_abandoned_call_sends_only_the_new_call(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=1)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.ABANDONED
    before = await last_row_id(house)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    assert [x.kind for _, x in await rows_after(house, before)] == ["call_opened"]
