# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offers against a migrated database (FR-4, FR-5): seats and capacity, closing at the stated
time, done and logged, feedback, joining an open call as a pick, replacing a call, the input
checks, and every call action an offer refuses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.models import CatalogItemRow, ProposalRow
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import (
    OFFER_JOIN_INSTEAD,
    AssignmentRole,
    Details,
    EventRuleViolation,
    EventState,
    InvalidResponse,
    RoleOffer,
    StaleRun,
    Status,
    Window,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.offers import FULL, OfferPath, offer_path, replaces_call, seats_left, seats_taken
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services import call_admin, calls, event_views, feedback, offers
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from dinnerbell.services.lifecycle import OfferedInstead
from dinnerbell.services.offers import OfferInput
from dinnerbell.services.preferences import set_my_preference
from tests.support.calls import OPENS, RESPOND_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio

MORNING = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
"""10:00 AM in Chicago, before lunch and the dinner call."""
LUNCH_AT = datetime(2026, 10, 22, 17, 30, tzinfo=UTC)
"""12:30 PM, lunch's usual meal time."""
DAY = date(2026, 10, 22)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    clock.now = MORNING
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


def lunch(house: House, **changes: Any) -> OfferInput:
    values: dict[str, Any] = {
        "slot": Slot.LUNCH,
        "day": DAY,
        "mode": Mode.PICKUP,
        "at": time(12, 30),
        "capacity": 3,
        "item_id": house.items["Siam Garden"].id,
    }
    return OfferInput(**(values | changes))


def tacos(house: House, **changes: Any) -> OfferInput:
    values: dict[str, Any] = {
        "slot": Slot.DINNER,
        "day": DAY,
        "mode": Mode.COOK,
        "at": time(18, 30),
        "capacity": 4,
        "item_id": house.items["Sam's Tacos"].id,
    }
    return OfferInput(**(values | changes))


async def post(house: House, who: str, data: OfferInput) -> offers.Posted:
    posted: offers.Posted = await act(house, offers.post_offer, house[who], data)
    return posted


# Domain ----------------------------------------------------------------------------------------


def test_the_path_an_offer_takes() -> None:
    from dinnerbell.domain.events import Event, EventKind

    def call(state: EventState) -> Event:
        return Event(1, 1, EventKind.CALL, Slot.DINNER, DAY, state, 1, MORNING, MORNING, 15, 45)

    assert offer_path(None, Mode.COOK) is OfferPath.NEW
    for state in (EventState.COLLECTING, EventState.VOTING):
        assert offer_path(call(state), Mode.PICKUP) is OfferPath.ATTACH
        assert offer_path(call(state), Mode.COOK) is OfferPath.ATTACH
    assert offer_path(call(EventState.SCHEDULED), Mode.COOK) is OfferPath.REPLACE
    assert offer_path(call(EventState.SCHEDULED), Mode.PICKUP) is OfferPath.NEW
    for state in (EventState.DECIDED, EventState.DONE, EventState.CANCELLED, EventState.ABANDONED):
        assert offer_path(call(state), Mode.COOK) is OfferPath.NEW
    assert not replaces_call(call(EventState.COLLECTING))


# A lunch offer fills, closes, and is done ------------------------------------------------------


async def test_a_lunch_offer_with_capacity_3_fills_and_closes(house: House) -> None:
    """The issue's first rehearsal: a lunch offer with capacity 3 fills and closes."""
    posted = await post(house, "Alex", lunch(house))
    assert posted.path is OfferPath.NEW
    offer_id = posted.event_id
    offer = await house.event(offer_id)
    assert (offer.state, offer.capacity, offer.created_by, offer.meal_time) == (
        EventState.COLLECTING,
        3,
        house["Alex"].id,
        LUNCH_AT,
    )
    ctx = await house.context(offer_id)
    assert [(a.role, a.member_id) for a in ctx.assignments] == [(AssignmentRole.PICKUP, house["Alex"].id)]
    assert ctx.response_of(house["Alex"].id) is not None  # the offerer eats, without taking a seat
    assert seats_taken(offer, ctx.responses) == 0

    seat = await act(house, offers.count_me_in, house["Sam"], offer_id)
    assert (seat.joined, seat.seats_left) == (True, 2)
    seat = await act(house, offers.add_guest, house["Kim"], offer_id)  # Kim and a guest
    assert (seat.joined, seat.guests, seat.seats_left) == (True, 1, 0)
    with pytest.raises(EventRuleViolation, match=FULL):
        await act(house, offers.count_me_in, house["Lee"], offer_id)
    with pytest.raises(EventRuleViolation, match=FULL):
        await act(house, offers.add_guest, house["Sam"], offer_id)
    assert (await house.kinds()).count("offer_full") == 1  # to Alex, once

    seat = await act(house, offers.count_me_in, house["Sam"], offer_id)  # a second tap keeps Sam in (#149)
    assert (seat.joined, seat.seats_left) == (True, 0)
    seat = await act(house, offers.leave, house["Sam"], offer_id)  # Sam drops out
    assert (seat.joined, seat.seats_left) == (False, 1)
    await act(house, offers.join, house["Lee"], offer_id)
    assert (await house.kinds()).count("offer_full") == 1  # full again, but Alex heard already

    house.clock.now = LUNCH_AT
    await house.tick()
    assert (await house.event(offer_id)).state is EventState.DECIDED
    with pytest.raises(EventRuleViolation, match="isn't taking people"):
        await act(house, offers.count_me_in, house["Sam"], offer_id)

    house.clock.now = LUNCH_AT + timedelta(hours=2)
    await house.tick()
    assert (await house.event(offer_id)).state is EventState.DONE
    (meal,) = await house.meals()
    assert (meal.event_id, meal.item_id, meal.slot) == (offer_id, house.items["Siam Garden"].id, "lunch")
    assert sorted(p["name"] for p in meal.participants) == ["Alex", "Kim", "Lee"]
    assert "offer_updated" in await house.kinds()

    # The meal takes feedback like a call's, from whoever ate it (M3a's How was it?).
    form = await act(house, feedback.form_for_event, house["Kim"], offer_id, run=1)
    assert form.facts.item_name == "Siam Garden"
    with pytest.raises(Exception, match="ate"):
        await act(house, feedback.form_for_event, house["Sam"], offer_id, run=1)


async def test_the_offer_full_message_and_posts_go_through_the_outbox(house: House) -> None:
    channel = RecordingChannel()
    offer_id = (await post(house, "Alex", lunch(house, capacity=1))).event_id
    await act(house, offers.join, house["Kim"], offer_id)
    await house.drain(channel)
    kinds = [(s.op, s.kind, s.recipient) for s in channel.sent]
    assert kinds[0] == ("deliver", "offer_posted", None)
    assert ("deliver", "offer_full", house["Alex"].id) in kinds
    assert ("update", "offer_updated", None) in kinds


async def test_the_offerer_can_bring_guests_but_not_drop_out(house: House) -> None:
    offer_id = (await post(house, "Alex", lunch(house, capacity=2))).event_id
    seat = await act(house, offers.add_guest, house["Alex"], offer_id)
    assert (seat.guests, seat.seats_left) == (1, 1)  # Alex's own seat was never spare; the guest's is
    seat = await act(house, offers.count_me_in, house["Alex"], offer_id)  # a tap changes nothing
    assert (seat.joined, seat.guests) == (True, 1)
    with pytest.raises(EventRuleViolation, match="your offer"):
        await act(house, offers.leave, house["Alex"], offer_id)
    assert not any(k == "offer_full" for k in await house.kinds())


async def test_minus_one_guest_keeps_their_needs_until_the_last_one_goes(house: House) -> None:
    """#149: Slack's -1 guest, as lowering the number on the web."""
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    offer_id = (await post(house, "Alex", lunch(house))).event_id
    await act(house, offers.join, house["Sam"], offer_id, guests=2, guest_needs=frozenset({veg.id}))
    seat = await act(house, offers.drop_guest, house["Sam"], offer_id)
    assert (seat.joined, seat.guests, seat.seats_left) == (True, 1, 1)
    mine = (await house.context(offer_id)).response_of(house["Sam"].id)
    assert mine is not None and mine.guest_needs == {veg.id}
    seat = await act(house, offers.drop_guest, house["Sam"], offer_id)
    mine = (await house.context(offer_id)).response_of(house["Sam"].id)
    assert seat.guests == 0 and mine is not None and mine.is_in and mine.guest_needs == frozenset()
    with pytest.raises(EventRuleViolation, match="not bringing any guests"):
        await act(house, offers.drop_guest, house["Sam"], offer_id)
    with pytest.raises(EventRuleViolation, match="not bringing any guests"):
        await act(house, offers.drop_guest, house["Kim"], offer_id, run=1)


async def test_guests_and_their_needs_fill_seats(house: House) -> None:
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    offer_id = (await post(house, "Alex", lunch(house))).event_id
    await act(house, offers.join, house["Sam"], offer_id, guests=2, guest_needs=frozenset({veg.id}))
    ctx = await house.context(offer_id)
    from dinnerbell.services import call_context as cc

    info = cc.offer_info(ctx)
    assert (info.taken, info.seats_left, info.dietary_notes) == (3, 0, ("Vegetarian",))
    # Fewer guests never needs a free seat; more does.
    await act(house, offers.join, house["Sam"], offer_id, guests=1)
    with pytest.raises(EventRuleViolation, match=FULL):
        await act(house, offers.join, house["Sam"], offer_id, guests=3)
    with pytest.raises(InvalidResponse, match="dietary"):
        await act(house, offers.join, house["Kim"], offer_id, guest_needs=frozenset({house.tags["Thai"].id}), guests=1)
    with pytest.raises(InvalidResponse, match="Guests run"):
        await act(house, offers.join, house["Kim"], offer_id, guests=-1)


async def test_a_join_from_an_earlier_run_or_another_event_is_refused(house: House) -> None:
    offer_id = (await post(house, "Alex", lunch(house))).event_id
    with pytest.raises(StaleRun):
        await act(house, offers.join, house["Sam"], offer_id, run=2)
    with pytest.raises(NotFound):
        await act(house, offers.join, house["Sam"], offer_id + 99)
    house.clock.now = OPENS
    await house.tick()
    with pytest.raises(NotFound, match="No such offer"):  # a call isn't an offer
        await act(house, offers.join, house["Sam"], await dinner_id(house))


# Calling it off --------------------------------------------------------------------------------


async def test_only_the_offerer_or_an_admin_calls_it_off(house: House) -> None:
    offer_id = (await post(house, "Sam", lunch(house))).event_id
    await act(house, offers.join, house["Kim"], offer_id)
    with pytest.raises(EventRuleViolation, match="whoever made the offer"):
        await act(house, calls.cancel_call, house["Kim"], offer_id)
    await act(house, calls.cancel_call, house["Sam"], offer_id, note="Siam is closed")
    offer = await house.event(offer_id)
    assert (offer.state, offer.ended_by, offer.end_note) == (EventState.CANCELLED, house["Sam"].id, "Siam is closed")
    entries = await house.outbox()
    cancelled = next(e for e in entries if e.kind == "event_cancelled")
    assert cancelled.thread_key == f"event:{offer_id}:run1:call"  # threads under the offer message
    assert entries[-1].kind in {"offer_updated", "event_cancelled"}
    house.clock.now = LUNCH_AT + timedelta(hours=3)
    await house.tick()
    assert await house.meals() == []  # nothing goes in the meal log

    other = (await post(house, "Sam", lunch(house, day=DAY + timedelta(days=1)))).event_id
    house.clock.now = MORNING
    await act(house, calls.cancel_call, house["Alex"], other)  # an admin may
    assert (await house.event(other)).state is EventState.CANCELLED


async def test_the_offerer_can_call_it_off_after_it_closed(house: House) -> None:
    offer_id = (await post(house, "Sam", lunch(house))).event_id
    house.clock.now = LUNCH_AT
    await house.tick()
    await act(house, calls.cancel_call, house["Sam"], offer_id)
    assert (await house.event(offer_id)).state is EventState.CANCELLED


# What an offer refuses -------------------------------------------------------------------------


async def test_every_call_action_is_refused_on_an_offer(house: House) -> None:
    """An offer has seats, not answers, votes, or windows. Each call action says so instead of
    acting on it."""
    offer_id = (await post(house, "Sam", lunch(house))).event_id
    kim, alex = house["Kim"], house["Alex"]
    refused: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...], dict[str, Any]]] = [
        (calls.respond, (kim, offer_id, Status.IN), {}),
        (calls.offer_role, (kim, offer_id, RoleOffer.COOK, True), {}),
        (calls.toggle_role, (kim, offer_id, RoleOffer.PICKUP), {}),
        (calls.save_details, (kim, offer_id, Details(guests=5)), {}),
        (calls.claim_coordinator, (kim, offer_id), {}),
        (calls.close_window, (alex, offer_id), {}),
        (calls.post_now, (kim, offer_id), {}),
        (call_admin.move_deadline, (alex, offer_id, Window.RESPOND, 15), {}),
        (call_admin.rerun, (alex, offer_id), {}),
        (call_admin.assign_coordinator, (alex, offer_id, kim.id), {}),
        (calls.hand_off, (house["Sam"], offer_id, kim.id), {}),
        (calls.vote, (kim, offer_id, 1), {}),
        (calls.add_proposal, (kim, offer_id, Mode.PICKUP), {"item_id": house.items["Taqueria"].id}),
        (calls.lock, (alex, offer_id, 1), {}),
    ]
    for use_case, args, kwargs in refused:
        with pytest.raises((EventRuleViolation, NotFound)):
            await act(house, use_case, *args, **kwargs)
    with pytest.raises(EventRuleViolation, match=OFFER_JOIN_INSTEAD):
        await act(house, calls.respond, kim, offer_id, Status.IN)
    house.clock.now = LUNCH_AT
    await house.tick()  # now it's on, like a decided call
    with pytest.raises(EventRuleViolation, match="call it off"):
        await act(house, calls.set_meal_time, alex, offer_id, time(13, 0))
    with pytest.raises(EventRuleViolation, match="call it off"):
        await act(house, calls.change_assignment, alex, offer_id, AssignmentRole.PICKUP, kim.id)
    await act(house, calls.cancel_call, alex, offer_id)
    with pytest.raises(EventRuleViolation, match="Only a call"):
        await act(house, calls.restart_call, kim, offer_id)
    ctx = await house.context(offer_id)
    assert [r.member_id for r in ctx.responses] == [house["Sam"].id]  # nobody's answer got in
    view = await act(house, event_views.event_view, kim, offer_id)
    assert view.can == event_views.Permissions()


