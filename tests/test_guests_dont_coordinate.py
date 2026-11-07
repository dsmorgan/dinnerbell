# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests don't coordinate (#227, FR-58): every way in is refused, for a guest with Slack and one
without, and they keep answering, offering, and being assigned roles. A call a guest coordinates
at upgrade loses its coordinator (0029), then follows the path for nobody coordinating."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack import preview as samples
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.channels.slack.views import adjust_modal
from dinnerbell.config import Settings
from dinnerbell.db.events import AssignmentRepository, EventRepository
from dinnerbell.db.models import ContributionRow, OutboxRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import (
    GUESTS_DONT,
    GUESTS_DONT_COORDINATE,
    AssignmentRole,
    Event,
    EventRuleViolation,
    EventState,
    RoleOffer,
    Status,
    check_can_mark_done,
)
from dinnerbell.domain.guests import NOT_INVITED, REFUSED, GuestAction
from dinnerbell.domain.members import Role
from dinnerbell.services import call_admin, calls, coordinator, event_views, guests
from dinnerbell.services.call_context import before_the_meal
from dinnerbell.services.coordinator import Adjustment
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, ZONE, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.test_coordinator import pick_ids
from tests.test_migrations import _downgrade_to, _upgrade_to

pytestmark = pytest.mark.anyio
CTX = RenderContext(samples.BASE_URL, samples.DIRECTORY)
BOTH = pytest.mark.parametrize("name", ["Pat", "Jo"], ids=["registered", "unregistered"])
"""Pat has Slack and answers for himself; Sam, who invited Jo, answers for Jo."""


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    await house.tag("Vegetarian", TagKind.DIETARY)
    async with house.sessionmaker() as db:
        members = MemberRepository(db)
        for name, slack in (("Pat", "U07PAT"), ("Jo", None)):
            house.people[name] = await members.add_guest(
                household_id=house.household.id,
                display_name=name,
                dietary_needs=frozenset(),
                slack_user_id=slack,
                now=clock(),
            )
        await db.commit()
    return house


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def open_with_guests(house: House, *guests_in: str) -> int:
    """Today's dinner, with Pat and Jo invited by Sam. ``guests_in`` say they're in."""
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    for name in ("Pat", "Jo"):
        await act(house, guests.invite, house["Sam"], event_id, guest_id=house[name].id)
    for name in guests_in:
        await answer(house, event_id, name, Status.IN)
    return event_id


async def answer(house: House, event_id: int, name: str, status: Status) -> None:
    if house[name].is_registered_guest or not house[name].is_guest:
        await act(house, calls.respond, house[name], event_id, status)
    else:
        await act(house, guests.answer_for, house["Sam"], event_id, house[name].id, status=status)


async def to_voting(house: House, *guests_in: str) -> int:
    event_id = await open_with_guests(house, *guests_in)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    return event_id


async def to_decided(house: House, *guests_in: str) -> int:
    event_id = await to_voting(house, *guests_in)
    ctx = await house.context(event_id)
    await act(house, calls.lock, house["Alex"], event_id, ctx.proposals[0].id)
    return event_id


async def refuse(
    house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
) -> EventRuleViolation:
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, use_case, *args, **kwargs)
    return refused.value


# Every way in ------------------------------------------------------------------------------------


@BOTH
async def test_a_guest_cant_claim_it_and_is_told_why(house: House, name: str) -> None:
    event_id = await open_with_guests(house, name)
    refused = await refuse(house, calls.claim_coordinator, house[name], event_id)
    assert (refused.code, str(refused)) == ("guests_dont", REFUSED[GuestAction.COORDINATE])
    assert (await house.event(event_id)).coordinator_id is None
    view = await act(house, event_views.event_view, house[name], event_id)
    assert not view.can.claim


