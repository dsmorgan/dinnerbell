# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests (#38) against a migrated database: the permission matrix by call state, invitations,
answering for a guest without Slack, guests' dietary needs as hard constraints, a guest
coordinating and ties, every action a guest is refused, joining from the channel, promotion and
removal, sign-in by Slack user, and scrubbing guests on a test instance."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.models import MemberRow, PreferenceRow, SessionRow
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import (
    Details,
    Event,
    EventKind,
    EventRuleViolation,
    EventState,
    RoleOffer,
    Status,
)
from dinnerbell.domain.feedback import FeedbackInput, Rating
from dinnerbell.domain.guests import (
    GUEST_RULES,
    NOT_INVITED,
    GuestAction,
    GuestInput,
    check_guest,
    tie_breaker,
)
from dinnerbell.domain.identity import Allow, IdentityClaims, Refusal, Refuse, decide_sign_in
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.members import Capabilities, InvalidMemberInput, Member, Role
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.domain.schedule import DEFAULT_SLOTS, slot_fingerprint
from dinnerbell.services import call_admin, calls, event_views, feedback, guests, meals, offers, scrub
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.offers import OfferInput
from dinnerbell.services.preferences import set_my_preference
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio
DAY = date(2026, 10, 22)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    await house.tag("Vegetarian", TagKind.DIETARY)
    await house.add_item("Green Leaf", Mode.PICKUP, cuisine="Indian", tags=("Vegetarian",))
    await add_guest(house, "Pat", slack="U07PAT")  # registered: a Slack account
    await add_guest(house, "Jo")  # unregistered: no Slack
    return house


async def add_guest(house: House, name: str, *, slack: str | None = None, needs: tuple[str, ...] = ()) -> Member:
    async with house.sessionmaker() as db:
        guest = await MemberRepository(db).add_guest(
            household_id=house.household.id,
            display_name=name,
            dietary_needs=frozenset(house.tags[t].id for t in needs),
            slack_user_id=slack,
            now=house.clock(),
        )
        await db.commit()
    house.people[name] = guest
    return guest


