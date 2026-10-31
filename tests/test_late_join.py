# SPDX-License-Identifier: AGPL-3.0-or-later
"""Joining a call late, during the vote (#76), against a migrated database.

Someone who missed the answers can still join while the call is voting. They're in by default,
and their vote shows but doesn't count unless the coordinator says so. Their dietary needs and
stances warn about the picks instead of removing them, and their own picks still pass everyone
who answered on time. Guests are unchanged: they come only by invitation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import date, time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.blocks import Directory
from dinnerbell.channels.slack.messages import RenderContext, render, your_answer
from dinnerbell.channels.slack.views import HomeLinks, home_view
from dinnerbell.config import Settings
from dinnerbell.db.events import ProposalRepository, ResponseRepository
from dinnerbell.db.models import ResponseRow, TagRow
from dinnerbell.domain.catalog import Mode, TagKind
from dinnerbell.domain.events import (
    JOIN_LATE_PROMPT,
    LATE_SWITCHERS,
    LATE_TOO_LATE,
    NOT_IN_JOIN_LATE,
    TAKEN_OFF,
    ActingAs,
    AssignmentRole,
    Details,
    Event,
    EventKind,
    EventRuleViolation,
    EventState,
    JoinLateFirst,
    ProposalSource,
    Response,
    RoleOffer,
    Status,
    check_can_join_late,
    check_can_offer_role,
    check_can_switch_late,
    counted_votes,
    counts_vote,
    veto_participants,
)
from dinnerbell.domain.guests import REFUSED, GuestAction, GuestInput
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.domain.notifications import from_payload
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.logs import JsonFormatter
from dinnerbell.services import activity, call_admin, calls, event_views, guests, lifecycle
from dinnerbell.services import call_context as cc
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from dinnerbell.services.event_views import TodayView
from dinnerbell.services.lifecycle import DOESNT_FIT
from dinnerbell.services.preferences import group_preferences, set_my_preference
from tests.conftest import invite, page, sign_in
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import state
from tests.support.slack import Slack, wire
from tests.support.web import submit
from tests.test_auth_flow import member, rows
from tests.test_events_web import admin as admin
from tests.test_events_web import proposal_ids, sign_in_as, start
from tests.test_events_web import to_voting as web_to_voting

pytestmark = pytest.mark.anyio
BASE_URL = "https://dinnerbell.example.com"


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


async def to_voting(house: House, *eaters: str, maybes: tuple[str, ...] = ()) -> int:
    """``eaters`` are in and Alex offers to pick up, so Siam Garden and Taqueria are the picks.
    Kim never answers unless she's named."""
    event_id = await open_dinner(house)
    for name in eaters:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    for name in maybes:
        await act(house, calls.respond, house[name], event_id, Status.MAYBE)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def mine(house: House, event_id: int, name: str) -> Response | None:
    return (await house.context(event_id)).response_of(house[name].id)


async def picks(house: House, event_id: int) -> dict[str, int]:
    ctx = await house.context(event_id)
    return {cc.item_info(ctx, p).name: p.id for p in sorted(ctx.proposals, key=lambda p: p.rank)}


async def info(house: House, event_id: int, proposal_id: int) -> Any:
    ctx = await house.context(event_id)
    found = ctx.proposal(proposal_id)
    assert found is not None
    return cc.proposal_info(ctx, found)


# The rule, in the domain ---------------------------------------------------------------------------


def test_a_late_vote_counts_only_while_taking_part_and_switched_on() -> None:
    on_time, coordinator_not_in = Response(1, Status.IN), None
    late = Response(2, Status.IN, late=True)
    assert counts_vote(on_time) and counts_vote(coordinator_not_in)
    assert not counts_vote(late)
    assert counts_vote(Response(2, Status.IN, late=True, vote_counts=True))
    assert not counts_vote(Response(2, Status.OUT, late=True, vote_counts=True))  # switched off: out
    votes = {10: [1, 2, 3], 11: [2]}
    assert counted_votes(votes, [on_time, late]) == {10: frozenset({1, 3}), 11: frozenset()}


def test_late_joiners_stances_never_filter_unless_they_were_a_maybe() -> None:
    responses = [
        Response(1, Status.IN),
        Response(2, Status.IN, late=True),  # never answered, or said out
        Response(3, Status.IN, late=True, maybe_at_deadline=True),  # a maybe on time
        Response(4, Status.OUT, late=True),  # switched off
    ]
    assert veto_participants(responses) == {1, 3}


def _event(state: EventState, *, kind: EventKind = EventKind.CALL, coordinator_id: int | None = None) -> Event:
    return Event(
        1, 1, kind, Slot.DINNER, date(2026, 10, 22), state, 1, OPENS, RESPOND_BY, 15, 45, coordinator_id=coordinator_id
    )


def _person(member_id: int, role: Role = Role.MEMBER, *, active: bool = True) -> Member:
    return Member(member_id, 1, f"P{member_id}", None, role, Capabilities(), active)


@pytest.mark.parametrize(
    ("event", "actor", "answer", "code"),
    [
        (_event(EventState.VOTING), _person(2, active=False), None, "inactive"),
        (_event(EventState.VOTING), _person(9, Role.GUEST), None, "guests_dont"),
        (_event(EventState.COLLECTING, kind=EventKind.OFFER), _person(2), None, "is_offer"),
        (_event(EventState.SCHEDULED), _person(2), None, "not_open"),
        (_event(EventState.DONE), _person(2), None, "call_finished"),
        (_event(EventState.CANCELLED), _person(2), None, "call_finished"),
        (_event(EventState.VOTING), _person(2), Response(2, Status.OUT, late=True), "taken_off"),
    ],
)
def test_who_may_join_late(event: Event, actor: Member, answer: Response | None, code: str) -> None:
    with pytest.raises(EventRuleViolation) as refused:
        check_can_join_late(event, actor, answer)
    assert refused.value.code == code


