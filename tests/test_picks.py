# SPDX-License-Identifier: AGPL-3.0-or-later
"""Picks a member adds to a call, against a migrated database. Whoever adds a pick does it (#74):
an added proposal takes the role its mode needs, as an offer that joins the call does. A call
has one pick per place, in any mode (#77): ranking, added proposals, offers, and the repository
backstop, with a property-style check across seeded random catalogs."""

from __future__ import annotations

import contextlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, time
from typing import Any

import pytest

from dinnerbell.config import Settings
from dinnerbell.db.events import ProposalRepository
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    NOT_IN_TO_ADD,
    DuplicatePick,
    EventRuleViolation,
    EventState,
    Proposal,
    ProposalSource,
    RoleOffer,
    Status,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Capabilities
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.domain.ranking import RankingSnapshot, rank
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls, lifecycle, offers, picks
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.preferences import MySettings, group_preferences, set_my_preference, update_my_settings
from tests.support.calls import OPENS, RESPOND_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio
DAY = date(2026, 10, 22)


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


async def to_voting(house: House, *eaters: str, offers_pickup: tuple[str, ...] = ()) -> int:
    """``eaters`` are in; Lee can order, so delivery is covered whenever Lee is in."""
    event_id = await open_dinner(house)
    for name in eaters:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    for name in offers_pickup:
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def offers_of(house: House, event_id: int, name: str) -> frozenset[RoleOffer]:
    mine = (await house.context(event_id)).response_of(house[name].id)
    return mine.offers if mine is not None else frozenset()


def offer(house: House, mode: Mode, **changes: Any) -> offers.OfferInput:
    values: dict[str, Any] = {"slot": Slot.DINNER, "day": DAY, "mode": mode, "at": time(18, 30), "capacity": 3}
    return offers.OfferInput(**(values | changes))


# Whoever adds a pick does it (#74) ---------------------------------------------------------------


async def test_a_member_who_is_in_adds_a_pickup_place_and_picks_it_up(house: House) -> None:
    """The report: three in, nobody offered to pick up, voting open. Kim adds a place for pickup.
    It used to be refused ("Nobody who's in offered to pickup for that."), and neither the
    Preferences checkbox nor I can pick up, closed while voting, could change that."""
    event_id = await to_voting(house, "Alex", "Kim", "Lee")
    assert (await house.context(event_id)).coverage.pickups == ()
    # Preferences > Pick up is about the member in general, not this call (FR-17 is unchanged).
    kim = await act(house, update_my_settings, house["Kim"], MySettings(Capabilities(can_pickup=True), False, False))
    house.people["Kim"] = kim
    assert (await house.context(event_id)).coverage.pickups == ()
    with pytest.raises(EventRuleViolation, match="Responses are closed"):
        await act(house, calls.offer_role, kim, event_id, RoleOffer.PICKUP, True)

    channel = RecordingChannel()
    await house.drain(channel)
    channel.sent.clear()
    viet = await house.tag("Vietnamese")
    pho = await act(house, calls.add_proposal, kim, event_id, Mode.PICKUP, free_text="Pho Place", cuisine_id=viet.id)
    ctx = await house.context(event_id)
    assert (pho.mode, pho.proposed_by) == (Mode.PICKUP, kim.id)
    assert ctx.coverage.pickups == (kim.id,)
    entry = next(e for e in cc.roster(ctx) if e.member.id == kim.id)
    assert (entry.status, entry.offers) == (Status.IN, (RoleOffer.PICKUP,))  # the roster shows who picks it up
    await house.drain(channel)
    assert sorted(channel.kinds) == ["call_updated", "proposals_updated"]