async def test_a_guest_not_invited_learns_nothing_about_the_call(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    refused = await refuse(house, calls.claim_coordinator, house["Pat"], await dinner_id(house))
    assert (refused.code, str(refused)) == ("not_invited", NOT_INVITED)


@BOTH
@pytest.mark.parametrize("state", [EventState.COLLECTING, EventState.DECIDED])
async def test_nobody_can_hand_it_to_a_guest(house: House, name: str, state: EventState) -> None:
    event_id = await (open_with_guests if state is EventState.COLLECTING else to_decided)(house, name)
    event = await house.event(event_id)
    if event.coordinator_id != house["Sam"].id:
        await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Sam"].id)
    refused = await refuse(house, calls.hand_off, house["Sam"], event_id, house[name].id)
    assert (refused.code, str(refused)) == ("guests_dont", GUESTS_DONT_COORDINATE)
    assert (await house.event(event_id)).coordinator_id == house["Sam"].id


@BOTH
async def test_adjust_doesnt_offer_a_guest_and_refuses_one_sent_anyway(house: House, name: str) -> None:
    event_id = await to_decided(house, name)
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Sam"].id)
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    assert view.roles is not None and house[name].id in {p.id for p in view.roles.people}  # roles, yes
    opened = ids.Opened(time_of(await house.event(event_id)), None, house["Sam"].id)
    built = adjust_modal(view, ids.ViewOrigin(event_id, "C1", run=1, opened=opened))
    (lead,) = [b for b in built["blocks"] if b.get("block_id") == "coordinator"]
    offered = {o["value"] for o in lead["element"]["options"]}
    assert str(house[name].id) not in offered and str(house["Alex"].id) in offered

    meal = time_of(await house.event(event_id))
    as_is = Adjustment(meal, None, house["Sam"].id)
    with pytest.raises(coordinator.AdjustmentRefused) as refused:
        await act(
            house, coordinator.adjust, house["Sam"], event_id, replace(as_is, coordinator_id=house[name].id),
            opened=as_is, run=1,
        )  # fmt: skip
    assert refused.value.part == "coordinator" and GUESTS_DONT_COORDINATE in str(refused.value)


def time_of(event: Event) -> time:
    assert event.meal_time is not None
    return event.meal_time.astimezone(ZoneInfo(ZONE)).time().replace(second=0, microsecond=0)


@BOTH
async def test_an_admin_cant_assign_a_guest_or_be_offered_one(house: House, name: str) -> None:
    event_id = await open_with_guests(house, name)
    refused = await refuse(house, call_admin.assign_coordinator, house["Alex"], event_id, house[name].id)
    assert (refused.code, str(refused)) == ("guests_dont", GUESTS_DONT_COORDINATE)
    assert (await house.event(event_id)).coordinator_id is None
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    assert house[name].id in {p.id for p in view.members}  # who can be assigned a role
    assert house[name].id not in {p.id for p in view.coordinators}
    assert house["Sam"].id in {p.id for p in view.coordinators}


async def test_an_admin_cant_assign_a_guest_who_isnt_invited_either(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    refused = await refuse(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Pat"].id)
    assert refused.code == "guests_dont"
    with pytest.raises(NotFound):
        await act(house, call_admin.assign_coordinator, house["Alex"], event_id, 4242)


@BOTH
async def test_set_coordinator_refuses_a_guest_for_any_caller(house: House, name: str) -> None:
    """Every way to become coordinator ends here, so nothing new can slip one past it."""
    event_id = await open_with_guests(house, name)

    async def directly(db: Any, *, now: Any) -> None:
        await calls.set_coordinator(
            db, await EventRepository(db).get(house.household.id, event_id), house[name].id, now=now
        )

    refused = await refuse(house, directly)
    assert refused.code == "guests_dont"


@BOTH
async def test_answering_and_offering_never_make_a_guest_coordinate(house: House, name: str) -> None:
    """Nothing implicit: not an answer, a role offer, or voting's random pick, even with a guest
    the only one in who's offered."""
    event_id = await open_with_guests(house, name)
    await answer(house, event_id, name, Status.IN)
    if name == "Pat":
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.PICKUP, True)
    else:
        await act(house, guests.answer_for, house["Sam"], event_id, house[name].id, role=RoleOffer.PICKUP, offered=True)
    await act(house, calls.respond, house["Alex"], event_id, Status.OUT)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    assert (await house.event(event_id)).coordinator_id is None
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).coordinator_id == house["Sam"].id  # the one member in


async def test_with_only_guests_in_nobody_coordinates(house: House) -> None:
    event_id = await open_with_guests(house, "Pat", "Jo")
    await act(house, calls.offer_role, house["Pat"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    event = await house.event(event_id)
    assert event.state is EventState.VOTING and event.coordinator_id is None


async def test_a_promoted_guest_coordinates(house: House) -> None:
    event_id = await open_with_guests(house, "Pat")
    promoted = await act(house, guests.promote, house["Alex"], house["Pat"].id, Role.MEMBER, email="pat@example.com")
    await act(house, calls.claim_coordinator, promoted, event_id)
    assert (await house.event(event_id)).coordinator_id == promoted.id
    assert promoted.id in {
        p.id for p in (await act(house, event_views.event_view, house["Alex"], event_id)).coordinators
    }


# What a coordinator can do, refused -------------------------------------------------------------


@BOTH
async def test_every_coordinator_power_is_refused_while_answers_are_open(house: House, name: str) -> None:
    event_id = await open_with_guests(house, name)
    refused = await refuse(house, calls.close_window, house[name], event_id)
    assert (refused.code, str(refused)) == ("guests_dont", REFUSED[GuestAction.COORDINATE])
    assert (await house.event(event_id)).state is EventState.COLLECTING
    view = await act(house, event_views.event_view, house[name], event_id)
    assert view.can.close_window is None and not view.can.hand_off


@BOTH
async def test_every_coordinator_power_is_refused_while_voting(house: House, name: str) -> None:
    event_id = await to_voting(house, name)
    guest, pick = house[name], (await house.context(event_id)).proposals[0]
    before = await house.event(event_id)
    powers: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...], dict[str, Any]]] = [
        (calls.close_window, (guest, event_id), {}),
        (calls.lock, (guest, event_id, pick.id), {}),
        (calls.hand_off, (guest, event_id, house["Sam"].id), {}),
        (calls.switch_late_joiner, (guest, event_id, house["Kim"].id), {"vote_counts": True}),
        (coordinator.shuffle, (guest, event_id), {}),
        (coordinator.break_tie, (guest, event_id, pick.id), {}),
        (coordinator.set_way, (guest, event_id, pick.id, Mode.DELIVERY), {"run": 1}),
        (coordinator.move_decision_time, (guest, event_id, 15), {"run": 1}),
    ]
    for use_case, args, kwargs in powers:
        refused = await refuse(house, use_case, *args, **kwargs)
        assert (refused.code, str(refused)) == ("guests_dont", REFUSED[GuestAction.COORDINATE]), use_case
    after = await house.event(event_id)
    assert (after.state, after.vote_deadline, after.coordinator_id) == (
        EventState.VOTING,
        before.vote_deadline,
        before.coordinator_id,
    )
    can = (await act(house, event_views.event_view, guest, event_id)).can
    assert not (can.set_way or can.move_vote or can.shuffle or can.break_tie or can.switch_late or can.lock)