def test_who_flips_the_switches() -> None:
    voting = _event(EventState.VOTING, coordinator_id=2)
    admin, guest = _person(1, Role.ADMIN), _person(9, Role.GUEST)
    assert check_can_switch_late(voting, _person(2), _person(2)) is ActingAs.COORDINATOR
    assert check_can_switch_late(_event(EventState.VOTING), admin, None) is ActingAs.ADMIN
    assert check_can_switch_late(replace(voting, coordinator_id=9), admin, guest) is ActingAs.ADMIN  # #38
    for event, actor, coordinator, code in [
        (voting, admin, _person(2), "not_coordinator"),
        (voting, _person(2, active=False), _person(2), "inactive"),
        (replace(voting, coordinator_id=9), guest, guest, "guests_dont"),
        (replace(voting, state=EventState.DECIDED), _person(2), _person(2), "voting_closed"),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            check_can_switch_late(event, actor, coordinator)
        assert refused.value.code == code


def test_only_a_late_joiner_taking_part_offers_a_role_while_voting() -> None:
    voting, kim = _event(EventState.VOTING), _person(3)
    check_can_offer_role(voting, kim, Response(3, Status.IN, late=True))
    for answer in (Response(3, Status.IN), Response(3, Status.OUT, late=True), None):
        with pytest.raises(EventRuleViolation, match="Responses are closed"):
            check_can_offer_role(voting, kim, answer)


# Joining late -------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["never", Status.OUT, Status.MAYBE])
async def test_someone_who_missed_the_answers_joins_in_and_marked_late(house: House, answer: object) -> None:
    event_id = await open_dinner(house)
    if isinstance(answer, Status):
        await act(house, calls.respond, house["Kim"], event_id, answer)  # a maybe is out at the deadline
    event_id = await to_voting(house, "Alex", "Sam")
    before = (await house.context(event_id)).head_count
    channel = RecordingChannel()
    await house.drain(channel)
    channel.sent.clear()

    assert await act(house, calls.join_late, house["Kim"], event_id, run=1) is True
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and (kim.status, kim.late, kim.vote_counts) == (Status.IN, True, False)
    ctx = await house.context(event_id)
    assert ctx.head_count == before + 1
    entry = next(e for e in cc.roster(ctx) if e.member.id == house["Kim"].id)
    assert (entry.late, entry.vote_counts) == (True, False)
    await house.drain(channel)
    assert sorted(channel.kinds) == ["call_updated", "proposals_updated"]
    assert await act(house, calls.join_late, house["Kim"], event_id) is False  # in already: nothing to do
    # The picks message keeps the joiner among the people it mentions (#92).
    assert "Kim" in {p.name for p in cc.who_is_in(ctx)}


async def test_joining_late_is_only_while_voting(house: House) -> None:
    event_id = await open_dinner(house)
    with pytest.raises(EventRuleViolation, match="just tap In") as refused:
        await act(house, calls.join_late, house["Kim"], event_id)
    assert refused.value.code == "responses_open"
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    with pytest.raises(EventRuleViolation) as stale:
        await act(house, calls.join_late, house["Kim"], event_id, run=2)
    assert stale.value.code == "stale_run"
    ctx = await house.context(event_id)
    await act(house, calls.lock, house["Alex"], event_id, ctx.proposals[0].id)
    with pytest.raises(EventRuleViolation) as decided:  # after the decision: out of scope (#76)
        await act(house, calls.join_late, house["Kim"], event_id)
    assert (str(decided.value), decided.value.code) == (LATE_TOO_LATE, "too_late")
    with pytest.raises(EventRuleViolation, match="Voting is closed"):  # nor does a vote join them
        await act(house, calls.vote, house["Kim"], event_id, ctx.proposals[0].id, join_late=True)


async def test_a_vote_or_a_pick_from_someone_not_in_asks_first_then_joins_them_late(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam")
    found = await picks(house, event_id)
    taqueria = found["Taqueria"]
    for attempt in (
        lambda: act(house, calls.vote, house["Kim"], event_id, taqueria),
        lambda: act(house, calls.toggle_vote, house["Kim"], event_id, taqueria),
        lambda: act(
            house,
            calls.add_proposal,
            house["Lee"],
            event_id,
            Mode.DELIVERY,
            item_id=None,
            free_text="Pho Place",
            cuisine_id=house.tags["Thai"].id,
        ),
    ):
        with pytest.raises(JoinLateFirst) as refused:
            await attempt()
        assert (str(refused.value), refused.value.code) == (NOT_IN_JOIN_LATE, "not_in")
    assert await mine(house, event_id, "Kim") is None and await mine(house, event_id, "Lee") is None

    await act(house, calls.vote, house["Kim"], event_id, taqueria, join_late=True)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and kim.late and kim.is_in
    assert house["Kim"].id in (await house.context(event_id)).votes[taqueria]

    pho = await act(
        house,
        calls.add_proposal,
        house["Lee"],
        event_id,
        Mode.DELIVERY,
        free_text="Pho Place",
        cuisine_id=house.tags["Thai"].id,
        join_late=True,
    )
    lee = await mine(house, event_id, "Lee")
    assert lee is not None and lee.late and lee.offers == {RoleOffer.ORDER}  # whoever adds it does it (#74)
    assert pho.proposed_by == house["Lee"].id


async def test_a_late_vote_shows_but_counts_only_once_the_coordinator_says_so(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam")
    taqueria = (await picks(house, event_id))["Taqueria"]
    await act(house, calls.vote, house["Sam"], event_id, taqueria)
    await act(house, calls.vote, house["Kim"], event_id, taqueria, join_late=True)
    shown = await info(house, event_id, taqueria)
    assert [p.name for p in shown.voters] == ["Sam", "Kim"]
    assert (shown.votes, [p.name for p in shown.not_counted]) == (1, ["Kim"])

    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    channel = RecordingChannel()
    await house.drain(channel)
    channel.sent.clear()
    await act(house, calls.switch_late_joiner, house["Sam"], event_id, house["Kim"].id, vote_counts=True, run=1)
    shown = await info(house, event_id, taqueria)
    assert (shown.votes, shown.not_counted) == (2, ())
    await house.drain(channel)
    assert sorted(channel.kinds) == ["call_updated", "proposals_updated"]  # no message to Kim for this one
    (audit,) = [a for a in await house.audits() if a.action == "event.late_joiner_changed"]
    assert audit.payload == {
        "member_id": house["Kim"].id,
        "taking_part": True,
        "vote_counts": True,
        "as": "coordinator",
    }
    # The same switch again changes nothing, and audits nothing.
    await act(house, calls.switch_late_joiner, house["Sam"], event_id, house["Kim"].id, vote_counts=True)
    assert len([a for a in await house.audits() if a.action == "event.late_joiner_changed"]) == 1


@pytest.mark.parametrize("counts", [False, True])
async def test_a_tie_at_the_deadline_with_and_without_the_late_vote(house: House, counts: bool) -> None:
    """Sam and Lee split their votes, so it's a tie that ranking order breaks: the first pick
    wins. Kim joins late and votes for the second. Her vote breaks the tie only if it counts."""
    event_id = await to_voting(house, "Alex", "Sam", "Lee")
    first, second = (await picks(house, event_id)).values()
    await act(house, calls.vote, house["Lee"], event_id, first)
    await act(house, calls.vote, house["Sam"], event_id, second)
    await act(house, calls.vote, house["Kim"], event_id, second, join_late=True)
    if counts:  # nobody coordinates, so the admin flips it
        await act(house, calls.switch_late_joiner, house["Alex"], event_id, house["Kim"].id, vote_counts=True)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED
    winner = ctx.winner
    assert winner is not None and winner.id == (second if counts else first)
    decision = cc.decision_info(ctx)
    assert decision is not None
    assert {p.member.name: p.late for p in decision.participants} == {
        "Alex": False,
        "Sam": False,
        "Lee": False,
        "Kim": True,
    }


async def test_a_late_vote_never_breaks_a_tie_as_the_coordinators(house: House) -> None:
    """The coordinator's vote breaks a tie (FR-16), but not when it's a late joiner's that doesn't count."""
    event_id = await to_voting(house, "Alex", "Sam", "Lee")
    first, second = (await picks(house, event_id)).values()
    await act(house, calls.vote, house["Lee"], event_id, first)
    await act(house, calls.vote, house["Sam"], event_id, second)
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)  # her vote counts now (#154)
    await act(house, calls.switch_late_joiner, house["Kim"], event_id, house["Kim"].id, vote_counts=False)
    await act(house, calls.vote, house["Kim"], event_id, second)
    house.clock.now = VOTE_BY
    await house.tick()
    winner = (await house.context(event_id)).winner
    assert winner is not None and winner.id == first  # ranking order, not Kim's vote


async def test_taking_part_off_drops_them_from_the_head_count_and_the_roles(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam", "Lee")
    taqueria = (await picks(house, event_id))["Taqueria"]
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.offer_role, house["Kim"], event_id, RoleOffer.PICKUP, True, run=1)  # late answer
    await act(house, calls.vote, house["Kim"], event_id, taqueria)
    ctx = await house.context(event_id)
    assert ctx.head_count == 4 and house["Kim"].id in ctx.coverage.pickups
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    with pytest.raises(EventRuleViolation, match=LATE_SWITCHERS):
        await act(house, calls.switch_late_joiner, house["Lee"], event_id, house["Kim"].id, taking_part=False)
    with pytest.raises(EventRuleViolation, match=LATE_SWITCHERS):  # an admin, while someone coordinates
        await act(house, calls.switch_late_joiner, house["Alex"], event_id, house["Kim"].id, taking_part=False)
    with pytest.raises(EventRuleViolation, match="on time") as on_time:
        await act(house, calls.switch_late_joiner, house["Sam"], event_id, house["Lee"].id, taking_part=False)
    assert on_time.value.code == "not_late"
    with pytest.raises(NotFound):
        await act(house, calls.switch_late_joiner, house["Sam"], event_id, 999_999, taking_part=False)

    channel = RecordingChannel()
    await house.drain(channel)
    channel.sent.clear()
    await act(
        house, calls.switch_late_joiner, house["Sam"], event_id, house["Kim"].id, taking_part=False, vote_counts=True
    )
    ctx = await house.context(event_id)
    kim = ctx.response_of(house["Kim"].id)
    assert kim is not None and kim.status is Status.OUT and kim.late
    assert ctx.head_count == 3 and house["Kim"].id not in ctx.coverage.pickups
    assert ctx.counted(taqueria) == frozenset()  # treated as out, whatever the vote switch says
    await house.drain(channel)
    (told,) = [s for s in channel.sent if s.kind == "late_join_changed"]
    assert told.recipient == house["Kim"].id
    with pytest.raises(EventRuleViolation, match=TAKEN_OFF) as again:  # she can't rejoin on her own
        await act(house, calls.join_late, house["Kim"], event_id)
    assert again.value.code == "taken_off"
    with pytest.raises(EventRuleViolation, match=TAKEN_OFF):
        await act(house, calls.vote, house["Kim"], event_id, taqueria, join_late=True)

    await act(house, calls.switch_late_joiner, house["Sam"], event_id, house["Kim"].id, taking_part=True)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and kim.is_in and kim.offers == {RoleOffer.PICKUP}  # her offer comes back
    await act(house, calls.switch_late_joiner, house["Sam"], event_id, house["Kim"].id, taking_part=False)
    await act(house, calls.lock, house["Sam"], event_id, taqueria)
    ctx = await house.context(event_id)
    assert house["Kim"].id not in {r.member_id for r in ctx.participants}
    assert house["Kim"].id not in {a.member_id for a in ctx.assignments}


async def test_a_late_joiner_taking_part_can_be_assigned_the_role(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee")  # Alex offered to pick up, so he's in too
    taqueria = (await picks(house, event_id))["Taqueria"]
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.offer_role, house["Kim"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    await act(house, calls.lock, house["Sam"], event_id, taqueria)
    async with house.sessionmaker() as db:
        await calls.change_assignment(
            db, house["Sam"], event_id, AssignmentRole.PICKUP, house["Kim"].id, now=house.clock()
        )
    ctx = await house.context(event_id)
    assert {(a.role.value, a.member_id) for a in ctx.assignments} >= {("pickup", house["Kim"].id)}
    assert ctx.head_count == 4


# Warnings, not refusals (FR-29) -------------------------------------------------------------------


async def _kim_vegetarian_vetoes_thai(house: House) -> None:
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    async with house.sessionmaker() as db:
        house.people["Kim"] = await guests.set_my_needs(db, house["Kim"], frozenset({veg.id}), now=house.clock())
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, house.tags["Thai"].id, Stance(veto=True))


def _rendered(notification: Any) -> str:
    return json.dumps(render(notification, RenderContext(BASE_URL, Directory())).payload(), ensure_ascii=False)


async def test_only_the_late_joiner_sees_which_picks_miss_their_own_stances(house: House) -> None:
    """Kim alone joins late, needs vegetarian, and vetoes Thai; Siam Garden (Thai) and Taqueria
    are the picks. Nothing is removed, and no surface anyone else sees says a pick misses
    anything: with one late joiner, that would say what Kim vetoes (FR-29). Kim sees it on her
    card, her Home, and her event page."""
    await _kim_vegetarian_vetoes_thai(house)
    event_id = await to_voting(house, "Alex", "Sam")
    found = await picks(house, event_id)
    assert set(found) == {"Siam Garden", "Taqueria"}
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.vote, house["Kim"], event_id, found["Siam Garden"])
    assert set(await picks(house, event_id)) == {"Siam Garden", "Taqueria"}  # nothing is removed

    # Every message the call sends, in the channel or to anyone.
    sent = [from_payload(e.kind, e.payload) for e in await house.outbox()]
    assert {n.kind for n in sent} >= {"call_updated", "proposals_updated"}
    for notification in sent:
        assert "⚠" not in _rendered(notification) and "fit" not in _rendered(notification), notification.kind

    ctx = await house.context(event_id)
    for name in ("Alex", "Sam", "Lee", "Kim"):
        view = event_views.view(ctx, house[name], ctx.event.day, house.clock())
        home = json.dumps(
            home_view(TodayView(ctx.event.day, [view], tuple(Slot)), HomeLinks(BASE_URL)), ensure_ascii=False
        )
        card = your_answer(
            view.info, view.mine, house[name].id, RenderContext(BASE_URL, Directory()), not_for_you=view.not_for_me
        ).payload()
        if name == "Kim":
            assert view.not_for_me == ("Siam Garden", "Taqueria")
            assert "Not a fit for your preferences: Siam Garden, Taqueria" in home
            assert "Not a fit for your preferences: Siam Garden, Taqueria" in json.dumps(card, ensure_ascii=False)
        else:
            assert view.not_for_me == () and "⚠" not in home and "⚠" not in json.dumps(card, ensure_ascii=False), name
        assert all(not p.info.warnings for p in view.proposals)

    await act(house, calls.lock, house["Alex"], event_id, found["Siam Garden"])  # the coordinator may still lock it
    ctx = await house.context(event_id)
    decision = cc.decision_info(ctx)
    assert decision is not None and decision.dietary_notes == ()  # hers stay off it: M1, FR-29
    assert "⚠" not in _rendered(cc.decision_updated(ctx)[0])


async def _late_with_guest(house: House, event_id: int, tag_id: int) -> None:
    """Kim joins late with a guest who needs ``tag_id``. Joining clears a stale guest, and details
    are closed while voting, so this saves the answer as a restored one would be."""
    await act(house, calls.join_late, house["Kim"], event_id)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None
    async with house.sessionmaker() as db:
        await ResponseRepository(db).save(
            house.household.id, event_id, replace(kim, guests=1, guest_needs=frozenset({tag_id})), house.clock()
        )
        await db.commit()


async def test_a_late_joiners_guests_needs_filter_new_picks_and_warn_on_existing_ones(house: House) -> None:
    """Guests' needs are public and safety-relevant (allergies), so they are held to as any
    guest's: picks added or drawn from now on must meet them, picks already proposed warn
    everyone, and the warning stays on the decision."""
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    event_id = await to_voting(house, "Alex", "Sam")
    found = await picks(house, event_id)
    pho = await house.add_item("Pho Place", Mode.PICKUP, cuisine="Thai")
    await _late_with_guest(house, event_id, veg.id)
    ctx = await house.context(event_id)
    assert ctx.guest_needs == {veg.id}  # the filter new picks pass
    needs = cc.LATE_GUEST_NEEDS.format(needs="Vegetarian")
    for name in found:  # already proposed: kept, with a warning for everyone
        warned = await info(house, event_id, found[name])
        assert warned.warnings == (needs,) and "Kim" not in warned.warnings[0]
    with pytest.raises(EventRuleViolation, match=DOESNT_FIT):  # new: refused
        await act(house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, item_id=pho.id)
    meatless = await house.add_item("Green Leaf", Mode.PICKUP, cuisine="Thai", tags=("Vegetarian",))
    await act(house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, item_id=meatless.id)
    assert (await info(house, event_id, (await picks(house, event_id))["Green Leaf"])).warnings == ()

    await act(house, calls.lock, house["Alex"], event_id, found["Siam Garden"])  # a kept pick can still win
    ctx = await house.context(event_id)
    decision = cc.decision_info(ctx)
    assert decision is not None
    assert decision.proposal.warnings == (needs,) and decision.dietary_notes == ("Vegetarian",)
    assert needs in _rendered(cc.decision_updated(ctx)[0])


async def test_ranking_holds_picks_to_the_guests_needs_of_a_re_run_late_joiner(house: House) -> None:
    """Newly drawn picks meet them too: ranking reads the same ``guest_needs``."""
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    event_id = await to_voting(house, "Alex", "Sam")
    await _late_with_guest(house, event_id, veg.id)
    assert (await house.context(event_id)).guest_needs == {veg.id}
    ctx = await house.context(event_id)
    async with house.sessionmaker() as db:
        snapshot = await lifecycle.ranking_snapshot(db, ctx, await group_preferences(db, 1, ctx.veto_people))
    assert snapshot.guest_needs == {veg.id}


async def test_a_late_joiners_own_needs_stay_off_the_decision_for_everyone_else(house: House) -> None:
    """With one late joiner, subtracting the guests' needs from the decision's line would give
    hers away (FR-29). Guests' needs, Sam's here, stay."""
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    gf = await house.tag("Gluten-free", TagKind.DIETARY)
    await house.add_item("GF Place", Mode.PICKUP, cuisine="Thai", tags=("Gluten-free",))
    async with house.sessionmaker() as db:
        house.people["Kim"] = await guests.set_my_needs(db, house["Kim"], frozenset({veg.id}), now=house.clock())
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await act(house, calls.save_details, house["Sam"], event_id, Details(guests=1, guest_needs=frozenset({gf.id})))
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    found = await picks(house, event_id)
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.claim_coordinator, house["Alex"], event_id)
    await act(house, calls.lock, house["Alex"], event_id, next(iter(found.values())))
    ctx = await house.context(event_id)
    decision = cc.decision_info(ctx)
    assert decision is not None and decision.dietary_notes == ("Gluten-free",)
    channel = RecordingChannel()
    await house.drain(channel)
    for notification in [from_payload(e.kind, e.payload) for e in await house.outbox()]:
        assert "Vegetarian" not in _rendered(notification), notification.kind
    assert "Vegetarian" not in _rendered(cc.assignment_made(ctx, house["Alex"].id, 1, decision))


def test_joining_late_clears_what_an_earlier_answer_left() -> None:
    from dinnerbell.domain.events import with_late_join

    stale = Response(
        3,
        Status.OUT,
        guests=2,
        guest_needs=frozenset({7}),
        eat_from=time(19, 0),
        eat_until=time(20, 0),
        note="Out, sorry",
    )
    joined = with_late_join(stale, 3, OPENS)
    assert (joined.status, joined.late, joined.vote_counts) == (Status.IN, True, False)
    assert (joined.guests, joined.guest_needs, joined.eat_from, joined.eat_until) == (0, frozenset(), None, None)
    assert joined.head_count == 1


async def test_joining_late_changes_neither_the_head_count_nor_the_meal_time_beyond_the_joiner(house: House) -> None:
    event_id = await open_dinner(house)
    await act(house, calls.respond, house["Alex"], event_id, Status.IN)
    await act(house, calls.respond, house["Sam"], event_id, Status.IN)
    await act(house, calls.save_details, house["Kim"], event_id, Details(guests=3, eat_from=time(19, 45)))
    await act(house, calls.respond, house["Kim"], event_id, Status.OUT)  # out keeps the details
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    before = (await house.context(event_id)).head_count
    await act(house, calls.join_late, house["Kim"], event_id)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and (kim.guests, kim.eat_from) == (0, None)
    ctx = await house.context(event_id)
    assert ctx.head_count == before + 1
    await act(house, calls.lock, house["Alex"], event_id, ctx.proposals[0].id)
    meal = (await house.event(event_id)).meal_time
    assert meal is not None and meal.hour == 23 and meal.minute == 30  # the usual 18:30, not Kim's 19:45


async def test_a_late_joiners_pick_passes_everyone_on_time_but_not_their_own_stances(house: House) -> None:
    await act(house, set_my_preference, house["Sam"], TargetType.TAG, house.tags["Mexican"].id, Stance(veto=True))
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, house.tags["Thai"].id, Stance(veto=True))
    event_id = await to_voting(house, "Alex", "Sam")
    mexican, thai = house.tags["Mexican"].id, house.tags["Thai"].id
    with pytest.raises(EventRuleViolation) as refused:
        await act(
            house,
            calls.add_proposal,
            house["Kim"],
            event_id,
            Mode.DELIVERY,
            free_text="Taco Hut",
            cuisine_id=mexican,
            join_late=True,
        )
    assert (str(refused.value), refused.value.code) == (DOESNT_FIT, "doesnt_fit")
    assert await mine(house, event_id, "Kim") is None  # refused, so she didn't join either

    thai_pick = await act(
        house,
        calls.add_proposal,
        house["Kim"],
        event_id,
        Mode.DELIVERY,
        free_text="Thai Express",
        cuisine_id=thai,
        join_late=True,
    )
    ctx = await house.context(event_id)
    assert not cc.fits_late_joiner(ctx, thai_pick, house["Kim"].id)  # her own veto: only she is told
    assert (await info(house, event_id, thai_pick.id)).warnings == ()


