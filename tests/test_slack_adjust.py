# SPDX-License-Identifier: AGPL-3.0-or-later
"""**Adjust** (spec 5.7, #149): the coordinator changes the meal time, who does the role, and who
coordinates, in one Slack modal. Only what changed is saved, through the use cases the web uses;
everyone else, an admin too, is refused."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import GUEST_TAPS
from dinnerbell.channels.slack.messages import render
from dinnerbell.channels.slack.views import adjust_opened
from dinnerbell.domain.events import (
    ADJUST_IS_THE_COORDINATORS,
    STALE_CALL,
    AssignmentRole,
    EventRuleViolation,
    RoleOffer,
    StaleRun,
    Status,
)
from dinnerbell.services import calls, coordinator, event_views, guests
from dinnerbell.services.coordinator import Adjustment, AdjustmentRefused
from dinnerbell.services.errors import NotFound
from tests.support.calls import RESPOND_BY, VOTE_BY, House
from tests.support.fake_slack import CHANNEL, state
from tests.support.slack import Slack, wire
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

pytestmark = pytest.mark.anyio


async def decided(house: House) -> int:
    """Sam coordinates; Alex (the admin) picks up Siam Garden; Lee is in and has no role; Kim never
    answered. Decided at 5:15 PM, for 6:30 PM."""
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    return event_id


async def meal(house: House, event_id: int) -> time:
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    return view.decision.meal_time.astimezone(ZoneInfo(view.info.timezone)).time()


def as_is(house: House, coordinator_name: str = "Sam", assignee: str = "Alex") -> Adjustment:
    return Adjustment(time(18, 30), house[assignee].id, house[coordinator_name].id)


async def adjust(
    house: House, who: str, event_id: int, wanted: Adjustment, *, opened: Adjustment | None = None, run: int = 1
) -> Adjustment:
    """``opened`` is what the modal showed: the plan as decided, by default."""
    shown = opened or as_is(house)
    return await act(house, coordinator.adjust, house[who], event_id, wanted, opened=shown, run=run)  # type: ignore[no-any-return]


# The use case ------------------------------------------------------------------------------------


async def test_nothing_changed_saves_nothing_and_sends_nothing(house: House) -> None:
    event_id = await decided(house)
    assert await meal(house, event_id) == time(18, 30)
    audits, outbox = len(await house.audits()), len(await house.outbox())
    changed = await adjust(house, "Sam", event_id, as_is(house))
    assert changed.empty
    assert (len(await house.audits()), len(await house.outbox())) == (audits, outbox)


async def test_only_the_meal_time_changes_when_only_it_is_changed(house: House) -> None:
    event_id = await decided(house)
    before = assigned(await house.context(event_id))
    changed = await adjust(house, "Sam", event_id, replace(as_is(house), meal_time=time(19, 15)))
    assert changed == Adjustment(meal_time=time(19, 15))
    assert await meal(house, event_id) == time(19, 15)
    assert assigned(await house.context(event_id)) == before
    assert (await house.audits())[-1].action == "event.meal_time_changed"
    assert not [a for a in await house.audits() if a.action in {"event.roles_assigned", "event.coordinator_handed_off"}]


async def test_the_role_goes_to_someone_who_is_in_and_they_are_told_who_to_ask(house: House) -> None:
    event_id = await decided(house)
    changed = await adjust(house, "Sam", event_id, replace(as_is(house), assignee_id=house["Lee"].id))
    assert changed == Adjustment(assignee_id=house["Lee"].id)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id
    (told,) = [n for n in await sent(house, "assignment_made") if n.member.id == house["Lee"].id]
    text = json.dumps(render(told, CTX).payload())
    assert "Sam assigned you this" in text and "tell Sam, who can pick someone else with *Adjust*" in text
    assert [a.action for a in await house.audits() if a.action == "event.roles_assigned"]


async def test_handing_off_comes_last_so_the_rest_still_counts_as_the_coordinators(house: House) -> None:
    event_id = await decided(house)
    wanted = Adjustment(time(19, 0), house["Lee"].id, house["Lee"].id)
    changed = await adjust(house, "Sam", event_id, wanted)
    assert changed == wanted
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.coordinator_id == house["Lee"].id
    assert assigned(ctx)[AssignmentRole.PICKUP] == house["Lee"].id
    assert assigned(ctx)[AssignmentRole.COORDINATOR] == house["Lee"].id
    assert await meal(house, event_id) == time(19, 0)
    actions = [a.action for a in await house.audits()]
    assert actions[-3:] == ["event.meal_time_changed", "event.roles_assigned", "event.coordinator_handed_off"]
    # Sam handed it off, so Adjust is Lee's now.
    with pytest.raises(EventRuleViolation) as refused:
        await adjust(house, "Sam", event_id, replace(wanted, meal_time=time(19, 30)))
    assert refused.value.code == "not_coordinator"


@pytest.mark.parametrize("who", ["Lee", "Alex"])
async def test_only_the_coordinator_adjusts_not_even_an_admin(house: House, who: str) -> None:
    event_id = await decided(house)
    assert house["Alex"].is_admin
    with pytest.raises(EventRuleViolation) as refused:
        await adjust(house, who, event_id, replace(as_is(house), meal_time=time(19, 0)))
    assert (str(refused.value), refused.value.code) == (ADJUST_IS_THE_COORDINATORS, "not_coordinator")
    assert await meal(house, event_id) == time(18, 30)
    view = await act(house, event_views.event_view, house[who], event_id)
    assert not view.can.adjust
    assert (await act(house, event_views.event_view, house["Sam"], event_id)).can.adjust


async def test_each_refused_part_names_itself_and_nothing_changes(house: House) -> None:
    event_id = await decided(house)
    pat = await add_guest(house, "Pat")  # a guest who isn't invited to this call
    for wanted, part, says in (
        (replace(as_is(house), meal_time=time(16, 0)), "meal_time", "after the decision"),
        (replace(as_is(house), assignee_id=house["Kim"].id), "assignee", "Pick someone who's in."),
        (replace(as_is(house), coordinator_id=pat.id), "coordinator", "No such member."),
    ):
        with pytest.raises(AdjustmentRefused) as refused:
            await adjust(house, "Sam", event_id, wanted)
        assert (refused.value.part, says in str(refused.value)) == (part, True)
    event = await house.event(event_id)
    assert (event.coordinator_id, assigned(await house.context(event_id))[AssignmentRole.PICKUP]) == (
        house["Sam"].id,
        house["Alex"].id,
    )
    assert await meal(house, event_id) == time(18, 30)


async def test_a_modal_from_an_earlier_run_or_another_call_changes_nothing(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(StaleRun):
        await adjust(house, "Sam", event_id, replace(as_is(house), meal_time=time(19, 0)), run=2)
    with pytest.raises(NotFound):
        await adjust(house, "Sam", event_id + 100, as_is(house))
    assert await meal(house, event_id) == time(18, 30)


async def test_a_done_meal_is_not_adjusted(house: House) -> None:
    event_id = await decided(house)
    house.clock.now = VOTE_BY.replace(hour=23, minute=45)
    await act(house, calls.mark_done, house["Sam"], event_id)
    with pytest.raises(EventRuleViolation) as refused:
        await adjust(house, "Sam", event_id, replace(as_is(house), meal_time=time(19, 0)))
    assert refused.value.code == "not_decided"


def test_a_guest_coordinator_may_tap_adjust() -> None:
    assert ids.ADJUST in GUEST_TAPS  # their logistics (FR-58); the service checks the rest


async def test_a_guest_coordinator_adjusts_the_plan(house: House) -> None:
    pat = await add_guest(house, "Pat")
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, calls.lock, house["Alex"], event_id, (await pick_ids(house, event_id))["Siam Garden"])
    assert (await act(house, event_views.event_view, pat, event_id)).can.adjust
    wanted = Adjustment(time(19, 0), house["Lee"].id, house["Sam"].id)
    opened = Adjustment(time(18, 30), house["Alex"].id, pat.id)
    assert await act(house, coordinator.adjust, pat, event_id, wanted, opened=opened, run=1) == wanted
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert (event.coordinator_id, assigned(ctx)[AssignmentRole.PICKUP]) == (house["Sam"].id, house["Lee"].id)


# Slack -----------------------------------------------------------------------------------------------


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def inputs(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {b["block_id"]: b for b in view["blocks"] if b["type"] == "input"}


async def opened_now(house: House, event_id: int) -> ids.ViewOrigin:
    """The metadata of an Adjust modal opened now, by Sam, in the channel."""
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    return ids.ViewOrigin(event_id, CHANNEL, run=1, opened=adjust_opened(view))


def form(house: House, *, at: str = "18:30", who: str = "Alex", lead: str = "Sam") -> dict[str, Any]:
    return state(
        meal_time={"selected_time": at},
        who={"selected_option": {"value": str(house[who].id)}},
        coordinator={"selected_option": {"value": str(house[lead].id)}},
    )


async def test_slack_adjust_opens_for_the_coordinator_only(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    value = ids.card_value(event_id, 1)
    await slack.click("Lee", ids.ADJUST, value)
    assert not slack.fake.of("views.open") and slack.ephemerals("Lee")[-1] == ADJUST_IS_THE_COORDINATORS
    await slack.click("Alex", ids.ADJUST, value)  # the admin's overrides are on the web
    assert not slack.fake.of("views.open") and "An admin can change it on the web" in slack.ephemerals("Alex")[-1]
    await slack.click("Sam", ids.ADJUST, ids.card_value(event_id, 2))
    assert not slack.fake.of("views.open") and slack.ephemerals("Sam")[-1] == STALE_CALL

    await slack.click("Sam", ids.ADJUST, value)
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert view["callback_id"] == ids.ADJUST_VIEW and ids.ViewOrigin.decode(view["private_metadata"]).run == 1
    fields = inputs(view)
    assert fields["meal_time"]["element"]["initial_time"] == "18:30"
    who = fields["who"]["element"]
    assert fields["who"]["label"]["text"] == "Who picks up"
    assert {o["text"]["text"] for o in who["options"]} == {"Alex", "Lee", "Sam"}  # whoever is in
    assert who["initial_option"]["text"]["text"] == "Alex"
    lead = fields["coordinator"]["element"]
    assert lead["initial_option"]["text"]["text"] == "Sam"
    assert [o["text"]["text"] for o in lead["options"]] == ["Sam", "Alex", "Lee"]  # Sam first, then who's in


async def test_slack_adjust_saves_what_changed_and_says_so(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    response = await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="19:00", who="Lee"), event_id, origin=origin)
    assert not response.body  # the modal closes
    assert slack.ephemerals("Sam")[-1] == "Saved. The meal time is 7:00 PM. Lee is picking up, and is told."
    assert await meal(house, event_id) == time(19, 0)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id
    await slack.drain()
    told = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == "U04LEE"]
    assert told and "Sam assigned you this" in json.dumps(told[-1].args)
    updated = json.dumps([c.args for c in slack.fake.of("chat.update")])
    assert "7:00 PM" in updated  # the decision shows the new time

    origin = await opened_now(house, event_id)  # Adjust opened again, on the plan as it is now
    await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="19:00", who="Lee", lead="Lee"), event_id, origin=origin)
    assert slack.ephemerals("Sam")[-1] == "Saved. Lee is coordinating now, and is told."
    assert (await house.event(event_id)).coordinator_id == house["Lee"].id


async def test_slack_adjust_with_nothing_changed_saves_nothing(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    audits, outbox = len(await house.audits()), len(await house.outbox())
    origin = await opened_now(house, event_id)
    response = await slack.submit("Sam", ids.ADJUST_VIEW, form(house), event_id, origin=origin)
    assert not response.body
    assert slack.ephemerals("Sam")[-1] == "Nothing changed."
    assert (len(await house.audits()), len(await house.outbox())) == (audits, outbox)


async def test_slack_adjust_shows_a_refusal_next_to_its_field_and_keeps_the_modal(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    early = await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="16:00", who="Lee"), event_id, origin=origin)
    body = json.loads(early.body)
    assert body["response_action"] == "errors" and "after the decision" in body["errors"]["meal_time"]
    out = await slack.submit("Sam", ids.ADJUST_VIEW, form(house, who="Kim"), event_id, origin=origin)
    assert json.loads(out.body)["errors"] == {"who": "Pick someone who's in."}
    missing = state(meal_time={"selected_time": "18:30"})
    nobody = await slack.submit("Sam", ids.ADJUST_VIEW, missing, event_id, origin=origin)
    assert json.loads(nobody.body)["errors"] == {"coordinator": "Pick who coordinates."}
    # Nothing was saved: not even the role, which was fine in the first one.
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Alex"].id
    assert await meal(house, event_id) == time(18, 30)
    assert not slack.ephemerals("Sam")


async def test_slack_adjust_refuses_everyone_else_and_old_modals_after_closing(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    response = await slack.submit("Lee", ids.ADJUST_VIEW, form(house, at="19:00"), event_id, origin=origin)
    assert not response.body and slack.ephemerals("Lee")[-1] == ADJUST_IS_THE_COORDINATORS
    await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="19:00"), event_id, origin=replace(origin, run=2))
    assert slack.ephemerals("Sam")[-1] == STALE_CALL
    await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="19:00"), event_id, origin=replace(origin, run=None))
    assert await meal(house, event_id) == time(18, 30)  # a modal that names no run isn't this app's


async def test_slack_home_shows_adjust_to_the_coordinator_only(slack: Slack, house: House) -> None:
    await decided(house)

    def action_ids(home: dict[str, Any]) -> list[str]:
        return [e.get("action_id") for b in home["blocks"] if b["type"] == "actions" for e in b["elements"]]

    await slack.open_home("Sam")
    assert ids.ADJUST in action_ids(slack.last_home("Sam"))
    for who in ("Lee", "Alex"):
        await slack.open_home(who)
        assert ids.ADJUST not in action_ids(slack.last_home(who))


# Review follow-ups: stale modals, partial saves, and who's told -----------------------------------


async def test_a_change_made_elsewhere_after_opening_is_not_written_back(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)  # Alex picks up, as the modal shows
    await act(house, coordinator.choose_roles, house["Sam"], event_id, member_id=house["Lee"].id, run=1)
    await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="19:00", who="Alex"), event_id, origin=origin)
    assert slack.ephemerals("Sam")[-1] == "Saved. The meal time is 7:00 PM."
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id  # Lee keeps it


async def test_a_part_changed_both_here_and_elsewhere_is_refused_by_its_field(slack: Slack, house: House) -> None:
    event_id = await decided(house)
    origin = await opened_now(house, event_id)
    await act(house, calls.set_meal_time, house["Sam"], event_id, time(19, 0))  # on the web, say
    response = await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="19:30", who="Lee"), event_id, origin=origin)
    assert json.loads(response.body)["errors"] == {"meal_time": coordinator.PLAN_CHANGED}
    assert await meal(house, event_id) == time(19, 0)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Alex"].id  # nothing saved


async def test_a_later_part_refused_after_an_earlier_one_saved_says_both(
    slack: Slack, house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await decided(house)

    async def left(*args: Any, **kwargs: Any) -> None:  # Lee left between the check and the save
        raise EventRuleViolation("Pick someone who's in.", code="not_in")

    monkeypatch.setattr(coordinator, "choose_roles", left)
    with pytest.raises(coordinator.PartlyAdjusted) as partly:
        await adjust(house, "Sam", event_id, Adjustment(time(19, 0), house["Lee"].id, house["Sam"].id))
    assert partly.value.saved == Adjustment(meal_time=time(19, 0)) and str(partly.value) == "Pick someone who's in."

    origin = await opened_now(house, event_id)
    await slack.submit("Sam", ids.ADJUST_VIEW, form(house, at="19:30", who="Lee"), event_id, origin=origin)
    assert slack.ephemerals("Sam")[-1] == (
        "Saved. The meal time is 7:30 PM. The rest didn't change: Pick someone who's in."
    )
    assert await meal(house, event_id) == time(19, 30)


async def test_with_a_hand_off_the_new_assignee_hears_once_and_after_it(house: House) -> None:
    event_id = await decided(house)
    await adjust(house, "Sam", event_id, Adjustment(time(18, 30), house["Lee"].id, house["Alex"].id))
    (told,) = [n for n in await sent(house, "assignment_made") if n.member.id == house["Lee"].id]
    assert told.event.coordinator is not None and told.event.coordinator.id == house["Alex"].id
    text = json.dumps(render(told, CTX).payload())
    assert "Sam assigned you this. If it doesn't work for you, tell Sam." in text and "Adjust" not in text


async def test_handing_off_to_the_new_assignee_sends_them_one_message(house: House) -> None:
    event_id = await decided(house)
    await adjust(house, "Sam", event_id, Adjustment(time(18, 30), house["Lee"].id, house["Lee"].id))
    (told,) = [n for n in await sent(house, "assignment_made") if n.member.id == house["Lee"].id]
    assert set(told.roles) == {AssignmentRole.PICKUP, AssignmentRole.COORDINATOR}


async def test_each_step_checks_the_coordinator_again_under_the_lock(house: House) -> None:
    event_id = await decided(house)
    assert house["Alex"].is_admin  # the web lets an admin change these; Adjust's steps don't
    for use in (
        lambda: calls.set_meal_time(db, house["Alex"], event_id, time(19, 0), as_coordinator=True, now=house.clock()),
        lambda: coordinator.choose_roles(
            db, house["Alex"], event_id, member_id=house["Lee"].id, as_coordinator=True, now=house.clock()
        ),
    ):
        async with house.sessionmaker() as db:
            with pytest.raises(EventRuleViolation) as refused:
                await use()
        assert refused.value.code == "not_coordinator"
    assert await meal(house, event_id) == time(18, 30)
