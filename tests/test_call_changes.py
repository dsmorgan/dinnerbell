# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calls that change under people (issue 331): a deactivated member leaves the open calls they
were in (B-4), a retried role message never follows "you're off it" (B-5), a guest invited
again gets a second invitation (B-6), and a cooking offer that's over before the call opens
doesn't hold the call back (FR-5)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.models import OutboxRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain import notifications as n
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import AssignmentRole, EventState, RoleOffer, Status
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import call_admin, calls, guests, members, offers
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.offers import OfferInput
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 22)
WORK_ROLES = {AssignmentRole.COOK, AssignmentRole.PICKUP, AssignmentRole.ORDER}


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


async def to_voting(house: House) -> int:
    """Alex (the admin), Sam, and Lee are in, and Kim is out; Lee picks up, and Sam coordinates."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    await act(house, calls.offer_role, house["Lee"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def decided(house: House) -> int:
    event_id = await to_voting(house)
    await house.settle_ways(event_id)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED
    assert {a.member_id for a in ctx.assignments if a.role in WORK_ROLES} == {house["Lee"].id}
    return event_id


async def lee_picks_up_alone(house: House) -> int:
    """Decided, with Lee picking up and Alex ordering, so Lee's only role is the pickup."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    await act(house, calls.offer_role, house["Lee"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.ORDER, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await house.settle_ways(event_id)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED
    assert {a.role for a in ctx.assignments if a.member_id == house["Lee"].id} == {AssignmentRole.PICKUP}
    return event_id


@dataclass
class Capturing(RecordingChannel):
    """Records the notifications it delivers too, as the drainer hands them over."""

    delivered: list[n.Notification] = field(default_factory=list)

    async def deliver(self, notification: n.Notification, *, thread: n.MessageRef | None = None) -> n.DeliveryResult:
        result = await super().deliver(notification, thread=thread)
        self.delivered.append(notification)
        return result


async def deactivate(house: House, name: str, active: bool = False) -> Member:
    member: Member = await act(house, members.set_member_active, house["Alex"], house[name].id, active)
    house.people[name] = member
    return member


async def rows(house: House) -> list[tuple[OutboxRow, n.Notification]]:
    async with house.sessionmaker() as db:
        found = list(await db.scalars(select(OutboxRow).order_by(OutboxRow.id)))
    return [(r, n.from_payload(r.kind, r.payload)) for r in found]


# B-4: a deactivated member leaves the open calls they were in ------------------------------------


async def test_a_deactivated_member_no_longer_counts_toward_the_head_count(house: House) -> None:
    """Two in, and one is deactivated: the call ends on-your-own at the deadline, as with one in,
    instead of going to a vote with a head count of 2."""
    event_id = await open_dinner(house)
    for name in ("Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Lee"], event_id, RoleOffer.PICKUP, True)

    await deactivate(house, "Lee")

    ctx = await house.context(event_id)
    lee = ctx.response_of(house["Lee"].id)
    assert lee is not None and lee.status is Status.OUT and not lee.offers
    assert ctx.head_count == 1 and not ctx.coverage.pickups
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.ABANDONED


async def test_a_deactivated_members_vote_stops_counting(house: House) -> None:
    event_id = await to_voting(house)
    ctx = await house.context(event_id)
    pick = ctx.proposals[0].id
    await act(house, calls.vote, house["Lee"], event_id, pick)
    await house.drain(RecordingChannel())
    channel = RecordingChannel()

    await deactivate(house, "Lee")

    ctx = await house.context(event_id)
    assert house["Lee"].id not in ctx.votes.get(pick, [])
    lee = ctx.response_of(house["Lee"].id)
    assert lee is not None and lee.status is Status.OUT
    assert ctx.head_count == 2
    await house.drain(channel)
    assert {"proposals_updated", "call_updated"} <= set(channel.kinds)  # the tally and the roster show it