async def test_a_maybe_who_joins_late_keeps_their_stances_applying(house: House) -> None:
    """A maybe's stances applied on time (FR-9), so they keep applying once they join late."""
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, house.tags["Thai"].id, Stance(veto=True))
    event_id = await to_voting(house, "Alex", "Sam", maybes=("Kim",))
    assert "Siam Garden" not in await picks(house, event_id)  # her veto applied to ranking
    await act(house, calls.join_late, house["Kim"], event_id)
    assert house["Kim"].id in (await house.context(event_id)).veto_people
    with pytest.raises(EventRuleViolation, match=DOESNT_FIT):
        await act(house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, item_id=house.items["Siam Garden"].id)


# Guests, and starting again ------------------------------------------------------------------------


async def test_guests_still_come_only_by_invitation(house: House) -> None:
    event_id = await open_dinner(house)
    pat = (await act(house, guests.invite, house["Kim"], event_id, new_guest=GuestInput("Pat"))).guest
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, guests.answer_for, house["Kim"], event_id, pat.id, status=Status.OUT)
    await act(house, calls.claim_coordinator, pat, event_id)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    taqueria = (await picks(house, event_id))["Taqueria"]
    with pytest.raises(EventRuleViolation, match=REFUSED[GuestAction.JOIN_LATE]):
        await act(house, calls.join_late, pat, event_id)
    with pytest.raises(EventRuleViolation, match=REFUSED[GuestAction.VOTE]):
        await act(house, calls.vote, pat, event_id, taqueria, join_late=True)
    # A guest coordinating doesn't flip the switches; an admin does (#38).
    await act(house, calls.join_late, house["Kim"], event_id)
    with pytest.raises(EventRuleViolation, match=REFUSED[GuestAction.LATE_SWITCHES]):
        await act(house, calls.switch_late_joiner, pat, event_id, house["Kim"].id, vote_counts=True)
    await act(house, calls.switch_late_joiner, house["Alex"], event_id, house["Kim"].id, vote_counts=True)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and kim.vote_counts