@BOTH
async def test_every_coordinator_power_is_refused_once_decided(house: House, name: str) -> None:
    event_id = await to_decided(house, name)
    guest = house[name]
    event = await house.event(event_id)
    meal = time_of(event)
    powers: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...], dict[str, Any]]] = [
        (calls.set_meal_time, (guest, event_id, meal), {}),
        (calls.change_assignment, (guest, event_id, AssignmentRole.PICKUP, house["Sam"].id), {}),
        (calls.hand_off, (guest, event_id, house["Sam"].id), {}),
        (coordinator.choose_roles, (guest, event_id), {"member_id": house["Sam"].id}),
        (
            coordinator.change_place,
            (guest, event_id),
            {"name": "Soup", "mode": Mode.PICKUP, "opened_item_id": event.decided_item_id},
        ),
    ]
    for use_case, args, kwargs in powers:
        refused = await refuse(house, use_case, *args, **kwargs)
        assert (refused.code, str(refused)) == ("guests_dont", REFUSED[GuestAction.COORDINATE]), use_case
    with pytest.raises(EventRuleViolation) as caught:
        async with house.sessionmaker() as db:
            await coordinator.plan_adjustment(db, guest, event_id, Adjustment(), opened=Adjustment(), run=1)
    assert caught.value.code == "guests_dont"
    can = (await act(house, event_views.event_view, guest, event_id)).can
    assert not (can.manage or can.choose_roles or can.adjust or can.change_place or can.hand_off or can.mark_done)


