# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calls that change under people (issue 331): a deactivated member leaves the open calls they
were in (B-4), a retried role message never follows "you're off it" (B-5), a guest invited
again gets a second invitation (B-6), and a cooking offer that's over before the call opens
doesn't hold the call back (FR-5)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
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


async def test_a_retried_role_message_closes_once_the_role_moved_on(house: House) -> None:
    """Lee's "You're picking up" fails once and waits to be tried again; meanwhile the pickup
    goes to Alex, and Lee hears it's off. The retry must not arrive after that."""
    event_id = await decided(house)
    (made,) = [r.id for r, x in await rows(house) if isinstance(x, n.AssignmentMade) and x.member.name == "Lee"]
    down = RecordingChannel(fail=1_000)
    await house.drain(down)
    house.clock.now = VOTE_BY + timedelta(minutes=1)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Alex"].id)
    channel = RecordingChannel()
    await house.drain(channel)
    house.clock.now = VOTE_BY + timedelta(hours=1)
    await house.drain(channel)

    lees = [s.kind for s in channel.sent if s.recipient == house["Lee"].id]
    assert lees == ["assignment_ended"]  # "You're off it" is their last word, and the only one
    async with house.sessionmaker() as db:
        row = await db.get(OutboxRow, made)
    assert row is not None and row.last_error == n.SUPERSEDED_BY_ROLE_MOVE


async def test_a_role_push_closes_once_the_role_moved_on(house: House) -> None:
    """The push drainer drops a role push by the same rule (``outbox.overtaken_by``)."""
    from dinnerbell.domain.members import NotifyVia
    from dinnerbell.services import web_app
    from tests.test_push import FakeSender, device, drain, queued, switch

    await switch(house, True)
    async with house.sessionmaker() as db:
        house.people["Lee"] = await web_app.set_my_notify_via(db, house["Lee"], NotifyVia.WEB, now=house.clock())
    await device(house, "Lee")
    event_id = await decided(house)
    await act(house, calls.change_assignment, house["Sam"], event_id, AssignmentRole.PICKUP, house["Alex"].id)

    sender = FakeSender()
    for _ in range(5):  # a pass sends a few to each device; the rest wait for the next
        await drain(house, sender)

    lees = [error for whose, _, error, *_ in await queued(house) if whose == "Lee"]
    assert n.SUPERSEDED_BY_ROLE_MOVE in lees


async def test_a_role_message_still_goes_while_the_role_is_theirs(house: House) -> None:
    await decided(house)
    channel = RecordingChannel()
    await house.drain(channel)
    assert [s.recipient for s in channel.sent if s.kind == "assignment_made"].count(house["Lee"].id) == 1


# B-6: a guest invited again gets a second invitation ---------------------------------------------


async def add_guest(house: House, name: str, slack: str) -> Member:
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
