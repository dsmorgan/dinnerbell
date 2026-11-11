# SPDX-License-Identifier: AGPL-3.0-or-later
"""Takeout gets an orderer and a pickup person (#178).

The rules first, in the domain: the default assignment, eligibility, when the roles or the way
are unsettled, and who may set the way. Then the use cases against a migrated database with a
fake clock: setting the way while voting, the decision's question and its timeout, Assign roles
and Adjust with two roles, switching to delivery, credit when it's done, an admin's correction
after (#162), a pickup offer, and the Slack and web surfaces."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date, time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.messages import render
from dinnerbell.channels.slack.views import adjust_modal, adjust_opened, assign_modal, parse_adjust, parse_assign
from dinnerbell.config import Settings
from dinnerbell.db.contributions import ContributionRepository
from dinnerbell.db.models import AssignmentRow, AuditRow, CatalogItemRow, ProposalRow
from dinnerbell.domain import labels
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import (
    STALE_CALL,
    AssignmentRole,
    Coverage,
    Event,
    EventKind,
    EventRuleViolation,
    EventState,
    Proposal,
    ProposalSource,
    RoleOffer,
    StaleRun,
    Status,
    Step,
    check_can_set_way,
    default_roles,
    roles_unclear,
    takeout_ways,
    way_unsettled,
)
from dinnerbell.domain.guests import GuestAction, check_guest
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls, coordinator, event_views, offers
from dinnerbell.services.coordinator import Adjustment
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.conftest import invite, page, sign_in
from tests.support.calls import OPENS, RESPOND_BY, House, furnish, make_house, nobody_coordinates
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.slack import Slack, wire
from tests.support.web import add_item, htmx, submit
from tests.test_auth_flow import member, rows
from tests.test_coordinator import (
    CTX,
    act,
    add_guest,
    assigned,
    at_deadline,
    open_dinner,
    pick_ids,
    sent,
    vote,
    web_voting,
)
from tests.test_events_web import admin as admin  # the fixture, re-exported for pytest
from tests.test_events_web import proposal_ids as web_proposal_ids
from tests.test_events_web import sign_in_as, start
from tests.test_events_web import tick as web_tick
from tests.test_picks import offer as picks_offer
from tests.test_picks import to_voting as picks_voting

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


ORDER, PICKUP, COOK, LEAD = AssignmentRole.ORDER, AssignmentRole.PICKUP, AssignmentRole.COOK, AssignmentRole.COORDINATOR


# The rules, in the domain ---------------------------------------------------------------------------


def test_pickup_goes_to_different_people_by_default() -> None:
    covered = Coverage(pickups=(1, 2), orderers=(1, 3, 4), order_offers=(1, 3))
    assert default_roles(Mode.PICKUP, covered) == {ORDER: 3, PICKUP: 1}  # the first other with I'll order on
    assert default_roles(Mode.DELIVERY, covered) == {ORDER: 1}
    assert default_roles(Mode.PICKUP, Coverage(pickups=(1,), orderers=(1,), order_offers=(1,))) == {ORDER: 1, PICKUP: 1}
    # 4 usually orders but turned I'll order off: a delivery's orderer, not a pickup's.
    usual = Coverage(pickups=(1,), orderers=(4,))
    assert default_roles(Mode.PICKUP, usual) == {ORDER: 1, PICKUP: 1}
    assert default_roles(Mode.DELIVERY, usual) == {ORDER: 4}
    assert default_roles(Mode.PICKUP, Coverage(pickups=(1,))) == {ORDER: 1, PICKUP: 1}  # nobody else: they order too
    assert default_roles(Mode.PICKUP, Coverage(order_offers=(3,))) == {ORDER: 3}  # nobody to pick up
    assert default_roles(Mode.COOK, Coverage(cooks=(5, 6)), frozenset({6})) == {COOK: 6}
    assert default_roles(Mode.DELIVERY, Coverage()) == {}


def test_choices_win_over_the_defaults_and_one_person_can_do_both() -> None:
    covered = Coverage(pickups=(1, 2), orderers=(1, 3), order_offers=(1, 3))
    assert default_roles(Mode.PICKUP, covered, pickup=2) == {ORDER: 1, PICKUP: 2}
    assert default_roles(Mode.PICKUP, covered, lead=2) == {ORDER: 2, PICKUP: 1}
    assert default_roles(Mode.PICKUP, covered, lead=9, pickup=9) == {ORDER: 9, PICKUP: 9}
    assert default_roles(Mode.DELIVERY, covered, lead=9, pickup=2) == {ORDER: 9}  # nobody picks up a delivery


def test_a_pickup_volunteer_is_still_all_a_pickup_needs() -> None:
    nobody = frozenset[int]()
    assert Coverage(pickups=(1,), orderers=(2,)).covers(Mode.PICKUP, nobody)
    assert Coverage(pickups=(1,)).covers(Mode.PICKUP, nobody)  # they order it too
    assert not Coverage(orderers=(2,)).covers(Mode.PICKUP, nobody)
    assert Coverage(orderers=(2,)).covers(Mode.DELIVERY, nobody)


def test_when_the_takeout_roles_have_no_clear_owner() -> None:
    assert not roles_unclear(Mode.PICKUP, Coverage(pickups=(1,), order_offers=(1,)))  # they do both
    assert not roles_unclear(Mode.PICKUP, Coverage(pickups=(1,), order_offers=(1, 2)))  # one other orderer
    assert roles_unclear(Mode.PICKUP, Coverage(pickups=(1,), order_offers=(1, 2, 3)))  # two others offered
    assert not roles_unclear(Mode.PICKUP, Coverage(pickups=(1,), orderers=(2, 3)))  # usually ordering doesn't count
    assert roles_unclear(Mode.PICKUP, Coverage(pickups=(1, 2), order_offers=(3,)))  # two could pick up
    assert roles_unclear(Mode.PICKUP, Coverage(orderers=(3,)))
    assert not roles_unclear(Mode.DELIVERY, Coverage(orderers=(3,)))
    assert roles_unclear(Mode.DELIVERY, Coverage(orderers=(3, 4)))
    assert not roles_unclear(Mode.COOK, Coverage(cooks=(5,)))


def test_the_way_is_open_only_at_a_place_with_both_until_someone_sets_it() -> None:
    pick = Proposal(1, 1, Mode.PICKUP, 0, ProposalSource.SYSTEM, item_id=3)
    both = frozenset({Mode.PICKUP, Mode.DELIVERY})
    assert takeout_ways(both) == (Mode.PICKUP, Mode.DELIVERY)
    assert takeout_ways({Mode.PICKUP}) == () and takeout_ways({Mode.COOK}) == ()
    assert way_unsettled(pick, both)
    assert not way_unsettled(replace(pick, mode_chosen=True), both)
    assert not way_unsettled(pick, {Mode.PICKUP})


def _event(state: EventState, coordinator_id: int | None = 2, kind: EventKind = EventKind.CALL) -> Event:
    return Event(1, 1, kind, Slot.DINNER, date(2026, 10, 22), state, 1, OPENS, RESPOND_BY, 15, 45, None, coordinator_id)


def _person(member_id: int, role: Role = Role.MEMBER, *, active: bool = True) -> Member:
    return Member(member_id, 1, f"P{member_id}", None, role, Capabilities(), active)


def test_who_may_set_the_way() -> None:
    voting = _event(EventState.VOTING)
    check_can_set_way(voting, _person(2))  # the coordinator
    check_can_set_way(voting, _person(1, Role.ADMIN))  # any admin, as for roles (FR-61)
    for event, actor, code in [
        (voting, _person(3), "not_coordinator"),
        (_event(EventState.DECIDED), _person(2), "voting_closed"),
        (_event(EventState.COLLECTING), _person(2), "voting_closed"),
        (voting, _person(2, active=False), "inactive"),
        (_event(EventState.COLLECTING, kind=EventKind.OFFER), _person(2), "is_offer"),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            check_can_set_way(event, actor)
        assert refused.value.code == code
    with pytest.raises(EventRuleViolation):
        check_guest(voting, _person(9, Role.GUEST), GuestAction.COORDINATE, invited=True)


def test_what_the_coordinator_is_asked_to_choose() -> None:
    assert labels.roles_to_choose(Mode.COOK, way_open=False) == "who cooks"
    assert labels.roles_to_choose(Mode.DELIVERY, way_open=False) == "who orders"
    assert labels.roles_to_choose(Mode.PICKUP, way_open=False) == "who orders and who picks it up"
    assert labels.roles_to_choose(Mode.PICKUP, way_open=True).startswith("pickup or delivery")


def test_way_values_say_the_way_the_call_the_run_and_the_pick() -> None:
    value = ids.way_value(Mode.DELIVERY, 12, 3, 45)
    assert value == "delivery:12:3:45" and ids.parse_way(value) == (Mode.DELIVERY, 12, 3, 45, None)
    either = ids.way_value(None, 12, 3, 45, clears=Mode.PICKUP)  # clears pickup (#212)
    assert either == "either-pickup:12:3:45" and ids.parse_way(either) == (None, 12, 3, 45, Mode.PICKUP)
    with pytest.raises(ValueError, match="names the way it clears"):
        ids.way_value(None, 12, 3, 45)
    bad_values = (
        "cook:12:3:45",
        "delivery:12:3",
        "delivery:0:3:45",
        7,
        "pickup:1:1:1:1",
        "either:1:1:1",  # a clear names the way it clears
        "either-cook:1:1:1",
    )
    for bad in bad_values:
        with pytest.raises(ids.BadValue):
            ids.parse_way(bad)


def test_an_adjust_modal_keeps_the_pickup_person_and_reads_one_from_before_without() -> None:
    opened = ids.Opened(time(18, 30), 3, 2, Mode.PICKUP, 4)
    assert opened.data()["pick"] == 4 and ids.Opened.parse(opened.data()) == opened
    old = {"at": "18:30", "who": 4, "lead": 2, "how": "pickup"}  # opened before the upgrade
    assert ids.Opened.parse(old) == ids.Opened(time(18, 30), 4, 2, Mode.PICKUP, None)
    with pytest.raises(ids.BadValue):
        ids.Opened.parse(old | {"pick": "4"})


# Against the database --------------------------------------------------------------------------------


async def to_voting(
    house: House,
    *eaters: str,
    pickups: tuple[str, ...] = ("Alex",),
    orders: tuple[str, ...] = (),
    coordinator_name: str | None = "Alex",
) -> int:
    """``eaters`` are in (Alex always is); ``pickups`` offer to pick up, ``orders`` to order. Lee
    can order whenever they're in. Siam Garden allows pickup and delivery; Taqueria, pickup only."""
    event_id = await open_dinner(house)
    for name in dict.fromkeys(("Alex", *eaters)):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    for name in pickups:
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.PICKUP, True)
    for name in orders:
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.ORDER, True)
    if coordinator_name is not None:
        await act(house, calls.claim_coordinator, house[coordinator_name], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    if coordinator_name is None:
        await nobody_coordinates(house, event_id)  # voting picked someone (#179)
    return event_id


async def decide_on(house: House, event_id: int, place: str) -> None:
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))[place])
    await at_deadline(house, event_id)


