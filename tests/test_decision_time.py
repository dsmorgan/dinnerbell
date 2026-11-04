# SPDX-License-Identifier: AGPL-3.0-or-later
"""The coordinator moves the decision time while voting (#198, FR-6, FR-61): the rule, the
service, the event page, and the ⋯ menu in Slack. It is the admin's vote-window move, with the
same limits and updated messages, audited as ``event.deadline_moved`` with the capacity."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.blocks import day_label
from dinnerbell.channels.slack.interactions import CLOSED, MOVED
from dinnerbell.channels.slack.views import FormError, parse_decision_time
from dinnerbell.config import Settings
from dinnerbell.db.models import EventRow
from dinnerbell.domain.events import (
    DECISION_TIME_MOVERS,
    GUEST_DECIDES_NOW,
    STALE_CALL,
    ActingAs,
    EventKind,
    EventRuleViolation,
    EventState,
    RoleOffer,
    StaleRun,
    Status,
    Window,
    check_can_move_decision_time,
    check_guest_keeps_voting_open,
)
from dinnerbell.domain.guests import GuestAction, check_guest
from dinnerbell.domain.members import Role
from dinnerbell.services import call_admin, calls, coordinator, event_views, guests
from dinnerbell.services.database import Database, open_database
from tests.conftest import invite, page
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import CHANNEL, state
from tests.support.slack import Slack, wire
from tests.support.web import submit
from tests.test_auth_flow import rows
from tests.test_coordinator import _event, _person, act, add_guest, open_dinner, sent, to_voting
from tests.test_early_close import everyone_in, open_lunch
from tests.test_events_web import admin as admin  # the fixture, re-exported for pytest
from tests.test_events_web import sign_in_as, start
from tests.test_events_web import tick as web_tick
from tests.test_post_now import TOMORROW

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


# The rule, in the domain ---------------------------------------------------------------------------


BEFORE = VOTE_BY - timedelta(minutes=1)


def test_who_may_move_the_decision_time() -> None:
    voting = replace(_event(EventState.VOTING, coordinator_id=2), vote_deadline=VOTE_BY)
    assert check_can_move_decision_time(voting, _person(2), now=BEFORE) is ActingAs.COORDINATOR
    assert check_can_move_decision_time(voting, _person(1, Role.ADMIN), now=BEFORE) is ActingAs.ADMIN
    guest_coordinating = replace(voting, coordinator_id=9)
    assert check_can_move_decision_time(guest_coordinating, _person(9, Role.GUEST), now=BEFORE) is ActingAs.COORDINATOR
    check_guest(guest_coordinating, _person(9, Role.GUEST), GuestAction.MOVE_DECISION_TIME, invited=True)
    for event, actor, now, code in [
        (voting, _person(3), BEFORE, "not_coordinator"),
        (voting, _person(9, Role.GUEST), BEFORE, "not_coordinator"),
        (voting, _person(2, active=False), BEFORE, "inactive"),
        (_event(EventState.COLLECTING, coordinator_id=2), _person(2), BEFORE, "voting_closed"),
        (_event(EventState.DECIDED, coordinator_id=2), _person(2), BEFORE, "voting_closed"),
        (replace(voting, kind=EventKind.OFFER), _person(2), BEFORE, "is_offer"),
        (voting, _person(2), VOTE_BY, "voting_closed"),  # passed: the scheduler's next tick decides
        (voting, _person(1, Role.ADMIN), VOTE_BY + timedelta(seconds=20), "voting_closed"),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            check_can_move_decision_time(event, actor, now=now)
        assert refused.value.code == code
    with pytest.raises(EventRuleViolation) as refused:
        check_guest(_event(EventState.COLLECTING), _person(9, Role.GUEST), GuestAction.MOVE_DECISION_TIME, invited=True)
    assert refused.value.code == "guest_not_now"


def test_a_guest_never_moves_it_to_now_which_would_decide() -> None:
    voting = replace(_event(EventState.VOTING, coordinator_id=9), vote_deadline=VOTE_BY)
    guest, member = _person(9, Role.GUEST), _person(2)
    check_guest_keeps_voting_open(voting, guest, -15, now=VOTE_BY - timedelta(minutes=16))
    check_guest_keeps_voting_open(voting, member, -15, now=VOTE_BY)  # a member may: that's Decide now
    with pytest.raises(EventRuleViolation) as refused:
        check_guest_keeps_voting_open(voting, guest, -15, now=VOTE_BY - timedelta(minutes=15))
    assert (refused.value.code, str(refused.value)) == ("guests_dont", GUEST_DECIDES_NOW)


# The service ---------------------------------------------------------------------------------------


async def test_the_coordinator_moves_the_decision_time_later_and_earlier(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    sam = house["Sam"]
    view = await act(house, event_views.event_view, sam, event_id)
    assert (view.can.move_vote, view.can.move_window) == (True, None)  # not an admin's controls
    moved = await act(house, coordinator.move_decision_time, sam, event_id, 30, run=1)
    assert moved == (await house.event(event_id)).vote_deadline == VOTE_BY + timedelta(minutes=30)
    await act(house, coordinator.move_decision_time, sam, event_id, -15, run=1)
    assert (await house.event(event_id)).vote_deadline == VOTE_BY + timedelta(minutes=15)
    updated = (await sent(house, "call_updated"))[-1]  # the call message shows the new time
    assert updated.event.vote_deadline == VOTE_BY + timedelta(minutes=15)
    assert (await sent(house, "proposals_updated"))[-1].event.vote_deadline == VOTE_BY + timedelta(minutes=15)
    audits = await house.audits("event.deadline_moved")
    assert [(a.actor_member_id, a.payload) for a in audits] == [
        (
            sam.id,
            {"window": "vote", "from": VOTE_BY.isoformat(), "to": moved.isoformat(), "as": "coordinator"},
        ),
        (
            sam.id,
            {
                "window": "vote",
                "from": moved.isoformat(),
                "to": (VOTE_BY + timedelta(minutes=15)).isoformat(),
                "as": "coordinator",
            },
        ),
    ]
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_moving_it_into_the_past_decides_now(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    house.clock.now = VOTE_BY - timedelta(minutes=10)
    moved = await act(house, coordinator.move_decision_time, house["Sam"], event_id, -15, run=1)
    assert moved == house.clock()
    assert (await house.event(event_id)).state is EventState.DECIDED


async def test_an_admin_moves_it_too_audited_as_admin(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    await act(house, coordinator.move_decision_time, house["Alex"], event_id, 15, run=1)
    (audit,) = await house.audits("event.deadline_moved")
    assert (audit.actor_member_id, audit.payload["as"]) == (house["Alex"].id, "admin")


async def test_everyone_else_and_outside_voting_is_refused_with_a_reason(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    with pytest.raises(EventRuleViolation) as refused:  # answers are open: no decision time to move yet
        await act(house, coordinator.move_decision_time, house["Sam"], event_id, 15, run=1)
    assert refused.value.code == "voting_closed"
    for name in ("Alex", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    for who in ("Lee", "Kim"):  # neither coordinates nor is an admin
        with pytest.raises(EventRuleViolation) as refused:
            await act(house, coordinator.move_decision_time, house[who], event_id, 15, run=1)
        assert (refused.value.code, str(refused.value)) == ("not_coordinator", DECISION_TIME_MOVERS)
    with pytest.raises(StaleRun):
        await act(house, coordinator.move_decision_time, house["Sam"], event_id, 15, run=2)
    with pytest.raises(EventRuleViolation) as refused:  # the admin's limit
        await act(house, coordinator.move_decision_time, house["Sam"], event_id, 24 * 60, run=1)
    assert refused.value.code == "bad_deadline"
    house.clock.now = VOTE_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.DECIDED
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.move_decision_time, house["Sam"], event_id, 15, run=1)
    assert refused.value.code == "voting_closed"
    assert await house.audits("event.deadline_moved") == []


async def test_a_passed_deadline_waits_for_the_scheduler_and_never_reopens(house: House) -> None:
    """Between a deadline and the scheduler's next tick, the window is still open in the database.
    Moving its time later then would reopen a window that has closed, for anyone."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY  # answers are due; the tick hasn't run
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, 15)
    assert refused.value.code == "responses_closed"
    await house.tick()
    house.clock.now = VOTE_BY + timedelta(seconds=20)  # the decision time has passed; the tick hasn't run
    for who, move in [
        ("Sam", lambda: act(house, coordinator.move_decision_time, house["Sam"], event_id, 15, run=1)),
        ("Alex", lambda: act(house, coordinator.move_decision_time, house["Alex"], event_id, 30, run=1)),
        ("Alex", lambda: act(house, call_admin.move_deadline, house["Alex"], event_id, Window.VOTE, 15)),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            await move()
        assert refused.value.code == "voting_closed", who
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    assert not view.can.move_vote
    assert await house.audits("event.deadline_moved") == []
    await house.tick()
    assert (await house.event(event_id)).vote_deadline == VOTE_BY


async def test_after_a_hand_off_only_the_new_coordinator_moves_it(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    await act(house, calls.hand_off, house["Sam"], event_id, house["Lee"].id, run=1)
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.move_decision_time, house["Sam"], event_id, 15, run=1)
    assert refused.value.code == "not_coordinator"
    await act(house, coordinator.move_decision_time, house["Lee"], event_id, 15, run=1)
    (audit,) = await house.audits("event.deadline_moved")
    assert (audit.actor_member_id, audit.payload["as"]) == (house["Lee"].id, "coordinator")


async def test_the_admins_own_move_is_audited_as_admin(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.VOTE, 15)
    (audit,) = await house.audits("event.deadline_moved")
    assert audit.payload == {
        "window": "vote",
        "from": VOTE_BY.isoformat(),
        "to": (VOTE_BY + timedelta(minutes=15)).isoformat(),
        "as": "admin",
    }


async def test_a_guest_coordinator_moves_it_but_never_to_now(house: House) -> None:
    pat = await add_guest(house, "Pat")
    event_id = await open_dinner(house)
    for name in ("Alex", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, guests.invite, house["Lee"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await act(house, event_views.event_view, pat, event_id)).can.move_vote
    await act(house, coordinator.move_decision_time, pat, event_id, 30, run=1)
    assert (await house.event(event_id)).vote_deadline == VOTE_BY + timedelta(minutes=30)
    house.clock.now = VOTE_BY + timedelta(minutes=20)
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.move_decision_time, pat, event_id, -15, run=1)
    assert refused.value.code == "guests_dont"
    assert (await house.event(event_id)).state is EventState.VOTING
    (audit,) = await house.audits("event.deadline_moved")
    assert (audit.actor_member_id, audit.payload["as"]) == (pat.id, "coordinator")


async def test_a_guest_not_invited_is_refused(house: House) -> None:
    pat = await add_guest(house, "Pat")
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.move_decision_time, pat, event_id, 15, run=1)
    assert refused.value.code == "not_invited"


