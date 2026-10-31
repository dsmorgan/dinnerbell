# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coordinating, complete (#154): whoever coordinates is in, and stops when they're out;
**Adjust** switches the way of getting the meal; the coordinator corrects who did what until the
meal is done; and each step of an Adjust checks, under its lock, that its part is as the modal
showed it."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.messages import render
from dinnerbell.config import Settings
from dinnerbell.db.contributions import ContributionRepository
from dinnerbell.db.models import AssignmentRow, CatalogItemRow, ProposalRow, ResponseRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import (
    TAKEN_OFF,
    AssignmentRole,
    EventRuleViolation,
    EventState,
    RoleOffer,
    Status,
    counted_votes,
)
from dinnerbell.services import call_admin, calls, coordinator, event_views, guests, members
from dinnerbell.services import call_context as cc
from dinnerbell.services.coordinator import PLAN_CHANGED, Adjustment, AdjustmentRefused, PartlyAdjusted
from tests.conftest import invite, page
from tests.support.calls import RESPOND_BY, VOTE_BY, House
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import state
from tests.support.slack import Slack, wire
from tests.support.web import add_item, submit
from tests.test_auth_flow import member, rows
from tests.test_coordinator import CTX, act, add_guest, assigned, open_dinner, pick_ids, sent, to_voting, vote
from tests.test_coordinator import database as database  # the fixtures, re-exported for pytest
from tests.test_coordinator import house as house
from tests.test_events_web import admin as admin
from tests.test_events_web import sign_in_as, start
from tests.test_events_web import tick as web_tick
from tests.test_slack_adjust import as_is, decided, meal, opened_now
from tests.test_slack_cards import action_ids, selected, text_of

pytestmark = pytest.mark.anyio

AFTER_THE_MEAL = VOTE_BY.replace(hour=23, minute=45)
"""After the 6:30 PM meal, and before the scheduler would mark it done."""


async def answer(house: House, event_id: int, name: str) -> Any:
    return (await house.context(event_id)).response_of(house[name].id)


async def coordinator_of(house: House, event_id: int) -> int | None:
    return (await house.event(event_id)).coordinator_id


async def released(house: House) -> list[dict[str, Any]]:
    return [a.payload for a in await house.audits("event.coordinator_released")]


# Coordinating puts you in -----------------------------------------------------------------------


@pytest.mark.parametrize("before", ["never", Status.OUT, Status.MAYBE])
async def test_claiming_while_answers_are_open_puts_you_in(house: House, before: object) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    if isinstance(before, Status):
        await act(house, calls.respond, house["Kim"], event_id, before)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    kim = await answer(house, event_id, "Kim")
    assert (kim.status, kim.late) == (Status.IN, False)
    assert await coordinator_of(house, event_id) == house["Kim"].id
    latest = (await sent(house, "call_updated"))[-1]
    assert latest.event.coordinator.id == house["Kim"].id
    assert any(r.member.id == house["Kim"].id and r.status is Status.IN for r in latest.roster)


async def test_claiming_while_voting_joins_late_with_the_vote_counting(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)  # Kim never answered
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    kim = await answer(house, event_id, "Kim")
    assert (kim.status, kim.late, kim.vote_counts) == (Status.IN, True, True)  # in, late, and her vote counts
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    await vote(house, "Kim", event_id, siam)  # no "join late first": she's in
    ctx = await house.context(event_id)
    assert house["Kim"].id in counted_votes(ctx.votes, ctx.responses)[siam]


async def test_a_late_joiner_switched_off_cannot_claim_it(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.switch_late_joiner, house["Alex"], event_id, house["Kim"].id, taking_part=False)
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, calls.claim_coordinator, house["Kim"], event_id)
    assert (str(refused.value), refused.value.code) == (TAKEN_OFF, "taken_off")
    assert not (await act(house, event_views.event_view, house["Kim"], event_id)).can.claim
    assert await coordinator_of(house, event_id) is None