async def test_a_re_run_makes_late_joiners_ordinary_answers(house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam", maybes=("Lee",))
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.join_late, house["Lee"], event_id)
    await act(house, calls.offer_role, house["Lee"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.switch_late_joiner, house["Alex"], event_id, house["Lee"].id, taking_part=False)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    ctx = await house.context(event_id)
    kim, lee = ctx.response_of(house["Kim"].id), ctx.response_of(house["Lee"].id)
    assert kim is not None and (kim.status, kim.late, kim.vote_counts) == (Status.IN, False, False)
    assert lee is not None and (lee.status, lee.late, lee.offers) == (Status.MAYBE, False, frozenset())


async def test_joining_and_switching_tell_the_story_in_the_logs(house: House, caplog: pytest.LogCaptureFixture) -> None:
    event_id = await to_voting(house, "Alex", "Sam")
    caplog.clear()
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.switch_late_joiner, house["Alex"], event_id, house["Kim"].id, taking_part=False)
    logged = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == activity.log.name]
    joined, switched = logged
    assert (joined["msg"], joined["member"], joined["answer_before"], joined["state_after"]) == (
        "join_late",
        "Kim",
        "never",
        "voting",
    )
    assert (switched["msg"], switched["to_member"], switched["taking_part"], switched["capacity"]) == (
        "late_switch",
        "Kim",
        False,
        "admin",
    )