async def test_what_a_member_may_do_with_an_offer(house: House) -> None:
    offer_id = (await post(house, "Sam", lunch(house, capacity=1))).event_id
    kim = await act(house, event_views.event_view, house["Kim"], offer_id)
    assert (kim.can.join, kim.can.add_guest, kim.can.leave, kim.can.cancel) == (True, True, False, False)
    sam = await act(house, event_views.event_view, house["Sam"], offer_id)
    assert (sam.can.join, sam.can.add_guest, sam.can.leave, sam.can.cancel) == (False, True, False, True)
    await act(house, offers.join, house["Kim"], offer_id)
    kim = await act(house, event_views.event_view, house["Kim"], offer_id)
    assert (kim.can.join, kim.can.add_guest, kim.can.leave) == (False, False, True)  # full: only leaving
    assert kim.offer is not None and kim.offer.full
    alex = await act(house, event_views.event_view, house["Alex"], offer_id)
    assert (alex.can.join, alex.can.cancel, alex.can.rerun, alex.can.move_window) == (False, True, False, None)


# Posting checks --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"capacity": 0}, "Spare servings"),
        ({"capacity": 21}, "Spare servings"),
        ({"at": time(9, 0)}, "still ahead"),
        ({"at": time(23, 50)}, "11:45 PM"),
        ({"day": DAY - timedelta(days=1)}, "today or a later day"),
        ({"day": DAY + timedelta(days=8)}, "7 days ahead"),
        ({"free_text": "Pho"}, "not both"),
        ({"item_id": None}, "not both"),
        ({"item_id": None, "free_text": "Pho Real"}, "cuisine"),
        ({"mode": Mode.DELIVERY, "item_id": "Taqueria"}, "isn't available by delivery"),
        ({"mode": Mode.COOK, "item_id": "Sam's Tacos"}, "cook list"),
        ({"item_id": 999_999}, "isn't in the catalog"),
    ],
)
async def test_an_offer_must_make_sense(house: House, changes: dict[str, Any], message: str) -> None:
    if isinstance(changes.get("item_id"), str):
        changes["item_id"] = house.items[changes["item_id"]].id
    with pytest.raises((EventRuleViolation, InvalidResponse), match=message):
        await post(house, "Kim", lunch(house, **changes))
    assert await house.kinds() == []
    assert await house.audits("event.offered") == []