async def test_a_guest_who_said_out_claims_it_and_is_in(house: House) -> None:
    pat = await add_guest(house, "Pat")
    event_id = await open_dinner(house)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.OUT)
    await act(house, calls.claim_coordinator, pat, event_id)
    assert (await answer(house, event_id, "Pat")).status is Status.IN
    assert await coordinator_of(house, event_id) == pat.id


async def test_handing_off_or_an_admin_assigning_it_puts_them_in(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Lee"], event_id, Status.OUT)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    await act(house, calls.hand_off, house["Sam"], event_id, house["Kim"].id)  # Kim hadn't answered
    assert (await answer(house, event_id, "Kim")).status is Status.IN
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Lee"].id)  # Lee said out
    assert (await answer(house, event_id, "Lee")).status is Status.IN
    assert await coordinator_of(house, event_id) == house["Lee"].id


async def test_once_decided_only_someone_in_can_be_given_it(house: House) -> None:
    event_id = await decided(house)  # Kim never answered
    for use in (
        lambda: act(house, calls.hand_off, house["Sam"], event_id, house["Kim"].id),
        lambda: act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id),
    ):
        with pytest.raises(EventRuleViolation) as refused:
            await use()
        assert refused.value.code == "not_in"
    assert await answer(house, event_id, "Kim") is None  # nobody joins once it's decided
    assert await coordinator_of(house, event_id) == house["Sam"].id


async def test_a_guest_who_coordinates_while_voting_keeps_their_needs(house: House) -> None:
    """A guest is in, never late, so their own dietary needs still hold (FR-57)."""
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    event_id = await open_dinner(house)
    async with house.sessionmaker() as db:
        pat = await MemberRepository(db).add_guest(
            household_id=house.household.id, display_name="Pat", dietary_needs=frozenset({veg.id}), now=house.clock()
        )
        await db.commit()
    house.people["Pat"] = pat
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.OUT)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, calls.claim_coordinator, pat, event_id)
    ctx = await house.context(event_id)
    pats = ctx.response_of(pat.id)
    assert pats is not None and (pats.status, pats.late) == (Status.IN, False)
    assert veg.id in ctx.guest_needs
    assert "Vegetarian" in cc.guest_dietary_notes(ctx)


async def test_a_late_joiner_who_takes_it_on_gets_a_counting_vote(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)
    await act(house, calls.join_late, house["Kim"], event_id)
    assert not (await answer(house, event_id, "Kim")).vote_counts
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    kim = await answer(house, event_id, "Kim")
    assert (kim.late, kim.vote_counts) == (True, True)
    assert not await sent(house, "coordinating_given")  # she was in already, and claimed it herself