@BOTH
async def test_a_guest_cant_mark_it_done_but_a_member_can_and_it_ends_on_its_own(house: House, name: str) -> None:
    event_id = await to_decided(house, name)
    event = await house.event(event_id)
    assert event.meal_time is not None
    house.clock.now = event.meal_time
    refused = await refuse(house, calls.mark_done, house[name], event_id)
    assert (refused.code, str(refused)) == ("guests_dont", REFUSED[GuestAction.MARK_DONE])
    assert not (await act(house, event_views.event_view, house[name], event_id)).can.mark_done
    assert (await house.event(event_id)).state is EventState.DECIDED
    house.clock.now = event.meal_time + timedelta(hours=2)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.DONE


async def test_the_domain_refuses_a_guest_who_is_in_on_its_own(house: House) -> None:
    event_id = await to_decided(house, "Pat")
    ctx = await house.context(event_id)
    assert ctx.event.meal_time is not None
    with pytest.raises(EventRuleViolation) as refused:
        check_can_mark_done(ctx.event, house["Pat"], ctx.response_of(house["Pat"].id), now=ctx.event.meal_time)
    assert (refused.value.code, str(refused.value)) == ("guests_dont", GUESTS_DONT)


# What a guest keeps ------------------------------------------------------------------------------


async def test_a_guest_with_slack_still_answers_offers_and_is_told_of_a_role(house: House) -> None:
    event_id = await open_with_guests(house)
    pat = house["Pat"]
    await act(house, calls.respond, pat, event_id, Status.OUT)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.offer_role, pat, event_id, RoleOffer.PICKUP, True)
    assert (await act(house, event_views.event_view, pat, event_id)).can.respond
    mine = (await house.context(event_id)).response_of(pat.id)
    assert mine is not None and mine.status is Status.IN and RoleOffer.PICKUP in mine.offers
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    await act(house, calls.lock, house["Alex"], event_id, ctx.proposals[0].id)
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, pat.id)
    held = {a.role: a.member_id for a in (await house.context(event_id)).assignments}
    assert held[AssignmentRole.PICKUP] == pat.id
    dms = [note.from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == "assignment_made"]
    assert (pat.id, pat.id) in {(d.member.id, d.recipient_id) for d in dms}  # type: ignore[union-attr]


async def test_a_guest_without_slack_is_told_through_whoever_invited_them(house: House) -> None:
    event_id = await open_with_guests(house, "Jo")
    jo = house["Jo"]
    await act(house, guests.answer_for, house["Sam"], event_id, jo.id, role=RoleOffer.PICKUP, offered=True)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    await act(house, calls.lock, house["Alex"], event_id, ctx.proposals[0].id)
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, jo.id)
    dms = [note.from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == "assignment_made"]
    assert (jo.id, house["Sam"].id) in {(d.member.id, d.recipient_id) for d in dms}  # type: ignore[union-attr]


async def test_answering_out_for_a_guest_changes_nothing_about_who_coordinates(house: House) -> None:
    event_id = await open_with_guests(house, "Jo")
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, guests.answer_for, house["Sam"], event_id, house["Jo"].id, status=Status.OUT)
    assert (await house.event(event_id)).coordinator_id == house["Kim"].id


async def test_a_guest_who_leaves_leaves_the_coordinator_alone(house: House) -> None:
    event_id = await open_with_guests(house, "Pat", "Jo")
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, guests.uninvite, house["Sam"], event_id, house["Jo"].id)
    await act(house, guests.remove, house["Alex"], house["Pat"].id)
    assert (await house.event(event_id)).coordinator_id == house["Kim"].id