# Slack -----------------------------------------------------------------------------------------------


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def test_slack_im_in_late_joins_and_shows_their_card(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam")
    await slack.click("Kim", ids.JOIN_LATE, ids.card_value(event_id, 1))
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and kim.late and kim.is_in
    card = slack.cards("Kim")[-1]
    text = json.dumps(card["blocks"])
    assert "In (joined late)" in text and "card_offer_pickup" in text  # she can offer a role from it
    await slack.tap_card("Kim", "card_offer_pickup", card)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and kim.offers == {RoleOffer.PICKUP}
    await slack.click("Kim", ids.JOIN_LATE, ids.card_value(event_id, 1), home=True, channel=None)
    assert "already" in json.dumps(slack.last_home("Kim"))


async def test_slack_vote_from_someone_not_in_asks_then_joins_and_votes(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam")
    taqueria = (await picks(house, event_id))["Taqueria"]
    await slack.click("Kim", ids.VOTE, ids.proposal_value(event_id, taqueria))
    assert await mine(house, event_id, "Kim") is None
    assert slack.ephemerals("Kim")[-1] == JOIN_LATE_PROMPT
    (prompt,) = [c for c in slack.fake.of("chat.postEphemeral") if c.args["text"] == JOIN_LATE_PROMPT]
    assert ids.JOIN_LATE_VOTE in json.dumps(prompt.args["blocks"])
    await slack.click("Kim", ids.JOIN_LATE_VOTE, ids.proposal_value(event_id, taqueria), ephemeral=True)
    await slack.click("Kim", ids.JOIN_LATE_VOTE, ids.proposal_value(event_id, taqueria), ephemeral=True)  # twice
    ctx = await house.context(event_id)
    assert ctx.votes[taqueria] == [house["Kim"].id] and ctx.counted(taqueria) == frozenset()
    assert "joining late" in slack.ephemerals("Kim")[-1]


async def test_slack_switches_on_the_proposals_message(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam")
    await act(house, calls.join_late, house["Kim"], event_id)
    on = ids.late_value(ids.LateSwitch.VOTE_COUNTS, True, event_id, 1, house["Kim"].id)
    await slack.click("Sam", ids.LATE_MENU, None, selected=on)
    assert slack.ephemerals("Sam")[-1] == LATE_SWITCHERS
    await slack.click("Alex", ids.LATE_MENU, None, selected=on)  # the admin, with nobody coordinating
    assert slack.ephemerals("Alex")[-1] == "Kim's vote counts now."
    off = ids.late_value(ids.LateSwitch.TAKING_PART, False, event_id, 1, house["Kim"].id)
    await slack.click("Alex", ids.LATE_MENU, None, selected=off)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and (kim.status, kim.vote_counts) == (Status.OUT, True)
    stale = ids.late_value(ids.LateSwitch.TAKING_PART, True, event_id, 2, house["Kim"].id)
    await slack.click("Alex", ids.LATE_MENU, None, selected=stale)
    assert (await mine(house, event_id, "Kim")).status is Status.OUT  # type: ignore[union-attr]
    await slack.click("Alex", ids.LATE_MENU, None, selected="part:on:1:1")  # not a value this app wrote
    assert "out of date" in slack.ephemerals("Alex")[-1]


async def test_slack_add_a_pick_asks_someone_not_in_to_join_late(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Alex", "Sam")
    await slack.click("Kim", ids.ADD_PROPOSAL, ids.event_value(event_id))
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert view["submit"]["text"] == "Join late and add" and JOIN_LATE_PROMPT in json.dumps(view["blocks"])
    origin = ids.ViewOrigin.decode(view["private_metadata"])
    assert origin.run == 1
    form = state(
        mode={"selected_option": {"value": "delivery"}},
        free_text={"value": "Thai Express"},
        cuisine={"selected_option": {"value": str(house.tags["Thai"].id)}},
    )
    await slack.submit("Kim", ids.PROPOSAL_VIEW, form, event_id, origin=origin)
    kim = await mine(house, event_id, "Kim")
    assert kim is not None and kim.late and kim.offers == {RoleOffer.ORDER}
    assert "Thai Express" in await picks(house, event_id)


# The web ---------------------------------------------------------------------------------------------


def test_web_join_late_vote_and_switches(
    admin: TestClient,
    provider: FakeProvider,
    clock: FakeClock,
    migrated: Settings,
) -> None:
    invite(admin, "kim@example.com", "Kim")
    event_id = start(admin)
    web_to_voting(admin, clock, event_id)
    first, _second = proposal_ids(migrated)

    sign_in_as(admin, provider, "kim@example.com")
    thai = next(t for t in rows(migrated, TagRow) if t.name == "Thai")
    assert submit(admin, "/preferences/stance", {"target": f"tag:{thai.id}", "level": "veto"}).status_code < 400
    html = page(admin.get(f"/events/{event_id}"))
    assert "fit your preferences" not in html  # not until she's in (#76)
    assert "I'm in (late)" in html and "Join late and vote" in html and "Join late and add" in html
    assert 'hx-confirm="You missed the answers. Join late?"' in admin.get(f"/events/{event_id}").text
    assert submit(admin, f"/events/{event_id}/join-late", {}).status_code == 400  # it names the run
    response = submit(admin, f"/events/{event_id}/proposals/{first}/vote", {"join_late": "1"})
    assert response.status_code == 303, page(response)
    kim = member(migrated, "kim@example.com")
    (late,) = [r for r in rows(migrated, ResponseRow) if r.member_id == kim.id]
    assert (late.status, late.late, late.vote_counts) == ("in", True, False)
    html = page(admin.get(f"/events/{event_id}"))
    assert "Joined late" in html and "Kim (late)" in html and "Taking part" in html
    assert html.count("⚠ This proposal doesn't fit your preferences.") == 1  # Siam Garden, Thai: hers to see
    assert f"/events/{event_id}/late/" not in html  # her own switches aren't hers to flip
    refused = submit(admin, f"/events/{event_id}/late/{kim.id}", {"switch": "vote", "on": "1", "run": "1"})
    assert refused.status_code == 409 and LATE_SWITCHERS in page(refused)

    admin.cookies.clear()
    sign_in(admin, provider)  # the admin again
    html = page(admin.get(f"/events/{event_id}"))
    assert f"/events/{event_id}/late/{kim.id}" in html  # the admin, with nobody coordinating
    assert "⚠" not in html and "fit your preferences" not in html  # her stances are hers (FR-29)
    assert "⚠" not in page(admin.get("/"))
    assert (
        submit(admin, f"/events/{event_id}/late/{kim.id}", {"switch": "vote", "on": "1", "run": "1"}).status_code == 303
    )
    assert submit(admin, f"/events/{event_id}/late/{kim.id}", {"switch": "x", "on": "1", "run": "1"}).status_code == 409
    assert submit(admin, f"/events/{event_id}/late/{kim.id}", {"switch": "part", "on": "0"}).status_code == 400
    (late,) = [r for r in rows(migrated, ResponseRow) if r.member_id == kim.id]
    assert (late.status, late.vote_counts) == ("in", True)
    assert "not counted" not in page(admin.get(f"/events/{event_id}"))
    assert submit(admin, f"/events/{event_id}/join-late", {"run": "1"}).status_code == 303  # in already


def test_web_decision_leaves_a_late_joiners_own_needs_off_the_page(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """The decision's "Dietary needs" line on the event page names guests' needs, never the
    late joiner's own (FR-29)."""
    from datetime import UTC, datetime

    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from dinnerbell.db.engine import create_sync_engine
    from dinnerbell.db.models import MemberRow

    invite(admin, "kim@example.com", "Kim")
    engine = create_sync_engine(migrated.sync_database_url)
    try:
        with Session(engine) as db, db.begin():
            household_id = db.scalars(select(MemberRow.household_id)).first()
            tag = TagRow(
                household_id=household_id,
                kind="dietary",
                name="Vegetarian",
                name_key="vegetarian",
                created_at=datetime(2026, 10, 22, tzinfo=UTC),
            )
            db.add(tag)
            db.flush()
            db.execute(update(MemberRow).where(MemberRow.email == "kim@example.com").values(dietary_needs=[tag.id]))
    finally:
        engine.dispose()
    event_id = start(admin)
    web_to_voting(admin, clock, event_id)
    first, _second = proposal_ids(migrated)
    sign_in_as(admin, provider, "kim@example.com")
    assert submit(admin, f"/events/{event_id}/proposals/{first}/vote", {"join_late": "1"}).status_code == 303
    admin.cookies.clear()
    sign_in(admin, provider)
    assert submit(admin, f"/events/{event_id}/proposals/{first}/lock", {}).status_code == 303
    for url in (f"/events/{event_id}", "/"):
        html = page(admin.get(url))
        assert "Kim (late)" in html and "eating" in html, url
        assert "Dietary needs:" not in html, url


# Decisions still enforce on-time guests' needs (settled=True only exempts a late joiner's guests) ----


async def _gluten_free_vote(house: House) -> tuple[int, int, int]:
    """Sam brings a Gluten-free guest on time, so ranking proposes only GF Place. Taqueria lacks
    the tag, and is put on the ballot directly, as a pick that stopped fitting would be. Returns
    the event and the two proposals' IDs (GF Place, Taqueria)."""
    gf = await house.tag("Gluten-free", TagKind.DIETARY)
    gf_place = await house.add_item("GF Place", Mode.PICKUP, cuisine="Thai", tags=("Gluten-free",))
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.save_details, house["Sam"], event_id, Details(guests=1, guest_needs=frozenset({gf.id})))
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert set(await picks(house, event_id)) == {"GF Place"}
    async with house.sessionmaker() as db:
        added = await ProposalRepository(db).add(
            household_id=house.household.id,
            event_id=event_id,
            item_id=house.items["Taqueria"].id,
            free_text=None,
            free_cuisine_id=None,
            mode=Mode.PICKUP,
            rank=9,
            source=ProposalSource.MEMBER,
            reasons=(),
            proposed_by=None,
            now=house.clock(),
        )
        await db.commit()
    assert gf_place.id
    return event_id, (await picks(house, event_id))["GF Place"], added.id


async def test_locking_a_pick_that_misses_an_on_time_guests_need_is_refused(house: House) -> None:
    event_id, _gf, taqueria = await _gluten_free_vote(house)
    with pytest.raises(EventRuleViolation, match=DOESNT_FIT):
        await act(house, calls.lock, house["Alex"], event_id, taqueria)
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_the_deadline_never_picks_a_pick_that_misses_an_on_time_guests_need(house: House) -> None:
    event_id, gf_place, taqueria = await _gluten_free_vote(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.vote, house[name], event_id, taqueria)
    await act(house, calls.vote, house["Alex"], event_id, gf_place)
    house.clock.now = VOTE_BY
    await house.tick()
    ctx = await house.context(event_id)
    assert ctx.event.state is EventState.DECIDED and ctx.winner is not None and ctx.winner.id == gf_place


async def test_only_a_late_guests_need_is_exempt_for_a_pick_already_proposed(house: House) -> None:
    """Sam's on-time guest needs Gluten-free, and a late joiner's guest needs Vegetarian. A pick
    with Gluten-free but not Vegetarian can still win (it warns); one with Vegetarian but not
    Gluten-free can't."""
    veg = await house.tag("Vegetarian", TagKind.DIETARY)
    event_id, gf_place, taqueria = await _gluten_free_vote(house)
    await _late_with_guest(house, event_id, veg.id)
    veg_place = await house.add_item("Veg Place", Mode.PICKUP, cuisine="Thai", tags=("Vegetarian",))
    async with house.sessionmaker() as db:
        veg_pick = await ProposalRepository(db).add(
            household_id=house.household.id,
            event_id=event_id,
            item_id=veg_place.id,
            free_text=None,
            free_cuisine_id=None,
            mode=Mode.PICKUP,
            rank=10,
            source=ProposalSource.MEMBER,
            reasons=(),
            proposed_by=None,
            now=house.clock(),
        )
        await db.commit()
    with pytest.raises(EventRuleViolation, match=DOESNT_FIT):  # misses the on-time need
        await act(house, calls.lock, house["Alex"], event_id, veg_pick.id)
    with pytest.raises(EventRuleViolation, match=DOESNT_FIT):
        await act(house, calls.lock, house["Alex"], event_id, taqueria)
    assert (await info(house, event_id, gf_place)).warnings != ()  # misses only the late guest's
    await act(house, calls.lock, house["Alex"], event_id, gf_place)
    assert (await house.event(event_id)).state is EventState.DECIDED