async def test_a_deactivated_member_leaves_their_role_on_a_decided_call(house: House) -> None:
    event_id = await decided(house)
    await house.drain(RecordingChannel())
    before = len(await rows(house))

    await deactivate(house, "Lee")

    ctx = await house.context(event_id)
    assert not [a for a in ctx.assignments if a.member_id == house["Lee"].id]
    sent = [x for _, x in (await rows(house))[before:]]
    vacated = [x for x in sent if isinstance(x, n.RoleVacated)]
    assert [(x.member.name, x.left.name) for x in vacated] == [("Sam", "Lee")]  # the coordinator picks again
    assert AssignmentRole.PICKUP in vacated[0].roles
    assert {"call_updated", "decision_updated"} <= {x.kind for x in sent}


async def test_reactivating_a_member_puts_nothing_back(house: House) -> None:
    """They answer again, as anyone out can: nothing guesses what they'd have said."""
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Lee"], event_id, Status.IN)
    await deactivate(house, "Lee")

    await deactivate(house, "Lee", active=True)

    lee = (await house.context(event_id)).response_of(house["Lee"].id)
    assert lee is not None and lee.status is Status.OUT
    await act(house, calls.respond, house["Lee"], event_id, Status.IN)
    assert (await house.context(event_id)).head_count == 1


