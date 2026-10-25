# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive a ``House`` dinner to done on a chosen item, for feedback tests."""

from __future__ import annotations

from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import EventState, RoleOffer, Status
from dinnerbell.services import calls
from tests.support.calls import DONE_AT, OPENS, RESPOND_BY, House, dinner_id


async def done_dinner(
    house: House,
    item: str = "Sam's Tacos",
    mode: Mode = Mode.COOK,
    *,
    eaters: tuple[str, ...] = ("Alex", "Sam", "Kim"),
) -> int:
    """Open the dinner call, have ``eaters`` say in (Sam offers to cook, Alex to pick up), lock
    ``item`` by ``mode`` as Alex, the admin, and tick past the meal so it's done. Returns the
    event ID; the meal is in the log."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    async with house.sessionmaker() as db:
        for name in eaters:
            await calls.respond(db, house[name], event_id, Status.IN, now=house.clock())
        if "Sam" in eaters:
            await calls.offer_role(db, house["Sam"], event_id, RoleOffer.COOK, True, now=house.clock())
        if "Alex" in eaters:
            await calls.offer_role(db, house["Alex"], event_id, RoleOffer.PICKUP, True, now=house.clock())
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    item_id = house.items[item].id
    proposal = next((p for p in ctx.proposals if p.item_id == item_id and p.mode is mode), None)
    async with house.sessionmaker() as db:
        if proposal is None:
            proposal = await calls.add_proposal(db, house["Alex"], event_id, mode, item_id=item_id, now=house.clock())
        await calls.lock(db, house["Alex"], event_id, proposal.id, now=house.clock())
    house.clock.now = DONE_AT
    await house.tick()
    assert (await house.event(event_id)).state is EventState.DONE
    return event_id


async def remind(house: House, *names: str) -> None:
    """Turn the feedback reminder on for ``names``."""
    async with house.sessionmaker() as db:
        for name in names:
            await MemberRepository(db).update(house.household.id, house[name].id, house.clock(), feedback_reminder=True)
        await db.commit()
    for name in names:
        await house.refresh(name)