async def test_whoever_is_put_in_by_a_hand_off_is_told(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    await act(house, calls.respond, house["Lee"], event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    await act(house, calls.hand_off, house["Sam"], event_id, house["Lee"].id)  # in already: nothing new to say
    assert not await sent(house, "coordinating_given")
    await act(house, calls.hand_off, house["Lee"], event_id, house["Kim"].id)
    (told,) = await sent(house, "coordinating_given")
    assert (told.member.id, told.by.id, told.late) == (house["Kim"].id, house["Lee"].id, False)
    text = json.dumps(render(told, CTX).payload())
    assert "Lee gave you coordinating dinner" in text and "so you're in" in text


async def test_an_admin_assigning_it_while_voting_says_they_joined_late(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id)
    (told,) = await sent(house, "coordinating_given")
    assert (told.member.id, told.late) == (house["Kim"].id, True)


async def test_a_late_joiner_switched_off_is_not_handed_it_either(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name="Sam")
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.switch_late_joiner, house["Sam"], event_id, house["Kim"].id, taking_part=False)
    for use in (
        lambda: act(house, calls.hand_off, house["Sam"], event_id, house["Kim"].id),
        lambda: act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Kim"].id),
    ):
        with pytest.raises(EventRuleViolation) as refused:
            await use()
        assert refused.value.code == "taken_off"
    assert (await answer(house, event_id, "Kim")).status is Status.OUT


async def test_deactivating_the_coordinator_releases_their_calls(house: House) -> None:
    event_id = await decided(house)  # Sam coordinates
    async with house.sessionmaker() as db:
        await members.set_member_active(db, house["Alex"], house["Sam"].id, False, now=house.clock())
    assert await coordinator_of(house, event_id) is None
    assert AssignmentRole.COORDINATOR not in assigned(await house.context(event_id))
    assert await released(house) == [{"member_id": house["Sam"].id, "why": "deactivated"}]


# Out lets it go ---------------------------------------------------------------------------------


async def test_saying_out_lets_coordinating_go(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    assert await coordinator_of(house, event_id) is None
    assert await released(house) == [{"member_id": house["Kim"].id, "why": "out"}]
    latest = (await sent(house, "call_updated"))[-1]
    assert latest.event.coordinator is None  # the message says nobody coordinates, so anyone can claim it
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    assert await coordinator_of(house, event_id) == house["Sam"].id


async def test_a_maybe_keeps_coordinating_until_answers_close(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, calls.respond, house["Kim"], event_id, Status.MAYBE)
    assert await coordinator_of(house, event_id) == house["Kim"].id  # she may still come
    assert not await released(house)
    house.clock.now = RESPOND_BY
    await house.tick()
    event = await house.event(event_id)
    assert (event.state, event.coordinator_id) == (EventState.VOTING, None)  # the maybe is out now
    assert await released(house) == [{"member_id": house["Kim"].id, "why": "maybe"}]
    assert [n.member.id for n in await sent(house, "maybe_resolved")] == [house["Kim"].id]
    assert (await sent(house, "call_updated"))[-1].event.coordinator is None


async def test_a_coordinator_switched_off_as_a_late_joiner_lets_it_go(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)  # joins late
    await act(house, calls.switch_late_joiner, house["Kim"], event_id, house["Kim"].id, taking_part=False)
    assert await coordinator_of(house, event_id) is None
    assert await released(house) == [{"member_id": house["Kim"].id, "why": "taken_off"}]


async def test_answering_out_for_a_guest_who_coordinates_lets_it_go(house: House) -> None:
    pat = await add_guest(house, "Pat")  # no Slack: Sam answers for them
    event_id = await open_dinner(house)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    await act(house, calls.hand_off, house["Sam"], event_id, pat.id)
    await act(house, guests.answer_for, house["Sam"], event_id, pat.id, status=Status.OUT)
    assert await coordinator_of(house, event_id) is None
    assert await released(house) == [{"member_id": pat.id, "why": "out"}]


# Adjust switches the way of getting it -----------------------------------------------------------


def opened_as_is(house: House) -> Adjustment:
    return replace(as_is(house), mode=Mode.PICKUP)


async def adjust(house: House, wanted: Adjustment, event_id: int, *, opened: Adjustment | None = None) -> Adjustment:
    shown = opened or opened_as_is(house)
    return await act(house, coordinator.adjust, house["Sam"], event_id, wanted, opened=shown, run=1)  # type: ignore[no-any-return]


async def test_adjust_switches_the_way_and_whoever_did_it_does_it_that_way(house: House) -> None:
    event_id = await decided(house)  # Alex picks up Siam Garden
    changed = await adjust(house, replace(opened_as_is(house), mode=Mode.DELIVERY), event_id)
    assert changed == Adjustment(assignee_id=house["Alex"].id, mode=Mode.DELIVERY)
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.decided_mode is Mode.DELIVERY
    roles = assigned(ctx)
    assert roles[AssignmentRole.ORDER] == house["Alex"].id and AssignmentRole.PICKUP not in roles
    (audit,) = await house.audits("event.roles_assigned")
    assert (audit.payload["mode"], audit.payload["switched_from"]) == ("delivery", "pickup")
    told = [n for n in await sent(house, "assignment_made") if n.member.id == house["Alex"].id]
    assert AssignmentRole.ORDER in told[-1].roles


async def test_adjust_switches_the_way_and_who_together(house: House) -> None:
    event_id = await decided(house)
    wanted = replace(opened_as_is(house), mode=Mode.DELIVERY, assignee_id=house["Lee"].id)
    assert await adjust(house, wanted, event_id) == Adjustment(assignee_id=house["Lee"].id, mode=Mode.DELIVERY)
    assert assigned(await house.context(event_id))[AssignmentRole.ORDER] == house["Lee"].id


async def test_adjust_refuses_a_way_the_place_has_not(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(AdjustmentRefused) as refused:
        await adjust(house, replace(opened_as_is(house), mode=Mode.COOK), event_id)
    assert refused.value.part == "mode" and "isn't available by cook" in str(refused.value)
    assert (await house.event(event_id)).decided_mode is Mode.PICKUP


async def test_a_way_changed_elsewhere_since_opening_is_refused(house: House) -> None:
    event_id = await decided(house)
    await act(
        house, coordinator.choose_roles, house["Sam"], event_id, mode=Mode.DELIVERY, member_id=house["Alex"].id, run=1
    )  # on the web, say
    for wanted, part in (
        (replace(opened_as_is(house), assignee_id=house["Lee"].id), "assignee"),
        (replace(opened_as_is(house), mode=Mode.DELIVERY, assignee_id=house["Lee"].id), "mode"),
    ):
        with pytest.raises(AdjustmentRefused) as refused:
            await adjust(house, wanted, event_id)
        assert (refused.value.part, str(refused.value)) == (part, PLAN_CHANGED)
    assert assigned(await house.context(event_id))[AssignmentRole.ORDER] == house["Alex"].id


def test_opened_names_the_way_and_reads_a_modal_opened_before_it_did() -> None:
    opened = ids.Opened(time(18, 30), 2, 1, Mode.PICKUP)
    assert ids.Opened.parse(opened.data()) == opened
    assert ids.Opened.parse({"at": "18:30", "who": 2, "lead": 1}) == ids.Opened(time(18, 30), 2, 1)
    for bad in ({"at": "18:30", "who": 2, "lead": 1, "how": "teleport"}, {"at": "18:30", "who": 2, "how": "cook"}):
        with pytest.raises(ids.BadValue):
            ids.Opened.parse(bad)


@pytest.fixture
async def slack(house: House) -> Any:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def test_slack_adjust_switches_the_way(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    assert origin.opened is not None and origin.opened.mode is Mode.PICKUP
    form = state(
        meal_time={"selected_time": "18:30"},
        mode={"selected_option": {"value": "delivery"}},
        who={"selected_option": {"value": str(house["Alex"].id)}},
        coordinator={"selected_option": {"value": str(house["Sam"].id)}},
    )
    response = await slack.submit("Sam", ids.ADJUST_VIEW, form, event_id, origin=origin)
    assert not response.body
    assert slack.ephemerals("Sam")[-1] == "Saved. It's delivery now. Alex is ordering, and is told."
    assert (await house.event(event_id)).decided_mode is Mode.DELIVERY
    bad = form | state(mode={"selected_option": {"value": "teleport"}})
    refused = await slack.submit("Sam", ids.ADJUST_VIEW, bad, event_id, origin=origin)
    assert json.loads(refused.body)["errors"] == {"mode": "Pick how to get it."}


async def test_slack_card_coordinate_puts_you_in_and_out_lets_it_go(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    await slack.click("Kim", "respond_out", str(event_id))
    card = slack.cards("Kim")[-1]
    assert selected(card) == ["✓ Out"]
    await slack.tap_card("Kim", "card_coordinate", card)
    card = slack.cards("Kim")[-1]
    assert selected(card) == ["✓ In"] and "You're coordinating." in text_of(card)
    await slack.tap_card("Kim", "card_out", card)
    card = slack.cards("Kim")[-1]
    assert selected(card) == ["✓ Out"] and "card_coordinate" in action_ids(card)  # anyone may claim it again
    assert await coordinator_of(house, event_id) is None


async def test_slack_home_says_coordinating_puts_you_in(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    await slack.click("Kim", "card_coordinate", ids.card_value(event_id, 1), home=True)
    assert "You're in, and coordinating dinner. Thanks for steering!" in json.dumps(slack.last_home("Kim"))
    assert (await answer(house, event_id, "Kim")).status is Status.IN


async def test_whoever_loses_a_role_is_told_before_the_meal(house: House) -> None:
    event_id = await decided(house)  # Alex picks up
    await adjust(house, replace(opened_as_is(house), assignee_id=house["Lee"].id), event_id)
    (ended,) = await sent(house, "assignment_ended")
    assert (ended.member.id, ended.roles, ended.by.id) == (house["Alex"].id, (AssignmentRole.PICKUP,), house["Sam"].id)
    assert "you're no longer picking up" in json.dumps(render(ended, CTX).payload())
    # The admin on the web: Lee hears too.
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, house["Sam"].id)
    assert [n.member.id for n in await sent(house, "assignment_ended")] == [house["Alex"].id, house["Lee"].id]


async def test_switching_the_way_for_the_same_person_tells_them_only_the_new_role(house: House) -> None:
    event_id = await decided(house)
    await adjust(house, replace(opened_as_is(house), mode=Mode.DELIVERY), event_id)
    assert not await sent(house, "assignment_ended")


async def test_after_the_meal_a_correction_messages_nobody(house: House) -> None:
    event_id = await decided(house)
    house.clock.now = AFTER_THE_MEAL
    made = len(await sent(house, "assignment_made"))
    await adjust(house, replace(opened_as_is(house), assignee_id=house["Lee"].id), event_id)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Alex"].id)
    assert len(await sent(house, "assignment_made")) == made
    assert not await sent(house, "assignment_ended")


# Each step checks its part under the lock ------------------------------------------------------


@pytest.fixture
def meanwhile(monkeypatch: pytest.MonkeyPatch, house: House) -> list[Any]:
    """Changes to make after ``plan_adjustment`` checked the modal and before ``adjust`` saves,
    as someone on the web might."""
    changes: list[Any] = []
    plan = coordinator.plan_adjustment

    async def plan_then_change(*args: Any, **kwargs: Any) -> Adjustment:
        planned = await plan(*args, **kwargs)
        for change in changes:
            await change()
        return planned

    monkeypatch.setattr(coordinator, "plan_adjustment", plan_then_change)
    return changes


async def test_a_role_changed_after_the_check_is_refused_not_written_over(house: House, meanwhile: list[Any]) -> None:
    event_id = await decided(house)
    meanwhile.append(
        lambda: act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, house["Sam"].id)
    )
    with pytest.raises(AdjustmentRefused) as refused:
        await adjust(house, replace(opened_as_is(house), assignee_id=house["Lee"].id), event_id)
    assert (refused.value.part, str(refused.value)) == ("assignee", PLAN_CHANGED)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Sam"].id  # the admin's stands


async def test_a_later_part_changed_after_the_check_says_what_was_saved(house: House, meanwhile: list[Any]) -> None:
    event_id = await decided(house)
    meanwhile.append(
        lambda: act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, house["Sam"].id)
    )
    wanted = replace(opened_as_is(house), meal_time=time(19, 0), assignee_id=house["Lee"].id)
    with pytest.raises(PartlyAdjusted) as partly:
        await adjust(house, wanted, event_id)
    assert partly.value.saved == Adjustment(meal_time=time(19, 0)) and str(partly.value) == PLAN_CHANGED
    assert await meal(house, event_id) == time(19, 0)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Sam"].id


async def test_a_meal_time_changed_after_the_check_is_refused(house: House, meanwhile: list[Any]) -> None:
    event_id = await decided(house)
    meanwhile.append(lambda: act(house, calls.set_meal_time, house["Alex"], event_id, time(19, 0)))
    with pytest.raises(AdjustmentRefused) as refused:
        await adjust(house, replace(opened_as_is(house), meal_time=time(19, 30)), event_id)
    assert (refused.value.part, str(refused.value)) == ("meal_time", PLAN_CHANGED)
    assert await meal(house, event_id) == time(19, 0)


async def test_a_coordinator_changed_after_the_check_is_refused(house: House, meanwhile: list[Any]) -> None:
    event_id = await decided(house)
    meanwhile.append(lambda: act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Lee"].id))
    with pytest.raises(AdjustmentRefused) as refused:
        await adjust(house, replace(opened_as_is(house), coordinator_id=house["Alex"].id), event_id)
    assert (refused.value.part, str(refused.value)) == ("coordinator", PLAN_CHANGED)
    assert await coordinator_of(house, event_id) == house["Lee"].id


# Correcting who did what after the meal ---------------------------------------------------------


async def test_the_coordinator_corrects_who_did_what_until_it_is_done(house: House) -> None:
    event_id = await decided(house)  # Alex was to pick up
    house.clock.now = AFTER_THE_MEAL  # Lee went instead
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Lee"].id)
    await act(house, calls.mark_done, house["Sam"], event_id)
    async with house.sessionmaker() as db:
        counts = ContributionRepository(db)
        lee = await counts.counts(house.household.id, house["Lee"].id)
        alex = await counts.counts(house.household.id, house["Alex"].id)
    assert (lee.get(Kind.PICKED_UP, 0), alex.get(Kind.PICKED_UP, 0)) == (1, 0)
    with pytest.raises(EventRuleViolation) as refused:  # once done, it's the admin's
        await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Alex"].id)
    assert refused.value.code == "not_decided"
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, house["Alex"].id)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Alex"].id


async def test_adjust_works_after_the_meal_time_too(house: House) -> None:
    event_id = await decided(house)
    house.clock.now = AFTER_THE_MEAL
    await adjust(house, replace(opened_as_is(house), assignee_id=house["Lee"].id), event_id)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id


# The web --------------------------------------------------------------------------------------------


def test_web_claiming_puts_you_in_and_saying_out_lets_it_go(
    admin: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    invite(admin, "kim@example.com", "Kim")
    event_id = start(admin)
    sign_in_as(admin, provider, "kim@example.com")
    kim = member(migrated, "kim@example.com")
    assert submit(admin, f"/events/{event_id}/coordinate", {}).status_code == 303
    assert [(r.member_id, r.status) for r in rows(migrated, ResponseRow)] == [(kim.id, "in")]
    assert "Coordinator: Kim." in page(admin.get(f"/events/{event_id}"))
    assert submit(admin, f"/events/{event_id}/respond", {"status": "out"}).status_code == 303
    assert "No coordinator yet." in page(admin.get(f"/events/{event_id}"))


def test_web_coordinator_switches_the_way_after_the_decision(
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
    assert submit(admin, f"/events/{event_id}/proposals/{pho}/lock", {}).status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="/events/{event_id}/roles"' in html and '<select name="mode">' in html
    kim = member(migrated, "kim@example.com")
    switched = {"mode": "delivery", "member_id": str(kim.id), "run": "1"}
    assert submit(admin, f"/events/{event_id}/roles", switched).status_code == 303
    assert [(a.role, a.member_id) for a in rows(migrated, AssignmentRow) if a.role != "coordinator"] == [
        ("order", kim.id)
    ]