async def test_something_new_joins_the_catalog_and_a_known_name_reuses_it(house: House, database: Database) -> None:
    thai = house.tags["Thai"].id
    first = await post(house, "Kim", lunch(house, item_id=None, free_text="Pho Real", cuisine_id=thai))
    second = await post(house, "Lee", lunch(house, item_id=None, free_text="pho real"))  # no cuisine needed
    async with database.sessionmaker() as db:
        made = list(await db.scalars(select(CatalogItemRow).where(CatalogItemRow.name == "Pho Real")))
    assert len(made) == 1 and made[0].created_by == house["Kim"].id and made[0].modes == ["pickup"]
    assert made[0].meals == 6  # the offer's meal, lunch, and the default: lunch and dinner (#72)
    assert first.item_name == second.item_name == "Pho Real"
    (created,) = await house.audits("catalog.item_created")
    assert created.payload["from_offer"] is True


async def test_an_inactive_member_cant_offer(house: House) -> None:
    from dataclasses import replace

    with pytest.raises(EventRuleViolation, match="active members"):
        await post_as(house, replace(house["Kim"], active=False), lunch(house))


async def post_as(house: House, member: Any, data: OfferInput) -> Any:
    async with house.sessionmaker() as db:
        return await offers.post_offer(db, member, data, now=house.clock())