async def test_a_pickup_picks_winner_is_picked_up_by_whoever_added_it(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Kim", "Lee")
    viet = await house.tag("Vietnamese")
    pho = await act(
        house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, free_text="Pho Place", cuisine_id=viet.id
    )
    await act(house, calls.lock, house["Alex"], event_id, pho.id)
    ctx = await house.context(event_id)
    assert {(a.role.value, a.member_id) for a in ctx.assignments} == {("pickup", house["Kim"].id)}


@pytest.mark.parametrize("answer", ["never", Status.OUT, Status.MAYBE, "coordinating"])
async def test_someone_not_in_cant_add_a_pick_yet(house: House, answer: object) -> None:
    """Joining late is #76; until then they're told why, whatever they'd add."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    if isinstance(answer, Status):
        await act(house, calls.respond, house["Kim"], event_id, answer)  # a maybe is out at the deadline
    house.clock.now = RESPOND_BY
    await house.tick()
    if answer == "coordinating":
        await act(house, calls.claim_coordinator, house["Kim"], event_id)
    for mode, target in [
        (Mode.PICKUP, {"item_id": house.items["Taqueria"].id}),
        (Mode.DELIVERY, {"free_text": "Thai Express", "cuisine_id": house.tags["Thai"].id}),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            await act(house, calls.add_proposal, house["Kim"], event_id, mode, **target)
        assert str(refused.value) == NOT_IN_TO_ADD
    assert await offers_of(house, event_id, "Kim") == frozenset()


async def test_cook_picks_respect_the_cook_list(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam", "Kim", "Lee")
    tacos = house.items["Sam's Tacos"].id  # Sam is on its cook list
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.COOK, item_id=tacos)
    assert str(refused.value) == "Sam's Tacos has a cook list, and you're not on it. Ask a cook on it to add it."
    assert await offers_of(house, event_id, "Kim") == frozenset()
    await act(house, calls.add_proposal, house["Sam"], event_id, Mode.COOK, item_id=tacos)
    assert RoleOffer.COOK in await offers_of(house, event_id, "Sam")
    # Something new has no cook list, so whoever adds it can cook it.
    mexican = house.tags["Mexican"].id
    await act(house, calls.add_proposal, house["Kim"], event_id, Mode.COOK, free_text="Kim's Chili", cuisine_id=mexican)
    assert await offers_of(house, event_id, "Kim") == {RoleOffer.COOK}


async def test_a_delivery_pick_makes_its_adder_the_orderer(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Kim", offers_pickup=("Alex",))
    assert (await house.context(event_id)).coverage.orderers == ()
    thai = house.tags["Thai"].id
    added = await act(
        house, calls.add_proposal, house["Kim"], event_id, Mode.DELIVERY, free_text="Thai Express", cuisine_id=thai
    )
    assert added.mode is Mode.DELIVERY
    assert (await house.context(event_id)).coverage.orderers == (house["Kim"].id,)


async def test_a_refused_pick_leaves_the_answer_as_it_was(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Kim", "Lee")
    noodles = await house.add_item("Noodle Bar", Mode.PICKUP, cuisine="Thai")
    await act(house, set_my_preference, house["Lee"], TargetType.ITEM, noodles.id, Stance(veto=True))
    with pytest.raises(EventRuleViolation, match="doesn't work for everyone"):
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, item_id=noodles.id)
    assert await offers_of(house, event_id, "Kim") == frozenset()


async def test_an_uncovered_mode_is_named_in_words(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Kim", "Lee")
    async with house.sessionmaker() as db:
        ctx = await cc.load(db, await house.event(event_id))
        group = await group_preferences(db, house.household.id, ctx.veto_people)
    candidate = Proposal(0, event_id, Mode.PICKUP, 0, ProposalSource.MEMBER, item_id=house.items["Taqueria"].id)
    assert lifecycle.misfit(ctx, candidate, group) == "Nobody who's in offered to pick up for that."


# One pick per place (#77) -------------------------------------------------------------------------


async def restaurants_with_both_modes(house: House, count: int) -> None:
    for n in range(count):
        await house.add_item(f"Place {n}", Mode.PICKUP, Mode.DELIVERY, cuisine="Thai")


async def test_places_that_allow_pickup_and_delivery_give_three_different_picks(house: House) -> None:
    await restaurants_with_both_modes(house, 4)
    event_id = await to_voting(house, "Alex", "Lee", offers_pickup=("Alex",))
    proposals = (await house.context(event_id)).proposals
    assert len(proposals) == 3
    assert len({p.item_id for p in proposals}) == 3


async def test_an_offers_pick_doesnt_shrink_the_ranked_picks(house: House) -> None:
    await restaurants_with_both_modes(house, 4)
    event_id = await open_dinner(house)
    for name in ("Alex", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    alone = rank(await snapshot_of(house, event_id))
    top = alone[0]
    await act(house, offers.post_offer, house["Kim"], offer(house, Mode.DELIVERY, item_id=top.item_id))
    house.clock.now = RESPOND_BY
    await house.tick()
    proposals = (await house.context(event_id)).proposals
    assert len(proposals) == 4  # three ranked, then the offer's
    assert len({p.item_id for p in proposals}) == 4
    assert (proposals[-1].item_id, proposals[-1].mode) == (top.item_id, Mode.DELIVERY)


async def snapshot_of(house: House, event_id: int) -> RankingSnapshot:
    async with house.sessionmaker() as db:
        ctx = await cc.load(db, await house.event(event_id))
        return await lifecycle.ranking_snapshot(
            db, ctx, await group_preferences(db, house.household.id, ctx.veto_people)
        )


async def test_an_offer_takes_over_a_pick_for_its_place_and_keeps_its_votes(house: House) -> None:
    """Siam Garden is a pick for pickup. Kim, who's in, offers to order it: the pick becomes
    delivery, Kim orders it, and the vote for it stays, since votes are for the place."""
    event_id = await to_voting(house, "Alex", "Kim", "Sam", offers_pickup=("Alex",))
    siam = house.items["Siam Garden"].id
    pick = next(p for p in (await house.context(event_id)).proposals if p.item_id == siam)
    assert pick.mode is Mode.PICKUP and "Pickup is a usual choice." in pick.reasons
    await act(house, calls.vote, house["Sam"], event_id, pick.id)
    with pytest.raises(EventRuleViolation, match=r"^Siam Garden is already a pick \(pickup\). Vote for it instead.$"):
        await act(house, offers.post_offer, house["Sam"], offer(house, Mode.PICKUP, item_id=siam))

    posted = await act(house, offers.post_offer, house["Kim"], offer(house, Mode.DELIVERY, item_id=siam))
    assert (posted.path, posted.event_id) == (offers.OfferPath.ATTACH, event_id)
    ctx = await house.context(event_id)
    (taken,) = [p for p in ctx.proposals if p.item_id == siam]
    assert (taken.id, taken.rank, taken.mode, taken.proposed_by) == (pick.id, pick.rank, Mode.DELIVERY, house["Kim"].id)
    assert taken.reasons[0] == "Kim offered to order it."
    assert "Pickup is a usual choice." not in taken.reasons
    assert ctx.votes[pick.id] == [house["Sam"].id]
    assert RoleOffer.ORDER in await offers_of(house, event_id, "Kim")
    audit = (await house.audits("event.offer_attached"))[-1]
    assert audit.payload == {"proposal_id": pick.id, "item_id": siam, "mode": "delivery", "took_over": "pickup"}
    assert (await house.kinds())[-1] == "proposals_updated"


async def test_an_offer_takes_over_a_free_text_pick_of_the_same_name(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Kim", "Lee", offers_pickup=("Alex",))
    viet = await house.tag("Vietnamese")
    pho = await act(
        house, calls.add_proposal, house["Alex"], event_id, Mode.PICKUP, free_text="Pho Place", cuisine_id=viet.id
    )
    data = offer(house, Mode.DELIVERY, free_text="pho place", cuisine_id=viet.id)
    await act(house, offers.post_offer, house["Kim"], data)
    ctx = await house.context(event_id)
    (taken,) = [p for p in ctx.proposals if p.id == pho.id]
    item = ctx.items[taken.item_id or 0]
    assert (item.name, taken.free_text, taken.mode) == ("pho place", None, Mode.DELIVERY)
    assert len(ctx.proposals) == len({picks.place(ctx, p) for p in ctx.proposals})


async def test_the_repository_never_adds_a_second_pick_for_a_place(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Lee", offers_pickup=("Alex",))
    siam = house.items["Siam Garden"].id
    tags = house.tags["Thai"].id
    attempts: list[dict[str, Any]] = [
        {"mode": Mode.DELIVERY, "item_id": siam},  # a catalog item, in its other mode
        {"mode": Mode.DELIVERY, "free_text": "SIAM  garden", "free_cuisine_id": tags},  # its name, as free text
    ]
    async with house.sessionmaker() as db:
        repo = ProposalRepository(db)
        await repo.add(
            household_id=house.household.id,
            event_id=event_id,
            mode=Mode.PICKUP,
            rank=9,
            source=ProposalSource.MEMBER,
            reasons=(),
            now=house.clock(),
            free_text="Jade",
            free_cuisine_id=tags,
        )
        attempts.append({"mode": Mode.DELIVERY, "free_text": "jade", "free_cuisine_id": tags})
        for attempt in attempts:
            with pytest.raises(DuplicatePick):
                await repo.add(
                    household_id=house.household.id,
                    event_id=event_id,
                    rank=10,
                    source=ProposalSource.MEMBER,
                    reasons=(),
                    now=house.clock(),
                    **attempt,
                )
        # A home meal and a restaurant can share a name: they're different places.
        await repo.add(
            household_id=house.household.id,
            event_id=event_id,
            mode=Mode.COOK,
            rank=10,
            source=ProposalSource.MEMBER,
            reasons=(),
            now=house.clock(),
            free_text="Jade",
            free_cuisine_id=tags,
        )
        await db.rollback()


@pytest.mark.parametrize("seed", range(6))
async def test_no_call_ever_has_two_picks_for_a_place(house: House, seed: int) -> None:
    """Property-style: a seeded random catalog and call, then members add proposals and offers
    for random places in random modes. Whatever is accepted or refused, the call never holds
    two picks for one place."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    names = [f"Place {n}" for n in range(rng.randint(3, 8))]
    for name in names:
        modes = rng.choice([(Mode.PICKUP,), (Mode.DELIVERY,), (Mode.PICKUP, Mode.DELIVERY)])
        await house.add_item(name, *modes, cuisine=rng.choice(["Thai", "Mexican"]))
    people = ["Alex", "Sam", "Kim", "Lee"]
    eaters = rng.sample(people, rng.randint(2, 4))
    event_id = await open_dinner(house)
    for name in eaters:
        await act(house, calls.respond, house[name], event_id, Status.IN)
        if rng.random() < 0.5:
            await act(house, calls.offer_role, house[name], event_id, RoleOffer.PICKUP, True)
    for _ in range(rng.randint(0, 2)):  # offers while answers are open
        await try_offer(house, rng, rng.choice(people), names)
    house.clock.now = RESPOND_BY
    await house.tick()
    for _ in range(12):
        who = rng.choice(people)
        if rng.random() < 0.5:
            await try_offer(house, rng, who, names)
            continue
        target = rng.choice([*names, "Something New", "something  new"])
        mode = rng.choice([Mode.PICKUP, Mode.DELIVERY])
        thai = house.tags["Thai"].id
        kwargs: dict[str, Any] = (
            {"item_id": house.items[target].id}
            if target in house.items and rng.random() < 0.5
            else {"free_text": target, "cuisine_id": thai}
        )
        with contextlib.suppress(EventRuleViolation):  # refused or not, the property holds
            await act(house, calls.add_proposal, house[who], event_id, mode, **kwargs)
    ctx = await house.context(event_id)
    places = [picks.place(ctx, p) for p in ctx.proposals]
    assert len(places) == len(set(places)), f"seed {seed}: {ctx.proposals}"


async def try_offer(house: House, rng: random.Random, who: str, names: list[str]) -> None:
    item = house.items[rng.choice(names)]
    mode = rng.choice(sorted(item.modes))
    with contextlib.suppress(EventRuleViolation):
        await act(house, offers.post_offer, house[who], offer(house, mode, item_id=item.id))