async def test_deactivating_a_member_leaves_calls_they_werent_in_alone(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    before = len(await rows(house))

    await deactivate(house, "Kim")

    assert (await house.context(event_id)).response_of(house["Kim"].id) is None
    assert len(await rows(house)) == before


# B-5: a retried role message never follows "you're off it" -------------------------------------


async def lee_message_fails_once(house: House, event_id: int) -> int:
    """Lee's "You're ..." message fails once and waits to be tried again. Returns its row's ID."""
    (made,) = [r.id for r, x in await rows(house) if isinstance(x, n.AssignmentMade) and x.member.name == "Lee"]
    await house.drain(RecordingChannel(fail=1_000))
    house.clock.now = VOTE_BY + timedelta(minutes=1)
    return made


async def drain_twice(house: House) -> Capturing:
    """Drain now, and again once every retry is due."""
    channel = Capturing()
    await house.drain(channel)
    house.clock.now = VOTE_BY + timedelta(hours=1)
    await house.drain(channel)
    return channel


async def test_a_retried_role_message_closes_once_the_role_moved_on(house: House) -> None:
    """Lee's "You're picking up" fails once and waits; meanwhile the pickup goes to Sam, and Lee
    hears it's off. The retry must not arrive after that."""
    event_id = await lee_picks_up_alone(house)
    made = await lee_message_fails_once(house, event_id)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Sam"].id)

    channel = await drain_twice(house)

    assert [s.kind for s in channel.sent if s.recipient == house["Lee"].id] == ["assignment_ended"]
    async with house.sessionmaker() as db:
        row = await db.get(OutboxRow, made)
    assert row is not None and row.last_error == n.SUPERSEDED_BY_ROLE_MOVE


async def test_a_retried_role_message_names_only_the_roles_still_theirs(house: House) -> None:
    """Lee orders and picks up; the pickup goes to Alex while the message waits. Lee still
    hears that they order, and only that."""
    event_id = await decided(house)
    await lee_message_fails_once(house, event_id)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Alex"].id)

    channel = await drain_twice(house)

    lees = [x for x in channel.delivered if x.recipient_id == house["Lee"].id]
    assert sorted(x.kind for x in lees) == ["assignment_ended", "assignment_made"]
    (made,) = [x for x in lees if isinstance(x, n.AssignmentMade)]
    assert made.roles == (AssignmentRole.ORDER,)


async def test_a_hand_off_leaves_the_rest_of_a_waiting_role_message(house: House) -> None:
    """Lee picks up and coordinates; the message waits, and Lee hands coordinating to Alex. Lee
    still picks up, so they're told so, without coordinating."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)
    await act(house, calls.offer_role, house["Lee"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Lee"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await house.settle_ways(event_id)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert AssignmentRole.COORDINATOR in {a.role for a in ctx.assignments if a.member_id == house["Lee"].id}
    await lee_message_fails_once(house, event_id)
    await act(house, calls.hand_off, house["Lee"], event_id, house["Alex"].id)

    channel = await drain_twice(house)

    made = [x for x in channel.delivered if isinstance(x, n.AssignmentMade) and x.member.name == "Lee"]
    assert len(made) == 1 and AssignmentRole.PICKUP in made[0].roles
    assert AssignmentRole.COORDINATOR not in made[0].roles


async def lee_on_the_web_app(house: House) -> None:
    from dinnerbell.domain.members import NotifyVia
    from dinnerbell.services import web_app
    from tests.test_push import device, switch

    await switch(house, True)
    async with house.sessionmaker() as db:
        house.people["Lee"] = await web_app.set_my_notify_via(db, house["Lee"], NotifyVia.WEB, now=house.clock())
    await device(house, "Lee")


async def push_all(house: House) -> Any:
    from tests.test_push import FakeSender, drain

    sender = FakeSender()
    for _ in range(5):  # a pass sends a few to each device; the rest wait for the next
        await drain(house, sender)
    return sender


async def test_a_role_push_closes_once_the_role_moved_on(house: House) -> None:
    """The push drainer drops a role push by the same rule (``outbox.overtaken_by``)."""
    from tests.test_push import queued

    await lee_on_the_web_app(house)
    event_id = await lee_picks_up_alone(house)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Sam"].id)

    await push_all(house)

    lees = [error for whose, _, error, *_ in await queued(house) if whose == "Lee"]
    assert n.SUPERSEDED_BY_ROLE_MOVE in lees


async def test_a_role_push_names_only_the_roles_still_theirs(house: House) -> None:
    """Lee orders and picks up, and the pickup goes to Alex before the push goes: it says only
    that Lee orders (``outbox.as_it_stands``)."""
    await lee_on_the_web_app(house)
    event_id = await decided(house)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Alex"].id)

    sender = await push_all(house)

    titles = [json.loads(body)["notification"]["title"] for _, body, *_ in sender.sent]
    assert any(t.endswith("you're ordering") for t in titles), titles
    assert not any("picking up" in t and "ordering" in t for t in titles), titles


async def test_a_role_message_still_goes_while_the_role_is_theirs(house: House) -> None:
    await decided(house)
    channel = RecordingChannel()
    await house.drain(channel)
    assert [s.recipient for s in channel.sent if s.kind == "assignment_made"].count(house["Lee"].id) == 1


# B-6: a guest invited again gets a second invitation ---------------------------------------------


async def add_guest(house: House, name: str, slack: str | None) -> Member:
    async with house.sessionmaker() as db:
        guest = await MemberRepository(db).add_guest(
            household_id=house.household.id,
            display_name=name,
            dietary_needs=frozenset(),
            slack_user_id=slack,
            now=house.clock(),
        )
        await db.commit()
    house.people[name] = guest
    return guest


async def test_a_guest_invited_again_gets_another_invitation(house: House) -> None:
    pat = await add_guest(house, "Pat", "U07PAT")
    event_id = await open_dinner(house)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    channel = RecordingChannel()
    await house.drain(channel)
    house.clock.now = OPENS.replace(minute=10)
    await act(house, guests.uninvite, house["Sam"], event_id, pat.id)
    house.clock.now = OPENS.replace(minute=20)

    await act(house, guests.invite, house["Kim"], event_id, guest_id=pat.id)
    await house.drain(channel)

    invitations = [s for s in channel.sent if s.kind == "guest_invited"]
    assert [s.recipient for s in invitations] == [pat.id, pat.id]
    assert invitations[0].key != invitations[1].key


async def test_the_same_invitation_is_stored_once(house: House) -> None:
    """Keyed by when the guest was invited, so publishing the same invitation again, as a
    retried transition would, still adds one row."""
    pat = await add_guest(house, "Pat", "U07PAT")
    event_id = await open_dinner(house)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    (invited,) = [x for _, x in await rows(house) if isinstance(x, n.GuestInvited)]
    async with house.sessionmaker() as db:
        from dinnerbell.db.outbox import OutboxRepository

        await OutboxRepository(db).enqueue(house.household.id, event_id, invited, house.clock())
        await db.commit()
    assert len([x for _, x in await rows(house) if isinstance(x, n.GuestInvited)]) == 1


async def test_a_rerun_invites_the_guest_again_once(house: House) -> None:
    pat = await add_guest(house, "Pat", "U07PAT")
    event_id = await open_dinner(house)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()

    await act(house, call_admin.rerun, house["Alex"], event_id)

    invited = [x for _, x in await rows(house) if isinstance(x, n.GuestInvited)]
    assert [x.event.run for x in invited] == [1, 2]


# FR-5: a cooking offer that's over before the call opens doesn't stand in for it ----------------

AFTER_LUNCH = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
"""1:00 PM in Chicago: past lunch's usual meal time, before the 3:00 PM dinner call."""


async def test_an_afternoon_cooking_offer_filed_under_dinner_doesnt_hold_back_the_call(house: House) -> None:
    """After lunch's usual meal time the offer form starts on dinner (``offer_form``), so a
    cooking offer for 1:30 PM is easily posted as dinner. It's over before the dinner call
    opens, so the call still goes out at 3:00 PM."""
    house.clock.now = AFTER_LUNCH
    form = await act(house, offers.offer_form, house["Sam"])
    assert form.slot is Slot.DINNER
    data = OfferInput(
        slot=form.slot,
        day=DAY,
        mode=Mode.COOK,
        at=time(13, 30),
        capacity=4,
        item_id=house.items["Sam's Tacos"].id,
    )
    posted = await act(house, offers.post_offer, house["Sam"], data)
    assert posted.path is offers.OfferPath.NEW

    house.clock.now = OPENS
    await house.tick()

    assert (await house.event(await dinner_id(house))).state is EventState.COLLECTING


async def test_an_evening_cooking_offer_still_stands_in_for_the_call(house: House) -> None:
    house.clock.now = AFTER_LUNCH
    data = OfferInput(
        slot=Slot.DINNER, day=DAY, mode=Mode.COOK, at=time(18, 0), capacity=4, item_id=house.items["Sam's Tacos"].id
    )
    await act(house, offers.post_offer, house["Sam"], data)
    house.clock.now = OPENS
    await house.tick()
    async with house.sessionmaker() as db:
        from dinnerbell.db.events import EventRepository

        assert await EventRepository(db).call_for(house.household.id, Slot.DINNER, DAY) is None


# Review of #383: after the meal, offers, App Home, the call's own time, and who's told ---------


async def test_deactivating_a_member_after_the_meal_keeps_what_they_did(house: House) -> None:
    """Dinner was at 6:30 PM and Lee picked it up; at 7:00 PM, before the call is done, an admin
    deactivates Lee. The record keeps Lee's answer and roles, and nobody is asked to pick
    someone else (#154: after the meal, a change corrects the record and messages nobody)."""
    from sqlalchemy import select as select_

    from dinnerbell.db.models import ContributionRow
    from tests.support.calls import DONE_AT, MEAL

    event_id = await decided(house)
    await house.drain(RecordingChannel())
    before = len(await rows(house))
    house.clock.now = MEAL + timedelta(minutes=30)

    await deactivate(house, "Lee")

    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED and ctx.head_count == 3
    lee = ctx.response_of(house["Lee"].id)
    assert lee is not None and lee.status is Status.IN
    assert {a.role for a in ctx.assignments if a.member_id == house["Lee"].id} >= {AssignmentRole.PICKUP}
    assert [x.kind for _, x in (await rows(house))[before:]] == []
    house.clock.now = DONE_AT
    await house.tick()
    async with house.sessionmaker() as db:
        kinds = set(
            await db.scalars(
                select_(ContributionRow.kind).where(
                    ContributionRow.event_id == event_id, ContributionRow.member_id == house["Lee"].id
                )
            )
        )
    assert {"picked_up", "participated"} <= kinds


async def test_removing_a_guest_after_the_meal_keeps_what_they_did(house: House) -> None:
    """The same for a guest an admin removes after the meal: they were there and did the pickup."""
    from tests.support.calls import MEAL

    jo = await add_guest(house, "Jo", None)
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    for name in ("Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=jo.id)
    await act(house, guests.answer_for, house["Sam"], event_id, jo.id, role=RoleOffer.PICKUP)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await house.settle_ways(event_id)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED
    assert AssignmentRole.PICKUP in {a.role for a in ctx.assignments if a.member_id == jo.id}
    await house.drain(RecordingChannel())
    before = len(await rows(house))
    house.clock.now = MEAL + timedelta(minutes=30)

    await act(house, guests.remove, house["Alex"], jo.id)

    ctx = await house.context(event_id)
    answer = ctx.response_of(jo.id)
    assert answer is not None and answer.status is Status.IN
    assert AssignmentRole.PICKUP in {a.role for a in ctx.assignments if a.member_id == jo.id}
    assert not [x for _, x in (await rows(house))[before:] if isinstance(x, n.RoleVacated)]


async def test_deactivating_a_member_leaves_offers_alone(house: House) -> None:
    house.clock.now = AFTER_LUNCH
    data = OfferInput(
        slot=Slot.DINNER, day=DAY, mode=Mode.COOK, at=time(18, 0), capacity=4, item_id=house.items["Sam's Tacos"].id
    )
    offer_id = (await act(house, offers.post_offer, house["Sam"], data)).event_id
    await act(house, offers.join, house["Lee"], offer_id)

    await deactivate(house, "Lee")

    lee = (await house.context(offer_id)).response_of(house["Lee"].id)
    assert lee is not None and lee.status is Status.IN


async def test_the_admin_who_deactivates_isnt_asked_to_pick_someone_else(house: House) -> None:
    """With nobody coordinating, each admin is asked, but not the one who deactivated them."""
    from tests.support.calls import nobody_coordinates

    await house.add_member("Max", admin=True)
    event_id = await decided(house)
    await nobody_coordinates(house, event_id)
    before = len(await rows(house))

    await deactivate(house, "Lee")

    vacated = [x for _, x in (await rows(house))[before:] if isinstance(x, n.RoleVacated)]
    assert [x.member.name for x in vacated] == ["Max"]


async def home_start_buttons(house: House) -> set[str]:
    from dinnerbell.channels.slack.views import HomeLinks, home_view
    from dinnerbell.services import event_views

    async with house.sessionmaker() as db:
        today = await event_views.today_view(db, house["Kim"], now=house.clock())
    view = json.dumps(home_view(today, HomeLinks("https://dinnerbell.example")))
    from dinnerbell.channels.slack import ids

    return {action for slot, action in ids.ACTION_FOR_START.items() if f'"{action}"' in view}


@pytest.mark.parametrize(("at", "held"), [(time(18, 0), True), (time(13, 30), False), (time(15, 0), False)])
async def test_app_home_offers_a_dinner_call_unless_a_cooking_offer_stands_in(
    house: House, at: time, held: bool
) -> None:
    """App Home hides **Start a dinner call** only while a cooking offer stands in for it
    (``TodayView.held``): not for one that's over by the call's time, 3:00 PM, or at it."""
    from dinnerbell.channels.slack import ids

    house.clock.now = AFTER_LUNCH
    data = OfferInput(
        slot=Slot.DINNER, day=DAY, mode=Mode.COOK, at=at, capacity=4, item_id=house.items["Sam's Tacos"].id
    )
    await act(house, offers.post_offer, house["Sam"], data)

    shown = ids.ACTION_FOR_START[Slot.DINNER] in await home_start_buttons(house)

    assert shown is not held


async def test_a_cooking_offer_at_the_calls_own_time_doesnt_hold_it(house: House) -> None:
    """An offer at 3:00 PM closes as the 3:00 PM call opens: its meal isn't the call's, which
    collects answers until 4:30 PM for a meal after that. The call goes out."""
    house.clock.now = AFTER_LUNCH
    data = OfferInput(
        slot=Slot.DINNER, day=DAY, mode=Mode.COOK, at=time(15, 0), capacity=4, item_id=house.items["Sam's Tacos"].id
    )
    await act(house, offers.post_offer, house["Sam"], data)
    house.clock.now = OPENS
    await house.tick()
    assert (await house.event(await dinner_id(house))).state is EventState.COLLECTING