# FR-5: offers and calls ------------------------------------------------------------------------


async def test_a_cooking_offer_before_the_call_replaces_that_days_call(house: House) -> None:
    """The issue's second rehearsal: a cooking offer posted before 15:00 replaces the dinner call."""
    offer_id = (await post(house, "Sam", tacos(house))).event_id
    house.clock.now = OPENS
    await house.tick()
    from dinnerbell.db.events import EventRepository

    async with house.sessionmaker() as db:
        assert await EventRepository(db).call_for(house.household.id, Slot.DINNER, DAY) is None
    with pytest.raises(OfferedInstead, match="Sam is cooking dinner"):
        await act(house, calls.start_call, house["Kim"], Slot.DINNER, DAY)
    # A pickup offer for dinner doesn't stand in for the call.
    assert (await post(house, "Lee", lunch(house, slot=Slot.DINNER, at=time(19, 0)))).path is OfferPath.NEW

    # Called off before the response deadline, it frees the slot: the next tick posts the call.
    await act(house, calls.cancel_call, house["Sam"], offer_id)
    house.clock.now = OPENS + timedelta(minutes=1)
    await house.tick()
    assert (await house.event(await dinner_id(house))).state is EventState.COLLECTING


async def test_a_cooking_offer_replaces_a_scheduled_call(house: House) -> None:
    tomorrow = DAY + timedelta(days=1)
    call = await act(house, calls.start_call, house["Kim"], Slot.DINNER, tomorrow)
    assert call.state is EventState.SCHEDULED
    posted = await post(house, "Sam", tacos(house, day=tomorrow))
    assert posted.path is OfferPath.REPLACE
    replaced = await house.event(call.id)
    assert (replaced.state, replaced.ended_by, replaced.end_note) == (
        EventState.CANCELLED,
        house["Sam"].id,
        "Sam is cooking instead.",
    )
    entries = await house.outbox()
    started = [e for e in entries if e.kind == "started_call_cancelled"]
    assert len(started) == 1 and started[0].payload["member"]["id"] == house["Kim"].id
    (audit,) = await house.audits("event.replaced_by_offer")
    assert audit.payload == {"offer_id": posted.event_id, "from": "scheduled"}
    with pytest.raises(OfferedInstead):  # nor can it start again while the offer stands
        await act(house, calls.start_call, house["Lee"], Slot.DINNER, tomorrow)


