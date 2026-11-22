# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stale picks (#330): a tap on picks shown before a shuffle or a re-run never acts on another place.

A shuffle deletes picks and adds new ones, and a re-run deletes a call's picks and draws them
again. ``proposal`` is AUTOINCREMENT (revision 0045), so the new picks never take the old IDs, and
a Vote, Lock this, Break the tie, or way from a message or page that still shows the old picks
gets "No such proposal." from the service, the web, and Slack alike. Shuffle carries the shuffle
count it was drawn at, so a second tap of the same button shuffles nothing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import ids
from dinnerbell.config import Settings
from dinnerbell.db.models import EventRow
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import SHUFFLED_SINCE, STALE_CALL, EventState, RoleOffer, ShuffledSince, Status
from dinnerbell.services import call_admin, calls, coordinator
from dinnerbell.services.calls import NO_SUCH_PROPOSAL
from dinnerbell.services.errors import NotFound
from tests.conftest import page
from tests.support.calls import House
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.slack import Slack
from tests.support.web import submit
from tests.test_auth_flow import rows
from tests.test_coordinator import act, pick_ids, shuffle, web_voting, with_many_places
from tests.test_coordinator import database as database
from tests.test_coordinator import house as house
from tests.test_coordinator import slack as slack
from tests.test_events_web import admin as admin
from tests.test_events_web import proposal_ids, sign_in_as
from tests.test_events_web import tick as web_tick

pytestmark = pytest.mark.anyio

HOW = ["shuffle", "rerun"]
NOT_THERE = f"^{NO_SUCH_PROPOSAL}$".replace(".", r"\.")


async def stale_pick(house: House, how: str) -> tuple[int, int, str]:
    """A voting call that Sam coordinates, with Alex, Sam, and Lee in, after Alex (an admin)
    shuffles its picks or re-runs it to voting again. Returns the call, the ID of the first pick
    shown before, and that pick's place."""
    event_id = await with_many_places(house, "Sam", "Lee", coordinator_name="Sam")
    before = await pick_ids(house, event_id)
    wanted, stale_id = next(iter(before.items()))
    if how == "shuffle":
        await shuffle(house, "Alex", event_id, run=1)  # nobody voted, so every pick is redrawn
    else:
        await act(house, call_admin.rerun, house["Alex"], event_id)
        for name in ("Alex", "Sam", "Lee"):
            await act(house, calls.respond, house[name], event_id, Status.IN)
        await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
        house.clock.now = (await house.event(event_id)).respond_deadline
        await house.tick()
    event = await house.event(event_id)
    assert event.state is EventState.VOTING and event.run == (2 if how == "rerun" else 1)
    after = await pick_ids(house, event_id)
    assert stale_id not in after.values() and min(after.values()) > max(before.values())  # no ID reused
    return event_id, stale_id, wanted


async def nothing_changed(house: House, event_id: int) -> None:
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.VOTING and ctx.event.decided_item_id is None
    assert not any(ctx.votes.values())


# The services -----------------------------------------------------------------------------------


@pytest.mark.parametrize("how", HOW)
async def test_a_stale_vote_is_refused(house: House, how: str) -> None:
    event_id, stale_id, _ = await stale_pick(house, how)
    for use_case in (calls.vote, calls.toggle_vote, calls.unvote):
        with pytest.raises(NotFound, match=NOT_THERE):
            await act(house, use_case, house["Lee"], event_id, stale_id)
    await nothing_changed(house, event_id)


@pytest.mark.parametrize("how", HOW)
async def test_a_stale_lock_tie_break_or_way_is_refused(house: House, how: str) -> None:
    """The Lock this and Break the tie modals, and the way menu, name the run, which a shuffle
    keeps; on the run they show now, the old pick is gone."""
    event_id, stale_id, _ = await stale_pick(house, how)
    run = (await house.event(event_id)).run
    with pytest.raises(NotFound, match=NOT_THERE):
        await act(house, calls.lock, house["Sam"], event_id, stale_id, run=run)
    with pytest.raises(NotFound, match=NOT_THERE):
        await act(house, coordinator.break_tie, house["Sam"], event_id, stale_id, run=run)
    with pytest.raises(NotFound, match=NOT_THERE):
        await act(house, coordinator.set_way, house["Sam"], event_id, stale_id, Mode.DELIVERY, run=run)
    await nothing_changed(house, event_id)