async def test_a_pickup_is_ordered_by_someone_else_when_someone_can(house: House) -> None:
    event_id = await to_voting(house, "Lee", coordinator_name=None)
    await decide_on(house, event_id, "Taqueria")
    ctx = await house.context(event_id)
    assert assigned(ctx) == {ORDER: house["Lee"].id, PICKUP: house["Alex"].id}
    assert {n.member.id for n in await sent(house, "assignment_made")} == {house["Lee"].id, house["Alex"].id}
    decision = cc.decision_info(ctx)
    assert decision is not None and [a.role for a in decision.assignments] == [ORDER, PICKUP]
    text = json.dumps(render((await sent(house, "decision_made"))[0], CTX).payload())
    assert "Ordering:" in text and "Picking up:" in text


async def test_with_nobody_else_who_can_order_the_pickup_person_orders_too(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)  # nobody who's in can order
    await decide_on(house, event_id, "Taqueria")
    assert assigned(await house.context(event_id)) == {ORDER: house["Alex"].id, PICKUP: house["Alex"].id}
    (told,) = await sent(house, "assignment_made")  # one message, naming both
    assert told.member.id == house["Alex"].id and set(told.roles) == {ORDER, PICKUP}
    assert "picking up and ordering" in json.dumps(render(told, CTX).payload())