async def open_dinner(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def test_an_offer_for_an_open_call_becomes_a_pick(house: House) -> None:
    event_id = await open_dinner(house)
    posted = await post(house, "Lee", lunch(house, slot=Slot.DINNER, at=time(18, 30)))
    assert (posted.path, posted.event_id) == (OfferPath.ATTACH, event_id)
    ctx = await house.context(event_id)
    mine = ctx.response_of(house["Lee"].id)
    assert mine is not None and mine.status is Status.IN and RoleOffer.PICKUP in mine.offers
    (pick,) = ctx.proposals
    assert (pick.item_id, pick.proposed_by, pick.reasons) == (
        house.items["Siam Garden"].id,
        house["Lee"].id,
        ("Lee offered to pick it up.",),
    )
    from dinnerbell.services import call_context as cc

    lee = next(e for e in cc.roster(ctx) if e.member.id == house["Lee"].id)
    assert lee.suggestions == ("Siam Garden",)  # shown on the call while answers are open
    with pytest.raises(EventRuleViolation, match="already a proposal"):
        await post(house, "Kim", lunch(house, slot=Slot.DINNER, at=time(18, 30)))

    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.VOTING
    ranks = {(p.item_id, p.mode): p.rank for p in ctx.proposals}
    assert ranks[(house.items["Siam Garden"].id, Mode.PICKUP)] == max(ranks.values())  # after the ranked picks
    assert len(ranks) == len(ctx.proposals)  # never proposed twice
    assert not any(e.kind == "offer_posted" for e in await house.outbox())


async def test_a_pick_from_an_offer_that_someone_later_vetoes_is_dropped(house: House) -> None:
    event_id = await open_dinner(house)
    await post(house, "Lee", lunch(house, slot=Slot.DINNER, at=time(18, 30)))
    async with house.sessionmaker() as db:
        thai = house.tags["Thai"].id
        await set_my_preference(db, house["Kim"], TargetType.TAG, thai, Stance(veto=True), now=house.clock())
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert house.items["Siam Garden"].id not in {p.item_id for p in ctx.proposals}
    (dropped,) = await house.audits("event.pick_dropped")
    assert dropped.payload["item_id"] == house.items["Siam Garden"].id


async def test_a_vetoed_offer_cant_join_the_call(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    async with house.sessionmaker() as db:
        thai = house.tags["Thai"].id
        await set_my_preference(db, house["Kim"], TargetType.TAG, thai, Stance(veto=True), now=house.clock())
    with pytest.raises(EventRuleViolation, match="doesn't work for everyone"):
        await post(house, "Lee", lunch(house, slot=Slot.DINNER, at=time(18, 30)))
    ctx = await house.context(event_id)
    assert ctx.proposals == [] and ctx.response_of(house["Lee"].id) is None  # nothing half-done


async def test_while_voting_only_someone_in_can_add_an_offer(house: House, database: Database) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    noodles = await house.add_item("Noodle Bar", Mode.PICKUP, cuisine="Thai")
    with pytest.raises(EventRuleViolation, match="you're not in it"):
        await post(house, "Kim", lunch(house, slot=Slot.DINNER, at=time(18, 30), item_id=noodles.id))
    before = len((await house.context(event_id)).proposals)
    posted = await post(house, "Sam", lunch(house, slot=Slot.DINNER, at=time(18, 30), item_id=noodles.id))
    assert posted.path is OfferPath.ATTACH
    ctx = await house.context(event_id)
    assert len(ctx.proposals) == before + 1
    sam = ctx.response_of(house["Sam"].id)
    assert sam is not None and RoleOffer.PICKUP in sam.offers
    assert "proposals_updated" in await house.kinds()
    async with database.sessionmaker() as db:
        rows = list(await db.scalars(select(ProposalRow).where(ProposalRow.proposed_by == house["Sam"].id)))
    assert len(rows) == 1


async def test_seats_count_everyone_but_the_offerer() -> None:
    from dinnerbell.domain.events import Event, EventKind, Response

    offer = Event(
        1,
        1,
        EventKind.OFFER,
        Slot.LUNCH,
        DAY,
        EventState.COLLECTING,
        1,
        MORNING,
        LUNCH_AT,
        0,
        0,
        created_by=1,
        capacity=4,
    )
    responses = [Response(1, Status.IN, guests=1), Response(2, Status.IN, guests=2), Response(3, Status.OUT, guests=0)]
    assert seats_taken(offer, responses) == 4 and seats_left(offer, responses) == 0


async def test_an_offer_catches_up_after_downtime(house: House) -> None:
    """Its plan was set when it was posted, so a server down through its time still logs it."""
    offer_id = (await post(house, "Alex", lunch(house))).event_id
    await act(house, offers.join, house["Kim"], offer_id)
    house.clock.now = LUNCH_AT + timedelta(hours=5)
    await house.tick()
    assert (await house.event(offer_id)).state is EventState.DONE
    assert len(await house.meals()) == 1


async def test_a_discarded_offer_leaves_nothing_behind(house: House) -> None:
    from dinnerbell.services import discard

    offer_id = (await post(house, "Alex", lunch(house))).event_id
    await act(house, offers.join, house["Kim"], offer_id)
    await house.drain(RecordingChannel())  # the offer message landed
    async with house.sessionmaker() as db:
        summary = await discard.discard_call(
            db, house.household.id, offer_id, actor=house["Alex"], instance_label="TEST", now=house.clock()
        )
    assert summary.rows["response"] == 2 and summary.rows["assignment"] == 1
    with pytest.raises(NotFound):
        await act(house, offers.join, house["Sam"], offer_id)
    kinds = await house.kinds()
    assert kinds.count("call_discarded") == 1  # the offer message says so


# Races, admin overrides, and limits (review of #67) --------------------------------------------


async def live(house: House, day: date = DAY) -> tuple[bool, bool]:
    """Whether a cooking offer stands, and whether a dinner call is open, for ``day``."""
    from dinnerbell.db.events import EventRepository

    async with house.sessionmaker() as db:
        events = EventRepository(db)
        call = await events.call_for(house.household.id, Slot.DINNER, day)
        offers_ = await events.offers_for(house.household.id, Slot.DINNER, day)
    return any(replaces_call(o) for o in offers_), call is not None and call.state.is_open


@pytest.mark.parametrize("offer_first", [True, False])
async def test_a_cooking_offer_and_the_tick_never_both_win(house: House, offer_first: bool) -> None:
    """The tick checks for a cooking offer and creates the call under the write lock, as
    posting an offer does, so exactly one of them holds the slot: a call with the offer as a
    pick, or the offer with no call."""
    house.clock.now = OPENS
    work = [act(house, offers.post_offer, house["Sam"], tacos(house)), house.tick()]
    await asyncio.gather(*(work if offer_first else reversed(work)))
    offer_live, call_live = await live(house)
    assert offer_live != call_live


@pytest.mark.parametrize("offer_first", [True, False])
async def test_a_cooking_offer_and_starting_a_call_never_both_win(house: House, offer_first: bool) -> None:
    work = [
        act(house, offers.post_offer, house["Sam"], tacos(house)),
        act(house, calls.start_call, house["Kim"], Slot.DINNER, DAY),
    ]
    results = await asyncio.gather(*(work if offer_first else reversed(work)), return_exceptions=True)
    assert all(r is None or not isinstance(r, Exception) or isinstance(r, OfferedInstead) for r in results)
    offer_live, call_live = await live(house)
    assert offer_live != call_live


async def test_an_admin_cant_rerun_or_restart_a_call_an_offer_replaced(house: House) -> None:
    tomorrow = DAY + timedelta(days=1)
    call = await act(house, calls.start_call, house["Kim"], Slot.DINNER, tomorrow)
    await post(house, "Sam", tacos(house, day=tomorrow))
    with pytest.raises(OfferedInstead):
        await act(house, call_admin.rerun, house["Alex"], call.id)
    with pytest.raises(OfferedInstead):
        await act(house, calls.restart_call, house["Kim"], call.id)
    assert (await house.event(call.id)).state is EventState.CANCELLED


async def test_an_admin_cant_give_an_offer_a_coordinator(house: House) -> None:
    offer_id = (await post(house, "Sam", lunch(house))).event_id
    for use_case, args in (
        (call_admin.assign_coordinator, (house["Alex"], offer_id, house["Kim"].id)),
        (call_admin.assign_coordinator, (house["Alex"], offer_id, None)),
        (calls.hand_off, (house["Sam"], offer_id, house["Kim"].id)),
    ):
        with pytest.raises(EventRuleViolation, match="no coordinator"):
            await act(house, use_case, *args)
    assert not any(k == "call_updated" for k in await house.kinds())


async def test_a_pick_whose_offerer_said_out_is_dropped(house: House) -> None:
    event_id = await open_dinner(house)
    await post(house, "Lee", lunch(house, slot=Slot.DINNER, at=time(18, 30)))
    await act(house, calls.respond, house["Lee"], event_id, Status.OUT)  # nobody picks it up now
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert all(p.proposed_by is None for p in ctx.proposals)  # only ranked picks, which Alex can fetch
    assert len(await house.audits("event.pick_dropped")) == 1


async def test_ranked_picks_leave_room_for_attached_ones(house: House, monkeypatch: pytest.MonkeyPatch) -> None:
    from dinnerbell.services import lifecycle

    monkeypatch.setattr(lifecycle, "MAX_PROPOSALS", 2)
    event_id = await open_dinner(house)
    await post(house, "Lee", lunch(house, slot=Slot.DINNER, at=time(18, 30)))
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert len(ctx.proposals) == 2  # one ranked, then the offer's pick
    assert ctx.proposals[-1].proposed_by == house["Lee"].id


async def test_a_member_has_at_most_three_offers_on_per_meal(house: House) -> None:
    for hour in (11, 12, 13):
        await post(house, "Kim", lunch(house, at=time(hour, 0)))
    with pytest.raises(EventRuleViolation, match="3 lunch offers"):
        await post(house, "Kim", lunch(house, at=time(13, 30)))
    await post(house, "Lee", lunch(house, at=time(13, 30)))  # someone else still can
    await post(house, "Kim", lunch(house, slot=Slot.DINNER, at=time(18, 0)))  # and Kim, for dinner


async def test_a_schedule_save_never_opens_a_call_next_to_a_cooking_offer(house: House) -> None:
    """Moving the dinner call earlier than now keeps today's call at the old time, as always; a
    cooking offer stands in for it (FR-5), so at that time the tick calls it off instead of
    posting it."""
    from dataclasses import replace as copy

    from dinnerbell.domain.schedule import DEFAULT_SLOTS, slot_fingerprint
    from dinnerbell.services import schedule as schedule_service

    await post(house, "Sam", tacos(house))
    before = DEFAULT_SLOTS[Slot.DINNER]
    earlier = copy(before, call_time=time(8, 0))
    changed = await act(
        house, schedule_service.update_slot, house["Kim"], Slot.DINNER, earlier, seen=slot_fingerprint(before)
    )
    assert changed
    from dinnerbell.db.events import EventRepository

    async with house.sessionmaker() as db:
        kept = await EventRepository(db).call_for(house.household.id, Slot.DINNER, DAY)
    assert kept is not None and kept.state is EventState.SCHEDULED  # kept at the old time, as for anyone
    house.clock.now = OPENS
    await house.tick()
    offer_live, call_live = await live(house)
    assert offer_live and not call_live
    assert (await house.event(kept.id)).state is EventState.CANCELLED  # the tick called it off, quietly
    (audit,) = await house.audits("event.replaced_by_offer")
    assert audit.payload["from"] == "scheduled" and audit.actor_member_id is None
    assert not any(e.kind == "call_opened" for e in await house.outbox())


async def test_the_tick_calls_off_a_scheduled_call_a_cooking_offer_stands_in_for(house: House) -> None:
    """A backstop: however a call came to be scheduled beside a live cooking offer, the tick
    calls it off quietly when its time comes instead of posting it."""
    from dinnerbell.db.events import EventRepository

    await post(house, "Sam", tacos(house))
    async with house.sessionmaker() as db:
        call = await EventRepository(db).add(
            household_id=house.household.id,
            slot=Slot.DINNER,
            day=DAY,
            state=EventState.SCHEDULED,
            opens_at=OPENS,
            respond_deadline=RESPOND_BY,
            reminder_minutes=15,
            vote_minutes=45,
            created_by=None,
            now=house.clock(),
        )
        await db.commit()
    house.clock.now = OPENS
    await house.tick()
    ended = await house.event(call.id)
    assert (ended.state, ended.ended_by, ended.end_note) == (
        EventState.CANCELLED,
        house["Sam"].id,
        "Sam is cooking instead.",
    )
    assert not any(e.kind == "call_opened" for e in await house.outbox())
    (audit,) = await house.audits("event.replaced_by_offer")
    assert audit.actor_member_id is None