async def test_shuffle_tapped_twice_shuffles_once(house: House) -> None:
    """A double tap, or the coordinator and an admin tapping together, sends the count the button
    was drawn at twice: the second is refused and uses no shuffle."""
    event_id = await with_many_places(house, "Sam", coordinator_name="Sam")
    await shuffle(house, "Sam", event_id, run=1, shuffles=0)
    first = await pick_ids(house, event_id)
    with pytest.raises(ShuffledSince, match=f"^{SHUFFLED_SINCE}$"):
        await shuffle(house, "Alex", event_id, run=1, shuffles=0)
    event = await house.event(event_id)
    assert event.shuffles == 1 and await pick_ids(house, event_id) == first
    await shuffle(house, "Alex", event_id, run=1, shuffles=1)  # drawn after the first
    await shuffle(house, "Alex", event_id, run=1)  # a surface that doesn't say isn't checked
    assert (await house.event(event_id)).shuffles == 3


# Slack ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", HOW)
async def test_slack_refuses_a_stale_vote_lock_or_tie_break(slack: Slack, house: House, how: str) -> None:
    event_id, stale_id, _ = await stale_pick(house, how)
    value = ids.proposal_value(event_id, stale_id)
    await slack.click("Lee", ids.VOTE, value)
    assert slack.ephemerals("Lee")[-1] == NO_SUCH_PROPOSAL
    await slack.click("Lee", ids.JOIN_LATE_VOTE, value)
    assert slack.ephemerals("Lee")[-1] == NO_SUCH_PROPOSAL
    for action in (ids.LOCK, ids.BREAK_TIE):  # buttons on messages posted before the ⋯ menu
        await slack.click("Sam", action, value)
        assert slack.ephemerals("Sam")[-1] == NO_SUCH_PROPOSAL
    run = (await house.event(event_id)).run
    for choice in ids.PickChoice:
        await slack.click("Sam", ids.PICK_MENU, None, selected=ids.pick_value(choice, event_id, run, stale_id))
        assert slack.ephemerals("Sam")[-1] == NO_SUCH_PROPOSAL
    await slack.click("Sam", ids.PICK_MENU, None, selected=ids.way_value(Mode.DELIVERY, event_id, run, stale_id))
    assert slack.ephemerals("Sam")[-1] == NO_SUCH_PROPOSAL
    assert slack.fake.of("views.open") == []
    await nothing_changed(house, event_id)


@pytest.mark.parametrize("choice", list(ids.PickChoice))
async def test_a_slack_modal_confirmed_after_a_shuffle_decides_nothing(
    slack: Slack, house: House, choice: ids.PickChoice
) -> None:
    """Sam opens Lock this (or Break the tie) for a pick; Alex shuffles; Sam confirms."""
    event_id = await with_many_places(house, "Sam", "Lee", coordinator_name="Sam")
    picks = list((await pick_ids(house, event_id)).values())
    if choice is ids.PickChoice.BREAK_TIE:
        await act(house, calls.vote, house["Lee"], event_id, picks[0])
        await act(house, calls.vote, house["Sam"], event_id, picks[1])
    await slack.click("Sam", ids.PICK_MENU, None, selected=ids.pick_value(choice, event_id, 1, picks[0]))
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    await shuffle(house, "Alex", event_id, run=1)
    if choice is ids.PickChoice.BREAK_TIE:  # the tied picks have votes, so they stay
        await act(house, calls.unvote, house["Lee"], event_id, picks[0])
        await act(house, calls.unvote, house["Sam"], event_id, picks[1])
        await shuffle(house, "Alex", event_id, run=1)
    assert picks[0] not in (await pick_ids(house, event_id)).values()
    origin = ids.ViewOrigin.decode(view["private_metadata"])
    await slack.submit("Sam", view["callback_id"], {}, event_id, origin=origin)
    assert slack.ephemerals("Sam")[-1] == NO_SUCH_PROPOSAL
    await nothing_changed(house, event_id)


async def test_slack_shuffle_tapped_twice_shuffles_once_and_an_old_button_still_works(
    slack: Slack, house: House
) -> None:
    event_id = await with_many_places(house, "Sam", coordinator_name="Sam")
    value = ids.shuffle_value(event_id, 1, 0)
    await slack.click("Sam", ids.SHUFFLE, value)
    assert slack.ephemerals("Sam")[-1].startswith("Shuffled: ")
    first = await pick_ids(house, event_id)
    await slack.click("Alex", ids.SHUFFLE, value)  # the same button, tapped at the same moment
    assert slack.ephemerals("Alex")[-1] == SHUFFLED_SINCE
    assert (await house.event(event_id)).shuffles == 1 and await pick_ids(house, event_id) == first

    await slack.click("Sam", ids.SHUFFLE, ids.shuffle_value(event_id, 2, 1))  # a message from another run
    assert slack.ephemerals("Sam")[-1] == STALE_CALL
    await slack.click("Sam", ids.SHUFFLE, ids.card_value(event_id, 1))  # a message drawn before the count
    assert slack.ephemerals("Sam")[-1].startswith("Shuffled: ")
    assert (await house.event(event_id)).shuffles == 2