async def act(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    async with house.sessionmaker() as db:
        return await use_case(db, *args, now=house.clock(), **kwargs)


async def open_dinner(house: House) -> int:
    house.clock.now = OPENS
    await house.tick()
    return await dinner_id(house)


async def invite(house: House, who: str, guest: str, event_id: int) -> guests.Invited:
    invited: guests.Invited = await act(house, guests.invite, house[who], event_id, guest_id=house[guest].id)
    return invited


def call(state: EventState, kind: EventKind = EventKind.CALL) -> Event:
    return Event(1, 1, kind, Slot.DINNER, DAY, state, 1, OPENS, RESPOND_BY, 15, 45)


GUEST = Member(9, 1, "Pat", None, Role.GUEST, Capabilities(), True, slack_user_id="U07PAT")
MEMBER = Member(2, 1, "Sam", "sam@example.com", Role.MEMBER, Capabilities(), True)


# The matrix --------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", list(GuestAction))
@pytest.mark.parametrize("state", list(EventState))
def test_the_guest_matrix_by_call_state(action: GuestAction, state: EventState) -> None:
    """An invited guest may take only the actions ``GUEST_RULES`` lists, in its states. Every other
    action and state is refused; a member is never stopped here."""
    allowed = state in GUEST_RULES.get(action, frozenset())
    if allowed:
        check_guest(call(state), GUEST, action, invited=True)
    else:
        with pytest.raises(EventRuleViolation):
            check_guest(call(state), GUEST, action, invited=True)
    with pytest.raises(EventRuleViolation, match=NOT_INVITED):  # never without an invitation, and says only that
        check_guest(call(state), GUEST, action, invited=False)
    with pytest.raises(EventRuleViolation):  # never on an offer
        check_guest(call(state, EventKind.OFFER), GUEST, action, invited=True)
    check_guest(call(state), MEMBER, action, invited=False)


def test_the_matrix_refuses_influence_everywhere() -> None:
    for action in (GuestAction.VOTE, GuestAction.PROPOSE, GuestAction.LOCK, GuestAction.DETAILS, GuestAction.MAYBE):
        assert action not in GUEST_RULES
    with pytest.raises(EventRuleViolation, match=NOT_INVITED):
        check_guest(None, GUEST, GuestAction.ANSWER, invited=False)


def test_a_guest_coordinator_never_breaks_a_tie() -> None:
    assert tie_breaker(GUEST, 2) == 2  # whoever invited them
    assert tie_breaker(GUEST, None) is None  # then the admins
    assert tie_breaker(MEMBER, None) == MEMBER.id
    assert tie_breaker(None, 2) is None
    from dinnerbell.domain.events import ProposalTally, choose_winner

    tied = [ProposalTally(1, 1, True, frozenset({5})), ProposalTally(2, 2, True, frozenset({1}))]
    admins = frozenset({1})
    # The inviter (7) didn't vote, so an admin's vote breaks the tie, not ranking order.
    assert choose_winner(tied, coordinator_id=7, admin_ids=admins, admins_next=True).id == 2  # type: ignore[union-attr]
    assert choose_winner(tied, coordinator_id=7, admin_ids=admins).id == 1  # type: ignore[union-attr]


# Inviting and answering --------------------------------------------------------------------------


async def test_inviting_a_guest_without_slack_counts_them_in(house: House) -> None:
    event_id = await open_dinner(house)
    invited = await invite(house, "Kim", "Jo", event_id)
    assert not invited.new
    ctx = await house.context(event_id)
    jo = ctx.response_of(house["Jo"].id)
    assert jo is not None and jo.status is Status.IN and ctx.invitations == {house["Jo"].id: house["Kim"].id}
    assert ctx.head_count == 1
    assert "guest_invited" not in await house.kinds()  # nobody to message
    with pytest.raises(EventRuleViolation, match="invited already"):
        await invite(house, "Sam", "Jo", event_id)
    (row,) = await house.audits("event.guest_invited")
    assert row.payload == {"guest_id": house["Jo"].id, "registered": False, "new": False}


async def test_inviting_a_guest_with_slack_messages_them(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    ctx = await house.context(event_id)
    assert ctx.response_of(house["Pat"].id) is None  # they answer themselves
    (dm,) = [e for e in await house.outbox() if e.kind == "guest_invited"]
    assert dm.payload["member"]["id"] == house["Pat"].id and dm.payload["invited_by"]["id"] == house["Kim"].id


async def test_inviting_someone_new_makes_a_guest_record(house: House) -> None:
    event_id = await open_dinner(house)
    veg = house.tags["Vegetarian"].id
    invited = await act(house, guests.invite, house["Kim"], event_id, new_guest=GuestInput(" Riley ", frozenset({veg})))
    assert invited.new and invited.guest.display_name == "Riley" and invited.guest.dietary_needs == {veg}
    assert invited.guest.is_guest and invited.guest.email is None
    with pytest.raises(InvalidMemberInput, match="dietary"):
        await act(
            house,
            guests.invite,
            house["Kim"],
            event_id,
            new_guest=GuestInput("Ash", frozenset({house.tags["Thai"].id})),
        )
    with pytest.raises(InvalidMemberInput, match="not both"):
        await act(house, guests.invite, house["Kim"], event_id, guest_id=house["Jo"].id, new_guest=GuestInput("Ash"))


async def test_who_may_invite_and_when(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    with pytest.raises(EventRuleViolation, match="household member can invite"):
        await act(house, guests.invite, house["Pat"], event_id, guest_id=house["Jo"].id)
    with pytest.raises(NotFound):  # a member isn't a guest
        await act(house, guests.invite, house["Kim"], event_id, guest_id=house["Sam"].id)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    with pytest.raises(EventRuleViolation, match="while answers are open"):
        await invite(house, "Kim", "Jo", event_id)


async def test_the_inviter_answers_for_a_guest_without_slack(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Jo", event_id)
    await act(house, guests.answer_for, house["Kim"], event_id, house["Jo"].id, role=RoleOffer.PICKUP)
    jo = (await house.context(event_id)).response_of(house["Jo"].id)
    assert jo is not None and RoleOffer.PICKUP in jo.offers
    with pytest.raises(Forbidden):  # not who invited them
        await act(house, guests.answer_for, house["Sam"], event_id, house["Jo"].id, status=Status.OUT)
    await act(house, guests.answer_for, house["Alex"], event_id, house["Jo"].id, status=Status.OUT)  # an admin may
    with pytest.raises(EventRuleViolation, match="in or out"):
        await act(house, guests.answer_for, house["Kim"], event_id, house["Jo"].id, status=Status.MAYBE)
    await invite(house, "Kim", "Pat", event_id)
    with pytest.raises(Forbidden):  # a guest with Slack answers themselves
        await act(house, guests.answer_for, house["Kim"], event_id, house["Pat"].id, status=Status.IN)


async def test_taking_a_guest_off_the_call(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Jo", event_id)
    with pytest.raises(Forbidden):
        await act(house, guests.uninvite, house["Sam"], event_id, house["Jo"].id)
    await act(house, guests.uninvite, house["Kim"], event_id, house["Jo"].id)
    ctx = await house.context(event_id)
    jo = ctx.response_of(house["Jo"].id)
    assert ctx.invitations == {} and jo is not None and jo.status is Status.OUT
    with pytest.raises(NotFound):
        await act(house, guests.uninvite, house["Kim"], event_id, house["Jo"].id)


# What an invited guest may and may not do ----------------------------------------------------------


async def test_an_invited_guest_answers_in_or_out_and_pitches_in(house: House) -> None:
    event_id = await open_dinner(house)
    pat = house["Pat"]
    with pytest.raises(EventRuleViolation, match="haven't been invited"):
        await act(house, calls.respond, pat, event_id, Status.IN)
    await invite(house, "Kim", "Pat", event_id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.offer_role, pat, event_id, RoleOffer.PICKUP, True)
    assert await act(house, calls.toggle_role, pat, event_id, RoleOffer.COOK) is True
    with pytest.raises(EventRuleViolation, match="in or out"):
        await act(house, calls.respond, pat, event_id, Status.MAYBE)
    with pytest.raises(EventRuleViolation, match="details"):
        await act(house, calls.save_details, pat, event_id, Details(guests=2, suggestion_texts=("Pizza",)))
    await act(house, calls.claim_coordinator, pat, event_id)
    view = await act(house, event_views.event_view, pat, event_id)
    assert (view.can.respond, view.can.vote, view.can.propose, view.can.cancel, view.can.invite) == (
        True,
        False,
        False,
        False,
        False,
    )
    assert view.invitable == () and view.can.close_window is not None  # a coordinating guest closes it


async def test_every_influence_and_member_action_is_refused_to_a_guest(house: House) -> None:
    """#38: a guest never reaches a vote, a proposal, a suggestion, a veto, the catalog,
    history, the schedule, offers, or feedback, and can't start, post, or call off a call, even
    while coordinating."""
    event_id = await open_dinner(house)
    pat = house["Pat"]
    await invite(house, "Kim", "Pat", event_id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.VOTING
    proposal = ctx.proposals[0].id
    thai = house.tags["Thai"].id
    refused: list[tuple[Callable[..., Awaitable[Any]], tuple[Any, ...], dict[str, Any]]] = [
        (calls.vote, (pat, event_id, proposal), {}),
        (calls.unvote, (pat, event_id, proposal), {}),
        (calls.toggle_vote, (pat, event_id, proposal), {}),
        (calls.add_proposal, (pat, event_id, Mode.PICKUP), {"item_id": house.items["Taqueria"].id}),
        (calls.lock, (pat, event_id, proposal), {}),
        (calls.cancel_call, (pat, event_id), {}),
        (calls.start_call, (pat, Slot.LUNCH, DAY), {}),
        (calls.restart_call, (pat, event_id), {}),
        (calls.post_now, (pat, event_id), {}),
        (calls.skip_day, (pat, Slot.LUNCH, DAY + timedelta(days=1)), {}),
        (call_admin.rerun, (pat, event_id), {}),
        (call_admin.assign_coordinator, (pat, event_id, None), {}),
        (calls.respond, (pat, event_id, Status.OUT), {}),  # answers are closed now
        (set_my_preference, (pat, TargetType.TAG, thai, Stance(veto=True)), {}),
        (catalog_service.create_tag, (pat, TagKind.CUISINE, "Greek"), {}),
        (
            schedule_service.update_slot,
            (pat, Slot.DINNER, DEFAULT_SLOTS[Slot.DINNER]),
            {"seen": slot_fingerprint(DEFAULT_SLOTS[Slot.DINNER])},
        ),
        (
            offers.post_offer,
            (pat, OfferInput(Slot.DINNER, DAY, Mode.PICKUP, time(19, 0), 2, item_id=house.items["Taqueria"].id)),
            {},
        ),
        (guests.invite, (pat, event_id), {"guest_id": house["Jo"].id}),
        (guests.add_guest, (pat, GuestInput("Ash")), {}),
        (guests.promote, (pat, house["Jo"].id, Role.MEMBER), {}),
        (guests.remove, (pat, house["Jo"].id), {}),
        (guests.set_joining, (pat, False), {}),
    ]
    for use_case, args, kwargs in refused:
        with pytest.raises((EventRuleViolation, Forbidden, NotFound)):
            await act(house, use_case, *args, **kwargs)
    readers: list[tuple[Callable[..., Awaitable[Any]], dict[str, Any]]] = [
        (catalog_service.catalog, {}),
        (guests.saved_guests, {}),
        (meals.history, {"today": DAY}),
        (offers.offer_form, {"now": house.clock()}),
        (
            meals.log_meal,
            {
                "data": MealInput(DAY, Slot.LUNCH, house.items["Taqueria"].id, frozenset({pat.id})),
                "today": DAY,
                "now": house.clock(),
            },
        ),
    ]
    for reader, kwargs in readers:
        with pytest.raises((Forbidden, EventRuleViolation)):
            await read(house, reader, pat, **kwargs)
    ctx = await house.context(event_id)
    assert all(pat.id not in voters for voters in ctx.votes.values())
    assert all(p.proposed_by != pat.id for p in ctx.proposals)
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_a_guest_sees_only_the_calls_they_are_invited_to(house: House) -> None:
    event_id = await open_dinner(house)
    pat = house["Pat"]
    with pytest.raises(NotFound):
        await act(house, event_views.event_view, pat, event_id)
    assert (await act(house, event_views.today_view, pat)).events == []
    await invite(house, "Kim", "Pat", event_id)
    today = await act(house, event_views.today_view, pat)
    assert [v.id for v in today.events] == [event_id]
    started = await act(house, calls.start_call, house["Sam"], Slot.LUNCH, DAY + timedelta(days=1))
    with pytest.raises(NotFound):
        await act(house, event_views.event_view, pat, started.id)
    assert [v.id for v in (await act(house, event_views.today_view, pat)).events] == [event_id]


# Dietary needs, preferences, ties, and messages ----------------------------------------------------


async def test_a_guests_dietary_needs_are_hard_constraints_and_their_stances_never_count(house: House) -> None:
    event_id = await open_dinner(house)
    pat = await add_guest(house, "Mo", slack="U08MO", needs=("Vegetarian",))
    async with house.sessionmaker() as db:  # a stance a guest can't set, planted directly
        green = house.items["Green Leaf"].id
        db.add(
            PreferenceRow(
                household_id=house.household.id,
                member_id=pat.id,
                item_id=green,
                veto=True,
                score=0,
                updated_at=house.clock(),
            )
        )
        await db.commit()
    await invite(house, "Kim", "Mo", event_id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert {p.item_id for p in ctx.proposals} == {house.items["Green Leaf"].id}  # vegetarian, and Mo's veto ignored
    assert ctx.head_count == 3
    house.clock.now = VOTE_BY
    await house.tick()
    decided = next(e for e in await house.outbox() if e.kind == "decision_made")
    assert decided.payload["decision"]["dietary_notes"] == ["Vegetarian"]


async def test_a_guest_coordinating_leaves_the_tie_to_whoever_invited_them(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    pat = house["Pat"]
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    for name in ("Alex", "Kim"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.ORDER, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    picks = (await house.context(event_id)).proposals
    assert len(picks) >= 2
    first, second = picks[0], picks[1]
    await act(house, calls.vote, house["Alex"], event_id, first.id)  # an admin's vote
    await act(house, calls.vote, house["Kim"], event_id, second.id)  # the inviter's vote
    house.clock.now = VOTE_BY
    await house.tick()
    event = await house.event(event_id)
    assert (event.decided_item_id, event.decided_mode) == (second.item_id, second.mode)


async def test_assignment_messages_for_a_guest_without_slack_go_to_the_inviter(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Jo", event_id)
    await act(house, guests.answer_for, house["Kim"], event_id, house["Jo"].id, role=RoleOffer.PICKUP)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    pick = next(p for p in ctx.proposals if p.mode is Mode.PICKUP)
    await act(house, calls.lock, house["Alex"], event_id, pick.id)
    dm = next(e for e in await house.outbox() if e.kind == "assignment_made")
    assert dm.payload["member"]["id"] == house["Jo"].id and dm.payload["via"]["id"] == house["Kim"].id
    from dinnerbell.domain.notifications import from_payload

    assert from_payload("assignment_made", dm.payload).recipient_id == house["Kim"].id
    await act(house, calls.cancel_call, house["Alex"], event_id)
    cancelled = next(e for e in await house.outbox() if e.kind == "assignment_cancelled")
    assert from_payload("assignment_cancelled", cancelled.payload).recipient_id == house["Kim"].id


async def test_a_guest_is_neither_waited_for_nor_reminded(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    ctx = await house.context(event_id)
    from dinnerbell.services import call_context as cc

    assert house["Pat"].id not in {p.id for p in cc.waiting_on(ctx)}
    assert house["Pat"].id not in {p.id for p in cc.call_opened(ctx).mentions}


async def test_guests_give_no_feedback(house: House) -> None:
    pat = house["Pat"]
    assert await act(house, feedback.awaiting, pat) == []
    with pytest.raises(Forbidden):
        await act(house, feedback.give, pat, 1, FeedbackInput(food=Rating.LOVED))


# Guest records -----------------------------------------------------------------------------------


async def test_only_an_admin_promotes_or_removes_a_guest(house: House) -> None:
    jo = house["Jo"]
    with pytest.raises(Forbidden):
        await act(house, guests.promote, house["Sam"], jo.id, Role.MEMBER)
    promoted = await act(house, guests.promote, house["Alex"], jo.id, Role.MEMBER, email="jo@example.com")
    assert (promoted.role, promoted.email, promoted.is_member) == (Role.MEMBER, "jo@example.com", True)
    (row,) = await house.audits("member.promoted")
    assert row.payload == {"to": "member"}
    with pytest.raises(InvalidMemberInput, match="email"):
        await act(house, guests.promote, house["Alex"], house["Pat"].id, Role.MEMBER)
    with pytest.raises(NotFound):  # not a guest any more
        await act(house, guests.remove, house["Alex"], jo.id)

    pat = house["Pat"]
    async with house.sessionmaker() as db:
        now = house.clock()
        db.add(
            SessionRow(
                id="b" * 64,
                household_id=house.household.id,
                member_id=pat.id,
                created_at=now,
                expires_at=now + timedelta(days=1),
                last_seen=now,
            )
        )
        await db.commit()
    removed = await act(house, guests.remove, house["Alex"], pat.id)
    assert (removed.active, removed.join_refused) == (False, True)
    async with house.sessionmaker() as db:
        assert list(await db.scalars(select(SessionRow).where(SessionRow.member_id == pat.id))) == []
    from dinnerbell.services import members as member_service

    back = await act(house, member_service.set_member_active, house["Alex"], pat.id, True)
    assert (back.active, back.join_refused) == (True, False)


async def test_people_set_their_own_dietary_needs(house: House) -> None:
    veg = house.tags["Vegetarian"].id
    updated = await act(house, guests.set_my_needs, house["Pat"], frozenset({veg}))
    assert updated.dietary_needs == {veg}
    sam = await act(house, guests.set_my_needs, house["Sam"], frozenset({veg}))  # a member's own, on Preferences
    assert sam.dietary_needs == {veg}
    with pytest.raises(InvalidMemberInput):
        await act(house, guests.set_my_needs, house["Pat"], frozenset({house.tags["Thai"].id}))


# Joining from the channel ------------------------------------------------------------------------


def person(slack_id: str = "U09NEW", name: str = "Nova", **kwargs: Any) -> guests.SlackPerson:
    return guests.SlackPerson(slack_id, name, **kwargs)


async def test_joining_makes_a_guest_and_tells_the_admins(house: House) -> None:
    joined = await act(house, guests.join, person(), via="slack")
    assert joined.joined_as is guests.JoinedAs.GUEST
    assert (joined.member.role, joined.member.slack_user_id, joined.member.email) == (Role.GUEST, "U09NEW", None)
    (dm,) = [e for e in await house.outbox() if e.kind == "guest_joined"]
    assert dm.payload["admin"]["id"] == house["Alex"].id and dm.event_id is None
    (row,) = await house.audits("member.joined")
    assert row.payload == {"slack_user_id": "U09NEW", "via": "slack", "slack_guest": False}
    again = await act(house, guests.join, person(), via="web")
    assert again.joined_as is guests.JoinedAs.ALREADY


async def test_joining_is_refused_while_off_or_after_a_removal(house: House) -> None:
    await act(house, guests.set_joining, house["Alex"], False)
    with pytest.raises(guests.JoinRefused) as refused:
        await act(house, guests.join, person(), via="slack")
    assert refused.value.reason is guests.NotJoinable.OFF
    await act(house, guests.set_joining, house["Alex"], True)
    await act(house, guests.remove, house["Alex"], house["Pat"].id)
    with pytest.raises(guests.JoinRefused) as refused:
        await act(house, guests.join, person("U07PAT", "Pat"), via="web")
    assert refused.value.reason is guests.NotJoinable.REMOVED
    reasons = [r.payload["reason"] for r in await house.audits("member.join_refused")]
    assert reasons == ["off", "removed"]
    assert len(await house.audits("household.joining_updated")) == 2


async def test_joining_links_an_invited_member_by_email(house: House) -> None:
    from dinnerbell.services import members as member_service

    invited = await act(
        house,
        member_service.invite_member,
        house["Alex"],
        member_service.MemberInput("Rae", "rae@example.com", Role.MEMBER, Capabilities()),
    )
    joined = await act(house, guests.join, person("U09RAE", "Rae", email="RAE@example.com"), via="slack")
    assert (joined.joined_as, joined.member.id, joined.member.slack_user_id) == (
        guests.JoinedAs.MEMBER,
        invited.id,
        "U09RAE",
    )
    slack_guest = await act(
        house, guests.join, person("U09SG", "Sky", email="sky@example.com", full_member=False), via="slack"
    )
    assert slack_guest.joined_as is guests.JoinedAs.GUEST


def test_the_join_limiter() -> None:
    limiter = guests.JoinLimiter(attempts=2, window=timedelta(minutes=10))
    now = datetime(2026, 10, 22, tzinfo=UTC)
    assert limiter.allow("U1", now) and limiter.allow("U1", now)
    assert not limiter.allow("U1", now + timedelta(minutes=1))
    assert limiter.allow("U2", now)  # per Slack user
    assert limiter.allow("U1", now + timedelta(minutes=11))
    assert set(limiter.seen) == {"U1"}  # forgotten once quiet


async def test_the_admin_imports_people_from_the_channel(house: House) -> None:
    people = [
        person("U09A", "Ann", email="ann@example.com"),
        person("U07PAT", "Pat"),
        person("U09B", "Bo", full_member=False),
    ]
    found = await act_read(house, guests.candidates, house["Alex"], people)
    assert [c.slack_user_id for c in found] == ["U09A", "U09B"]  # Pat is in Dinnerbell already
    with pytest.raises(Forbidden):
        await act_read(house, guests.candidates, house["Sam"], people)
    member = await act(house, guests.import_person, house["Alex"], people[0], Role.MEMBER)
    assert (member.role, member.email, member.slack_user_id) == (Role.MEMBER, "ann@example.com", "U09A")
    from dinnerbell.domain.members import MemberRuleViolation

    with pytest.raises(MemberRuleViolation, match="as a guest"):
        await act(house, guests.import_person, house["Alex"], people[2], Role.MEMBER)
    guest = await act(house, guests.import_person, house["Alex"], people[2], Role.GUEST)
    assert guest.is_guest
    assert len(await house.audits("member.imported")) == 2


async def act_read(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    return await read(house, use_case, *args)


async def read(house: House, use_case: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """``use_case`` in its own session, without the ``now`` that ``act`` adds."""
    async with house.sessionmaker() as db:
        return await use_case(db, *args, **kwargs)


# Sign-in by Slack user ---------------------------------------------------------------------------


def claims(slack_user: str = "U07PAT", **changes: Any) -> IdentityClaims:
    values: dict[str, Any] = {
        "subject": "sub-pat",
        "email": None,
        "email_verified": False,
        "slack_team_id": "T0HOUSE",
        "slack_user_id": slack_user,
    }
    return IdentityClaims(**(values | changes))


def test_a_guest_signs_in_with_their_linked_slack_user() -> None:
    from dataclasses import replace

    slack = {"required_team_id": "T0HOUSE", "slack_sign_in": True}
    decision = decide_sign_in(claims(), by_subject=None, by_email=None, by_slack_user=GUEST, **slack)  # type: ignore[arg-type]
    assert isinstance(decision, Allow) and decision.member is GUEST and decision.link_subject
    # Not without Slack as the issuer, not for the wrong workspace, and never for a member.
    for kwargs in (
        {"required_team_id": "T0HOUSE"},  # a team ID set, but another issuer
        {"required_team_id": None, "slack_sign_in": True},
        {"required_team_id": "T0EVIL", "slack_sign_in": True},
    ):
        refused = decide_sign_in(claims(), by_subject=None, by_email=None, by_slack_user=GUEST, **kwargs)  # type: ignore[arg-type]
        assert isinstance(refused, Refuse), kwargs
    member = decide_sign_in(claims(), by_subject=None, by_email=None, by_slack_user=MEMBER, **slack)  # type: ignore[arg-type]
    assert isinstance(member, Refuse) and member.reason is Refusal.EMAIL_NOT_VERIFIED
    gone = decide_sign_in(claims(), by_subject=None, by_email=None, by_slack_user=replace(GUEST, active=False), **slack)  # type: ignore[arg-type]
    assert isinstance(gone, Refuse) and gone.reason is Refusal.INACTIVE


def test_an_invited_members_email_wins_over_a_guest_holding_their_slack() -> None:
    """Kim joined from the channel before the admin added her: her verified email matches her
    membership, which wins over the guest record that holds her Slack account."""
    kim = Member(5, 1, "Kim", "kim@example.com", Role.MEMBER, Capabilities(), True)
    guest = Member(9, 1, "Kim K", "kim@example.com", Role.GUEST, Capabilities(), True, slack_user_id="U07PAT")
    verified = claims(email="kim@example.com", email_verified=True)
    decision = decide_sign_in(
        verified, by_subject=None, by_email=kim, by_slack_user=guest, required_team_id="T0HOUSE", slack_sign_in=True
    )
    assert isinstance(decision, Allow) and decision.member is kim


# Scrubbing a test instance -----------------------------------------------------------------------


async def test_scrub_makes_guests_anonymous(house: House) -> None:
    veg = house.tags["Vegetarian"].id
    await act(house, guests.set_my_needs, house["Pat"], frozenset({veg}))
    summary = await act(house, scrub.scrub_test, instance_label="TEST", keep=["admin@example.com"])
    assert summary.guests_scrubbed == 2
    async with house.sessionmaker() as db:
        rows = list(await db.scalars(select(MemberRow).where(MemberRow.role == "guest").order_by(MemberRow.id)))
    for row in rows:
        assert (row.display_name, row.slack_user_id, row.dietary_needs, row.active, row.oidc_subject) == (
            f"Guest {row.id}",
            None,
            [],
            False,
            None,
        )
    again = await act(house, scrub.scrub_test, instance_label="TEST", keep=["admin@example.com"])
    assert again.guests_scrubbed == 0


async def test_a_call_off_mentions_guests_who_were_in_and_a_discard_takes_invitations(house: House) -> None:
    from dinnerbell.services import discard

    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    cancelled = next(e for e in await house.outbox() if e.kind == "event_cancelled")
    assert house["Pat"].id in {p["id"] for p in cancelled.payload["audience"]}
    async with house.sessionmaker() as db:
        summary = await discard.discard_call(
            db, house.household.id, event_id, actor=house["Alex"], instance_label="TEST", now=house.clock()
        )
    assert summary.rows["invitation"] == 1


async def test_a_guest_who_leaves_a_call_leaves_no_coordinator_behind(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Pat"], event_id)
    await act(house, guests.uninvite, house["Kim"], event_id, house["Pat"].id)
    assert (await house.event(event_id)).coordinator_id is None
    await invite(house, "Kim", "Pat", event_id)
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    await act(house, calls.claim_coordinator, house["Pat"], event_id)
    await act(house, guests.remove, house["Alex"], house["Pat"].id)
    ctx = await house.context(event_id)
    assert ctx.event.coordinator_id is None and ctx.response_of(house["Pat"].id).status is Status.OUT  # type: ignore[union-attr]
    assert ctx.head_count == 0


async def test_relinking_a_guests_slack_signs_out_the_old_account(house: House) -> None:
    pat = house["Pat"]
    async with house.sessionmaker() as db:
        await MemberRepository(db).update(house.household.id, pat.id, house.clock(), oidc_subject="sub-old")
        now = house.clock()
        db.add(
            SessionRow(
                id="c" * 64,
                household_id=house.household.id,
                member_id=pat.id,
                created_at=now,
                expires_at=now + timedelta(days=1),
                last_seen=now,
            )
        )
        await db.commit()
    updated = await act(house, guests.set_guest_slack_user, house["Alex"], pat.id, "U07OTHER")
    assert (updated.slack_user_id, updated.oidc_subject, updated.slack_link_locked) == ("U07OTHER", None, True)
    async with house.sessionmaker() as db:
        assert list(await db.scalars(select(SessionRow).where(SessionRow.member_id == pat.id))) == []
    from dinnerbell.domain.members import MemberRuleViolation

    with pytest.raises(MemberRuleViolation, match="already linked"):
        await act(house, guests.set_guest_slack_user, house["Alex"], house["Jo"].id, "U07OTHER")


async def test_a_guests_needs_hold_still_while_their_call_votes(house: House) -> None:
    veg = house.tags["Vegetarian"].id
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    await act(house, guests.set_my_needs, house["Pat"], frozenset({veg}))  # fine while answering
    pat = await house.refresh("Pat")
    await act(house, calls.respond, pat, event_id, Status.IN)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    with pytest.raises(EventRuleViolation, match="voting now"):
        await act(house, guests.set_my_needs, pat, frozenset())
    await act(house, guests.set_my_needs, pat, frozenset({veg}))  # the same needs again is no change


async def test_a_deactivated_members_email_cant_come_back_as_a_guest(house: House) -> None:
    from dinnerbell.services import members as member_service

    await act(house, member_service.set_member_active, house["Alex"], house["Kim"].id, False)
    with pytest.raises(guests.JoinRefused) as refused:
        await act(house, guests.join, person("U09KIM", "Kim", email="kim@example.com"), via="slack")
    assert refused.value.reason is guests.NotJoinable.REMOVED


# The #38 "Done when", end to end, and the follow-ups from #68's review ------------------------------


async def test_done_when_end_to_end(house: House) -> None:
    """#38: a member invites a registered and an unregistered guest; the registered guest taps In
    and offers to pick up, and is refused a vote; the member answers for the other; the decision
    honors both guests' needs, and the assignment message reaches the right person."""
    await house.tag("Nut-free", TagKind.DIETARY)
    await house.add_item("Nutty Thai", Mode.PICKUP, cuisine="Thai", tags=("Vegetarian",))
    await house.add_item("Safe Bowl", Mode.PICKUP, cuisine="Thai", tags=("Vegetarian", "Nut-free"))
    rae = await add_guest(house, "Rae", slack="U09RAE", needs=("Nut-free",))
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Rae", event_id)
    veg = frozenset({house.tags["Vegetarian"].id})
    uma = (await act(house, guests.invite, house["Kim"], event_id, new_guest=GuestInput("Uma", veg))).guest
    await act(house, calls.respond, rae, event_id, Status.IN)
    await act(house, calls.offer_role, rae, event_id, RoleOffer.PICKUP, True)
    await act(house, guests.answer_for, house["Kim"], event_id, uma.id, status=Status.IN)
    await act(house, calls.respond, house["Kim"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.VOTING
    assert {ctx.items[p.item_id].name for p in ctx.proposals if p.item_id} == {"Safe Bowl"}
    with pytest.raises(EventRuleViolation):
        await act(house, calls.vote, rae, event_id, ctx.proposals[0].id)
    await act(house, calls.vote, house["Kim"], event_id, ctx.proposals[0].id)
    house.clock.now = VOTE_BY
    await house.tick()
    from dinnerbell.domain.notifications import from_payload

    dms = [from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == "assignment_made"]
    assert {(d.member.id, d.recipient_id) for d in dms} == {(rae.id, rae.id)}  # type: ignore[union-attr]


async def test_a_vote_decided_assignment_for_a_guest_without_slack_reaches_the_inviter(house: House) -> None:
    from dinnerbell.domain.notifications import from_payload

    event_id = await open_dinner(house)
    await invite(house, "Kim", "Jo", event_id)
    await act(house, guests.answer_for, house["Kim"], event_id, house["Jo"].id, role=RoleOffer.PICKUP)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    pick = next(p for p in (await house.context(event_id)).proposals if p.mode is Mode.PICKUP)
    await act(house, calls.vote, house["Sam"], event_id, pick.id)
    house.clock.now = VOTE_BY
    await house.tick()
    dms = [from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == "assignment_made"]
    assert {(d.member.id, d.recipient_id) for d in dms} == {(house["Jo"].id, house["Kim"].id)}  # type: ignore[union-attr]


async def test_everyone_out_closes_early_with_saved_guests_in_the_household(house: House) -> None:
    """Nobody waits for a guest: Pat and Jo are saved guests, not invited, and the call still
    closes as soon as every member is out."""
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=1)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.ABANDONED


async def test_an_invited_guest_who_is_in_counts_toward_the_early_close(house: House) -> None:
    """Every member is out, but Jo, invited, is in and offering to cook: the call isn't ended at
    once as if nobody were eating; it waits out the quiet spell, then closes on the head count."""
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Jo", event_id)
    await act(house, guests.answer_for, house["Kim"], event_id, house["Jo"].id, role=RoleOffer.COOK)
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.OUT)
    house.clock.now = OPENS + timedelta(minutes=5)
    await house.tick()
    assert (await house.event(event_id)).state is EventState.COLLECTING
    house.clock.now = OPENS + timedelta(minutes=11)
    await house.tick()
    ended = await house.event(event_id)
    assert ended.state is EventState.ABANDONED and ended.end_reason.value == "too_few"  # type: ignore[union-attr]


async def test_a_promoted_guest_keeps_their_dietary_needs_as_a_hard_constraint(house: House) -> None:
    nut = await house.tag("Nut-free", TagKind.DIETARY)
    await house.add_item("Safe Bowl", Mode.PICKUP, cuisine="Thai", tags=("Nut-free",))
    jo = await add_guest(house, "Mo", needs=("Nut-free",))
    promoted = await act(house, guests.promote, house["Alex"], jo.id, Role.MEMBER, email="mo@example.com")
    assert promoted.dietary_needs == {nut.id}  # a need has no exact veto to become, so it stays a need
    event_id = await open_dinner(house)
    for who in (promoted, house["Alex"]):
        await act(house, calls.respond, who, event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert {ctx.items[p.item_id].name for p in ctx.proposals if p.item_id} == {"Safe Bowl"}


async def test_the_admin_links_a_joined_guest_to_their_membership(house: House) -> None:
    """Kim was invited by email, but her Slack email differs, so she joined as a guest. The
    admin's message names the members with no Slack yet, and the admin links her."""
    joined = await act(house, guests.join, person("U09KIM", "Kim K"), via="slack")
    dm = next(e for e in await house.outbox() if e.kind == "guest_joined")
    assert {p["name"] for p in dm.payload["unlinked"]} == {"Alex", "Kim", "Lee", "Sam"}
    event_id = await open_dinner(house)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=joined.member.id)
    with pytest.raises(Forbidden):
        await act(house, guests.link_to_member, house["Sam"], joined.member.id, house["Kim"].id)
    linked = await act(house, guests.link_to_member, house["Alex"], joined.member.id, house["Kim"].id)
    assert (linked.id, linked.slack_user_id, linked.slack_link_locked) == (house["Kim"].id, "U09KIM", True)
    async with house.sessionmaker() as db:
        retired = await MemberRepository(db).get(house.household.id, joined.member.id)
    assert retired is not None and (retired.active, retired.slack_user_id) == (False, None)
    again = await act(house, guests.join, person("U09KIM", "Kim K"), via="slack")
    assert again.joined_as is guests.JoinedAs.ALREADY and again.member.id == house["Kim"].id
    from dinnerbell.domain.members import MemberRuleViolation

    with pytest.raises(MemberRuleViolation, match="no Slack"):
        await act(house, guests.link_to_member, house["Alex"], house["Jo"].id, house["Lee"].id)
    with pytest.raises(MemberRuleViolation, match="linked to a Slack account"):
        await act(house, guests.link_to_member, house["Alex"], house["Pat"].id, house["Kim"].id)


async def test_member_pickers_leave_out_saved_guests(house: House) -> None:
    from dinnerbell.domain.catalog import ItemInput, ItemKind

    people = await read(house, meals.participant_choices, house["Kim"])
    assert {p.name for p in people} == {"Alex", "Kim", "Lee", "Sam"}
    data = await read(house, catalog_service.catalog, house["Kim"])
    assert all(not m.is_guest for m in data.members)
    with pytest.raises(Exception, match="cook"):
        await act(
            house,
            catalog_service.create_item,
            house["Kim"],
            ItemInput(
                kind=ItemKind.HOME,
                name="Jo's Soup",
                modes=frozenset({Mode.COOK}),
                tag_ids=frozenset({house.tags["Thai"].id}),
                cook_ids=frozenset({house["Jo"].id}),
            ),
        )


async def test_a_new_run_invites_registered_guests_again(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    await act(house, calls.start_call, house["Sam"], Slot.DINNER, DAY)
    keys = [e.idempotency_key for e in await house.outbox() if e.kind == "guest_invited"]
    assert keys == [
        f"event:{event_id}:run1:guest_invited:member{house['Pat'].id}",
        f"event:{event_id}:run2:guest_invited:member{house['Pat'].id}",
    ]


async def test_invited_registered_guests_get_the_reminder(house: House) -> None:
    from dinnerbell.domain.notifications import from_payload
    from tests.support.calls import REMIND

    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    await invite(house, "Kim", "Jo", event_id)  # no Slack: no reminder
    house.clock.now = REMIND
    await house.tick()
    reminders = [from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == "response_reminder"]
    pat = [r for r in reminders if r.recipient_id == house["Pat"].id]
    assert len(pat) == 1 and pat[0].guest  # type: ignore[union-attr]
    assert house["Jo"].id not in {r.recipient_id for r in reminders}


async def test_removing_a_guest_from_a_decided_call_clears_their_role(house: House) -> None:
    from dinnerbell.domain.events import AssignmentRole

    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    pat = house["Pat"]
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.offer_role, pat, event_id, RoleOffer.PICKUP, True)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    house.clock.now = RESPOND_BY
    await house.tick()
    pick = next(p for p in (await house.context(event_id)).proposals if p.mode is Mode.PICKUP)
    await act(house, calls.lock, house["Alex"], event_id, pick.id)
    assert any(
        a.member_id == pat.id and a.role is AssignmentRole.PICKUP for a in (await house.context(event_id)).assignments
    )
    await act(house, guests.remove, house["Alex"], pat.id)
    ctx = await house.context(event_id)
    assert all(a.member_id != pat.id for a in ctx.assignments)
    (vacated,) = [e for e in await house.outbox() if e.kind == "role_vacated"]
    assert vacated.payload["member"]["id"] == house["Alex"].id and vacated.payload["roles"] == ["pickup"]


@pytest.mark.parametrize("use", ["offer_role", "toggle_role", "claim_coordinator", "post_offer", "join_offer"])
async def test_an_uninvited_guest_is_refused_each_action(house: House, use: str) -> None:
    """Each guard on its own line: these fail if the line that refuses an uninvited guest goes."""
    from dinnerbell.domain.guests import REFUSED

    event_id = await open_dinner(house)
    pat = house["Pat"]
    lunch = OfferInput(
        Slot.LUNCH, DAY + timedelta(days=1), Mode.PICKUP, time(12, 30), 3, item_id=house.items["Taqueria"].id
    )
    cases: dict[str, tuple[Callable[..., Awaitable[Any]], tuple[Any, ...], str]] = {
        "offer_role": (calls.offer_role, (pat, event_id, RoleOffer.COOK, True), NOT_INVITED),
        "toggle_role": (calls.toggle_role, (pat, event_id, RoleOffer.PICKUP), NOT_INVITED),
        "claim_coordinator": (calls.claim_coordinator, (pat, event_id), NOT_INVITED),
        "post_offer": (offers.post_offer, (pat, lunch), REFUSED[GuestAction.OFFER]),
    }
    if use == "join_offer":
        offer = await act(house, offers.post_offer, house["Sam"], lunch)
        cases["join_offer"] = (offers.join, (pat, offer.event_id), REFUSED[GuestAction.JOIN_OFFER])
    use_case, args, message = cases[use]
    before = await house.kinds()
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, use_case, *args)
    assert str(refused.value) == message
    assert await house.kinds() == before


def test_the_domain_refuses_an_uninvited_guest_on_its_own() -> None:
    """A second layer under ``guests.gate``: the rules themselves need the invitation."""
    from dinnerbell.domain.events import check_can_respond, check_claim_coordinator
    from dinnerbell.domain.offers import check_can_join

    collecting = call(EventState.COLLECTING)
    for check in (check_can_respond, check_claim_coordinator):
        with pytest.raises(EventRuleViolation, match=NOT_INVITED):
            check(collecting, GUEST)
        check(collecting, GUEST, invited=True)
        check(collecting, MEMBER)
    offer = Event(2, 1, EventKind.OFFER, Slot.LUNCH, DAY, EventState.COLLECTING, 1, OPENS, RESPOND_BY, 0, 0, capacity=3)
    with pytest.raises(EventRuleViolation, match="bring you along"):
        check_can_join(offer, GUEST)
    check_can_join(offer, MEMBER)


# The #68 security review ---------------------------------------------------------------------------


async def test_a_member_never_lands_on_a_guest_record(house: House) -> None:
    """Nina joins from the channel before the admin adds her. Adding her by email offers to
    promote her guest record instead, and promoting it makes her a member with her Slack."""
    from dinnerbell.services import members as member_service

    joined = await act(house, guests.join, person("U09NINA", "Nina", email="Nina@Example.com"), via="slack")
    assert joined.joined_as is guests.JoinedAs.GUEST and joined.member.email == "nina@example.com"
    data = member_service.MemberInput("Nina", "nina@example.com", Role.MEMBER, Capabilities())
    with pytest.raises(member_service.GuestHoldsEmail) as conflict:
        await act(house, member_service.invite_member, house["Alex"], data)
    assert conflict.value.guest.id == joined.member.id
    promoted = await act(house, guests.promote, house["Alex"], joined.member.id, Role.MEMBER, email="nina@example.com")
    assert (promoted.role, promoted.slack_user_id, promoted.email) == (Role.MEMBER, "U09NINA", "nina@example.com")


async def test_a_join_whose_email_belongs_to_a_member_asks_the_admin(house: House) -> None:
    from dinnerbell.services import members as member_service

    await act(house, member_service.set_slack_user, house["Alex"], house["Kim"].id, "U03KIM")  # Kim is linked
    with pytest.raises(guests.JoinRefused) as refused:  # a second Slack account with Kim's email
        await act(house, guests.join, person("U09KIM2", "Kim", email="kim@example.com"), via="slack")
    assert refused.value.reason is guests.NotJoinable.ASK_ADMIN
    with pytest.raises(guests.JoinRefused):  # a Slack guest account with Lee's email
        await act(house, guests.join, person("U09LEE", "Lee", email="lee@example.com", full_member=False), via="slack")
    notices = [e for e in await house.outbox() if e.kind == "link_needed"]
    assert {(n.payload["member"]["name"], n.payload["reason"]) for n in notices} == {("Kim", "join"), ("Lee", "join")}
    async with house.sessionmaker() as db:
        assert await MemberRepository(db).by_slack_user_id(house.household.id, "U09KIM2") is None


async def test_the_hourly_match_tells_the_admins_about_a_guest_holding_a_members_slack(house: House) -> None:
    from dinnerbell.services import slack_members

    async with house.sessionmaker() as db:
        linked = await slack_members.link_slack_users(
            db, house.household.id, {house["Kim"].id: "U07PAT"}, now=house.clock()
        )
    assert linked == []
    (notice,) = [e for e in await house.outbox() if e.kind == "link_needed"]
    assert (notice.payload["member"]["id"], notice.payload["slack_user_id"], notice.payload["reason"]) == (
        house["Kim"].id,
        "U07PAT",
        "hourly",
    )


async def test_the_member_routes_hand_guests_to_the_guest_paths(house: House) -> None:
    from dinnerbell.domain.members import MemberRuleViolation
    from dinnerbell.services import members as member_service

    with pytest.raises(MemberRuleViolation, match="Guests section"):
        await act(house, member_service.set_slack_user, house["Alex"], house["Pat"].id, "U07NEW")
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    await act(house, calls.respond, house["Pat"], event_id, Status.IN)
    deactivated = await act(house, member_service.set_member_active, house["Alex"], house["Pat"].id, False)
    assert (deactivated.active, deactivated.join_refused) == (False, True)  # as Remove does
    assert (await house.context(event_id)).response_of(house["Pat"].id).status is Status.OUT  # type: ignore[union-attr]


async def test_an_inactive_guests_needs_dont_count(house: House) -> None:
    from sqlalchemy import update

    event_id = await open_dinner(house)
    mo = await add_guest(house, "Mo", slack="U08MO", needs=("Vegetarian",))
    await invite(house, "Kim", "Mo", event_id)
    await act(house, calls.respond, mo, event_id, Status.IN)
    assert (await house.context(event_id)).guest_needs == {house.tags["Vegetarian"].id}
    async with house.sessionmaker() as db:
        await db.execute(update(MemberRow).where(MemberRow.id == mo.id).values(active=False))
        await db.commit()
    assert (await house.context(event_id)).guest_needs == frozenset()


async def test_scrub_redacts_audit_rows_about_guests(house: House) -> None:
    from dinnerbell.db.models import AuditRow

    await act(house, guests.join, person("U09NEW", "Nova", email="nova@example.com"), via="slack")
    await act(house, guests.add_guest, house["Alex"], GuestInput("Ash", frozenset({house.tags["Vegetarian"].id})))
    async with house.sessionmaker() as db:
        await guests.record_refusal(db, "U09BAD", guests.NotJoinable.NOT_IN_CHANNEL, via="slack", now=house.clock())
    summary = await act(house, scrub.scrub_test, instance_label="TEST", keep=["admin@example.com"])
    assert summary.audit_rows_redacted == 3
    async with house.sessionmaker() as db:
        rows = list(
            await db.scalars(
                select(AuditRow).where(AuditRow.action.in_(["member.joined", "guest.added", "member.join_refused"]))
            )
        )
    assert rows and all(r.payload == {"redacted": "scrub-test"} for r in rows)
    assert all("U09" not in (r.target or "") for r in rows)
    async with house.sessionmaker() as db:
        emails = list(await db.scalars(select(MemberRow.email).where(MemberRow.role == "guest")))
    assert emails and all(e is None for e in emails)


async def test_dietary_needs_are_capped_at_five(house: House) -> None:
    tags = [await house.tag(f"Need {n}", TagKind.DIETARY) for n in range(6)]
    with pytest.raises(InvalidMemberInput, match="up to 5"):
        await act(house, guests.set_my_needs, house["Pat"], frozenset(t.id for t in tags))
    await act(house, guests.set_my_needs, house["Pat"], frozenset(t.id for t in tags[:5]))


async def test_a_guest_coordinator_closes_answers_but_not_the_vote(house: House) -> None:
    event_id = await open_dinner(house)
    await invite(house, "Kim", "Pat", event_id)
    pat = house["Pat"]
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.close_window, pat, event_id)  # "Start the vote now": logistics
    assert (await house.event(event_id)).state is EventState.VOTING
    with pytest.raises(EventRuleViolation):  # "Decide now" picks the plan
        await act(house, calls.close_window, pat, event_id)
    view = await act(house, event_views.event_view, pat, event_id)
    assert view.can.close_window is None


async def test_when_dietary_needs_rule_out_every_pick_the_call_says_so(house: House) -> None:
    await house.tag("Halal", TagKind.DIETARY)
    event_id = await open_dinner(house)
    mo = await add_guest(house, "Mo", slack="U08MO", needs=("Halal",))
    await invite(house, "Kim", "Mo", event_id)
    await act(house, calls.respond, mo, event_id, Status.IN)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.ABANDONED
    (abandoned,) = [e for e in await house.outbox() if e.kind == "call_abandoned"]
    assert abandoned.payload["needs_unmet"] is True and abandoned.payload["reason"] == "no_coverage"
