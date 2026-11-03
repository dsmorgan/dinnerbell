# SPDX-License-Identifier: AGPL-3.0-or-later
"""Voting picks a coordinator when nobody claimed it (#179): one of the members who are in, at
random but reproducibly from the call and run, once a run, audited, logged, and told. Never a
guest, a maybe, or someone deactivated; never over a claim; nobody when no member is in."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from dinnerbell.domain.events import (
    AssignmentRole,
    Event,
    EventKind,
    EventRuleViolation,
    EventState,
    Response,
    RoleOffer,
    Status,
    Step,
    TieHow,
    Window,
    coordinator_candidates,
    coordinator_seed,
    pick_coordinator,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.services import call_admin, calls, guests, lifecycle, members
from dinnerbell.services import coordinator as coordinator_service
from tests.support.calls import RESPOND_BY, VOTE_BY, House, RecordingChannel
from tests.test_coordinator import act, add_guest, open_dinner, sent
from tests.test_coordinator import database as database  # the fixtures, re-exported for pytest
from tests.test_coordinator import house as house

pytestmark = pytest.mark.anyio

WHEN = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)


def _event(*, coordinator_id: int | None = None, run: int = 1) -> Event:
    return Event(
        7,
        1,
        EventKind.CALL,
        Slot.DINNER,
        date(2026, 10, 22),
        EventState.COLLECTING,
        run,
        WHEN,
        WHEN,
        15,
        45,
        coordinator_id=coordinator_id,
    )


def _answer(member_id: int, status: Status) -> Response:
    return Response(member_id=member_id, status=status)


def _person(member_id: int, *, role: Role = Role.MEMBER, active: bool = True) -> Member:
    return Member(member_id, 1, f"P{member_id}", None, role, Capabilities(), active)


PEOPLE = {
    1: _person(1),
    2: _person(2, role=Role.ADMIN),
    3: _person(3),
    4: _person(4, role=Role.GUEST),
    5: _person(5, active=False),
    6: _person(6),
}
ANSWERS = [
    _answer(6, Status.IN),
    _answer(2, Status.IN),
    _answer(1, Status.IN),
    _answer(3, Status.MAYBE),
    _answer(4, Status.IN),
    _answer(5, Status.IN),
]


def test_only_active_members_who_are_in_can_be_picked() -> None:
    assert coordinator_candidates(ANSWERS, PEOPLE) == [1, 2, 6]  # no maybe, guest, or deactivated member


def test_the_pick_is_reproducible_from_the_call_and_its_run() -> None:
    picked = pick_coordinator(_event(), ANSWERS, PEOPLE)
    assert picked in {1, 2, 6}
    assert pick_coordinator(_event(), list(reversed(ANSWERS)), PEOPLE) == picked  # order doesn't matter
    assert coordinator_seed(1, 7, 1) == coordinator_seed(1, 7, 1)
    assert len({coordinator_seed(1, 7, run) for run in range(1, 6)}) == 5  # each run picks afresh
    runs = {pick_coordinator(_event(run=run), ANSWERS, PEOPLE) for run in range(1, 30)}
    assert runs == {1, 2, 6}  # over many runs, each of them gets picked


def test_nobody_is_picked_over_a_coordinator_or_with_no_member_in() -> None:
    assert pick_coordinator(_event(coordinator_id=3), ANSWERS, PEOPLE) is None
    only_guests_and_maybes = [_answer(4, Status.IN), _answer(3, Status.MAYBE), _answer(1, Status.OUT)]
    assert pick_coordinator(_event(), only_guests_and_maybes, PEOPLE) is None


# Against the database ---------------------------------------------------------------------------


async def to_voting(house: House, *eaters: str, coordinator_name: str | None = None) -> int:
    """Alex and ``eaters`` are in, Alex offers to pick up, and ``coordinator_name`` claims
    coordinating, or nobody does; then answers close."""
    event_id = await open_dinner(house)
    for name in ("Alex", *eaters):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    if coordinator_name is not None:
        await act(house, calls.claim_coordinator, house[coordinator_name], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def test_voting_picks_a_member_who_is_in_and_tells_them(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Kim", coordinator_name=None)  # Lee never answered
    event = await house.event(event_id)
    in_ids = sorted(house[n].id for n in ("Alex", "Sam", "Kim"))
    assert event.coordinator_id in in_ids
    ctx = await house.context(event_id)
    assert event.coordinator_id == pick_coordinator(replace(event, coordinator_id=None), ctx.responses, ctx.members)
    (audit,) = await house.audits("event.coordinator_picked")
    assert audit.actor_member_id is None
    assert audit.payload == {"member_id": event.coordinator_id, "among": in_ids, "run": event.run}
    (told,) = await sent(house, "coordinator_picked")
    assert told.member.id == event.coordinator_id and told.recipient_id == event.coordinator_id
    latest = (await sent(house, "call_updated"))[-1]
    assert latest.event.coordinator.id == event.coordinator_id  # the call message names them
    (ready,) = await sent(house, "proposals_ready")
    assert ready.event.coordinator.id == event.coordinator_id
    picked = (await house.context(event_id)).response_of(event.coordinator_id)
    assert (picked.status, picked.late) == (Status.IN, False)  # in already: nothing about their answer changes


async def test_a_repeated_tick_picks_once(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)
    picked = (await house.event(event_id)).coordinator_id
    await house.tick()
    await house.tick()
    assert (await house.event(event_id)).coordinator_id == picked
    assert len(await house.audits("event.coordinator_picked")) == 1
    assert len(await sent(house, "coordinator_picked")) == 1


async def test_someone_who_claimed_it_keeps_it(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Kim", coordinator_name="Kim")
    assert (await house.event(event_id)).coordinator_id == house["Kim"].id
    assert not await house.audits("event.coordinator_picked")
    assert not await sent(house, "coordinator_picked")


async def test_a_claim_after_the_pick_is_refused(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)
    picked = (await house.event(event_id)).coordinator_id
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, calls.claim_coordinator, house["Kim"], event_id)
    assert refused.value.code == "has_coordinator"
    assert (await house.event(event_id)).coordinator_id == picked


async def test_a_maybe_coordinator_is_out_and_a_member_who_is_in_is_picked(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, calls.respond, house["Kim"], event_id, Status.MAYBE)
    house.clock.now = RESPOND_BY
    await house.tick()
    event = await house.event(event_id)
    assert event.state is EventState.VOTING
    assert event.coordinator_id in {house["Alex"].id, house["Sam"].id}  # not Kim, who's out now
    assert [a.payload["why"] for a in await house.audits("event.coordinator_released")] == ["maybe"]
    assert len(await house.audits("event.coordinator_picked")) == 1


async def test_nobody_is_picked_when_only_guests_are_in(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Pat", "Jo"):
        guest = await add_guest(house, name)
        await act(house, guests.invite, house["Sam"], event_id, guest_id=guest.id)
        await act(house, calls.respond, guest, event_id, Status.IN)
    await act(house, calls.offer_role, house["Pat"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    event = await house.event(event_id)
    assert (event.state, event.coordinator_id) == (EventState.VOTING, None)
    assert not await house.audits("event.coordinator_picked")


async def test_starting_the_vote_early_picks_too(house: House) -> None:
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.close_window, house["Alex"], event_id)  # Start the vote now, as an admin
    event = await house.event(event_id)
    assert event.state is EventState.VOTING
    assert event.coordinator_id in {house["Alex"].id, house["Sam"].id}
    assert len(await sent(house, "coordinator_picked")) == 1


async def test_the_message_is_not_sent_after_a_call_off_or_a_hand_off(house: House) -> None:
    event_id = await to_voting(house, "Sam", coordinator_name=None)
    picked = (await house.event(event_id)).coordinator_id
    other = house["Sam"] if picked == house["Alex"].id else house["Alex"]
    coordinator = next(m for m in house.people.values() if m.id == picked)
    await act(house, calls.hand_off, coordinator, event_id, other.id)
    channel = RecordingChannel()
    await house.drain(channel)
    assert "coordinator_picked" not in channel.kinds  # they don't coordinate any more

    await act(house, calls.cancel_call, house["Alex"], event_id)
    assert "coordinator_picked" not in channel.kinds


async def test_deactivating_whoever_was_picked_leaves_nobody_coordinating(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Kim", coordinator_name=None)
    picked = (await house.event(event_id)).coordinator_id
    assert picked is not None
    admin = await house.add_member("Max", admin=True)  # not in, so never picked
    await act(house, members.set_member_active, admin, picked, False)
    assert (await house.event(event_id)).coordinator_id is None
    assert [a.payload["why"] for a in await house.audits("event.coordinator_released")] == ["deactivated"]


async def test_a_rerun_keeps_whoever_was_picked_and_a_restart_picks_again(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Kim", coordinator_name=None)
    picked = (await house.event(event_id)).coordinator_id
    await act(house, call_admin.rerun, house["Alex"], event_id)
    rerun = await house.event(event_id)
    assert (rerun.run, rerun.coordinator_id) == (2, picked)  # an admin re-run keeps the coordinator
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, None)
    assert len(await house.audits("event.coordinator_picked")) == 1  # someone coordinates: no new pick

    await act(house, calls.cancel_call, house["Alex"], event_id)
    await act(house, calls.start_call, house["Kim"], Slot.DINNER, rerun.day)
    restarted = await house.event(event_id)
    assert (restarted.run, restarted.coordinator_id) == (3, None)
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, None)
    event = await house.event(event_id)
    assert event.state is EventState.VOTING and event.coordinator_id is not None
    picks = await house.audits("event.coordinator_picked")
    assert [a.payload["run"] for a in picks] == [1, 3]


async def test_a_failure_mid_step_picks_nothing_and_the_next_tick_picks_once(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pick commits with the move to voting, so a crash or restart partway leaves neither."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    real = lifecycle._coordinator_picked

    async def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the server stopped")

    monkeypatch.setattr(lifecycle, "_coordinator_picked", crash)
    house.clock.now = RESPOND_BY
    await house.tick()  # the tick logs the failure and rolls the step back
    event = await house.event(event_id)
    assert (event.state, event.coordinator_id) == (EventState.COLLECTING, None)
    monkeypatch.setattr(lifecycle, "_coordinator_picked", real)
    await house.tick()
    await house.tick()
    event = await house.event(event_id)
    assert event.state is EventState.VOTING and event.coordinator_id is not None
    assert len(await house.audits("event.coordinator_picked")) == 1
    assert len(await sent(house, "coordinator_picked")) == 1


async def test_whoever_was_picked_chooses_an_unclear_role_until_the_time_runs_out(house: House) -> None:
    """A picked coordinator gets the usual wait to choose who does it (FR-61), and when they
    don't choose, the first volunteer does it."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.PICKUP, True)  # two volunteers
    house.clock.now = RESPOND_BY
    await house.tick()
    picked = (await house.event(event_id)).coordinator_id
    assert picked in {house["Alex"].id, house["Sam"].id}
    (told,) = await sent(house, "coordinator_picked")
    assert told.choose_minutes == 15
    house.clock.now = VOTE_BY
    await house.tick()
    event = await house.event(event_id)
    assert event.state is EventState.DECIDED and event.roles_due_at == VOTE_BY + timedelta(minutes=15)
    (asked,) = await sent(house, "roles_to_choose")
    assert asked.recipient_id == picked
    ctx = await house.context(event_id)
    assert {(a.role, a.member_id) for a in ctx.assignments} == {(AssignmentRole.COORDINATOR, picked)}

    house.clock.now = VOTE_BY + timedelta(minutes=15)
    assert await house.tick() == [Step.ASSIGN_ROLES]
    ctx = await house.context(event_id)
    assert ctx.event.roles_due_at is None
    roles = {(a.role, a.member_id) for a in ctx.assignments}
    assert roles == {(AssignmentRole.COORDINATOR, picked), (AssignmentRole.PICKUP, house["Alex"].id)}  # first to offer


async def test_whoever_was_picked_breaks_a_tie(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Kim")
    picked = (await house.event(event_id)).coordinator_id
    assert picked is not None
    coordinator = next(m for m in house.people.values() if m.id == picked)
    others = [n for n in ("Alex", "Sam", "Kim") if house[n].id != picked]
    ctx = await house.context(event_id)
    first, second = (p.id for p in sorted(ctx.proposals, key=lambda p: p.rank)[:2])
    await act(house, calls.vote, house[others[0]], event_id, first)
    await act(house, calls.vote, house[others[1]], event_id, second)
    await act(house, coordinator_service.break_tie, coordinator, event_id, second)
    event = await house.event(event_id)
    assert (event.state, event.tie_how, event.tie_by) == (EventState.DECIDED, TieHow.CHOSE, picked)
    (made,) = await sent(house, "decision_made")
    assert made.decision.tie_note == f"Tied; {coordinator.display_name} (coordinator) chose"