async def test_the_proposals_message_shuffle_button_carries_the_count(slack: Slack, house: House) -> None:
    event_id = await with_many_places(house, "Sam", coordinator_name="Sam")
    await shuffle(house, "Sam", event_id, run=1)
    await slack.drain()
    posted = [c for c in slack.fake.calls if c.method in {"chat.postMessage", "chat.update"}]
    values = [
        element["value"]
        for call in posted
        for block in call.args.get("blocks") or []
        for element in block.get("elements", [])
        if isinstance(element, dict) and element.get("action_id") == ids.SHUFFLE
    ]
    assert values[0] == ids.shuffle_value(event_id, 1, 0) and values[-1] == ids.shuffle_value(event_id, 1, 1)


@pytest.mark.parametrize(
    "value", ["1:1:", "1:1:-1", "1:1:01", "1:1:1234", "1:1:1:1", "0:1:0", "1:0:0", "x:1:0", None, 7]
)
def test_a_shuffle_value_this_app_didnt_write_is_refused(value: object) -> None:
    with pytest.raises(ids.BadValue):
        ids.parse_shuffle(value)


def test_a_shuffle_value_reads_back() -> None:
    assert ids.parse_shuffle(ids.shuffle_value(5, 2, 0)) == (5, 2, 0)
    assert ids.parse_shuffle(ids.shuffle_value(5, 2, 3)) == (5, 2, 3)
    assert ids.shuffle_value(5, 2, None) == ids.card_value(5, 2)
    assert ids.parse_shuffle(ids.card_value(5, 2)) == (5, 2, None)


# The web ----------------------------------------------------------------------------------------


def web_rerun(admin: TestClient, clock: FakeClock, migrated: Settings, event_id: int) -> None:
    """An admin re-runs the call, answers In and offers to pick up, and responses close."""
    assert submit(admin, f"/events/{event_id}/admin/rerun", {}).status_code == 303
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    (row,) = rows(migrated, EventRow)
    assert (row.state, row.run) == ("voting", 2)


@pytest.mark.parametrize("how", HOW)
def test_the_web_refuses_a_stale_vote_lock_tie_break_or_way(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings, how: str
) -> None:
    event_id = web_voting(admin, provider, clock, migrated)
    before = proposal_ids(migrated)
    stale = before[0]
    if how == "shuffle":
        assert submit(admin, f"/events/{event_id}/shuffle", {"run": "1", "shuffles": "0"}).status_code == 303
    else:
        web_rerun(admin, clock, migrated, event_id)
    after = proposal_ids(migrated)
    assert stale not in after and min(after) > max(before)
    run = str(rows(migrated, EventRow)[0].run)
    base = f"/events/{event_id}/proposals/{stale}"
    forms: list[tuple[str, dict[str, Any]]] = [
        ("vote", {}),
        ("vote", {"join_late": "1"}),
        ("unvote", {}),
        ("lock", {}),
        ("break-tie", {"run": run}),
        ("way", {"mode": "delivery", "run": run}),
    ]
    for action, data in forms:
        refused = submit(admin, f"{base}/{action}", data)
        assert refused.status_code == 404 and NO_SUCH_PROPOSAL in page(refused), action
    (row,) = rows(migrated, EventRow)
    assert row.state == "voting" and row.decided_item_id is None


def test_web_shuffle_tapped_twice_shuffles_once(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    event_id = web_voting(admin, provider, clock, migrated)
    assert 'name="shuffles" value="0"' in page(admin.get(f"/events/{event_id}"))
    form = {"run": "1", "shuffles": "0"}
    assert submit(admin, f"/events/{event_id}/shuffle", form).status_code == 303
    first = proposal_ids(migrated)
    assert 'name="shuffles" value="1"' in page(admin.get(f"/events/{event_id}"))
    again = submit(admin, f"/events/{event_id}/shuffle", form)  # the second tap, or a page left open
    assert again.status_code == 409 and SHUFFLED_SINCE in page(again)
    assert rows(migrated, EventRow)[0].shuffles == 1 and proposal_ids(migrated) == first
    bad = submit(admin, f"/events/{event_id}/shuffle", {"run": "1", "shuffles": "one"})
    assert bad.status_code == 400
    assert submit(admin, f"/events/{event_id}/shuffle", {"run": "1"}).status_code == 303  # a page from before
    assert rows(migrated, EventRow)[0].shuffles == 2
    sign_in_as(admin, provider, "kim@example.com")  # not the coordinator: refused as before
    refused = submit(admin, f"/events/{event_id}/shuffle", {"run": "1", "shuffles": "2"})
    assert refused.status_code == 409 and "Only the coordinator" in page(refused)
