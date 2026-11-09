# SPDX-License-Identifier: AGPL-3.0-or-later
"""Change the place after a decision (#219, spec 5.7): the use case against a migrated database
with a fake clock, the row that keeps the decided place findable, who hears, what the record says
afterward, and the Slack and web surfaces."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.messages import render
from dinnerbell.channels.slack.views import adjust_modal, adjust_opened, parse_adjust
from dinnerbell.config import Settings
from dinnerbell.db.contributions import ContributionRepository
from dinnerbell.db.models import AuditRow, CatalogItemRow, EventRow, ProposalRow
from dinnerbell.domain.catalog import ItemKind, Mode, TagKind
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import (
    AFTER_DECISION_RANK,
    AssignmentRole,
    EventRuleViolation,
    EventState,
    RoleOffer,
    StaleRun,
    Status,
)
from dinnerbell.domain.guests import REFUSED, GuestAction
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import AssignmentMade, PlaceChanged, from_payload
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.logs import JsonFormatter
from dinnerbell.services import activity, call_admin, calls, coordinator, event_views, feedback, guests
from dinnerbell.services import call_context as cc
from dinnerbell.services.coordinator import PLAN_CHANGED, Adjustment, AdjustmentRefused
from dinnerbell.services.preferences import set_my_preference
from tests.conftest import invite, page, sign_in
from tests.support.calls import RESPOND_BY, VOTE_BY, House, RecordingChannel
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import CHANNEL, state
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
    to_voting,
    vote,
)
from tests.test_coordinator import database as database  # the fixtures, re-exported for pytest
from tests.test_coordinator import house as house
from tests.test_events_web import admin as admin
from tests.test_events_web import proposal_ids, sign_in_as, start
from tests.test_events_web import tick as web_tick
from tests.test_slack_adjust import decided, form, inputs, opened_now

pytestmark = pytest.mark.anyio


async def change(house: House, who: str, event_id: int, **kwargs: Any) -> Any:
    """Change the place, from what the call has now unless ``opened_item_id`` says otherwise."""
    shown = kwargs.pop("opened_item_id", (await house.event(event_id)).decided_item_id)
    return await act(house, coordinator.change_place, house[who], event_id, opened_item_id=shown, **kwargs)


def tacos(house: House) -> int:
    return house.items["Sam's Tacos"].id


def role_holders(ctx: cc.CallContext) -> dict[AssignmentRole, int]:
    return {role: who for role, who in assigned(ctx).items() if role is not AssignmentRole.COORDINATOR}


async def told(house: House, kind: str) -> set[int]:
    return {n.recipient_id for n in await sent(house, kind)}


# The decided place ----------------------------------------------------------------------------------


async def test_a_place_that_was_not_a_pick_becomes_the_decision_without_becoming_a_pick(house: House) -> None:
    event_id = await decided(house)
    picks_before = [p.id for p in (await house.context(event_id)).proposals]
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)

    event, ctx = await house.event(event_id), await house.context(event_id)
    assert (event.decided_item_id, event.decided_mode) == (tacos(house), Mode.COOK)
    assert ctx.winner is not None and ctx.winner.after_decision and ctx.winner.rank == AFTER_DECISION_RANK
    assert (ctx.winner.proposed_by, ctx.winner.reasons, ctx.winner.mode_chosen) == (None, (), True)
    assert [p.id for p in ctx.picks] == picks_before  # the new row isn't one of them
    assert [p.id for p in cc.proposals(ctx)] == picks_before
    assert ctx.votes.get(ctx.winner.id) is None

    decision = cc.decision_info(ctx)
    assert decision is not None and decision.proposal.item.name == "Sam's Tacos"
    assert decision.proposal.mode is Mode.COOK and not decision.proposal.way_open
    plan = cc.plan_info(ctx)
    assert plan is not None and plan.item == "Sam's Tacos"
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    assert [p.info.id for p in view.proposals] == picks_before
    assert view.roles is not None and view.roles.mode is Mode.COOK


async def test_the_picks_message_never_shows_the_new_row_as_something_to_vote_on(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    (*_, updated) = await sent(house, "proposals_updated")
    text = json.dumps(render(updated, CTX).payload())
    assert "Sam's Tacos" not in text and "Siam Garden" in text and "Taqueria" in text


async def test_a_place_that_was_a_pick_keeps_its_row_and_its_votes(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    picks = await pick_ids(house, event_id)
    await vote(house, "Alex", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    await at_deadline(house, event_id)
    count = len((await house.context(event_id)).proposals)

    await change(house, "Sam", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    ctx = await house.context(event_id)
    assert len(ctx.proposals) == count
    assert ctx.winner is not None and ctx.winner.id == picks["Taqueria"] and not ctx.winner.after_decision
    assert ctx.winner.mode_chosen  # chosen now, so nobody is asked about its way
    assert ctx.votes[picks["Taqueria"]] == [house["Lee"].id]
    assert cc.decision_info(ctx).proposal.voters  # type: ignore[union-attr]


async def test_a_pick_chosen_in_another_way_switches_its_row(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Taqueria"])
    await at_deadline(house, event_id)
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await change(house, "Sam", event_id, item_id=house.items["Siam Garden"].id, mode=Mode.DELIVERY)
    ctx = await house.context(event_id)
    assert ctx.winner is not None and (ctx.winner.id, ctx.winner.mode) == (siam, Mode.DELIVERY)
    assert role_holders(ctx) == {AssignmentRole.ORDER: house["Lee"].id}


async def test_changing_it_again_leaves_at_most_one_row_and_going_back_finds_the_pick(house: House) -> None:
    event_id = await decided(house)
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    count = len((await house.context(event_id)).proposals)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    await house.add_item("Noodle Bar", Mode.PICKUP)
    house.clock.advance(timedelta(minutes=1))
    await change(house, "Sam", event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.PICKUP)
    ctx = await house.context(event_id)
    assert len(ctx.proposals) == count + 1
    house.clock.advance(timedelta(minutes=1))
    await change(house, "Sam", event_id, item_id=house.items["Siam Garden"].id, mode=Mode.PICKUP)
    ctx = await house.context(event_id)
    assert len(ctx.proposals) == count and ctx.winner is not None and ctx.winner.id == siam


async def test_how_the_old_place_won_stops_showing(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await act(house, calls.lock, house["Sam"], event_id, (await pick_ids(house, event_id))["Taqueria"])
    assert (await house.event(event_id)).locked_by == house["Sam"].id
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert (event.locked_by, event.tie_how, event.tie_by) == (None, None, None)
    (*_, updated) = await sent(house, "decision_updated")
    assert updated.locked_by is None and cc.tie_note(ctx) is None


# What it isn't held to --------------------------------------------------------------------------------


async def test_vetoes_needs_variety_the_meal_and_coverage_do_not_block_it(house: House) -> None:
    event_id = await decided(house)
    item = house.items["Sam's Tacos"]
    async with house.sessionmaker() as db:
        await set_my_preference(db, house["Lee"], TargetType.ITEM, item.id, Stance(veto=True), now=house.clock())
    assert house["Sam"].id not in (await house.context(event_id)).coverage.volunteers(
        Mode.COOK, AssignmentRole.COOK, item.cook_ids
    )
    await change(house, "Sam", event_id, item_id=item.id, mode=Mode.COOK)  # vetoed, and nobody offered to cook
    assert (await house.event(event_id)).decided_item_id == item.id


# Refusals ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["Kim", "Lee"])
async def test_someone_who_does_not_coordinate_is_refused(house: House, who: str) -> None:
    event_id = await decided(house)
    with pytest.raises(EventRuleViolation) as caught:
        await change(house, who, event_id, item_id=tacos(house), mode=Mode.COOK)
    assert caught.value.code == "not_coordinator"
    assert (await house.event(event_id)).decided_item_id == house.items["Siam Garden"].id


async def test_an_admin_may_change_it_but_not_as_slacks_adjust(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(EventRuleViolation) as caught:
        await change(house, "Alex", event_id, item_id=tacos(house), mode=Mode.COOK, as_coordinator=True)
    assert caught.value.code == "not_coordinator"
    await change(house, "Alex", event_id, item_id=tacos(house), mode=Mode.COOK)
    (audit,) = [a for a in await house.audits() if a.action == "event.place_changed"]
    assert audit.payload["as"] == "admin" and audit.actor_member_id == house["Alex"].id


async def guest_at_a_decided_call(house: House) -> tuple[int, Member]:
    """Pat, a guest, coordinates a call decided on Siam Garden. Taqueria was only a pick."""
    pat = await add_guest(house, "Pat")
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, calls.lock, house["Alex"], event_id, (await pick_ids(house, event_id))["Siam Garden"])
    return event_id, pat


async def test_a_guest_may_not_change_the_place(house: House) -> None:
    event_id, pat = await guest_at_a_decided_call(house)
    siam = house.items["Siam Garden"].id
    for place in ({"item_id": tacos(house), "mode": Mode.COOK}, {"name": "Noodle Bar", "mode": Mode.PICKUP}):
        with pytest.raises(EventRuleViolation) as caught:
            await act(house, coordinator.change_place, pat, event_id, opened_item_id=siam, **place)
        assert caught.value.code == "guests_dont" and str(caught.value) == REFUSED[GuestAction.COORDINATE]
    ctx = await house.context(event_id)
    assert (await house.event(event_id)).decided_item_id == siam
    assert not [i for i in ctx.items.values() if i.name == "Noodle Bar"]


async def test_a_done_call_refuses_even_an_admin(house: House) -> None:
    event_id = await decided(house)
    house.clock.now = VOTE_BY + timedelta(hours=2)
    await act(house, calls.mark_done, house["Alex"], event_id)
    for who in ("Sam", "Alex"):
        with pytest.raises(EventRuleViolation) as caught:
            await change(house, who, event_id, item_id=tacos(house), mode=Mode.COOK)
        assert caught.value.code == "not_decided"


async def test_a_call_that_is_not_decided_yet_refuses(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    with pytest.raises(EventRuleViolation):
        await act(house, coordinator.change_place, house["Sam"], event_id, item_id=tacos(house), opened_item_id=None)


async def test_a_page_from_an_earlier_run_is_refused(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(StaleRun):
        await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK, run=2)


async def test_a_way_the_new_place_does_not_allow_is_refused_and_nothing_changes(house: House) -> None:
    event_id = await decided(house)
    audits = len(await house.audits())
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.PICKUP)
    assert caught.value.part == "place_mode" and caught.value.code == "unavailable"
    assert (await house.event(event_id)).decided_item_id == house.items["Siam Garden"].id
    assert len(await house.audits()) == audits


async def test_a_place_changed_elsewhere_since_it_was_shown_is_refused(house: House) -> None:
    event_id = await decided(house)
    shown = (await house.event(event_id)).decided_item_id
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Alex", event_id, item_id=house.items["Taqueria"].id, opened_item_id=shown)
    assert (caught.value.part, caught.value.code, str(caught.value)) == ("place", "plan_changed", PLAN_CHANGED)
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Alex", event_id, name="Noodle Bar", mode=Mode.PICKUP, opened_item_id=shown)
    assert (caught.value.part, caught.value.code) == ("new_place", "plan_changed")
    assert (await house.event(event_id)).decided_item_id == tacos(house)


async def test_the_place_it_has_is_refused_since_nothing_would_change(house: House) -> None:
    event_id = await decided(house)
    item = house.items["Siam Garden"].id
    with pytest.raises(EventRuleViolation) as caught:
        await change(house, "Sam", event_id, item_id=item, mode=Mode.DELIVERY)
    assert caught.value.code == "same_place"
    with pytest.raises(EventRuleViolation) as caught:
        await change(house, "Sam", event_id, name="siam garden", mode=Mode.PICKUP)
    assert caught.value.code == "same_place"
    with pytest.raises(EventRuleViolation) as caught:
        await change(house, "Sam", event_id)
    assert caught.value.code == "same_place"


async def take_out_of_catalog(house: House, name: str) -> None:
    async with house.sessionmaker() as db:
        await db.execute(update(CatalogItemRow).where(CatalogItemRow.id == house.items[name].id).values(active=False))
        await db.commit()


async def test_a_place_not_in_the_catalog_or_taken_out_of_it_is_refused(house: House) -> None:
    event_id = await decided(house)
    await take_out_of_catalog(house, "Taqueria")
    for item_id in (999_999, house.items["Taqueria"].id):
        with pytest.raises(AdjustmentRefused) as caught:
            await change(house, "Sam", event_id, item_id=item_id, mode=Mode.PICKUP)
        assert caught.value.part == "place" and caught.value.code == "not_found"


async def test_a_place_and_a_new_name_together_are_refused(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, item_id=tacos(house), name="Noodle Bar", mode=Mode.PICKUP)
    assert caught.value.part == "new_place" and "pick Siam Garden" in str(caught.value)


# The way ------------------------------------------------------------------------------------------------


async def test_no_way_picked_takes_the_only_one_or_the_one_it_has_or_asks(house: House) -> None:
    event_id = await decided(house)  # pickup at Siam Garden
    await change(house, "Sam", event_id, item_id=tacos(house))  # only cook
    assert (await house.event(event_id)).decided_mode is Mode.COOK
    await house.add_item("Noodle Bar", Mode.PICKUP, Mode.DELIVERY)
    house.clock.advance(timedelta(minutes=1))
    with pytest.raises(AdjustmentRefused) as caught:  # it has cook now, and this place has two others
        await change(house, "Sam", event_id, item_id=house.items["Noodle Bar"].id)
    assert (caught.value.part, caught.value.code) == ("place_mode", "no_way_chosen")
    await change(house, "Sam", event_id, item_id=house.items["Siam Garden"].id, mode=Mode.DELIVERY)
    house.clock.advance(timedelta(minutes=1))
    await change(house, "Sam", event_id, item_id=house.items["Noodle Bar"].id)  # keeps delivery
    assert (await house.event(event_id)).decided_mode is Mode.DELIVERY


# A new name --------------------------------------------------------------------------------------------


async def test_a_new_name_becomes_a_catalog_item_for_its_way(house: House) -> None:
    event_id = await decided(house)
    cuisine = house.tags["Thai"].id
    await change(house, "Sam", event_id, name="  Noodle   Bar ", mode=Mode.DELIVERY, cuisine_id=cuisine)
    ctx = await house.context(event_id)
    item = ctx.items[(await house.event(event_id)).decided_item_id or 0]
    assert (item.name, item.kind, item.modes, item.tag_ids) == (
        "Noodle Bar",
        ItemKind.RESTAURANT,
        frozenset({Mode.DELIVERY}),
        frozenset({cuisine}),
    )
    assert item.created_by is None  # no Scout credit for a correction
    assert ctx.winner is not None and ctx.winner.after_decision and ctx.winner.free_text is None
    (audit,) = [a for a in await house.audits() if a.action == "event.place_changed"]
    assert audit.payload["new_item"] is True and audit.payload["to_item_id"] == item.id
    assert [a for a in await house.audits("catalog.") if a.action == "catalog.item_created"]


async def test_a_new_name_with_cook_is_a_home_meal_and_the_cuisine_is_optional(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, name="Leftovers", mode=Mode.COOK)
    ctx = await house.context(event_id)
    item = ctx.items[(await house.event(event_id)).decided_item_id or 0]
    assert (item.kind, item.modes, item.tag_ids) == (ItemKind.HOME, frozenset({Mode.COOK}), frozenset())


async def test_a_new_name_needs_its_way_and_a_valid_name_and_cuisine(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, name="Noodle Bar")
    assert caught.value.part == "place_mode"
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, name="x" * 101, mode=Mode.PICKUP)
    assert (caught.value.part, caught.value.code) == ("new_place", "invalid_name")
    dietary = await house.tag("Vegan", TagKind.DIETARY)
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, name="Noodle Bar", mode=Mode.PICKUP, cuisine_id=dietary.id)
    assert (caught.value.part, caught.value.code) == ("place_cuisine", "no_cuisine")
    assert not [i for i in (await house.context(event_id)).items.values() if i.name == "Noodle Bar"]


@pytest.mark.parametrize("taken_out", [False, True])
async def test_a_name_the_catalog_has_is_that_place_even_if_it_was_taken_out(house: House, taken_out: bool) -> None:
    event_id = await decided(house)
    if taken_out:
        await take_out_of_catalog(house, "Taqueria")
    items = len((await house.context(event_id)).items)
    await change(house, "Sam", event_id, name="taqueria", mode=Mode.PICKUP)
    ctx = await house.context(event_id)
    assert len(ctx.items) == items and (await house.event(event_id)).decided_item_id == house.items["Taqueria"].id


async def test_a_name_the_catalog_has_in_a_way_it_does_not_allow_is_refused(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, name="Taqueria", mode=Mode.DELIVERY)
    assert (caught.value.part, caught.value.code) == ("place_mode", "unavailable")


async def test_a_new_name_that_was_a_free_text_pick_uses_that_pick(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await act(
        house,
        calls.add_proposal,
        house["Lee"],
        event_id,
        Mode.PICKUP,
        free_text="Noodle Bar",
        cuisine_id=house.tags["Thai"].id,
    )
    free = (await pick_ids(house, event_id))["Noodle Bar"]
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    count = len((await house.context(event_id)).proposals)
    await change(house, "Sam", event_id, name="noodle bar", mode=Mode.DELIVERY)
    ctx = await house.context(event_id)
    item = ctx.items[(await house.event(event_id)).decided_item_id or 0]
    assert len(ctx.proposals) == count and ctx.winner is not None and ctx.winner.id == free
    assert (item.name, item.modes) == ("Noodle Bar", frozenset({Mode.DELIVERY}))
    assert ctx.winner.proposed_by == house["Lee"].id  # who added it did suggest it


async def test_a_catalog_place_with_the_name_of_a_free_text_pick_links_that_pick(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await act(
        house,
        calls.add_proposal,
        house["Lee"],
        event_id,
        Mode.PICKUP,
        free_text="Noodle Bar",
        cuisine_id=house.tags["Thai"].id,
    )
    free = (await pick_ids(house, event_id))["Noodle Bar"]
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    item = await house.add_item("Noodle Bar", Mode.PICKUP)
    await change(house, "Sam", event_id, item_id=item.id, mode=Mode.PICKUP)
    winner = (await house.context(event_id)).winner
    assert winner is not None and (winner.id, winner.item_id, winner.free_text) == (free, item.id, None)


# Roles -------------------------------------------------------------------------------------------------


async def test_roles_the_new_way_has_stay_and_nobody_new_is_told_about_them(house: House) -> None:
    event_id = await decided(house)  # Lee orders, Alex picks up
    before = role_holders(await house.context(event_id))
    made = len(await sent(house, "assignment_made"))
    await change(house, "Sam", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    assert role_holders(await house.context(event_id)) == before
    assert not await sent(house, "assignment_ended")
    assert len(await sent(house, "assignment_made")) == made


async def test_a_role_the_new_way_does_not_have_is_dropped_and_its_holder_told(house: House) -> None:
    event_id = await decided(house)
    await house.add_item("Noodle Bar", Mode.DELIVERY)
    await change(house, "Sam", event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.DELIVERY)
    assert role_holders(await house.context(event_id)) == {AssignmentRole.ORDER: house["Lee"].id}
    assert not await sent(house, "assignment_ended")
    (ended,) = [n for n in await sent(house, "place_changed") if n.member.id == house["Alex"].id]
    assert ended.ended_roles == (AssignmentRole.PICKUP,)
    assert "You're no longer picking up." in json.dumps(render(ended, CTX).payload())
    (audit,) = [a for a in await house.audits() if a.action == "event.place_changed"]
    assert audit.payload["dropped"] == ["pickup"]
    assert audit.payload["assignments"] == {"order": house["Lee"].id}


async def test_cooking_goes_to_its_first_volunteer_or_to_nobody(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), cooks=("Sam",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    await change(house, "Alex", event_id, item_id=tacos(house), mode=Mode.COOK)
    assert role_holders(await house.context(event_id)) == {AssignmentRole.COOK: house["Sam"].id}
    made = [n for n in await sent(house, "assignment_made") if n.member.id == house["Sam"].id][-1]
    assert made.decision.proposal.item.name == "Sam's Tacos" and made.assigned_by is None


async def test_nobody_to_do_it_does_not_block_it(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    assert role_holders(await house.context(event_id)) == {}


async def test_a_cook_off_the_new_places_list_keeps_cooking(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=(), cooks=("Lee",), coordinator_name="Sam")
    await house.add_item("Lee's Curry", Mode.COOK, cooks=("Lee",))
    await act(
        house,
        calls.add_proposal,
        house["Lee"],
        event_id,
        Mode.COOK,
        item_id=house.items["Lee's Curry"].id,
    )
    await act(house, calls.lock, house["Sam"], event_id, (await pick_ids(house, event_id))["Lee's Curry"])
    assert role_holders(await house.context(event_id)) == {AssignmentRole.COOK: house["Lee"].id}
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)  # only Sam is on its list
    assert role_holders(await house.context(event_id)) == {AssignmentRole.COOK: house["Lee"].id}


async def test_naming_someone_holds_them_to_the_rules_of_assigning(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK, member_id=house["Lee"].id)
    assert (caught.value.part, caught.value.code) == ("assignee", "cook_list")
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, name="Soup", mode=Mode.COOK, member_id=house["Kim"].id)
    assert (caught.value.part, caught.value.code) == ("assignee", "not_in")
    with pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, name="Soup", mode=Mode.COOK, member_id=999_999)
    assert (caught.value.part, caught.value.code) == ("assignee", "not_found")
    assert (await house.event(event_id)).decided_item_id == house.items["Siam Garden"].id


async def test_naming_someone_who_did_not_offer_tells_them_who_assigned_them(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, name="Soup", mode=Mode.COOK, member_id=house["Lee"].id)
    assert role_holders(await house.context(event_id)) == {AssignmentRole.COOK: house["Lee"].id}
    made = [n for n in await sent(house, "assignment_made") if n.member.id == house["Lee"].id][-1]
    assert made.assigned_by is not None and made.assigned_by.id == house["Sam"].id
    assert made.decision.proposal.item.name == "Soup"


# The coordinator still choosing -----------------------------------------------------------------------


async def pending(house: House) -> int:
    """Alex and Lee offered to pick up, so Sam, coordinating, is asked to choose."""
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex", "Lee"), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    assert (await house.event(event_id)).roles_due_at is not None
    return event_id


async def test_while_the_coordinator_chooses_the_time_runs_on_and_resolves_at_the_new_place(house: House) -> None:
    event_id = await pending(house)
    due = (await house.event(event_id)).roles_due_at
    await house.add_item("Noodle Bar", Mode.PICKUP, Mode.DELIVERY)
    await change(house, "Alex", event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.PICKUP)
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.roles_due_at == due and role_holders(ctx) == {}
    assert cc.assign_roles_open(ctx)
    assert due is not None
    house.clock.now = due
    await house.tick()
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.roles_due_at is None
    assert role_holders(ctx) == {AssignmentRole.PICKUP: house["Alex"].id, AssignmentRole.ORDER: house["Lee"].id}
    made = [n for n in await sent(house, "assignment_made") if n.member.id == house["Alex"].id][-1]
    assert made.decision.proposal.item.name == "Noodle Bar"


async def test_assign_roles_still_works_at_the_new_place_while_it_is_open(house: House) -> None:
    event_id = await pending(house)
    await change(house, "Alex", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    await act(house, coordinator.choose_roles, house["Sam"], event_id, member_id=house["Sam"].id, run=1)
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.roles_due_at is None and role_holders(ctx)[AssignmentRole.ORDER] == house["Sam"].id


async def test_marking_done_during_the_choosing_logs_the_new_place(house: House) -> None:
    event_id = await pending(house)
    await change(house, "Alex", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    await act(house, calls.mark_done, house["Alex"], event_id)
    (meal,) = await house.meals()
    assert meal.item_id == house.items["Taqueria"].id
    assert role_holders(await house.context(event_id))[AssignmentRole.PICKUP] == house["Alex"].id


async def test_naming_someone_during_the_choosing_ends_it(house: House) -> None:
    event_id = await pending(house)
    await change(
        house, "Sam", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP, pickup_id=house["Lee"].id
    )
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.roles_due_at is None and role_holders(ctx)[AssignmentRole.PICKUP] == house["Lee"].id


# Who hears ---------------------------------------------------------------------------------------------


async def test_before_the_meal_everyone_in_hears_but_whoever_changed_it(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    assert await told(house, "place_changed") == {house["Alex"].id, house["Lee"].id}  # Kim never answered
    message = (await sent(house, "place_changed"))[0]
    text = json.dumps(render(message, CTX).payload(), ensure_ascii=False)
    assert "*Sam changed the place for dinner" in text and "Sam's Tacos" in text and "It was Siam Garden." in text
    assert "• *When:* 6:30 PM" in text
    assert not await sent(house, "assignment_ended")  # their one message says what they lost
    assert {e.kind for e in await house.outbox()} >= {"decision_updated", "call_updated", "proposals_updated"}


async def test_from_the_meal_time_on_it_corrects_the_record_and_messages_nobody(house: House) -> None:
    event_id = await decided(house)
    event = await house.event(event_id)
    assert event.meal_time is not None
    house.clock.now = event.meal_time
    direct = {k: len(await sent(house, k)) for k in ("place_changed", "assignment_ended", "assignment_made")}
    updates = len(await sent(house, "decision_updated"))
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    assert {k: len(await sent(house, k)) for k in direct} == direct
    assert len(await sent(house, "decision_updated")) == updates + 1  # the message itself still shows it
    assert (await house.event(event_id)).decided_item_id == tacos(house)


async def test_a_guest_without_slack_is_told_through_whoever_invited_them_only_if_they_are_out(house: House) -> None:
    pat = await add_guest(house, "Pat")
    jo = await add_guest(house, "Jo")
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, guests.invite, house["Lee"], event_id, guest_id=pat.id)
    await act(house, guests.invite, house["Kim"], event_id, guest_id=jo.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.respond, jo, event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, calls.lock, house["Sam"], event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    messages = await sent(house, "place_changed")
    assert {n.recipient_id for n in messages} == {house["Alex"].id, house["Lee"].id, house["Kim"].id}
    (through,) = [n for n in messages if n.via is not None]
    assert (through.member.id, through.via.id) == (jo.id, house["Kim"].id)
    assert "Your guest Jo is in for it." in json.dumps(render(through, CTX).payload())


async def test_a_call_off_closes_the_message_unsent(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    await act(house, calls.cancel_call, house["Sam"], event_id)
    channel = RecordingChannel()
    await house.drain(channel)
    assert "place_changed" not in channel.kinds and "event_cancelled" in channel.kinds


# Audit and log ------------------------------------------------------------------------------------------


async def test_it_is_audited_with_the_old_and_new_place_and_way(house: House) -> None:
    event_id = await decided(house)
    old = house.items["Siam Garden"].id
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    (audit,) = [a for a in await house.audits() if a.action == "event.place_changed"]
    assert audit.actor_member_id == house["Sam"].id and audit.target == f"event:{event_id}"
    assert audit.payload == {
        "from_item_id": old,
        "from_mode": "pickup",
        "to_item_id": tacos(house),
        "to_mode": "cook",
        "new_item": False,
        "assignments": {},
        "dropped": ["order", "pickup"],
        "as": "coordinator",
    }


async def test_it_is_logged_with_the_old_and_new_place(house: House, caplog: pytest.LogCaptureFixture) -> None:
    event_id = await decided(house)
    with caplog.at_level(logging.INFO, logger=activity.log.name):
        await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    (line,) = [
        json.loads(JsonFormatter().format(r))
        for r in caplog.records
        if r.name == activity.log.name and r.getMessage() == "change_place"
    ]
    assert (line["item_id"], line["mode"], line["from_mode"]) == (tacos(house), "cook", "pickup")
    assert line["from_item_id"] == house.items["Siam Garden"].id and line["capacity"] == "coordinator"
    assert line["event_id"] == event_id and line["dropped"] == ["order", "pickup"]


async def test_a_refused_change_logs_no_change(house: House, caplog: pytest.LogCaptureFixture) -> None:
    event_id = await decided(house)
    with caplog.at_level(logging.INFO, logger=activity.log.name), pytest.raises(AdjustmentRefused) as caught:
        await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.PICKUP)
    assert caught.value.code == "unavailable"
    assert not [r for r in caplog.records if r.getMessage() == "change_place"]


# The record afterward ------------------------------------------------------------------------------------


async def test_marking_done_logs_the_new_place_and_feedback_names_it(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    await act(house, calls.mark_done, house["Alex"], event_id)
    (meal,) = await house.meals()
    assert meal.item_id == tacos(house) and meal.event_id == event_id
    form_ = await act(house, feedback.form_for_event, house["Lee"], event_id, run=1)
    assert form_.facts.item_name == "Sam's Tacos" and form_.facts.mode is Mode.COOK
    (*_, last) = await sent(house, "decision_updated")
    assert last.decision.proposal.item.name == "Sam's Tacos"
    assert "Sam's Tacos" in json.dumps(render(last, CTX).payload())


async def test_a_correction_earns_nobody_idea_credit_and_does_not_change_gambler(house: House) -> None:
    event_id = await decided(house)  # two picks, so nobody can be a gambler
    await change(house, "Sam", event_id, name="Noodle Bar", mode=Mode.PICKUP)
    await act(house, calls.mark_done, house["Alex"], event_id)
    household = house.household.id
    async with house.sessionmaker() as db:
        counts = ContributionRepository(db)
        for kind in (Kind.SUGGESTED, Kind.EXPLORED, Kind.ADDED_ITEM, Kind.GAMBLED):
            assert await counts.credited(household, event_id, kind) == set(), kind
        assert await counts.credited(household, event_id, Kind.PICKED_UP) == {house["Alex"].id}
        assert await counts.credited(household, event_id, Kind.ORDERED) == {house["Lee"].id}


async def test_whoever_added_a_pick_it_changes_to_does_get_credit_for_suggesting_it(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await house.add_item("Noodle Bar", Mode.PICKUP)
    await act(house, calls.add_proposal, house["Lee"], event_id, Mode.PICKUP, item_id=house.items["Noodle Bar"].id)
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    await change(house, "Sam", event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.PICKUP)
    await act(house, calls.mark_done, house["Alex"], event_id)
    async with house.sessionmaker() as db:
        credited = await ContributionRepository(db).credited(house.household.id, event_id, Kind.SUGGESTED)
    assert credited == {house["Lee"].id}


async def test_a_rerun_clears_the_row_with_the_rest_of_the_decision(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    ctx = await house.context(event_id)
    assert not [p for p in ctx.proposals if p.after_decision]


# Slack ------------------------------------------------------------------------------------------------


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def with_place(values: dict[str, Any], **parts: dict[str, Any]) -> dict[str, Any]:
    return values | state(**parts)


async def test_the_modal_lists_the_catalog_with_the_place_chosen_and_every_way(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    await slack.click("Sam", ids.ADJUST, ids.card_value(event_id, 1))
    (opened,) = slack.fake.of("views.open")
    fields = inputs(opened.args["view"])
    place = fields["place"]["element"]
    labels = [o["text"]["text"] for g in place["option_groups"] for o in g["options"]]
    assert {"Siam Garden", "Taqueria", "Sam's Tacos"} <= set(labels)
    assert place["initial_option"]["value"] == str(house.items["Siam Garden"].id)
    assert fields["place"]["optional"] and fields["new_place"]["optional"] and fields["place_mode"]["optional"]
    assert [o["value"] for o in fields["place_mode"]["element"]["options"]] == ["cook", "pickup", "delivery"]
    assert "place_cuisine" in fields
    origin = ids.ViewOrigin.decode(opened.args["view"]["private_metadata"])
    assert origin.opened is not None and origin.opened.item_id == house.items["Siam Garden"].id


async def test_the_modal_holds_a_big_catalog_with_the_place_kept(house: House) -> None:
    event_id = await decided(house)
    for number in range(105):
        await house.add_item(f"Aaa Place {number:03}", Mode.PICKUP)
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    built = adjust_modal(view, ids.ViewOrigin(event_id, CHANNEL, run=1, opened=adjust_opened(view)))
    place = inputs(built)["place"]["element"]
    assert all(len(g["options"]) <= 100 for g in place["option_groups"])
    assert place["initial_option"]["text"]["text"] == "Siam Garden"
    assert place["initial_option"] in [o for g in place["option_groups"] for o in g["options"]]


async def test_a_modal_for_a_place_taken_out_of_the_catalog_has_no_choice_made(house: House) -> None:
    event_id = await decided(house)
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    gone = tuple(i for i in view.items if i.name != "Siam Garden")
    built = adjust_modal(
        replace(view, items=gone), ids.ViewOrigin(event_id, CHANNEL, run=1, opened=adjust_opened(view))
    )
    assert "initial_option" not in inputs(built)["place"]["element"]


def test_a_modal_opened_before_it_showed_the_place_still_parses() -> None:
    old = {"at": "18:30", "who": 1, "lead": 2, "how": "pickup", "pick": 3}
    assert ids.Opened.parse(old).item_id is None
    assert ids.Opened.parse(old | {"place": 9}).item_id == 9
    assert ids.Opened.parse(old | {"place": 9}).data()["place"] == 9
    with pytest.raises(ids.BadValue):
        ids.Opened.parse(old | {"place": "nine"})


def test_parsing_reads_the_place_parts() -> None:
    values = state(
        meal_time={"selected_time": "18:30"},
        coordinator={"selected_option": {"value": "2"}},
        place={"selected_option": {"value": "5"}},
        new_place={"value": "  Noodle Bar "},
        place_mode={"selected_option": {"value": "delivery"}},
        place_cuisine={"selected_option": {"value": "7"}},
    )
    parsed = parse_adjust(values)
    assert (parsed.item_id, parsed.new_place, parsed.place_mode, parsed.cuisine_id) == (
        5,
        "Noodle Bar",
        Mode.DELIVERY,
        7,
    )
    bad = values | state(place_mode={"selected_option": {"value": "teleport"}})
    with pytest.raises(Exception, match="how to get it"):
        parse_adjust(bad)


async def test_slack_saves_a_catalog_place_and_says_so(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    values = with_place(
        form(house),
        place={"selected_option": {"value": str(house.items["Taqueria"].id)}},
    )
    response = await slack.submit("Sam", ids.ADJUST_VIEW, values, event_id, origin=origin)
    assert not response.body
    assert slack.ephemerals("Sam")[-1] == "Saved. It's Taqueria, pickup, now."
    assert (await house.event(event_id)).decided_item_id == house.items["Taqueria"].id
    await slack.drain()
    updated = json.dumps([c.args for c in slack.fake.of("chat.update")])
    assert "Dinner is decided: Taqueria" in updated  # the decision, in place
    direct = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == "U04LEE"]
    assert any("It was Siam Garden." in json.dumps(c.args) for c in direct)


async def test_slack_saves_a_new_name_and_says_who_is_missing(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    values = with_place(
        form(house),
        new_place={"value": "Soup Night"},
        place_mode={"selected_option": {"value": "cook"}},
    )
    response = await slack.submit("Sam", ids.ADJUST_VIEW, values, event_id, origin=origin)
    assert not response.body
    assert slack.ephemerals("Sam")[-1] == "Saved. It's Soup Night, cook, now. Nobody is cooking yet."
    ctx = await house.context(event_id)
    assert ctx.winner is not None and ctx.items[ctx.winner.item_id or 0].name == "Soup Night"


async def test_slack_shows_a_refused_way_next_to_its_field_and_keeps_the_modal(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    values = with_place(
        form(house),
        place={"selected_option": {"value": str(tacos(house))}},
        place_mode={"selected_option": {"value": "delivery"}},
    )
    response = await slack.submit("Sam", ids.ADJUST_VIEW, values, event_id, origin=origin)
    body = json.loads(response.body)
    assert body["response_action"] == "errors" and "isn't available by delivery" in body["errors"]["place_mode"]
    assert (await house.event(event_id)).decided_item_id == house.items["Siam Garden"].id


async def test_slack_refuses_a_place_and_a_name_next_to_the_name(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    values = with_place(
        form(house),
        place={"selected_option": {"value": str(tacos(house))}},
        new_place={"value": "Soup Night"},
    )
    response = await slack.submit("Sam", ids.ADJUST_VIEW, values, event_id, origin=origin)
    assert "pick Siam Garden" in json.loads(response.body)["errors"]["new_place"]


async def test_slack_refuses_a_stale_modal_by_the_place_field(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    await change(house, "Alex", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    values = with_place(form(house), place={"selected_option": {"value": str(tacos(house))}})
    response = await slack.submit("Sam", ids.ADJUST_VIEW, values, event_id, origin=origin)
    assert json.loads(response.body)["errors"]["place"] == PLAN_CHANGED
    assert (await house.event(event_id)).decided_item_id == house.items["Taqueria"].id


async def test_slack_leaving_the_place_alone_after_it_changed_elsewhere_keeps_it(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    await change(house, "Alex", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    siam = {"selected_option": {"value": str(house.items["Siam Garden"].id)}}
    await slack.submit("Sam", ids.ADJUST_VIEW, with_place(form(house), place=siam), event_id, origin=origin)
    assert (await house.event(event_id)).decided_item_id == house.items["Taqueria"].id


async def test_adjust_saves_the_place_then_the_named_roles_then_the_hand_off(house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    assert origin.opened is not None
    shown = Adjustment(
        origin.opened.meal_time,
        origin.opened.assignee_id,
        origin.opened.coordinator_id,
        origin.opened.mode,
        origin.opened.pickup_id,
        item_id=origin.opened.item_id,
    )
    wanted = Adjustment(
        shown.meal_time,
        house["Alex"].id,
        house["Lee"].id,
        item_id=house.items["Taqueria"].id,
        place_mode=Mode.PICKUP,
    )
    changed = await act(house, coordinator.adjust, house["Sam"], event_id, wanted, opened=shown, run=1)
    assert (changed.item_id, changed.place_mode, changed.assignee_id, changed.coordinator_id) == (
        house.items["Taqueria"].id,
        Mode.PICKUP,
        house["Alex"].id,
        house["Lee"].id,
    )
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.decided_item_id == house.items["Taqueria"].id and event.coordinator_id == house["Lee"].id
    assert role_holders(ctx)[AssignmentRole.ORDER] == house["Alex"].id
    alex_told = [n for n in await sent(house, "assignment_made") if n.member.id == house["Alex"].id][-1]
    assert alex_told.event.coordinator.id == house["Lee"].id  # the held message names the new coordinator
    actions = [a.action for a in await house.audits()]
    assert actions.index("event.place_changed") < actions.index("event.coordinator_handed_off")


async def test_adjust_with_a_named_role_changed_elsewhere_is_refused(house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    assert origin.opened is not None
    shown = Adjustment(
        origin.opened.meal_time,
        origin.opened.assignee_id,
        origin.opened.coordinator_id,
        origin.opened.mode,
        origin.opened.pickup_id,
        item_id=origin.opened.item_id,
    )
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.ORDER, house["Sam"].id)
    wanted = Adjustment(assignee_id=house["Alex"].id, item_id=tacos(house), place_mode=Mode.COOK)
    with pytest.raises(AdjustmentRefused) as caught:
        await act(house, coordinator.adjust, house["Sam"], event_id, wanted, opened=shown, run=1)
    assert caught.value.code == "plan_changed"


async def test_a_refused_later_part_says_the_place_was_saved(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    values = with_place(form(house, lead="Lee"), place={"selected_option": {"value": str(tacos(house))}})

    # Lee leaves between the modal's check and the save of the hand-off: Slack's reply says both.
    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise EventRuleViolation("Pick someone who's in.", code="not_in")

    monkeypatch.setattr(coordinator, "hand_off", refuse)
    await slack.submit("Sam", ids.ADJUST_VIEW, values, event_id, origin=origin)
    said = slack.ephemerals("Sam")[-1]
    assert said.startswith("Saved. It's Sam's Tacos, cook, now.") and "The rest didn't change: Pick someone" in said


async def test_slack_app_home_shows_the_new_place(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    await slack.open_home("Sam")
    assert "Sam's Tacos" in json.dumps(slack.last_home("Sam"))


# The web ---------------------------------------------------------------------------------------------


def web_decided(admin: TestClient, clock: FakeClock, migrated: Settings) -> tuple[int, int, int]:
    """The admin coordinates and locks the first pick. Returns the call, the place, and Sam's
    Tacos, which is a home meal."""
    add_item(admin, "Sam's Tacos", kind="home", modes=["cook"], cuisines="Mexican")
    event_id = start(admin)
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/guests", {"name": "Jo", "run": "1"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/coordinate", {}).status_code == 303
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    first = proposal_ids(migrated)[0]
    assert submit(admin, f"/events/{event_id}/proposals/{first}/lock", {}).status_code == 303
    (row,) = [p for p in rows(migrated, ProposalRow) if p.id == first]
    (home,) = [i.id for i in rows(migrated, CatalogItemRow) if i.name == "Sam's Tacos"]
    return event_id, row.item_id, home


def test_web_changes_the_place_from_change_the_plan(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, home = web_decided(admin, clock, migrated)
    html = page(admin.get(f"/events/{event_id}"))
    assert "Change the plan" in html and "Change the time or who does what" not in html
    assert f'action="/events/{event_id}/place"' in html and f'name="opened_item" value="{shown}"' in html
    place_menu = html.split(f'action="/events/{event_id}/place"')[1].split('name="item_id"')[1].split("</select>")[0]
    assert f'value="{shown}"' not in place_menu and f'value="{home}"' in place_menu
    form_ = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    assert submit(admin, f"/events/{event_id}/place", form_).status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert "<h3>Sam's Tacos <span" in html.replace("&#39;", "'")
    (audit,) = [a for a in rows(migrated, AuditRow) if a.action == "event.place_changed"]
    assert audit.payload["to_item_id"] == home and audit.payload["to_mode"] == "cook"


def test_web_saves_a_new_name_over_htmx(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, _ = web_decided(admin, clock, migrated)
    form_ = {"new_place": " Noodle Bar ", "mode": "delivery", "opened_item": str(shown), "run": "1"}
    response = htmx(admin, f"/events/{event_id}/place", form_)
    assert response.status_code == 200 and "Noodle Bar" in page(response)
    (item,) = [i for i in rows(migrated, CatalogItemRow) if i.name == "Noodle Bar"]
    assert item.kind == "restaurant"


def test_web_refuses_a_way_the_place_does_not_allow_a_stale_page_and_no_change(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    event_id, shown, home = web_decided(admin, clock, migrated)
    path = f"/events/{event_id}/place"
    bad = submit(admin, path, {"item_id": str(home), "mode": "pickup", "opened_item": str(shown), "run": "1"})
    assert bad.status_code == 409 and "available by pickup" in page(bad)
    same = submit(admin, path, {"mode": "pickup", "opened_item": str(shown), "run": "1"})
    assert same.status_code == 409 and "the place already" in page(same)
    assert (
        submit(admin, path, {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}).status_code
        == 303
    )
    stale = submit(admin, path, {"new_place": "Soup", "mode": "cook", "opened_item": str(shown), "run": "1"})
    assert stale.status_code == 409 and "plan changed" in page(stale)
    assert not [i for i in rows(migrated, CatalogItemRow) if i.name == "Soup"]


@pytest.mark.parametrize(
    "bad",
    [
        {"opened_item": "x", "run": "1"},
        {"opened_item": "1", "run": ""},
        {"opened_item": "1", "run": "1", "item_id": "abc"},
    ],
)
def test_web_a_malformed_form_is_a_400(
    admin: TestClient, clock: FakeClock, migrated: Settings, bad: dict[str, str]
) -> None:
    event_id, _, _ = web_decided(admin, clock, migrated)
    assert submit(admin, f"/events/{event_id}/place", bad).status_code == 400


def test_web_refuses_a_member_who_does_not_coordinate_and_a_done_call(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    invite(admin, "kim@example.com", "Kim")
    event_id, shown, home = web_decided(admin, clock, migrated)
    form_ = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    sign_in_as(admin, provider, "kim@example.com")
    assert f'action="/events/{event_id}/place"' not in page(admin.get(f"/events/{event_id}"))
    assert submit(admin, f"/events/{event_id}/place", form_).status_code == 409
    admin.cookies.clear()
    sign_in(admin, provider)
    assert submit(admin, f"/events/{event_id}/done", {}).status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="/events/{event_id}/place"' not in html  # the admin's other corrections stay
    refused = submit(admin, f"/events/{event_id}/place", form_)
    assert refused.status_code == 409 and "no decision to change" in page(refused)
    assert member(migrated, "kim@example.com").display_name == "Kim"


def test_web_history_shows_the_new_place(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, home = web_decided(admin, clock, migrated)
    form_ = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    assert submit(admin, f"/events/{event_id}/place", form_).status_code == 303
    assert submit(admin, f"/events/{event_id}/done", {}).status_code == 303
    assert "Sam's Tacos" in page(admin.get("/history"))
    (event_row,) = rows(migrated, EventRow)
    assert (event_row.state, event_row.decided_item_id) == (EventState.DONE.value, home)


async def test_everything_after_the_change_still_finds_the_place(house: House) -> None:
    event_id = await decided(house)
    await change(house, "Sam", event_id, name="Noodle Bar", mode=Mode.PICKUP)
    await act(house, calls.set_meal_time, house["Sam"], event_id, time(19, 0))
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.ORDER, house["Sam"].id)
    await act(house, coordinator.choose_roles, house["Sam"], event_id, mode=Mode.PICKUP, member_id=house["Lee"].id)
    (*_, updated) = await sent(house, "decision_updated")
    assert updated.decision.proposal.item.name == "Noodle Bar"
    assert updated.decision.assignments
    await act(house, calls.cancel_call, house["Sam"], event_id)
    (*_, cancelled) = await sent(house, "decision_updated")
    assert "~Noodle Bar~" in json.dumps(render(cancelled, CTX).payload())


# A modal or page left open while the place changed --------------------------------------------------


def as_opened(origin: ids.ViewOrigin) -> Adjustment:
    shown = origin.opened
    assert shown is not None
    return Adjustment(
        shown.meal_time, shown.assignee_id, shown.coordinator_id, shown.mode, shown.pickup_id, shown.item_id
    )


@pytest.mark.parametrize("part", ["assignee", "mode", "pickup"])
async def test_adjust_opened_on_another_place_saves_no_roles_on_this_one(house: House, part: str) -> None:
    event_id = await decided(house)
    opened = as_opened(await opened_now(house, event_id))
    await house.add_item("Noodle Bar", Mode.PICKUP, Mode.DELIVERY)
    await change(house, "Alex", event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.PICKUP)
    before = (await house.event(event_id)), role_holders(await house.context(event_id))
    audits = len(await house.audits())
    field = {
        "assignee": {"assignee_id": house["Alex"].id},
        "mode": {"mode": Mode.DELIVERY},
        "pickup": {"pickup_id": house["Lee"].id},
    }
    with pytest.raises(AdjustmentRefused) as caught:
        await act(
            house, coordinator.adjust, house["Sam"], event_id, replace(opened, **field[part]), opened=opened, run=1
        )
    assert (caught.value.part, caught.value.code, str(caught.value)) == (part, "plan_changed", PLAN_CHANGED)
    assert (await house.event(event_id)).decided_item_id == before[0].decided_item_id
    assert role_holders(await house.context(event_id)) == before[1] and len(await house.audits()) == audits


async def test_the_role_step_under_the_lock_names_the_place_it_changed_from(house: House) -> None:
    event_id = await decided(house)
    opened = as_opened(await opened_now(house, event_id))
    await change(house, "Alex", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    with pytest.raises(AdjustmentRefused) as caught:
        coordinator._still(opened, "role")(await house.context(event_id))
    assert caught.value.part == "place"


async def test_slack_adjust_opened_on_another_place_refuses_a_new_way_by_its_field(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    await change(house, "Alex", event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    values = with_place(form(house), mode={"selected_option": {"value": "delivery"}})
    response = await slack.submit("Sam", ids.ADJUST_VIEW, values, event_id, origin=origin)
    assert json.loads(response.body)["errors"]["mode"] == PLAN_CHANGED
    assert (await house.event(event_id)).decided_mode is Mode.PICKUP


async def assign_form(house: House, way: str = "delivery") -> dict[str, Any]:
    return state(mode={"selected_option": {"value": way}}, who={"selected_option": {"value": str(house["Lee"].id)}})


async def test_slack_assign_roles_opened_on_another_place_or_way_saves_nothing(slack: Slack, house: House) -> None:
    event_id = await pending(house)
    await slack.click("Sam", ids.ASSIGN_ROLES, ids.card_value(event_id, 1))
    (opened,) = slack.fake.of("views.open")
    origin = ids.ViewOrigin.decode(opened.args["view"]["private_metadata"])
    assert (origin.place, origin.way) == (house.items["Siam Garden"].id, Mode.PICKUP)
    await house.add_item("Noodle Bar", Mode.PICKUP, Mode.DELIVERY)
    await change(house, "Alex", event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.PICKUP)
    await slack.submit("Sam", ids.ASSIGN_VIEW, await assign_form(house), event_id, origin=origin)
    assert slack.ephemerals("Sam")[-1] == PLAN_CHANGED
    event = await house.event(event_id)
    assert event.decided_mode is Mode.PICKUP and event.roles_due_at is not None
    assert role_holders(await house.context(event_id)) == {}

    same_place = replace(origin, place=event.decided_item_id, way=Mode.DELIVERY)  # the way moved on instead
    await slack.submit("Sam", ids.ASSIGN_VIEW, await assign_form(house), event_id, origin=same_place)
    assert slack.ephemerals("Sam")[-1] == PLAN_CHANGED
    assert (await house.event(event_id)).decided_mode is Mode.PICKUP


async def test_slack_assign_roles_opened_before_it_carried_the_place_still_works(slack: Slack, house: House) -> None:
    event_id = await pending(house)
    old = ids.ViewOrigin(event_id, CHANNEL, run=1)
    await slack.submit("Sam", ids.ASSIGN_VIEW, await assign_form(house), event_id, origin=old)
    event = await house.event(event_id)
    assert event.decided_mode is Mode.DELIVERY and event.roles_due_at is None


def test_view_origin_round_trips_what_assign_roles_showed() -> None:
    origin = ids.ViewOrigin(5, CHANNEL, run=1, place=9, way=Mode.COOK)
    assert ids.ViewOrigin.decode(origin.encode()) == origin
    for bad in ({"place": "nine"}, {"way": "teleport"}):
        with pytest.raises(ids.BadValue):
            ids.ViewOrigin.decode(json.dumps({"event": 5, "channel": None, "home": False, **bad}))


def test_web_assign_roles_left_open_is_refused_and_an_old_page_still_works(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    event_id, shown, home = web_decided(admin, clock, migrated)
    alex = member(migrated, "admin@example.com").id
    html = page(admin.get(f"/events/{event_id}"))
    assert f'name="shown_item" value="{shown}"' in html and 'name="shown_way" value="pickup"' in html
    path = f"/events/{event_id}/roles"
    roles = {"mode": "cook", "member_id": str(alex), "run": "1"}
    place = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    assert submit(admin, f"/events/{event_id}/place", place).status_code == 303
    stale = submit(admin, path, roles | {"shown_item": str(shown), "shown_way": "pickup"})
    assert stale.status_code == 409 and "The plan changed since you opened this. Open it again." in page(stale).replace(
        "&#39;", "'"
    )
    way = submit(admin, path, roles | {"shown_item": str(home), "shown_way": "pickup"})
    assert way.status_code == 409
    assert submit(admin, path, roles | {"shown_item": "abc"}).status_code == 400
    assert submit(admin, path, roles | {"shown_item": str(home), "shown_way": "cook"}).status_code == 303
    assert submit(admin, path, roles).status_code == 303  # a page from before it sent them


# One message per person ---------------------------------------------------------------------------------


async def direct(house: House, skip: int) -> Counter[int | None]:
    """How many direct messages each person gets from outbox rows after the first ``skip``."""
    kinds = {"place_changed", "assignment_made", "assignment_ended"}
    rows_ = (await house.outbox())[skip:]
    return Counter(from_payload(e.kind, e.payload).recipient_id for e in rows_ if e.kind in kinds)


async def test_a_lost_role_and_a_kept_one_each_get_the_one_message_about_the_change(house: House) -> None:
    event_id = await decided(house)
    skip = len(await house.outbox())
    await house.add_item("Noodle Bar", Mode.DELIVERY)
    await change(house, "Sam", event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.DELIVERY)
    assert await direct(house, skip) == {house["Alex"].id: 1, house["Lee"].id: 1}  # Sam changed it
    by_person = {n.member.id: n for n in await sent(house, "place_changed")}
    assert by_person[house["Lee"].id].ended_roles == ()
    assert by_person[house["Alex"].id].ended_roles == (AssignmentRole.PICKUP,)


async def test_a_new_role_is_the_one_message_and_it_names_the_old_place(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), cooks=("Sam",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    skip = len(await house.outbox())
    await change(house, "Alex", event_id, item_id=tacos(house), mode=Mode.COOK)
    assert await direct(house, skip) == {house["Sam"].id: 1, house["Lee"].id: 1}  # Alex changed it
    made = [n for n in await sent(house, "assignment_made") if n.member.id == house["Sam"].id][-1]
    assert made.was == "Siam Garden"
    text = json.dumps(render(made, CTX).payload(), ensure_ascii=False)
    assert (
        "*You're cooking and coordinating dinner" in text
        and "It was Siam Garden." in text
        and "• *When:* 6:30 PM" in text
    )
    (lee,) = await sent(house, "place_changed")
    assert lee.ended_roles == (AssignmentRole.ORDER,)


async def test_trading_one_role_for_another_is_one_message(house: House) -> None:
    event_id = await decided(house)  # Lee orders, Alex picks up
    skip = len(await house.outbox())
    await change(
        house,
        "Sam",
        event_id,
        item_id=house.items["Taqueria"].id,
        mode=Mode.PICKUP,
        member_id=house["Alex"].id,
        pickup_id=house["Lee"].id,
    )
    assert await direct(house, skip) == {house["Alex"].id: 1, house["Lee"].id: 1}
    assert not await sent(house, "place_changed") and not await sent(house, "assignment_ended")


async def test_whoever_changes_it_hears_only_about_a_role_they_took(house: House) -> None:
    event_id = await decided(house)
    skip = len(await house.outbox())
    await change(house, "Sam", event_id, name="Soup", mode=Mode.COOK, member_id=house["Sam"].id)
    counted = await direct(house, skip)
    assert counted == {house["Sam"].id: 1, house["Alex"].id: 1, house["Lee"].id: 1}
    assert house["Sam"].id not in await told(house, "place_changed")


async def test_with_a_hand_off_the_new_coordinator_hears_of_the_place_and_of_their_roles(house: House) -> None:
    event_id = await decided(house)
    opened = as_opened(await opened_now(house, event_id))
    skip = len(await house.outbox())
    wanted = replace(
        opened,
        assignee_id=house["Alex"].id,  # Alex orders now, and hears from the held message
        pickup_id=house["Lee"].id,  # Lee picks up and coordinates: the held message is skipped
        coordinator_id=house["Lee"].id,
        item_id=house.items["Taqueria"].id,
        place_mode=Mode.PICKUP,
    )
    await act(house, coordinator.adjust, house["Sam"], event_id, wanted, opened=opened, run=1)
    new = [from_payload(e.kind, e.payload) for e in (await house.outbox())[skip:]]
    (place,) = [n for n in new if isinstance(n, PlaceChanged)]
    assert (place.member.id, place.was, place.ended_roles) == (house["Lee"].id, "Siam Garden", (AssignmentRole.ORDER,))
    made = {n.member.id: n for n in new if isinstance(n, AssignmentMade)}
    assert set(made) == {house["Alex"].id, house["Lee"].id} and await direct(house, skip) == {
        house["Alex"].id: 1,
        house["Lee"].id: 2,  # the change, and the hand-off's own message with all their roles
    }
    assert made[house["Alex"].id].was == "Siam Garden"
    assert made[house["Alex"].id].event.coordinator.id == house["Lee"].id
    assert made[house["Lee"].id].was is None
    assert set(made[house["Lee"].id].roles) == {AssignmentRole.PICKUP, AssignmentRole.COORDINATOR}


async def test_a_guest_who_loses_a_role_is_told_through_whoever_invited_them(house: House) -> None:
    pat = await add_guest(house, "Pat")
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, guests.invite, house["Lee"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.offer_role, pat, event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, calls.lock, house["Sam"], event_id, (await pick_ids(house, event_id))["Taqueria"])
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == pat.id
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    (through,) = [n for n in await sent(house, "place_changed") if n.member.id == pat.id]
    assert through.via is not None and through.via.id == house["Lee"].id
    assert "Your guest Pat is no longer picking up." in json.dumps(render(through, CTX).payload())


async def test_an_inviter_who_changes_the_place_is_not_told_their_guest_lost_a_role(house: House) -> None:
    pat = await add_guest(house, "Pat")
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.offer_role, pat, event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, calls.lock, house["Sam"], event_id, (await pick_ids(house, event_id))["Taqueria"])
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == pat.id
    skip = len(await house.outbox())
    await change(house, "Sam", event_id, item_id=tacos(house), mode=Mode.COOK)
    assert AssignmentRole.PICKUP not in assigned(await house.context(event_id))
    counted = await direct(house, skip)
    assert house["Sam"].id not in counted and counted == {house["Alex"].id: 1, house["Lee"].id: 1}
    assert not await sent(house, "assignment_ended")


async def test_after_the_meal_time_no_case_messages_anyone(house: House) -> None:
    event_id = await decided(house)
    event = await house.event(event_id)
    assert event.meal_time is not None
    house.clock.now = event.meal_time
    skip = len(await house.outbox())
    await change(house, "Sam", event_id, name="Soup", mode=Mode.COOK, member_id=house["Lee"].id)
    assert await direct(house, skip) == {}


async def test_after_the_meal_time_a_hand_off_still_tells_whoever_takes_over(house: House) -> None:
    event_id = await decided(house)
    opened = as_opened(await opened_now(house, event_id))
    event = await house.event(event_id)
    assert event.meal_time is not None
    house.clock.now = event.meal_time
    skip = len(await house.outbox())
    wanted = replace(
        opened,
        assignee_id=house["Alex"].id,
        coordinator_id=house["Lee"].id,
        item_id=house.items["Taqueria"].id,
        place_mode=Mode.PICKUP,
    )
    await act(house, coordinator.adjust, house["Sam"], event_id, wanted, opened=opened, run=1)
    assert await direct(house, skip) == {house["Lee"].id: 1}  # not Alex, who got a role, or anyone else
    (made,) = [n for n in (await sent(house, "assignment_made")) if n.member.id == house["Lee"].id][-1:]
    assert AssignmentRole.COORDINATOR in made.roles and made.decision.proposal.item.name == "Taqueria"
    assert not [e for e in (await house.outbox())[skip:] if e.kind == "place_changed"]