# Rows queued before the upgrade ------------------------------------------------------------------


async def test_a_row_queued_with_the_old_fields_still_reads(house: House) -> None:
    await to_voting(house)
    (row,) = [e for e in await house.outbox() if e.kind == "proposals_ready"]
    payload = dict(row.payload)
    payload["event"] = {**payload["event"], "coordinator_is_guest": True}
    decoded = note.from_payload(row.kind, payload)
    assert not hasattr(decoded.event, "coordinator_is_guest")


# The upgrade: 0029 -------------------------------------------------------------------------------


async def guest_coordinates(house: House, event_id: int, name: str, migrated: Settings) -> None:
    """A guest coordinates, as they could until #227, and the app upgrades."""
    async with house.sessionmaker() as db:
        event = await EventRepository(db).get(house.household.id, event_id)
        assert event is not None
        await EventRepository(db).save(replace(event, coordinator_id=house[name].id), house.clock())
        if event.state in {EventState.DECIDED, EventState.DONE}:
            await AssignmentRepository(db).assign(
                house.household.id, event_id, AssignmentRole.COORDINATOR, house[name].id, house.clock()
            )
        await db.commit()
    upgrade(migrated)


def upgrade(migrated: Settings) -> None:
    """Back to before 0029, then up to head, so 0029 runs on today's rows and every later
    revision comes back for the app."""
    _downgrade_to(migrated, "0028_unregistered_guest_badges")
    _upgrade_to(migrated, "head")


@BOTH
async def test_upgrade_while_answers_are_open_lets_voting_pick_a_member(
    house: House, migrated: Settings, name: str
) -> None:
    event_id = await open_with_guests(house, name)
    await guest_coordinates(house, event_id, name, migrated)
    ctx = await house.context(event_id)
    assert ctx.event.coordinator_id is None
    guest_answer = ctx.response_of(house[name].id)
    assert guest_answer is not None and guest_answer.status is Status.IN  # they stay in
    for member in ("Alex", "Sam"):
        await act(house, calls.respond, house[member], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    event = await house.event(event_id)
    assert event.state is EventState.VOTING and event.coordinator_id in {house["Alex"].id, house["Sam"].id}


async def test_upgrade_while_voting_leaves_the_tie_to_the_admins_and_a_member_can_claim(
    house: House, migrated: Settings
) -> None:
    event_id = await to_voting(house, "Pat")
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, None)
    await guest_coordinates(house, event_id, "Pat", migrated)
    assert (await house.event(event_id)).coordinator_id is None
    picks = (await house.context(event_id)).proposals
    await act(house, calls.vote, house["Sam"], event_id, picks[0].id)
    await act(house, calls.vote, house["Alex"], event_id, picks[1].id)  # the admin's vote
    refused = await refuse(house, calls.claim_coordinator, house["Pat"], event_id)
    assert refused.code == "guests_dont"
    house.clock.now = VOTE_BY
    await house.tick()
    event = await house.event(event_id)
    assert (event.state, event.decided_item_id) == (EventState.DECIDED, picks[1].item_id)
    assert event.coordinator_id is None  # nobody claimed it


async def test_upgrade_while_voting_a_member_can_still_claim(house: House, migrated: Settings) -> None:
    event_id = await to_voting(house, "Pat")
    await guest_coordinates(house, event_id, "Pat", migrated)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    assert (await house.event(event_id)).coordinator_id == house["Kim"].id


