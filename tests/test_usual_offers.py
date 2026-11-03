# SPDX-License-Identifier: AGPL-3.0-or-later
"""Usual offers start on (#180, FR-8): answering In turns on the offers a member's usual
settings stand for, once per call, and an offer they turn off stays off."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from dinnerbell.config import Settings
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import Details, RoleOffer, Status, with_pick_role, with_usual_offers
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.services import call_admin, calls
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, RESPOND_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio

COOK, PICKUP, ORDER = RoleOffer.COOK, RoleOffer.PICKUP, RoleOffer.ORDER
STAMP = datetime(2026, 10, 22, 18, tzinfo=UTC)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    """``furnish``'s household, plus Pat, who usually cooks and picks up."""
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    await house.add_member("Pat", can_cook=True, can_pickup=True)
    return house


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def open_dinner(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def answer(house: House, event_id: int, name: str) -> tuple[Status | None, frozenset[RoleOffer]]:
    mine = (await house.context(event_id)).response_of(house[name].id)
    return (None, frozenset()) if mine is None else (mine.status, mine.offers)


async def test_in_turns_on_exactly_the_usual_offers(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Pat", "Lee", "Kim"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({COOK, PICKUP}))
    assert await answer(house, event_id, "Lee") == (Status.IN, frozenset({ORDER}))
    assert await answer(house, event_id, "Kim") == (Status.IN, frozenset())


async def test_maybe_and_out_turn_none_on_and_in_after_them_does(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Pat"], event_id, Status.MAYBE)
    assert await answer(house, event_id, "Pat") == (Status.MAYBE, frozenset())
    await act(house, calls.respond, house["Pat"], event_id, Status.OUT)
    assert await answer(house, event_id, "Pat") == (Status.OUT, frozenset())
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({COOK, PICKUP}))


async def test_an_offer_turned_off_stays_off(house: House) -> None:
    """Once per call: In again, Maybe then In, and Out then In never turn an offer back on."""
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Pat"], event_id, COOK, False)
    for status in (Status.IN, Status.MAYBE, Status.IN):
        await act(house, calls.respond, house["Pat"], event_id, status)
        assert await answer(house, event_id, "Pat") == (status, frozenset({PICKUP}))
    await act(house, calls.respond, house["Pat"], event_id, Status.OUT)
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset())


async def test_an_offer_turned_off_while_a_maybe_keeps_them_all_off(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Pat"], event_id, Status.MAYBE)
    await act(house, calls.offer_role, house["Pat"], event_id, PICKUP, True)  # a maybe stays a maybe
    await act(house, calls.offer_role, house["Pat"], event_id, PICKUP, False)
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset())


async def test_offering_a_role_first_counts_you_in_with_only_that_role(house: House) -> None:
    """Being counted in by an offer is your own choice of offers, so tapping In later turns on
    nothing more."""
    event_id = await open_dinner(house)
    await act(house, calls.offer_role, house["Pat"], event_id, PICKUP, True)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({PICKUP}))
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({PICKUP}))


async def test_adding_a_pick_while_answers_are_open_counts_you_in_with_only_its_role() -> None:
    """An offer that joins an open call as a pick (FR-5) counts its maker in with the pick's
    role, which ends the turn of their usual offers."""
    pat = Member(7, 1, "Pat", "pat@example.com", Role.MEMBER, Capabilities(can_cook=True, can_pickup=True), True)
    picked = with_pick_role(None, pat.id, Mode.PICKUP, STAMP, join=True)
    assert (picked.status, picked.offers, picked.usual_offers_done) == (Status.IN, frozenset({PICKUP}), True)
    assert with_usual_offers(picked, pat) == picked


async def test_details_before_answering_turn_them_on(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.save_details, house["Pat"], event_id, Details(guests=1))
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({COOK, PICKUP}))


async def test_details_after_maybe_turn_none_on(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Pat"], event_id, Status.MAYBE)
    await act(house, calls.save_details, house["Pat"], event_id, Details(guests=1))
    assert await answer(house, event_id, "Pat") == (Status.MAYBE, frozenset())


async def test_claiming_coordinating_turns_them_on(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.claim_coordinator, house["Pat"], event_id)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({COOK, PICKUP}))


async def test_a_hand_off_turns_them_on(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, calls.hand_off, house["Kim"], event_id, house["Pat"].id)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({COOK, PICKUP}))


async def test_an_admin_assigning_coordinating_turns_them_on_once(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Pat"].id)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({COOK, PICKUP}))
    await act(house, calls.offer_role, house["Pat"], event_id, COOK, False)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Pat"].id)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({PICKUP}))


async def test_joining_late_turns_them_on(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Kim"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert await act(house, calls.join_late, house["Pat"], event_id)
    assert await answer(house, event_id, "Pat") == (Status.IN, frozenset({COOK, PICKUP}))


async def test_cooking_still_needs_the_cook_list(house: House) -> None:
    """Pat usually cooks but isn't on the tacos' cook list, so only places Pat picks up from
    make the vote."""
    await house.add_item("Pat's Curry", Mode.COOK, cuisine="Thai", cooks=("Pat",))
    event_id = await open_dinner(house)
    for name in ("Alex", "Pat"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    picks = {(ctx.items[p.item_id].name, p.mode) for p in ctx.proposals if p.item_id}
    assert ("Pat's Curry", Mode.COOK) in picks
    assert all(name != "Sam's Tacos" for name, _ in picks)