async def test_an_open_way_asks_the_coordinator_even_when_the_roles_are_clear(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    await decide_on(house, event_id, "Siam Garden")
    event = await house.event(event_id)
    assert event.roles_due_at is not None
    assert assigned(await house.context(event_id)) == {LEAD: house["Alex"].id}  # the roles wait
    (ask,) = await sent(house, "roles_to_choose")
    text = json.dumps(render(ask, CTX).payload())
    assert "pickup or delivery, who orders, and who picks it up" in text and "it stays pickup" in text
    (made,) = await sent(house, "decision_made")
    assert "is choosing pickup or delivery" in json.dumps(render(made, CTX).payload())


async def test_a_place_with_one_way_and_clear_roles_asks_nothing(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    await decide_on(house, event_id, "Taqueria")
    assert (await house.event(event_id)).roles_due_at is None
    assert await sent(house, "roles_to_choose") == []
    assert assigned(await house.context(event_id)) == {
        ORDER: house["Lee"].id,
        PICKUP: house["Alex"].id,
        LEAD: house["Alex"].id,
    }


async def test_unclear_ordering_on_a_pickup_asks_the_coordinator(house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam", orders=("Sam",))  # Sam and Lee could both order
    await decide_on(house, event_id, "Taqueria")
    assert (await house.event(event_id)).roles_due_at is not None
    (ask,) = await sent(house, "roles_to_choose")
    assert "who orders and who picks it up" in json.dumps(render(ask, CTX).payload())


async def test_setting_the_way_while_voting_settles_it(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await act(house, coordinator.set_way, house["Alex"], event_id, siam, Mode.PICKUP, run=1)  # the way it has
    ctx = await house.context(event_id)
    pick = ctx.proposal(siam)
    assert pick is not None and pick.mode is Mode.PICKUP and pick.mode_chosen
    info = cc.proposal_info(ctx, pick)
    assert info.ways == (Mode.PICKUP, Mode.DELIVERY) and not info.way_open
    (audit,) = [a for a in await house.audits() if a.action == "event.way_set"]
    assert "pickup" in str(audit.payload)
    updated = (await sent(house, "proposals_updated"))[-1]
    assert "(chosen)" in json.dumps(render(updated, CTX).payload())
    await decide_on(house, event_id, "Siam Garden")
    assert (await house.event(event_id)).roles_due_at is None  # nothing left to ask
    assert assigned(await house.context(event_id))[ORDER] == house["Lee"].id


async def test_switching_a_pick_to_delivery_while_voting(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await act(house, coordinator.set_way, house["Alex"], event_id, siam, Mode.DELIVERY, run=1)
    await decide_on(house, event_id, "Siam Garden")
    event = await house.event(event_id)
    assert event.decided_mode is Mode.DELIVERY and event.roles_due_at is None
    assert assigned(await house.context(event_id)) == {ORDER: house["Lee"].id, LEAD: house["Alex"].id}


async def test_setting_the_way_is_refused_with_a_reason(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    for who, pick, mode, code in [
        ("Lee", picks["Siam Garden"], Mode.DELIVERY, "not_coordinator"),
        ("Sam", picks["Taqueria"], Mode.DELIVERY, "not_both_ways"),
        ("Sam", picks["Siam Garden"], Mode.COOK, "not_both_ways"),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            await act(house, coordinator.set_way, house[who], event_id, pick, mode, run=1)
        assert refused.value.code == code, who
    with pytest.raises(StaleRun):
        await act(house, coordinator.set_way, house["Sam"], event_id, picks["Siam Garden"], Mode.DELIVERY, run=2)
    await act(
        house, coordinator.set_way, house["Alex"], event_id, picks["Siam Garden"], Mode.DELIVERY, run=1
    )  # an admin
    await act(house, calls.lock, house["Alex"], event_id, picks["Taqueria"])
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.set_way, house["Sam"], event_id, picks["Siam Garden"], Mode.PICKUP, run=1)
    assert refused.value.code == "voting_closed"


async def test_a_way_nobody_who_is_in_can_do_is_refused(house: House) -> None:
    event_id = await to_voting(house, "Sam")  # Alex picks up; nobody who's in can order a delivery
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.set_way, house["Alex"], event_id, siam, Mode.DELIVERY, run=1)
    assert refused.value.code == "no_coverage" and "order" in str(refused.value)
    pick = (await house.context(event_id)).proposal(siam)
    assert pick is not None and pick.mode is Mode.PICKUP and not pick.mode_chosen


async def test_the_coordinator_assigns_the_orderer_and_the_pickup_person(house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam", "Kim")
    await decide_on(house, event_id, "Siam Garden")
    await act(
        house,
        coordinator.choose_roles,
        house["Alex"],
        event_id,
        mode=Mode.PICKUP,
        member_id=house["Kim"].id,
        pickup_id=house["Sam"].id,
        run=1,
    )
    ctx = await house.context(event_id)
    assert assigned(ctx) == {ORDER: house["Kim"].id, PICKUP: house["Sam"].id, LEAD: house["Alex"].id}
    told = {n.member.id: n for n in await sent(house, "assignment_made")}
    assert set(told) == {house["Kim"].id, house["Sam"].id}
    assert all(n.assigned_by is not None for n in told.values())  # neither offered
    (audit,) = [a for a in await house.audits() if a.action == "event.roles_assigned"]
    assert "'pickup': " in str(audit.payload) or '"pickup":' in str(audit.payload)


async def test_one_person_can_take_every_role(house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam")
    await decide_on(house, event_id, "Taqueria")
    await act(
        house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Alex"].id, pickup_id=house["Alex"].id
    )
    assert assigned(await house.context(event_id)) == {
        ORDER: house["Alex"].id,
        PICKUP: house["Alex"].id,
        LEAD: house["Alex"].id,
    }


async def test_switching_to_delivery_drops_the_pickup_person_who_is_told(house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam", pickups=("Sam",))
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await act(house, coordinator.set_way, house["Alex"], event_id, siam, Mode.PICKUP, run=1)
    await decide_on(house, event_id, "Siam Garden")
    assert assigned(await house.context(event_id)) == {
        ORDER: house["Lee"].id,
        PICKUP: house["Sam"].id,
        LEAD: house["Alex"].id,
    }
    await act(house, coordinator.choose_roles, house["Alex"], event_id, mode=Mode.DELIVERY, member_id=house["Lee"].id)
    ctx = await house.context(event_id)
    assert ctx.event.decided_mode is Mode.DELIVERY
    assert assigned(ctx) == {ORDER: house["Lee"].id, LEAD: house["Alex"].id}  # the orderer stays
    (ended,) = await sent(house, "assignment_ended")
    assert ended.member.id == house["Sam"].id and ended.roles == (PICKUP,)
    assert "no longer picking up" in json.dumps(render(ended, CTX).payload())
    # Done: Lee ordered, and Sam gets no pickup credit.
    await act(house, calls.mark_done, house["Alex"], event_id)
    async with house.sessionmaker() as db:
        counts = ContributionRepository(db)
        assert await counts.credited(house.household.id, event_id, Kind.ORDERED) == {house["Lee"].id}
        assert await counts.credited(house.household.id, event_id, Kind.PICKED_UP) == set()


async def test_ordering_and_picking_up_count_separately_when_done(house: House) -> None:
    event_id = await to_voting(house, "Lee", coordinator_name=None)
    await decide_on(house, event_id, "Taqueria")
    await act(house, calls.mark_done, house["Alex"], event_id)
    async with house.sessionmaker() as db:
        counts = ContributionRepository(db)
        assert await counts.credited(house.household.id, event_id, Kind.ORDERED) == {house["Lee"].id}
        assert await counts.credited(house.household.id, event_id, Kind.PICKED_UP) == {house["Alex"].id}


async def test_an_admin_corrects_the_orderer_after_the_meal_and_the_count_moves(house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam", coordinator_name=None)
    await decide_on(house, event_id, "Taqueria")
    await act(house, calls.mark_done, house["Alex"], event_id)
    await act(house, calls.change_assignment, house["Alex"], event_id, ORDER, house["Sam"].id)
    await act(house, calls.change_assignment, house["Alex"], event_id, PICKUP, house["Lee"].id)
    async with house.sessionmaker() as db:
        counts = ContributionRepository(db)
        assert await counts.credited(house.household.id, event_id, Kind.ORDERED) == {house["Sam"].id}
        assert await counts.credited(house.household.id, event_id, Kind.PICKED_UP) == {house["Lee"].id}
    kinds = [str(a.payload) for a in await house.audits() if a.action == "event.contributions_recounted"]
    assert len(kinds) == 2 and "ordered" in kinds[0] and "picked_up" in kinds[1]
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, calls.change_assignment, house["Alex"], event_id, COOK, house["Sam"].id)
    assert refused.value.code == "wrong_role"


async def test_the_timeout_gives_both_roles_and_tells_each_person_once(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    await decide_on(house, event_id, "Siam Garden")
    due = (await house.event(event_id)).roles_due_at
    assert due is not None
    house.clock.now = due
    assert Step.ASSIGN_ROLES in await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.decided_mode is Mode.PICKUP  # it stays the way it was
    assert assigned(ctx) == {ORDER: house["Lee"].id, PICKUP: house["Alex"].id, LEAD: house["Alex"].id}
    assert sorted(n.member.id for n in await sent(house, "assignment_made")) == sorted(
        [house["Lee"].id, house["Alex"].id]
    )
    await house.tick()
    assert len(await sent(house, "assignment_made")) == 2


async def test_marking_done_while_the_coordinator_chooses_gives_both_roles(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    await decide_on(house, event_id, "Siam Garden")
    await act(house, calls.mark_done, house["Alex"], event_id)
    async with house.sessionmaker() as db:
        counts = ContributionRepository(db)
        assert await counts.credited(house.household.id, event_id, Kind.ORDERED) == {house["Lee"].id}
        assert await counts.credited(house.household.id, event_id, Kind.PICKED_UP) == {house["Alex"].id}


async def test_a_pickup_offer_makes_the_offerer_order_and_pick_up(house: House) -> None:
    form = offers.OfferInput(
        slot=Slot.DINNER,
        day=date(2026, 10, 22),
        mode=Mode.PICKUP,
        at=time(19),
        capacity=2,
        item_id=house.items["Taqueria"].id,
    )
    posted = await act(house, offers.post_offer, house["Sam"], form)
    assert assigned(await house.context(posted.event_id)) == {ORDER: house["Sam"].id, PICKUP: house["Sam"].id}


# Adjust (spec 5.7) --------------------------------------------------------------------------------------


async def decided_pickup(house: House) -> int:
    """Siam Garden, set to pickup while voting: Lee orders, Sam picks up, Alex coordinates."""
    event_id = await to_voting(house, "Lee", "Sam", "Kim", pickups=("Sam",))
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await act(house, coordinator.set_way, house["Alex"], event_id, siam, Mode.PICKUP, run=1)
    await decide_on(house, event_id, "Siam Garden")
    return event_id


async def opened(house: House, event_id: int) -> Adjustment:
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    shown = adjust_opened(view)
    return Adjustment(shown.meal_time, shown.assignee_id, shown.coordinator_id, shown.mode, shown.pickup_id)


async def test_adjust_opens_with_both_roles_and_changes_the_pickup_person_alone(house: House) -> None:
    event_id = await decided_pickup(house)
    before = await opened(house, event_id)
    assert (before.assignee_id, before.pickup_id, before.mode) == (house["Lee"].id, house["Sam"].id, Mode.PICKUP)
    changed = await act(
        house,
        coordinator.adjust,
        house["Alex"],
        event_id,
        replace(before, pickup_id=house["Kim"].id),
        opened=before,
        run=1,
    )
    assert changed.pickup_id == house["Kim"].id
    assert assigned(await house.context(event_id)) == {
        ORDER: house["Lee"].id,
        PICKUP: house["Kim"].id,
        LEAD: house["Alex"].id,
    }
    (ended,) = await sent(house, "assignment_ended")
    assert ended.member.id == house["Sam"].id


async def test_adjust_to_delivery_keeps_the_orderer(house: House) -> None:
    event_id = await decided_pickup(house)
    before = await opened(house, event_id)
    await act(
        house, coordinator.adjust, house["Alex"], event_id, replace(before, mode=Mode.DELIVERY), opened=before, run=1
    )
    ctx = await house.context(event_id)
    assert ctx.event.decided_mode is Mode.DELIVERY
    assert assigned(ctx) == {ORDER: house["Lee"].id, LEAD: house["Alex"].id}


async def test_an_adjust_opened_before_the_pickup_person_showed_cannot_change_the_roles(house: House) -> None:
    event_id = await decided_pickup(house)
    before = replace(await opened(house, event_id), pickup_id=None)  # an old modal: no "pick"
    with pytest.raises(coordinator.AdjustmentRefused) as refused:
        await act(
            house,
            coordinator.adjust,
            house["Alex"],
            event_id,
            replace(before, assignee_id=house["Kim"].id),
            opened=before,
            run=1,
        )
    assert refused.value.code == "plan_changed"
    later = replace(before, meal_time=time(19, 30))  # the time still saves
    assert (
        await act(house, coordinator.adjust, house["Alex"], event_id, later, opened=before, run=1)
    ).meal_time == time(19, 30)


async def test_a_pickup_person_who_is_not_in_is_refused_beside_their_field(house: House) -> None:
    event_id = await decided_pickup(house)
    before = await opened(house, event_id)
    outsider = await add_guest(house, "Pat")  # not invited, not in
    wanted = replace(before, pickup_id=outsider.id)
    async with house.sessionmaker() as db:
        with pytest.raises(coordinator.AdjustmentRefused) as refused:
            await coordinator.plan_adjustment(db, house["Alex"], event_id, wanted, opened=before, run=1)
    assert refused.value.part == "pickup"


# Slack ---------------------------------------------------------------------------------------------------


async def test_the_modals_ask_who_orders_and_who_picks_up(house: House) -> None:
    event_id = await decided_pickup(house)
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    origin = ids.ViewOrigin(event_id, "C1", run=1)
    blocks = {b.get("block_id"): b for b in assign_modal(view, origin)["blocks"]}
    assert blocks["who"]["label"]["text"] == "Who orders" and blocks["pickup"]["label"]["text"] == "Who picks up"
    assert blocks["who"]["element"]["initial_option"]["value"] == str(house["Lee"].id)
    assert blocks["pickup"]["element"]["initial_option"]["value"] == str(house["Sam"].id)
    values = {
        "mode": {"value": {"selected_option": {"value": "pickup"}}},
        "who": {"value": {"selected_option": {"value": str(house["Kim"].id)}}},
        "pickup": {"value": {"selected_option": {"value": str(house["Kim"].id)}}},
    }
    form = parse_assign(values)
    assert (form.mode, form.member_id, form.pickup_id) == (Mode.PICKUP, house["Kim"].id, house["Kim"].id)
    adjust_origin = replace(origin, opened=adjust_opened(view))
    adjust_blocks = {b.get("block_id"): b for b in adjust_modal(view, adjust_origin)["blocks"]}
    assert {"who", "pickup", "mode"} <= set(adjust_blocks)
    assert adjust_blocks["pickup"]["element"]["initial_option"]["value"] == str(house["Sam"].id)
    parsed = parse_adjust(
        values
        | {
            "meal_time": {"value": {"selected_time": "19:00"}},
            "coordinator": {"value": {"selected_option": {"value": "1"}}},
        }
    )
    assert parsed.pickup_id == house["Kim"].id


async def test_the_proposals_message_offers_the_way_beside_a_place_with_both(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    (ready,) = await sent(house, "proposals_ready")
    payload = render(ready, CTX).payload()
    picks = await pick_ids(house, event_id)
    rows = {b["block_id"]: b for b in payload["blocks"] if b.get("block_id", "").startswith("proposal-actions-")}
    siam_menu = [
        e for e in rows[f"proposal-actions-{picks['Siam Garden']}"]["elements"] if e["action_id"] == ids.PICK_MENU
    ]
    assert len(siam_menu) == 1
    assert [o["value"] for o in siam_menu[0]["options"]] == [
        ids.pick_value(ids.PickChoice.LOCK, event_id, 1, picks["Siam Garden"]),
        ids.way_value(Mode.PICKUP, event_id, 1, picks["Siam Garden"]),
        ids.way_value(Mode.DELIVERY, event_id, 1, picks["Siam Garden"]),
    ]
    (taqueria_menu,) = [
        e for e in rows[f"proposal-actions-{picks['Taqueria']}"]["elements"] if e["action_id"] == ids.PICK_MENU
    ]
    assert not any(ids.is_way_value(o["value"]) for o in taqueria_menu["options"])
    assert "pickup or delivery with ⋯" in json.dumps(payload, ensure_ascii=False)


# The web -------------------------------------------------------------------------------------------------


def test_web_sets_the_way_while_voting_and_assigns_both_roles(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    add_item(admin, "Pho Place", cuisines="Vietnamese", modes=["pickup", "delivery"])
    invite(admin, "kim@example.com", "Kim")
    event_id = start(admin)
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    sign_in_as(admin, provider, "kim@example.com")  # not an admin
    assert submit(admin, f"/events/{event_id}/coordinate", {}).status_code == 303
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    (item,) = [i for i in rows(migrated, CatalogItemRow) if i.name == "Pho Place"]
    (pho,) = [p.id for p in rows(migrated, ProposalRow) if p.item_id == item.id]
    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="/events/{event_id}/proposals/{pho}/way"' in html and "Not chosen yet" in html
    assert submit(admin, f"/events/{event_id}/proposals/{pho}/way", {"mode": "pickup"}).status_code == 400  # no run
    bad = submit(admin, f"/events/{event_id}/proposals/{pho}/way", {"mode": "cook", "run": "1"})
    assert bad.status_code == 409 and "one way" in page(bad)
    assert submit(admin, f"/events/{event_id}/proposals/{pho}/way", {"mode": "pickup", "run": "1"}).status_code == 303
    (row,) = [p for p in rows(migrated, ProposalRow) if p.id == pho]
    assert (row.mode, row.mode_chosen) == ("pickup", True)
    assert submit(admin, f"/events/{event_id}/proposals/{pho}/lock", {}).status_code == 303
    kim = member(migrated, "kim@example.com")
    alex = member(migrated, "admin@example.com")
    html = page(admin.get(f"/events/{event_id}"))
    assert "Picking up: " in html and "Ordering: " in html
    assert "<span>Who orders</span>" in html and "<span>Who picks up (pickup only)</span>" in html
    assert '<option value="order">Ordering</option><option value="pickup">Picking up</option>' in html
    form = {"mode": "pickup", "member_id": str(kim.id), "pickup_id": str(alex.id), "run": "1"}
    assert submit(admin, f"/events/{event_id}/roles", form).status_code == 303
    assert sorted((a.role, a.member_id) for a in rows(migrated, AssignmentRow) if a.role != "coordinator") == [
        ("order", kim.id),
        ("pickup", alex.id),
    ]


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def test_slack_sets_the_way_from_the_menu_beside_a_pick(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    delivery = ids.way_value(Mode.DELIVERY, event_id, 1, siam)
    await slack.click("Lee", ids.SET_WAY, None, selected=delivery)
    assert slack.ephemerals("Lee")[-1] == "Only the coordinator or an admin can choose pickup or delivery."
    await slack.click("Sam", ids.SET_WAY, None, selected=ids.way_value(Mode.DELIVERY, event_id, 2, siam))
    assert slack.ephemerals("Sam")[-1] == STALE_CALL
    await slack.click("Sam", ids.SET_WAY, None, selected=delivery)
    assert slack.ephemerals("Sam")[-1] == "It's delivery if it wins."
    pick = (await house.context(event_id)).proposal(siam)
    assert pick is not None and (pick.mode, pick.mode_chosen) == (Mode.DELIVERY, True)
    await slack.click("Alex", ids.SET_WAY, None, selected=ids.way_value(Mode.PICKUP, event_id, 1, siam))  # an admin
    assert slack.ephemerals("Alex")[-1] == "It's pickup if it wins."


async def test_a_pickup_with_nobody_to_pick_it_up_asks_who_will(house: House) -> None:
    event_id = await to_voting(house, "Lee", pickups=(), coordinator_name="Alex")  # Lee can order: delivery only
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    with pytest.raises(NotFound):
        await act(house, coordinator.set_way, house["Alex"], event_id, 999_999, Mode.PICKUP, run=1)
    await act(house, calls.lock, house["Alex"], event_id, siam)
    assert (await house.event(event_id)).decided_mode is Mode.DELIVERY
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.choose_roles, house["Alex"], event_id, mode=Mode.PICKUP)
    assert (refused.value.code, str(refused.value)) == ("no_one_chosen", "Pick who will pick it up.")
    await act(house, coordinator.choose_roles, house["Alex"], event_id, mode=Mode.PICKUP, pickup_id=house["Lee"].id)
    assert assigned(await house.context(event_id)) == {
        ORDER: house["Lee"].id,
        PICKUP: house["Lee"].id,
        LEAD: house["Alex"].id,
    }


async def test_a_pick_someone_added_keeps_the_way_they_chose(house: House) -> None:
    event_id = await to_voting(house, "Lee")
    pho = await house.add_item("Pho Place", Mode.PICKUP, Mode.DELIVERY, cuisine="Vietnamese")  # not drawn
    added = await act(house, calls.add_proposal, house["Alex"], event_id, Mode.PICKUP, item_id=pho.id)
    assert added.mode_chosen
    siam = (await house.context(event_id)).proposal((await pick_ids(house, event_id))["Siam Garden"])
    assert siam is not None and not siam.mode_chosen  # drawn: nobody chose its way
    await act(house, calls.lock, house["Alex"], event_id, added.id)
    assert (await house.event(event_id)).roles_due_at is None  # nothing to ask
    assert assigned(await house.context(event_id)) == {
        ORDER: house["Lee"].id,
        PICKUP: house["Alex"].id,
        LEAD: house["Alex"].id,
    }


async def test_someone_who_turned_ill_order_off_orders_a_delivery_but_not_a_pickup(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.offer_role, house["Lee"], event_id, RoleOffer.ORDER, False)  # Lee usually orders
    ctx = await house.context(event_id)
    assert default_roles(Mode.DELIVERY, ctx.coverage) == {ORDER: house["Lee"].id}  # #190's fallback
    assert default_roles(Mode.PICKUP, ctx.coverage) == {ORDER: house["Alex"].id, PICKUP: house["Alex"].id}
    assert not roles_unclear(Mode.PICKUP, ctx.coverage)


# Review follow-ups --------------------------------------------------------------------------------------


@pytest.mark.parametrize("chosen", ["order", "pickup"])
async def test_setting_one_role_while_the_coordinator_chooses_fills_the_others(house: House, chosen: str) -> None:
    """On the web, the coordinator sets only one of a pickup's roles while choosing: the other goes
    to its first volunteer at once, and each person is told once, as when the time runs out."""
    event_id = await to_voting(house, "Lee", "Kim")
    await decide_on(house, event_id, "Siam Garden")  # the way is open, so the coordinator is choosing
    due = (await house.event(event_id)).roles_due_at
    assert due is not None
    role = AssignmentRole(chosen)
    await act(house, calls.change_assignment, house["Alex"], event_id, role, house["Kim"].id)
    expected = {ORDER: house["Lee"].id, PICKUP: house["Alex"].id, LEAD: house["Alex"].id} | {role: house["Kim"].id}
    assert assigned(await house.context(event_id)) == expected
    assert (await house.event(event_id)).roles_due_at is None
    (audit,) = [a for a in await house.audits() if a.action == "event.assignment_changed"]
    other = PICKUP if role is ORDER else ORDER
    assert audit.payload["filled"] == {other.value: expected[other]}  # what was given out automatically
    told = sorted(n.member.id for n in await sent(house, "assignment_made"))
    assert told == sorted({house["Kim"].id, *(who for r, who in expected.items() if r is not LEAD)})
    house.clock.now = due
    assert Step.ASSIGN_ROLES not in await house.tick()
    await act(house, calls.mark_done, house["Alex"], event_id)
    assert assigned(await house.context(event_id)) == expected  # nothing left empty


async def test_an_offer_taking_over_a_set_pick_keeps_the_offers_way(house: House) -> None:
    event_id = await picks_voting(house, "Alex", "Kim", "Lee", offers_pickup=("Alex",))
    await nobody_coordinates(house, event_id)
    siam = house.items["Siam Garden"].id
    pick = next(p for p in (await house.context(event_id)).proposals if p.item_id == siam)
    await act(house, coordinator.set_way, house["Alex"], event_id, pick.id, Mode.PICKUP, run=1)
    await act(house, offers.post_offer, house["Kim"], picks_offer(house, Mode.DELIVERY, item_id=siam))
    taken = (await house.context(event_id)).proposal(pick.id)
    assert taken is not None and (taken.mode, taken.mode_chosen) == (Mode.DELIVERY, True)  # Kim's way now


async def test_a_cook_who_now_orders_hears_only_the_new_role(house: House) -> None:
    """The catalog gave a home meal a takeout way after it was decided. Switching keeps the cook as
    the one who orders: one message with the new role, not also "no longer cooking"."""
    event_id = await to_voting(house, "Sam", "Lee", pickups=(), coordinator_name="Alex")
    tacos = await act(
        house, calls.add_proposal, house["Sam"], event_id, Mode.COOK, item_id=house.items["Sam's Tacos"].id
    )
    await act(house, calls.lock, house["Alex"], event_id, tacos.id)
    assert assigned(await house.context(event_id))[COOK] == house["Sam"].id
    async with house.sessionmaker() as db:
        await db.execute(
            text("UPDATE catalog_item SET modes = :m WHERE id = :id"),
            {"m": '["cook", "delivery"]', "id": house.items["Sam's Tacos"].id},
        )
        await db.commit()
    before = len(await sent(house, "assignment_made"))
    await act(house, coordinator.choose_roles, house["Alex"], event_id, mode=Mode.DELIVERY, member_id=house["Sam"].id)
    assert assigned(await house.context(event_id)) == {ORDER: house["Sam"].id, LEAD: house["Alex"].id}
    assert await sent(house, "assignment_ended") == []
    (told,) = (await sent(house, "assignment_made"))[before:]
    assert (told.member.id, told.roles) == (house["Sam"].id, (ORDER,))


# Clearing the way (#212) --------------------------------------------------------------------------------


def _menu_options(payload: dict[str, object], proposal_id: int) -> list[dict[str, Any]]:
    """The options in the ⋯ beside a pick on a proposals message."""
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    (row,) = [b for b in blocks if b.get("block_id") == f"proposal-actions-{proposal_id}"]
    (menu,) = [e for e in row["elements"] if e["action_id"] == ids.PICK_MENU]
    options: list[dict[str, Any]] = menu["options"]
    return options


def _menu(payload: dict[str, object], proposal_id: int) -> list[str]:
    return [o["text"]["text"] for o in _menu_options(payload, proposal_id)]


def _menu_values(payload: dict[str, object], proposal_id: int) -> list[str]:
    return [o["value"] for o in _menu_options(payload, proposal_id)]


async def test_clearing_the_way_while_voting_opens_it_again(house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await act(house, coordinator.set_way, house["Sam"], event_id, siam, Mode.DELIVERY, run=1)
    chosen = render((await sent(house, "proposals_updated"))[-1], CTX).payload()
    assert _menu(chosen, siam) == [
        labels.LOCK,
        labels.WAY_LABELS[Mode.PICKUP],
        labels.WAY_LABELS[Mode.DELIVERY],
        labels.EITHER_WAY,
    ]
    assert _menu_values(chosen, siam)[-1] == ids.way_value(None, event_id, 1, siam, clears=Mode.DELIVERY)
    await act(house, coordinator.set_way, house["Sam"], event_id, siam, None, run=1)
    ctx = await house.context(event_id)
    pick = ctx.proposal(siam)
    assert pick is not None and (pick.mode, pick.mode_chosen) == (Mode.DELIVERY, False)  # the way it had, open
    assert cc.proposal_info(ctx, pick).way_open
    cleared = [a.payload for a in await house.audits() if a.action == "event.way_set"][-1]
    assert cleared == {"proposal_id": siam, "mode": None, "switched_from": "delivery"}
    updated = render((await sent(house, "proposals_updated"))[-1], CTX).payload()
    assert "(chosen)" not in json.dumps(updated)
    assert _menu(updated, siam) == [labels.LOCK, labels.WAY_LABELS[Mode.PICKUP], labels.WAY_LABELS[Mode.DELIVERY]]
    assert "pickup or delivery with ⋯" in json.dumps(updated, ensure_ascii=False)
    await decide_on(house, event_id, "Siam Garden")
    assert (await house.event(event_id)).roles_due_at is not None  # the decision asks again
    (ask,) = await sent(house, "roles_to_choose")
    assert "pickup or delivery, who orders, and who picks it up" in json.dumps(render(ask, CTX).payload())


async def test_clearing_the_way_is_refused_with_a_reason(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    refusals = [("Lee", picks["Siam Garden"], "not_coordinator"), ("Sam", picks["Taqueria"], "not_both_ways")]
    for who, pick, code in refusals:
        with pytest.raises(EventRuleViolation) as refused:
            await act(house, coordinator.set_way, house[who], event_id, pick, None, run=1)
        assert refused.value.code == code, who
    with pytest.raises(StaleRun):
        await act(house, coordinator.set_way, house["Sam"], event_id, picks["Siam Garden"], None, run=2)
    with pytest.raises(NotFound):
        await act(house, coordinator.set_way, house["Sam"], event_id, 999_999, None, run=1)
    assert [a for a in await house.audits() if a.action == "event.way_set"] == []
    await act(house, calls.lock, house["Alex"], event_id, picks["Taqueria"])
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.set_way, house["Sam"], event_id, picks["Siam Garden"], None, run=1)
    assert refused.value.code == "voting_closed"


async def test_a_clear_for_a_way_that_changed_since_is_refused(house: House) -> None:
    """The coordinator saw pickup chosen, but an admin made it delivery meanwhile: clearing pickup
    would undo the admin's choice, so it's refused and nothing is written. Once cleared, a second
    clear from a page that still showed it chosen is refused too."""
    event_id = await to_voting(house, "Lee", "Sam", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await act(house, coordinator.set_way, house["Alex"], event_id, siam, Mode.DELIVERY, run=1)
    before = len(await house.audits())
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.set_way, house["Sam"], event_id, siam, None, clears=Mode.PICKUP, run=1)
    assert (refused.value.code, str(refused.value)) == ("way_changed", "Someone made it delivery since you looked.")
    pick = (await house.context(event_id)).proposal(siam)
    assert pick is not None and (pick.mode, pick.mode_chosen) == (Mode.DELIVERY, True)
    assert len(await house.audits()) == before
    await act(house, coordinator.set_way, house["Sam"], event_id, siam, None, clears=Mode.DELIVERY, run=1)
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.set_way, house["Alex"], event_id, siam, None, clears=Mode.DELIVERY, run=1)
    assert str(refused.value) == "Someone set it back to either way since you looked."
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.set_way, house["Sam"], event_id, siam, None, clears=Mode.COOK, run=1)
    assert refused.value.code == "not_both_ways"
    assert len(await house.audits()) == before + 1


async def test_clearing_an_open_way_is_accepted_as_setting_the_way_it_has_is(house: House) -> None:
    """Nobody who's in can order, so setting delivery is refused; clearing needs no coverage, and
    an admin may. The way was open already, so the pick is unchanged, but the tap is audited."""
    event_id = await to_voting(house, "Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.set_way, house["Alex"], event_id, siam, Mode.DELIVERY, run=1)
    assert refused.value.code == "no_coverage"
    await act(house, coordinator.set_way, house["Alex"], event_id, siam, None, run=1)
    pick = (await house.context(event_id)).proposal(siam)
    assert pick is not None and (pick.mode, pick.mode_chosen) == (Mode.PICKUP, False)
    (audit,) = [a for a in await house.audits() if a.action == "event.way_set"]
    assert audit.payload == {"proposal_id": siam, "mode": None, "switched_from": "pickup"}


async def test_slack_clears_the_way_with_either_way(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Lee", "Sam", coordinator_name="Sam")
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    either = ids.way_value(None, event_id, 1, siam, clears=Mode.DELIVERY)
    await slack.click("Sam", ids.SET_WAY, None, selected=ids.way_value(Mode.DELIVERY, event_id, 1, siam))
    await slack.click("Sam", ids.SET_WAY, None, selected=ids.way_value(None, event_id, 1, siam, clears=Mode.PICKUP))
    assert slack.ephemerals("Sam")[-1] == "Someone made it delivery since you looked."  # a stale message
    await slack.click("Lee", ids.SET_WAY, None, selected=either)
    assert slack.ephemerals("Lee")[-1] == "Only the coordinator or an admin can choose pickup or delivery."
    await slack.click("Sam", ids.SET_WAY, None, selected=either)
    assert slack.ephemerals("Sam")[-1] == "It's either way again. If it wins, pickup or delivery gets chosen then."
    pick = (await house.context(event_id)).proposal(siam)
    assert pick is not None and (pick.mode, pick.mode_chosen) == (Mode.DELIVERY, False)


def _proposal_card(html: str, item_id: int) -> str:
    """The proposal on the event page whose way posts to ``item_id``'s pick, as one string."""
    cards = re.findall(r'<li class="proposal">.*?</li>', html, re.S)
    (card,) = [c for c in cards if f"/proposals/{item_id}/" in c]
    return card


def test_web_vote_card_puts_vote_first_and_the_coordinators_row_below(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """Vote is the primary button for everyone. The coordinator and an admin get a quiet row below
    it: Pickup | Delivery, Break the tie when tied, and Lock this. A member sees only Vote. The
    chosen way posts "either" and the way it clears, so tapping it again clears it, and a tap
    from a page that's out of date is refused; its id stays the way's."""
    add_item(admin, "Pho Place", cuisines="Vietnamese", modes=["pickup", "delivery"])
    invite(admin, "kim@example.com", "Kim")
    invite(admin, "lee@example.com", "Lee")
    event_id = start(admin)
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/offer", {"role": "order", "offered": "1"}).status_code == 303
    sign_in_as(admin, provider, "lee@example.com")
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    sign_in_as(admin, provider, "kim@example.com")
    assert submit(admin, f"/events/{event_id}/coordinate", {}).status_code == 303
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    (item,) = [i for i in rows(migrated, CatalogItemRow) if i.name == "Pho Place"]
    (pho,) = [p.id for p in rows(migrated, ProposalRow) if p.item_id == item.id]
    way = f"/events/{event_id}/proposals/{pho}/way"

    card = _proposal_card(page(admin.get(f"/events/{event_id}")), pho)  # Kim coordinates
    assert 'class="button small primary choice" aria-pressed="false">Vote<' in card
    assert '<div class="coordinator-row"' in card and '<span class="coordinator-label">Coordinator</span>' in card
    assert card.index(">Vote<") < card.index("coordinator-row")
    assert 'class="button quiet choice" aria-pressed="false">Pickup<' in card
    assert 'class="button quiet choice" aria-pressed="false">Delivery<' in card
    assert "Not chosen yet" in card
    lock = card.index(">Lock this<")
    assert card.index(">Delivery</button>") < lock and 'class="button quiet">Lock this<' in card
    assert 'hx-confirm="' in card[card.rindex("<form", 0, lock) : lock]  # still confirmed
    assert "primary" not in card[card.index("coordinator-row") :]
    html = page(admin.get(f"/events/{event_id}"))
    assert 'class="button small">Shuffle proposals<' in html

    assert submit(admin, way, {"mode": "pickup", "run": "1"}).status_code == 303
    card = _proposal_card(page(admin.get(f"/events/{event_id}")), pho)
    end = card.index(">Pickup</button>")
    pressed = card[card.rindex("<form", 0, end) : end]
    assert '<input type="hidden" name="mode" value="either">' in pressed and 'aria-pressed="true"' in pressed
    assert '<input type="hidden" name="clears" value="pickup">' in pressed
    assert 'name="clears"' not in card.replace(pressed, "")  # only the chosen way clears
    assert f'id="event-{event_id}-proposals-{pho}-way-pickup"' in pressed  # the way's id, for the swap
    assert "Not chosen yet" not in card
    assert submit(admin, way, {"mode": "either", "clears": "pickup", "run": "1"}).status_code == 303
    (row,) = [p for p in rows(migrated, ProposalRow) if p.id == pho]
    assert (row.mode, row.mode_chosen) == ("pickup", False)
    garbage: list[dict[str, str]] = [
        {"mode": "neither"},
        {"mode": ""},
        {"mode": "Either", "clears": "pickup"},
        {"mode": "either"},  # a clear names the way it clears
        {"mode": "either", "clears": "nope"},
        {"mode": "pickup", "clears": "pickup"},
    ]
    for form in garbage:
        refused = submit(admin, way, form | {"run": "1"})
        assert refused.status_code == 409 and "Pick pickup or delivery." in page(refused), form

    # Kim's page shows pickup chosen, but the admin made it delivery since: her tap is refused.
    assert submit(admin, way, {"mode": "pickup", "run": "1"}).status_code == 303
    admin.cookies.clear()
    sign_in(admin, provider)
    assert submit(admin, way, {"mode": "delivery", "run": "1"}).status_code == 303
    sign_in_as(admin, provider, "kim@example.com")
    audits = len(rows(migrated, AuditRow))
    stale = htmx(admin, way, {"mode": "either", "clears": "pickup", "run": "1", "from": "event"})
    assert stale.status_code == 409 and "Someone made it delivery since you looked." in page(stale)
    assert 'aria-pressed="true">Delivery</button>' in _proposal_card(page(stale), pho)  # the card as it is now
    (row,) = [p for p in rows(migrated, ProposalRow) if p.id == pho]
    assert (row.mode, row.mode_chosen) == ("delivery", True) and len(rows(migrated, AuditRow)) == audits

    sign_in_as(admin, provider, "lee@example.com")  # a member: Vote only
    card = _proposal_card(page(admin.get(f"/events/{event_id}")), pho)
    assert 'class="button small primary choice" aria-pressed="false">Vote<' in card
    assert "coordinator-row" not in card and ">Pickup</button>" not in card and ">Lock this<" not in card
    assert submit(admin, f"/events/{event_id}/proposals/{pho}/vote", {}).status_code == 303
    card = _proposal_card(page(admin.get(f"/events/{event_id}")), pho)
    assert 'class="button small primary choice" aria-pressed="true">Voted<' in card
    assert submit(admin, way, {"mode": "either", "run": "1"}).status_code == 409  # not the coordinator

    admin.cookies.clear()
    sign_in(admin, provider)  # an admin gets the coordinator's row too
    card = _proposal_card(page(admin.get(f"/events/{event_id}")), pho)
    assert "coordinator-row" in card and ">Lock this<" in card and ">Pickup</button>" in card


def test_the_quiet_style_keeps_its_tap_height_and_shows_a_border_on_hover_or_focus(client: TestClient) -> None:
    css = client.get("/static/app.css").text
    quiet = css[css.index(".button.quiet {") :]
    quiet = quiet[: quiet.index("}")]
    assert "min-height: 40px" in quiet and "border-color: transparent" in quiet and "color: var(--muted)" in quiet
    assert ".button.quiet:hover, .button.quiet:focus-visible { border-color: var(--line); color: var(--text); }" in css
    # A chosen way is the accent fill with its own text color, like every choice (#230).
    chosen = '.button.quiet.choice[aria-pressed="true"] { background: var(--accent); border-color: transparent; '
    assert chosen + "color: var(--accent-text);" in css
    # Vote keeps the primary fill, the action everyone takes; Voted drops to the soft tint with a
    # check, so it doesn't read as a next step (#212).
    vote = ".button.primary { background: var(--accent); border-color: var(--accent); "
    assert vote + "color: var(--accent-text); }" in css
    assert '.button.primary.choice:not([aria-pressed="true"])' not in css
    voted = '.button.primary.choice[aria-pressed="true"] { background: var(--accent-soft); '
    assert voted + "border-color: var(--accent); color: var(--accent-soft-text); }" in css
    check = '.button.primary.choice[aria-pressed="true"]::before { content: "\\2713\\00a0"; '
    assert check + 'content: "\\2713\\00a0" / ""; }' in css


def test_web_break_the_tie_comes_before_lock_in_the_quiet_row(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = web_voting(admin, provider, clock, migrated)
    first, second, third = web_proposal_ids(migrated)
    assert submit(admin, f"/events/{event_id}/proposals/{first}/vote", {}).status_code == 303
    sign_in_as(admin, provider, "kim@example.com")
    assert submit(admin, f"/events/{event_id}/proposals/{second}/vote", {}).status_code == 303
    admin.cookies.clear()
    sign_in(admin, provider)  # an admin, with nobody coordinating
    html = page(admin.get(f"/events/{event_id}"))
    for tied in (first, second):
        card = _proposal_card(html, tied)
        row = card[card.index('<div class="coordinator-row"') :]
        assert 'class="button quiet">Break the tie<' in row and 'class="button quiet">Lock this<' in row
        assert row.index(">Break the tie<") < row.index(">Lock this<")
        tie = row.index(">Break the tie<")
        assert 'hx-confirm="' in row[row.rindex("<form", 0, tie) : tie]
    card = _proposal_card(html, third)
    assert ">Break the tie<" not in card and ">Lock this<" in card  # not tied