async def test_upgrade_with_roles_pending_still_resolves(house: House, migrated: Settings) -> None:
    """The guest was choosing the roles: they're told no more, an admin can choose, and the first
    volunteers do the roles when the time runs out. A guest who offered gets their role."""
    event_id = await open_with_guests(house, "Pat")
    await guest_coordinates_before_the_decision(house, event_id)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Pat"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    # Siam Garden allows pickup and delivery, and nobody chose its way, so the decision asks.
    await act(house, calls.lock, house["Alex"], event_id, (await pick_ids(house, event_id))["Siam Garden"])
    event = await house.event(event_id)
    assert event.roles_due_at is not None and event.coordinator_id == house["Pat"].id
    (ask,) = [e for e in await house.outbox() if e.kind == "roles_to_choose"]
    upgrade(migrated)

    ctx = await house.context(event_id)
    assert ctx.event.coordinator_id is None and ctx.event.roles_due_at == event.roles_due_at
    assert AssignmentRole.COORDINATOR not in {a.role for a in ctx.assignments}
    async with house.sessionmaker() as db:
        closed = await db.get(OutboxRow, ask.id)
    assert closed is not None and closed.delivered_at is not None and "#227" in (closed.last_error or "")
    can = (await act(house, event_views.event_view, house["Alex"], event_id)).can
    assert can.choose_roles
    assert not (await act(house, event_views.event_view, house["Pat"], event_id)).can.choose_roles
    house.clock.now = event.roles_due_at + timedelta(seconds=1)  # type: ignore[operator]
    assert before_the_meal(ctx.event, house.clock())
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED and ctx.event.roles_due_at is None
    assert house["Pat"].id in {a.member_id for a in ctx.assignments}  # the one who offered, and told
    dms = [note.from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == "assignment_made"]
    assert house["Pat"].id in {d.member.id for d in dms}  # type: ignore[union-attr]


def test_a_pending_choice_with_nobody_coordinating_says_an_admin_can() -> None:
    """The decision never says "the coordinator is choosing" when there's no coordinator."""
    told = render(note.DecisionMade(replace(samples.DECIDED, coordinator=None), samples.PENDING_DECISION), CTX)
    shown = json.dumps(told.blocks, ensure_ascii=False)
    assert "An admin can choose" in shown and "If nobody does" in shown and "is choosing" not in shown


async def guest_coordinates_before_the_decision(house: House, event_id: int) -> None:
    async with house.sessionmaker() as db:
        event = await EventRepository(db).get(house.household.id, event_id)
        assert event is not None
        await EventRepository(db).save(replace(event, coordinator_id=house["Pat"].id), house.clock())
        await db.commit()


async def test_upgrade_after_the_decision_an_admin_can_assign_and_the_meal_finishes(
    house: House, migrated: Settings
) -> None:
    event_id = await to_decided(house, "Pat")
    await guest_coordinates(house, event_id, "Pat", migrated)
    ctx = await house.context(event_id)
    assert ctx.event.coordinator_id is None
    assert AssignmentRole.COORDINATOR not in {a.role for a in ctx.assignments}
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Sam"].id)
    assert (await house.event(event_id)).coordinator_id == house["Sam"].id
    event = await house.event(event_id)
    assert event.meal_time is not None
    house.clock.now = event.meal_time + timedelta(hours=2)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.DONE
    async with house.sessionmaker() as db:
        rows = (await db.scalars(select(ContributionRow).where(ContributionRow.event_id == event_id))).all()
    coordinated = {r.member_id for r in rows if r.kind == Kind.COORDINATED.value}
    assert coordinated == {house["Sam"].id}


async def test_upgrade_keeps_the_history_of_a_finished_call_and_a_rerun_drops_the_guest(
    house: House, migrated: Settings
) -> None:
    event_id = await to_voting(house, "Pat")
    await act(house, calls.cancel_call, house["Alex"], event_id)
    await guest_coordinates(house, event_id, "Pat", migrated)
    assert (await house.event(event_id)).coordinator_id == house["Pat"].id  # cancelled: it stays
    await act(house, call_admin.rerun, house["Alex"], event_id)
    event = await house.event(event_id)
    assert event.state is EventState.COLLECTING and event.coordinator_id is None


async def test_a_rerun_keeps_a_member_coordinating(house: House) -> None:
    event_id = await to_voting(house, "Pat")
    await act(house, call_admin.assign_coordinator, house["Alex"], event_id, house["Sam"].id)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    assert (await house.event(event_id)).coordinator_id == house["Sam"].id
