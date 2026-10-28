# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests at a dinner call (spec 5.5, FR-8): the issue #13 rehearsal of a dinner with two guests,
one of them vegetarian. Their dietary need is a hard filter alongside members' vetoes, they count
toward the head count, and the decision message and the assignee's direct message both say so."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from dinnerbell.config import Settings
from dinnerbell.db.outbox import OutboxEntry
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import Details, EventState, RoleOffer, Status
from dinnerbell.domain.notifications import AssignmentMade, DecisionMade, PersonInfo, ProposalsReady, from_payload
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.preferences import set_my_preference
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    await house.tag("Vegetarian", TagKind.DIETARY)
    await house.add_item("Green Leaf", Mode.PICKUP, cuisine="Indian", tags=("Vegetarian",))
    await house.add_item("Veggie Pizza", Mode.PICKUP, cuisine="Italian", tags=("Vegetarian",))
    return house


def made_picks(entries: dict[str, OutboxEntry]) -> tuple[PersonInfo, ...]:
    ready = from_payload("proposals_ready", entries["proposals_ready"].payload)
    assert isinstance(ready, ProposalsReady)
    return ready.participants


async def test_a_dinner_with_two_guests_one_vegetarian(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    veg = house.tags["Vegetarian"].id
    async with house.sessionmaker() as db:
        italian = house.tags["Italian"].id
        await set_my_preference(db, house["Kim"], TargetType.TAG, italian, Stance(veto=True), now=house.clock())
    async with house.sessionmaker() as db:
        await calls.respond(db, house["Alex"], event_id, Status.IN, now=house.clock())
        await calls.offer_role(db, house["Alex"], event_id, RoleOffer.PICKUP, True, now=house.clock())
        await calls.respond(db, house["Kim"], event_id, Status.IN, now=house.clock())
        # Sam brings two guests; one is vegetarian, so the guests need vegetarian food.
        details = Details(guests=2, guest_needs=frozenset({veg}))
        await calls.save_details(db, house["Sam"], event_id, details, now=house.clock())

    ctx = await house.context(event_id)
    assert ctx.head_count == 5
    sam = next(e for e in cc.roster(ctx) if e.member.id == house["Sam"].id)
    assert (sam.guests, sam.guest_needs) == (2, ("Vegetarian",))

    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.VOTING
    # Only vegetarian items survive, and Kim's veto takes the pizza out too.
    assert {p.item_id for p in ctx.proposals} == {house.items["Green Leaf"].id}

    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED
    entries = {e.kind: e for e in await house.outbox()}
    made = from_payload("decision_made", entries["decision_made"].payload)
    assert isinstance(made, DecisionMade)
    assert (made.decision.head_count, made.decision.dietary_notes) == (5, ("Vegetarian",))
    assert [(p.member.name, p.guests) for p in made.decision.participants if p.guests] == [("Sam", 2)]
    # Everyone who's in is mentioned at the top level, not only those with a role (#92).
    assert [p.name for p in made_picks(entries)] == ["Alex", "Kim", "Sam"]
    dm = from_payload("assignment_made", entries["assignment_made"].payload)
    assert isinstance(dm, AssignmentMade) and dm.member.id == house["Alex"].id
    assert (dm.decision.head_count, dm.decision.dietary_notes) == (5, ("Vegetarian",))