# The event page ------------------------------------------------------------------------------------


def _vote_deadline(migrated: Settings, event_id: int) -> object:
    (row,) = [e for e in rows(migrated, EventRow) if e.id == event_id]
    return row.vote_deadline


def test_the_coordinator_moves_it_on_the_event_page(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    invite(admin, "kim@example.com", "Kim")
    invite(admin, "lee@example.com", "Lee")
    event_id = start(admin)
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="/events/{event_id}/decision-time"' not in html  # nothing to move while answering
    sign_in_as(admin, provider, "kim@example.com")  # not an admin
    assert submit(admin, f"/events/{event_id}/coordinate", {}).status_code == 303
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    before = _vote_deadline(migrated, event_id)
    html = page(admin.get(f"/events/{event_id}"))
    assert "Move the decision time" in html and f'action="/events/{event_id}/decision-time"' in html
    confirm = "Move voting 15 minutes earlier? If the new time has already passed, voting closes now"
    assert html.count(f'hx-confirm="{confirm}') == 1  # only "minus15" asks, as Decide now does
    assert submit(admin, f"/events/{event_id}/decision-time", {"shift": "plus30"}).status_code == 400  # no run
    bad = submit(admin, f"/events/{event_id}/decision-time", {"shift": "close", "run": "1"})  # Decide now is apart
    assert bad.status_code == 409 and "Pick how to move the decision time." in page(bad)
    assert submit(admin, f"/events/{event_id}/decision-time", {"shift": "plus30", "run": "1"}).status_code == 303
    assert _vote_deadline(migrated, event_id) == before + timedelta(minutes=30)  # type: ignore[operator]
    sign_in_as(admin, provider, "lee@example.com")  # neither coordinating nor an admin
    assert f'action="/events/{event_id}/decision-time"' not in page(admin.get(f"/events/{event_id}"))
    refused = submit(admin, f"/events/{event_id}/decision-time", {"shift": "minus15", "run": "1"})
    assert refused.status_code == 409 and DECISION_TIME_MOVERS in page(refused)
    assert _vote_deadline(migrated, event_id) == before + timedelta(minutes=30)  # type: ignore[operator]


def test_an_admin_keeps_the_admin_controls_without_a_second_row(admin: TestClient, clock: FakeClock) -> None:
    event_id = start(admin)
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"})
    submit(admin, f"/events/{event_id}/details", {"guests": "1"})
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="/events/{event_id}/admin/deadline"' in html
    assert f'action="/events/{event_id}/decision-time"' not in html


# Slack ---------------------------------------------------------------------------------------------


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


def move(event_id: int, run: int = 1) -> str:
    return ids.menu_value(ids.MenuChoice.MOVE, event_id, run)


def shift(value: str) -> dict[str, object]:
    return state(shift={"type": "radio_buttons", "selected_option": {"value": value}})


def opened(slack: Slack) -> list[dict[str, object]]:
    return [c.args["view"] for c in slack.fake.of("views.open")]


async def test_the_menu_opens_the_modal_for_the_coordinator_and_tells_others_why_not(
    slack: Slack, house: House
) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    slack.fake.clear()
    await slack.choose("Lee", move(event_id))
    assert opened(slack) == [] and slack.ephemerals("Lee") == [DECISION_TIME_MOVERS]
    await slack.choose("Sam", move(event_id, run=2))
    assert opened(slack) == [] and slack.ephemerals("Sam") == [STALE_CALL]
    await slack.choose("Sam", move(event_id))
    (modal,) = opened(slack)
    assert modal["callback_id"] == ids.DECISION_TIME_VIEW
    assert ids.ViewOrigin.decode(modal["private_metadata"]) == ids.ViewOrigin(event_id, CHANNEL, run=1)
    assert "Voting closes at *5:15 PM*." in str(modal["blocks"])


async def test_the_modal_moves_it_and_says_when_voting_closes(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    await slack.submit("Lee", ids.DECISION_TIME_VIEW, shift("plus15"), event_id, origin=origin)
    assert slack.ephemerals("Lee") == [DECISION_TIME_MOVERS]
    await slack.submit("Sam", ids.DECISION_TIME_VIEW, shift("plus15"), event_id, origin=origin)
    assert slack.ephemerals("Sam") == [MOVED.format(at="5:30 PM")] == ["Voting now closes at 5:30 PM."]
    assert (await house.event(event_id)).vote_deadline == VOTE_BY + timedelta(minutes=15)
    (audit,) = await house.audits("event.deadline_moved")
    assert (audit.actor_member_id, audit.payload["as"]) == (house["Sam"].id, "coordinator")
    house.clock.now = VOTE_BY + timedelta(minutes=10)
    await slack.submit("Sam", ids.DECISION_TIME_VIEW, shift("minus15"), event_id, origin=origin)
    assert slack.ephemerals("Sam")[-1] == CLOSED[Window.VOTE].format(slot="dinner")
    assert (await house.event(event_id)).state is EventState.DECIDED


async def test_the_reply_names_the_day_for_a_call_posted_early(slack: Slack, house: House) -> None:
    house.clock.now = OPENS
    event_id = await open_lunch(house, early=True)  # tomorrow's lunch, posted today
    await everyone_in(house, event_id)
    house.clock.now = OPENS + timedelta(minutes=11)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    await slack.submit("Alex", ids.DECISION_TIME_VIEW, shift("plus15"), event_id, origin=origin)
    assert slack.ephemerals("Alex") == [f"Voting now closes at 12:15 PM on {day_label(TOMORROW)}."]


async def test_the_menu_refuses_while_answering(slack: Slack, house: House) -> None:
    event_id = await open_dinner(house)
    await slack.choose("Alex", move(event_id))
    assert opened(slack) == []
    assert slack.ephemerals("Alex") == ["You can move the decision time only while voting is open."]


async def test_a_modal_without_a_run_is_dropped(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    await slack.submit("Sam", ids.DECISION_TIME_VIEW, shift("plus15"), event_id)
    assert slack.ephemerals("Sam") == []
    assert await house.audits("event.deadline_moved") == []


def test_the_modal_reads_only_the_shifts_it_offers() -> None:
    assert [parse_decision_time(shift(v)) for v in ("plus15", "plus30", "minus15")] == [15, 30, -15]
    for bad in (shift("close"), shift("plus45"), {}):
        with pytest.raises(FormError):
            parse_decision_time(bad)
