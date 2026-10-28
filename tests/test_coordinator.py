# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coordinator powers (#87): break a tie, shuffle the picks, and choose the mode and roles.

The rules first, in the domain; then each power against a migrated database with a fake clock:
who may use it, what it changes, who is told, what the logs and the audit say, and how it
treats a late joiner (FR-60) and what members keep private (FR-29)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import date, time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.blocks import Directory
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.config import Settings
from dinnerbell.db.events import ResponseRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.models import AssignmentRow, AuditRow, EventRow
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import Meal, Mode, TagKind
from dinnerbell.domain.events import (
    MAX_SHUFFLES,
    SHUFFLERS,
    STALE_CALL,
    TIE_BREAKERS,
    ActingAs,
    AssignmentRole,
    Details,
    Event,
    EventKind,
    EventRuleViolation,
    EventState,
    Proposal,
    ProposalSource,
    ProposalTally,
    RoleOffer,
    StaleRun,
    Status,
    Step,
    TieHow,
    check_can_break_tie,
    check_can_choose_roles,
    check_can_shuffle,
    choose_winner,
    decide_winner,
    due_step,
    roles_unclear,
    shuffle_candidates,
    tie_line,
    tied_leaders,
)
from dinnerbell.domain.guests import REFUSED, GuestAction
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.domain.notifications import from_payload
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.domain.ranking import rank, ranking_seed
from dinnerbell.logs import JsonFormatter
from dinnerbell.services import activity, calls, coordinator, event_views, guests, lifecycle, offers
from dinnerbell.services import call_context as cc
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import group_preferences, set_my_preference
from tests.conftest import invite, page
from tests.support.calls import OPENS, RESPOND_BY, VOTE_BY, House, RecordingChannel, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.fake_slack import CHANNEL, state
from tests.support.slack import Slack, wire
from tests.support.web import add_item, submit
from tests.test_auth_flow import member, rows
from tests.test_events_web import admin as admin
from tests.test_events_web import proposal_ids, ranking_form, sign_in_as, start
from tests.test_events_web import tick as web_tick

STALE = STALE_CALL
pytestmark = pytest.mark.anyio


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


async def to_voting(
    house: House,
    *eaters: str,
    pickups: tuple[str, ...] = ("Alex",),
    cooks: tuple[str, ...] = (),
    coordinator_name: str | None = "Alex",
) -> int:
    """``eaters`` are in; ``pickups`` offer to pick up; ``coordinator_name`` coordinates (or nobody).
    With the furnished catalog, Siam Garden and Taqueria are the picks."""
    event_id = await open_dinner(house)
    for name in dict.fromkeys(("Alex", *eaters)):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    for name in pickups:
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.PICKUP, True)
    for name in cooks:
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.COOK, True)
    if coordinator_name is not None:
        await act(house, calls.claim_coordinator, house[coordinator_name], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    assert (await house.event(event_id)).state is EventState.VOTING
    return event_id


async def pick_ids(house: House, event_id: int) -> dict[str, int]:
    ctx = await house.context(event_id)
    return {cc.item_info(ctx, p).name: p.id for p in sorted(ctx.proposals, key=lambda p: p.rank)}


async def vote(house: House, who: str, event_id: int, pick: int) -> None:
    await act(house, calls.vote, house[who], event_id, pick)


async def at_deadline(house: House, event_id: int) -> Event:
    house.clock.now = VOTE_BY
    await house.tick()
    event = await house.event(event_id)
    assert event.state is EventState.DECIDED
    return event


# The rules, in the domain -------------------------------------------------------------------------


def tally(proposal_id: int, rank: int, *voters: int, covered: bool = True, eaten: int | None = None) -> ProposalTally:
    return ProposalTally(proposal_id, rank, covered, frozenset(voters), days_since_eaten=eaten)


ADMINS = frozenset({50})


def test_most_votes_wins_without_a_tie() -> None:
    won = decide_winner([tally(1, 1, 7), tally(2, 2, 7, 8)], coordinator_id=None, admin_ids=ADMINS)
    assert won is not None and (won.tally.id, won.tie) == (2, None)
    assert decide_winner([tally(1, 1, 7, covered=False)], coordinator_id=None, admin_ids=ADMINS) is None


@pytest.mark.parametrize(
    ("tallies", "coordinator", "admins_next", "winner", "how", "by"),
    [
        # The coordinator's counted vote on a tied pick comes first, even over an admin's.
        ([tally(1, 1, 50), tally(2, 2, 7)], 7, True, 2, TieHow.COORDINATOR_VOTE, 7),
        # A coordinator who voted for none of them leaves it to the next rule: the admins only
        # break it with no coordinator, or a guest one.
        ([tally(1, 1, 50), tally(2, 2, 8)], 7, False, 1, TieHow.RANDOM, None),
        ([tally(1, 1, 50), tally(2, 2, 8)], 7, True, 1, TieHow.ADMIN_VOTE, 50),
        # No coordinator: the admin's vote.
        ([tally(1, 1, 8), tally(2, 2, 50)], None, False, 2, TieHow.ADMIN_VOTE, 50),
        # Then the least recently eaten: never eaten counts as the longest ago.
        ([tally(1, 1, 8, eaten=30), tally(2, 2, 9, eaten=10)], None, False, 1, TieHow.LEAST_RECENT, None),
        ([tally(1, 1, 8, eaten=30), tally(2, 2, 9, eaten=None)], None, False, 2, TieHow.LEAST_RECENT, None),
        ([tally(1, 1, 8, eaten=None), tally(2, 2, 9, eaten=400)], 7, False, 1, TieHow.LEAST_RECENT, None),
        # Then the stored rank, which is the draw.
        ([tally(1, 2, 8, eaten=5), tally(2, 1, 9, eaten=5)], 7, False, 2, TieHow.RANDOM, None),
        ([tally(1, 1, 8), tally(2, 2, 9)], None, False, 1, TieHow.RANDOM, None),
    ],
)
def test_the_tie_is_broken_in_order(
    tallies: list[ProposalTally], coordinator: int | None, admins_next: bool, winner: int, how: TieHow, by: int | None
) -> None:
    won = decide_winner(tallies, coordinator_id=coordinator, admin_ids=ADMINS, admins_next=admins_next)
    assert won is not None and (won.tally.id, won.tie, won.by) == (winner, how, by)
    chosen = choose_winner(tallies, coordinator_id=coordinator, admin_ids=ADMINS, admins_next=admins_next)
    assert chosen is not None and chosen.id == winner


def test_each_rule_only_narrows_the_tie_for_the_next() -> None:
    """The coordinator voted for two of three tied picks, so the third is out; of the two, the one
    eaten longest ago wins, and that is how the tie was broken."""
    tallies = [tally(1, 1, 7, eaten=3), tally(2, 2, 7, eaten=40), tally(3, 3, 8, eaten=900)]
    won = decide_winner(tallies, coordinator_id=7, admin_ids=ADMINS)
    assert won is not None and (won.tally.id, won.tie) == (2, TieHow.LEAST_RECENT)


def test_an_uncovered_pick_never_wins_a_tie() -> None:
    won = decide_winner([tally(1, 1, 7, covered=False), tally(2, 2, 8)], coordinator_id=7, admin_ids=ADMINS)
    assert won is not None and (won.tally.id, won.tie) == (2, None)


def test_tied_leaders_are_the_covered_picks_sharing_the_most_votes() -> None:
    tallies = [tally(1, 2, 7), tally(2, 1, 8), tally(3, 3, 7, 8), tally(4, 4, 9, covered=False)]
    assert tied_leaders(tallies) == []
    assert [t.id for t in tied_leaders([tally(1, 2, 7), tally(2, 1, 8), tally(3, 3)])] == [2, 1]
    assert tied_leaders([tally(1, 1, 7), tally(2, 2, 7, covered=False)]) == []


def test_how_a_tie_was_broken_is_said_in_words() -> None:
    assert tie_line(TieHow.CHOSE, "Kim", coordinator=True) == "Tied; Kim (coordinator) chose"
    assert tie_line(TieHow.CHOSE, "Alex") == "Tied; Alex chose"
    assert tie_line(TieHow.COORDINATOR_VOTE, "Kim", coordinator=True) == "Tied; Kim (coordinator) voted for it"
    assert tie_line(TieHow.COORDINATOR_VOTE, "Pat") == "Tied; Pat voted for it"  # the inviter of a guest coordinator
    assert tie_line(TieHow.ADMIN_VOTE, None) == "Tied; an admin voted for it"
    assert tie_line(TieHow.LEAST_RECENT, None) == "Tied; least recently eaten"
    assert tie_line(TieHow.RANDOM, None) == "Tied; drawn at random"


def proposal(proposal_id: int, *, source: ProposalSource = ProposalSource.SYSTEM, by: int | None = None) -> Proposal:
    return Proposal(proposal_id, 1, Mode.PICKUP, proposal_id, source, item_id=100 + proposal_id, proposed_by=by)


def test_a_shuffle_redraws_every_ranked_pick_until_someone_votes() -> None:
    picks = [proposal(1), proposal(2), proposal(3)]
    assert [p.id for p in shuffle_candidates(picks, {}, [])] == [1, 2, 3]
    assert [p.id for p in shuffle_candidates(picks, {2: [7]}, [])] == [1, 3]  # a vote keeps its pick
    assert [p.id for p in shuffle_candidates(picks, {1: [7], 2: [8], 3: [9]}, [])] == []


def test_a_shuffle_never_redraws_added_offer_or_suggested_picks() -> None:
    picks = [
        proposal(1),
        proposal(2, source=ProposalSource.MEMBER, by=7),  # a member added it
        proposal(3, source=ProposalSource.COORDINATOR, by=8),
        proposal(4, by=9),  # an offer's pick, or a ranked one an offer took over
        proposal(5),  # suggested
        proposal(6),
    ]
    assert [p.id for p in shuffle_candidates(picks, {}, [105])] == [1, 6]


def test_shuffle_zero_keeps_the_seed_every_call_has_had_and_each_shuffle_has_its_own() -> None:
    assert ranking_seed(1, 7, 1) == ranking_seed(1, 7, 1, 0) == 0x606586BC869DF272
    seeds = {ranking_seed(1, 7, 1, n) for n in range(5)}
    assert len(seeds) == 5 and ranking_seed(1, 7, 1, 2) == ranking_seed(1, 7, 1, 2)
    assert ranking_seed(1, 7, 2, 1) != ranking_seed(1, 7, 1, 1)  # a re-run draws again


def _event(
    state: EventState, *, coordinator_id: int | None = None, shuffles: int = 0, kind: EventKind = EventKind.CALL
) -> Event:
    return Event(
        1,
        1,
        kind,
        Slot.DINNER,
        date(2026, 10, 22),
        state,
        1,
        OPENS,
        RESPOND_BY,
        15,
        45,
        coordinator_id=coordinator_id,
        shuffles=shuffles,
    )


def _person(member_id: int, role: Role = Role.MEMBER, *, active: bool = True) -> Member:
    return Member(member_id, 1, f"P{member_id}", None, role, Capabilities(), active)


@pytest.mark.parametrize("check", [check_can_shuffle, check_can_break_tie])
def test_who_may_use_the_coordinators_picks_powers(check: Callable[[Event, Member, Member | None], ActingAs]) -> None:
    voting = _event(EventState.VOTING, coordinator_id=2)
    admin, guest = _person(1, Role.ADMIN), _person(9, Role.GUEST)
    assert check(voting, _person(2), _person(2)) is ActingAs.COORDINATOR
    assert check(_event(EventState.VOTING), admin, None) is ActingAs.ADMIN
    assert check(replace(voting, coordinator_id=9), admin, guest) is ActingAs.ADMIN  # choosing isn't a guest's (#38)
    for event, actor, coordinating, code in [
        (voting, admin, _person(2), "not_coordinator"),  # an admin doesn't override a coordinator
        (voting, _person(3), _person(2), "not_coordinator"),
        (_event(EventState.VOTING), _person(3), None, "not_coordinator"),
        (voting, _person(2, active=False), _person(2), "inactive"),
        (replace(voting, coordinator_id=9), guest, guest, "guests_dont"),
        (replace(voting, state=EventState.DECIDED), _person(2), _person(2), "voting_closed"),
        (replace(voting, state=EventState.COLLECTING), _person(2), _person(2), "voting_closed"),
        (_event(EventState.COLLECTING, kind=EventKind.OFFER), _person(2), None, "is_offer"),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            check(event, actor, coordinating)
        assert refused.value.code == code, (event.state, actor.id)


def test_the_shuffle_limit_is_three() -> None:
    voting = _event(EventState.VOTING, coordinator_id=2)
    for used in range(MAX_SHUFFLES):
        check_can_shuffle(replace(voting, shuffles=used), _person(2), _person(2))
    with pytest.raises(EventRuleViolation) as refused:
        check_can_shuffle(replace(voting, shuffles=MAX_SHUFFLES), _person(2), _person(2))
    assert refused.value.code == "shuffle_limit" and MAX_SHUFFLES == 3


def test_who_may_choose_roles() -> None:
    decided = _event(EventState.DECIDED, coordinator_id=2)
    check_can_choose_roles(decided, _person(2))  # the coordinator, a guest coordinator too (FR-58)
    check_can_choose_roles(replace(decided, coordinator_id=9), _person(9, Role.GUEST))
    check_can_choose_roles(decided, _person(1, Role.ADMIN))  # an admin can always
    for event, actor, code in [
        (decided, _person(3), "not_coordinator"),
        (replace(decided, state=EventState.VOTING), _person(2), "not_decided"),
        (replace(decided, state=EventState.DONE), _person(2), "not_decided"),
        (replace(decided, state=EventState.DONE), _person(1, Role.ADMIN), "not_decided"),
        (decided, _person(2, active=False), "inactive"),
    ]:
        with pytest.raises(EventRuleViolation) as refused:
            check_can_choose_roles(event, actor)
        assert refused.value.code == code


def test_a_role_has_a_clear_owner_only_with_exactly_one_volunteer() -> None:
    assert not roles_unclear((4,))
    assert roles_unclear(()) and roles_unclear((4, 5))


def test_the_automatic_assignment_is_due_when_the_coordinators_time_runs_out() -> None:
    decided = replace(_event(EventState.DECIDED, coordinator_id=2), meal_time=OPENS + timedelta(hours=4))
    cutoff = OPENS + timedelta(days=1)
    assert due_step(decided, OPENS, stale_after=cutoff) is None
    pending = replace(decided, roles_due_at=OPENS + timedelta(minutes=15))
    assert due_step(pending, OPENS + timedelta(minutes=14), stale_after=cutoff) is None
    assert due_step(pending, OPENS + timedelta(minutes=15), stale_after=cutoff) is Step.ASSIGN_ROLES


def test_guests_are_refused_the_picks_powers_with_words_to_show() -> None:
    assert "shuffle" in REFUSED[GuestAction.SHUFFLE] and "tie" in REFUSED[GuestAction.BREAK_TIE]


# Fixtures for the services -------------------------------------------------------------------------

DAY = date(2026, 10, 22)
CUISINES = ("Thai", "Mexican", "Italian", "Indian", "Greek", "Korean", "French", "Ethiopian", "Vietnamese")


async def stock(house: House, count: int) -> list[str]:
    """``count`` more places that can be picked up, one cuisine each, so the picks have choices."""
    names = [f"Place {n}" for n in range(count)]
    for n, name in enumerate(names):
        await house.add_item(name, Mode.PICKUP, cuisine=CUISINES[n % len(CUISINES)])
    return names


async def sent(house: House, kind: str) -> list[Any]:
    return [from_payload(e.kind, e.payload) for e in await house.outbox() if e.kind == kind]


async def add_guest(house: House, name: str) -> Member:

    async with house.sessionmaker() as db:
        guest = await MemberRepository(db).add_guest(
            household_id=house.household.id, display_name=name, dietary_needs=frozenset(), now=house.clock()
        )
        await db.commit()
    house.people[name] = guest
    return guest


def names(ctx: cc.CallContext) -> list[str]:
    return [cc.item_info(ctx, p).name for p in sorted(ctx.proposals, key=lambda p: p.rank)]


# Break the tie ---------------------------------------------------------------------------------------


async def test_the_coordinator_breaks_a_tie_without_having_voted(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    siam, taqueria = picks["Siam Garden"], picks["Taqueria"]
    await vote(house, "Alex", event_id, siam)
    await vote(house, "Lee", event_id, taqueria)
    ctx = await house.context(event_id)
    assert cc.tied_ids(ctx) == {siam, taqueria}
    assert {cc.proposal_info(ctx, p).tied for p in ctx.proposals} == {True}

    await act(house, coordinator.break_tie, house["Sam"], event_id, taqueria, run=1)
    event = await house.event(event_id)
    assert (event.state, event.tie_how, event.tie_by, event.locked_by) == (EventState.DECIDED, TieHow.CHOSE, 2, None)
    ctx = await house.context(event_id)
    assert ctx.winner is not None and ctx.winner.id == taqueria
    (made,) = await sent(house, "decision_made")
    assert made.decision.tie_note == "Tied; Sam (coordinator) chose" and made.locked_by is None
    audits = {
        a.action: json.loads(a.payload) if isinstance(a.payload, str) else a.payload for a in await house.audits()
    }
    assert audits["event.decided"]["how"] == "tie_break" and audits["event.decided"]["tie"] == "chose"


async def test_an_admin_breaks_the_tie_when_nobody_coordinates(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name=None)
    picks = await pick_ids(house, event_id)
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.break_tie, house["Sam"], event_id, picks["Siam Garden"])
    assert refused.value.code == "not_coordinator"
    await act(house, coordinator.break_tie, house["Alex"], event_id, picks["Siam Garden"])
    (made,) = await sent(house, "decision_made")
    assert made.decision.tie_note == "Tied; Alex chose"  # an admin standing in isn't "(coordinator)"


async def test_only_a_tied_pick_can_be_chosen_and_a_stale_choice_decides_nothing(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee")
    picks = await pick_ids(house, event_id)
    siam, taqueria = picks["Siam Garden"], picks["Taqueria"]
    with pytest.raises(EventRuleViolation) as refused:  # nobody has voted: that's a lock, not a tie
        await act(house, coordinator.break_tie, house["Alex"], event_id, siam)
    assert refused.value.code == "not_tied"
    await vote(house, "Sam", event_id, siam)
    await vote(house, "Lee", event_id, siam)
    await vote(house, "Alex", event_id, taqueria)
    with pytest.raises(EventRuleViolation) as refused:  # Siam leads, so it isn't tied
        await act(house, coordinator.break_tie, house["Alex"], event_id, taqueria)
    assert refused.value.code == "not_tied"
    await act(house, calls.unvote, house["Lee"], event_id, siam)
    with pytest.raises(StaleRun):
        await act(house, coordinator.break_tie, house["Alex"], event_id, taqueria, run=9)
    with pytest.raises(NotFound):
        await act(house, coordinator.break_tie, house["Alex"], event_id, 999_999)
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_break_the_tie_is_refused_to_everyone_else(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    await vote(house, "Alex", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    for who in ("Alex", "Lee", "Kim"):  # an admin doesn't override a coordinator
        with pytest.raises(EventRuleViolation) as refused:
            await act(house, coordinator.break_tie, house[who], event_id, picks["Siam Garden"])
        assert refused.value.code == "not_coordinator", who
    assert (await house.event(event_id)).state is EventState.VOTING


async def test_a_guest_coordinator_cannot_break_a_tie_but_an_admin_can(house: House) -> None:
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
    picks = await pick_ids(house, event_id)
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.break_tie, pat, event_id, picks["Siam Garden"])
    assert refused.value.code == "guests_dont"
    await act(house, coordinator.break_tie, house["Alex"], event_id, picks["Siam Garden"])
    assert (await house.event(event_id)).tie_how is TieHow.CHOSE


async def test_a_tie_is_not_broken_after_voting_closes(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee")
    picks = await pick_ids(house, event_id)
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    await act(house, calls.lock, house["Alex"], event_id, picks["Siam Garden"])
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.break_tie, house["Alex"], event_id, picks["Taqueria"])
    assert refused.value.code == "voting_closed"


# The deadline decides a tie in order ---------------------------------------------------------------


async def tied_at_the_deadline(
    house: House, *, coordinator_name: str | None, voting: dict[str, str]
) -> tuple[int, dict[str, int]]:
    """Alex, Sam, and Lee are in, and ``voting`` says who votes for which pick."""
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name=coordinator_name)
    picks = await pick_ids(house, event_id)
    for who, place in voting.items():
        await vote(house, who, event_id, picks[place])
    return event_id, picks


async def test_at_the_deadline_the_coordinators_vote_breaks_the_tie_first(house: House) -> None:
    event_id, picks = await tied_at_the_deadline(
        house, coordinator_name="Sam", voting={"Alex": "Siam Garden", "Sam": "Taqueria"}
    )
    event = await at_deadline(house, event_id)
    assert (event.tie_how, event.tie_by) == (TieHow.COORDINATOR_VOTE, house["Sam"].id)
    assert (await house.context(event_id)).winner.id == picks["Taqueria"]  # type: ignore[union-attr]
    (made,) = await sent(house, "decision_made")
    assert made.decision.tie_note == "Tied; Sam (coordinator) voted for it"


async def test_at_the_deadline_an_admins_vote_breaks_it_when_nobody_coordinates(house: House) -> None:
    event_id, picks = await tied_at_the_deadline(
        house, coordinator_name=None, voting={"Alex": "Taqueria", "Lee": "Siam Garden"}
    )
    event = await at_deadline(house, event_id)
    assert (event.tie_how, event.tie_by) == (TieHow.ADMIN_VOTE, house["Alex"].id)
    assert (await house.context(event_id)).winner.id == picks["Taqueria"]  # type: ignore[union-attr]


async def eat(house: House, place: str, *, days_ago: int) -> None:
    async with house.sessionmaker() as db:
        await MealLogRepository(db).add(
            household_id=house.household.id,
            day=DAY - timedelta(days=days_ago),
            slot=Slot.LUNCH,
            item_id=house.items[place].id,
            cuisines=(),
            participants=(),
            logged_by=None,
            now=house.clock(),
        )
        await db.commit()


async def test_at_the_deadline_the_least_recently_eaten_pick_wins_when_no_vote_breaks_it(house: House) -> None:
    await eat(house, "Taqueria", days_ago=20)
    await eat(house, "Siam Garden", days_ago=40)  # outside the 7-day variety window, so both are picks
    event_id, picks = await tied_at_the_deadline(
        house, coordinator_name="Sam", voting={"Alex": "Taqueria", "Lee": "Siam Garden"}
    )
    event = await at_deadline(house, event_id)
    assert (event.tie_how, event.tie_by) == (TieHow.LEAST_RECENT, None)
    assert (await house.context(event_id)).winner.id == picks["Siam Garden"]  # type: ignore[union-attr]
    (made,) = await sent(house, "decision_made")
    assert made.decision.tie_note == "Tied; least recently eaten"


async def test_a_pick_never_eaten_counts_as_the_least_recent(house: House) -> None:
    await eat(house, "Taqueria", days_ago=20)  # Siam Garden never has been
    event_id, picks = await tied_at_the_deadline(
        house, coordinator_name="Sam", voting={"Alex": "Taqueria", "Lee": "Siam Garden"}
    )
    event = await at_deadline(house, event_id)
    assert event.tie_how is TieHow.LEAST_RECENT
    assert (await house.context(event_id)).winner.id == picks["Siam Garden"]  # type: ignore[union-attr]


async def test_at_the_deadline_the_stored_rank_is_the_last_resort(house: House) -> None:
    event_id, _picks = await tied_at_the_deadline(
        house, coordinator_name="Sam", voting={"Alex": "Taqueria", "Lee": "Siam Garden"}
    )
    ctx = await house.context(event_id)
    first = min(ctx.proposals, key=lambda p: p.rank)
    event = await at_deadline(house, event_id)
    assert (event.tie_how, event.tie_by) == (TieHow.RANDOM, None)
    assert (await house.context(event_id)).winner.id == first.id  # type: ignore[union-attr]
    (made,) = await sent(house, "decision_made")
    assert made.decision.tie_note == "Tied; drawn at random"


async def test_no_tie_leaves_no_note(house: House) -> None:
    event_id, _ = await tied_at_the_deadline(
        house, coordinator_name="Sam", voting={"Alex": "Siam Garden", "Lee": "Siam Garden"}
    )
    event = await at_deadline(house, event_id)
    assert (event.tie_how, event.tie_by) == (None, None)
    (made,) = await sent(house, "decision_made")
    assert made.decision.tie_note is None


async def test_a_late_joiners_uncounted_vote_never_breaks_or_makes_a_tie(house: House) -> None:
    """Sam (the coordinator) and Lee split the vote. Kim joins late and votes for Taqueria; her vote
    shows but doesn't count, so the tie stays a tie and the coordinator's vote breaks it."""
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    await act(house, calls.vote, house["Kim"], event_id, picks["Taqueria"], join_late=True)
    ctx = await house.context(event_id)
    assert ctx.votes[picks["Taqueria"]] and len(ctx.counted(picks["Taqueria"])) == 1  # shown, not counted
    assert cc.tied_ids(ctx) == {picks["Siam Garden"], picks["Taqueria"]}
    event = await at_deadline(house, event_id)
    assert (event.tie_how, event.tie_by) == (TieHow.COORDINATOR_VOTE, house["Sam"].id)
    assert (await house.context(event_id)).winner.id == picks["Siam Garden"]  # type: ignore[union-attr]


async def test_a_late_coordinators_uncounted_vote_does_not_break_the_tie(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name=None)
    picks = await pick_ids(house, event_id)
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await vote(house, "Kim", event_id, picks["Taqueria"])
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    first = min((await house.context(event_id)).proposals, key=lambda p: p.rank)
    event = await at_deadline(house, event_id)
    assert event.tie_how is TieHow.RANDOM  # not her uncounted vote, and an admin only decides with no coordinator
    assert (await house.context(event_id)).winner.id == first.id  # type: ignore[union-attr]
    # With the coordinator counting her vote, it is the vote that breaks it.


async def test_a_late_coordinators_vote_breaks_the_tie_once_it_counts(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name=None)
    picks = await pick_ids(house, event_id)
    await act(house, calls.join_late, house["Kim"], event_id)
    await act(house, calls.claim_coordinator, house["Kim"], event_id)
    await act(house, calls.switch_late_joiner, house["Kim"], event_id, house["Kim"].id, vote_counts=True)
    await vote(house, "Kim", event_id, picks["Taqueria"])
    await vote(house, "Sam", event_id, picks["Siam Garden"])
    event = await at_deadline(house, event_id)
    assert (event.tie_how, event.tie_by) == (TieHow.COORDINATOR_VOTE, house["Kim"].id)
    assert (await house.context(event_id)).winner.id == picks["Taqueria"]  # type: ignore[union-attr]


# Choose the mode and assign the roles --------------------------------------------------------------

CTX = RenderContext("https://dinnerbell.example.com", Directory())


async def two_pickups_decided(house: House, *, coordinator_name: str | None = "Alex", lock: bool = False) -> int:
    """Alex and Sam both offered to pick up, so whoever wins has no clear owner. Lee is in and didn't
    offer anything. Siam Garden wins."""
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex", "Sam"), coordinator_name=coordinator_name)
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    if lock:
        await act(house, calls.lock, house["Alex"], event_id, siam)
    else:
        await vote(house, "Alex", event_id, siam)
        await at_deadline(house, event_id)
    return event_id


def assigned(ctx: cc.CallContext) -> dict[AssignmentRole, int]:
    return {a.role: a.member_id for a in ctx.assignments}


async def test_an_unclear_role_waits_for_the_coordinator_who_is_told(house: House) -> None:
    event_id = await two_pickups_decided(house)
    event = await house.event(event_id)
    ctx = await house.context(event_id)
    assert event.roles_due_at == VOTE_BY + timedelta(minutes=15)  # the default
    assert assigned(ctx) == {AssignmentRole.COORDINATOR: house["Alex"].id}  # nobody picks up yet
    decision = cc.decision_info(ctx)
    assert decision is not None and decision.assign_roles and decision.roles_due_at == event.roles_due_at

    (ask,) = await sent(house, "roles_to_choose")
    assert (ask.member.id, ask.recipient_id, ask.due_at) == (house["Alex"].id, house["Alex"].id, event.roles_due_at)
    assert await sent(house, "assignment_made") == []  # not the volunteers, and not Alex: nothing is settled
    assert "Assign roles" in json.dumps(render(ask, CTX).payload())
    (made,) = await sent(house, "decision_made")
    decision_post = json.dumps(render(made, CTX).payload())
    assert "Assign roles" in decision_post and "is choosing who picks up" in decision_post
    (decided,) = [a for a in await house.audits() if a.action == "event.decided"]
    assert "roles_pending" in str(decided.payload)


async def test_a_lock_leaves_an_unclear_role_to_the_coordinator_too(house: House) -> None:
    event_id = await two_pickups_decided(house, lock=True)
    assert (await house.event(event_id)).roles_due_at is not None
    assert len(await sent(house, "roles_to_choose")) == 1


async def test_a_clear_role_is_assigned_at_once_with_no_question(house: House) -> None:
    event_id = await to_voting(house, "Sam", pickups=("Alex",))
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    ctx = await house.context(event_id)
    assert (await house.event(event_id)).roles_due_at is None
    assert assigned(ctx) == {AssignmentRole.PICKUP: house["Alex"].id, AssignmentRole.COORDINATOR: house["Alex"].id}
    assert await sent(house, "roles_to_choose") == [] and len(await sent(house, "assignment_made")) == 1
    decision = cc.decision_info(ctx)
    assert decision is not None and not decision.assign_roles


async def test_the_coordinator_assigns_anyone_who_is_in_and_they_are_told_who_assigned_them(house: House) -> None:
    event_id = await two_pickups_decided(house)
    await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Lee"].id, run=1)
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.roles_due_at is None  # the choice is made
    assert assigned(ctx)[AssignmentRole.PICKUP] == house["Lee"].id
    (told,) = [n for n in await sent(house, "assignment_made") if n.member.id == house["Lee"].id]
    assert told.assigned_by is not None and told.assigned_by.name == "Alex" and told.recipient_id == house["Lee"].id
    text = json.dumps(render(told, CTX).payload())
    assert "Alex assigned you this" in text and "tell Alex" in text
    (audit,) = [a for a in await house.audits() if a.action == "event.roles_assigned"]
    assert "volunteered" in str(audit.payload)


async def test_a_volunteer_is_told_without_being_told_they_were_assigned(house: House) -> None:
    event_id = await two_pickups_decided(house)
    await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Sam"].id)
    (told,) = await sent(house, "assignment_made")
    assert told.member.id == house["Sam"].id and told.assigned_by is None


async def test_the_coordinator_can_take_the_role_themselves_and_is_told_it_once(house: House) -> None:
    event_id = await two_pickups_decided(house)
    await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Alex"].id)
    ctx = await house.context(event_id)
    assert assigned(ctx)[AssignmentRole.PICKUP] == house["Alex"].id
    (told,) = await sent(house, "assignment_made")
    assert told.member.id == house["Alex"].id and told.assigned_by is None
    assert set(told.roles) == {AssignmentRole.PICKUP, AssignmentRole.COORDINATOR}


async def test_without_a_member_the_first_volunteer_is_preselected(house: House) -> None:
    event_id = await two_pickups_decided(house)
    await act(house, coordinator.choose_roles, house["Alex"], event_id)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Alex"].id  # answered first


async def test_the_coordinator_switches_the_mode_of_a_place_that_allows_more_than_one(house: House) -> None:
    event_id = await two_pickups_decided(house)
    await act(house, coordinator.choose_roles, house["Alex"], event_id, mode=Mode.DELIVERY, member_id=house["Lee"].id)
    event, ctx = await house.event(event_id), await house.context(event_id)
    assert event.decided_mode is Mode.DELIVERY and event.roles_due_at is None
    assert ctx.winner is not None and ctx.winner.mode is Mode.DELIVERY  # the pick changed with the decision
    assert assigned(ctx) == {AssignmentRole.ORDER: house["Lee"].id, AssignmentRole.COORDINATOR: house["Alex"].id}
    info = cc.decision_info(ctx)
    assert info is not None and info.proposal.mode is Mode.DELIVERY
    assert ctx.event.decided_item_id == house.items["Siam Garden"].id


async def test_a_mode_the_place_does_not_allow_is_refused(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex", "Sam"))
    taqueria = (await pick_ids(house, event_id))["Taqueria"]  # pickup only
    await act(house, calls.lock, house["Alex"], event_id, taqueria)
    with pytest.raises(EventRuleViolation) as refused:
        await act(
            house, coordinator.choose_roles, house["Alex"], event_id, mode=Mode.DELIVERY, member_id=house["Lee"].id
        )
    assert refused.value.code == "unavailable"
    assert (await house.event(event_id)).decided_mode is Mode.PICKUP


async def test_only_someone_who_is_in_can_be_assigned(house: House) -> None:
    event_id = await two_pickups_decided(house)
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Kim"].id)
    assert refused.value.code == "not_in"
    with pytest.raises(NotFound):
        await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=999_999)
    assert (await house.event(event_id)).roles_due_at is not None  # still the coordinator's to choose


async def test_a_cook_must_be_on_the_cook_list(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), cooks=("Sam", "Lee"))
    picks = await pick_ids(house, event_id)
    if "Sam's Tacos" not in picks:
        added = await act(
            house, calls.add_proposal, house["Sam"], event_id, Mode.COOK, item_id=house.items["Sam's Tacos"].id
        )
        picks["Sam's Tacos"] = added.id
    await act(house, calls.lock, house["Alex"], event_id, picks["Sam's Tacos"])
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Lee"].id)
    assert refused.value.code == "cook_list"
    await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Sam"].id)
    assert assigned(await house.context(event_id))[AssignmentRole.COOK] == house["Sam"].id


async def test_the_automatic_assignment_stands_when_the_coordinator_does_not_act(house: House) -> None:
    event_id = await two_pickups_decided(house)
    decided = await house.event(event_id)
    assert decided.roles_due_at is not None
    house.clock.now = decided.roles_due_at - timedelta(seconds=1)
    await house.tick()
    assert assigned(await house.context(event_id)) == {AssignmentRole.COORDINATOR: house["Alex"].id}

    house.clock.now = decided.roles_due_at
    steps = await house.tick()
    assert Step.ASSIGN_ROLES in steps
    ctx = await house.context(event_id)
    assert (await house.event(event_id)).roles_due_at is None
    assert assigned(ctx)[AssignmentRole.PICKUP] == house["Alex"].id  # the first to offer, as it always was
    (told,) = await sent(house, "assignment_made")
    assert told.member.id == house["Alex"].id and told.assigned_by is None
    (audit,) = [a for a in await house.audits() if a.action == "event.roles_assigned"]
    assert "'by': None" in str(audit.payload) or '"by": null' in str(audit.payload)
    decision = cc.decision_info(ctx)
    assert decision is not None and not decision.assign_roles and decision.roles_due_at is None
    await house.tick()
    assert len(await sent(house, "assignment_made")) == 1  # once


async def test_a_coordinator_who_acts_in_time_is_not_overridden(house: House) -> None:
    event_id = await two_pickups_decided(house)
    due = (await house.event(event_id)).roles_due_at
    assert due is not None
    await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Lee"].id)
    house.clock.now = due + timedelta(minutes=1)
    assert Step.ASSIGN_ROLES not in await house.tick()
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id


async def test_the_households_time_to_choose_is_a_setting(house: House) -> None:
    async with house.sessionmaker() as db:
        stored = await household_settings(db, house.household.id)
        await HouseholdRepository(db).update_settings(house.household.id, stored | {"roles_choice_minutes": 40})
        await db.commit()
    event_id = await two_pickups_decided(house)
    assert (await house.event(event_id)).roles_due_at == VOTE_BY + timedelta(minutes=40)


async def test_the_old_way_of_assigning_ends_the_wait_too(house: House) -> None:
    event_id = await two_pickups_decided(house)
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, house["Sam"].id)
    assert (await house.event(event_id)).roles_due_at is None


async def test_with_no_coordinator_the_role_is_assigned_now_and_an_admin_can_choose(house: House) -> None:
    event_id = await two_pickups_decided(house, coordinator_name=None, lock=True)
    ctx = await house.context(event_id)
    assert (await house.event(event_id)).roles_due_at is None
    assert assigned(ctx) == {AssignmentRole.PICKUP: house["Alex"].id}  # the automatic assignment, as before
    assert await sent(house, "roles_to_choose") == []
    decision = cc.decision_info(ctx)
    assert decision is not None and decision.assign_roles  # Assign roles stays for an admin
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.choose_roles, house["Sam"], event_id, member_id=house["Sam"].id)
    assert refused.value.code == "not_coordinator"
    await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Lee"].id)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id


async def test_only_the_coordinator_and_admins_choose_roles(house: House) -> None:
    event_id = await two_pickups_decided(house, coordinator_name="Sam")
    for who in ("Lee", "Kim"):
        with pytest.raises(EventRuleViolation) as refused:
            await act(house, coordinator.choose_roles, house[who], event_id, member_id=house["Lee"].id)
        assert refused.value.code == "not_coordinator"
    with pytest.raises(StaleRun):
        await act(house, coordinator.choose_roles, house["Sam"], event_id, member_id=house["Lee"].id, run=2)
    await act(house, coordinator.choose_roles, house["Sam"], event_id, member_id=house["Lee"].id, run=1)


async def test_a_guest_coordinator_chooses_roles_and_gets_the_question_through_whoever_invited_them(
    house: House,
) -> None:
    pat = await add_guest(house, "Pat")
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    for name in ("Alex", "Sam"):
        await act(house, calls.offer_role, house[name], event_id, RoleOffer.PICKUP, True)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    await act(house, calls.lock, house["Alex"], event_id, (await pick_ids(house, event_id))["Siam Garden"])
    (ask,) = await sent(house, "roles_to_choose")
    assert (ask.member.id, ask.recipient_id) == (pat.id, house["Sam"].id)  # an unregistered guest: their inviter
    view = await act(house, event_views.event_view, pat, event_id)
    assert view.can.choose_roles and view.roles is not None
    await act(house, coordinator.choose_roles, pat, event_id, member_id=house["Lee"].id)
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id


async def test_roles_cannot_be_chosen_before_the_decision_or_after_the_meal(house: House) -> None:
    event_id = await to_voting(house, "Sam", pickups=("Alex", "Sam"))
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Sam"].id)
    assert refused.value.code == "not_decided"
    await act(house, calls.lock, house["Alex"], event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await act(house, calls.mark_done, house["Alex"], event_id)  # an admin can at any time
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.choose_roles, house["Alex"], event_id, member_id=house["Sam"].id)
    assert refused.value.code == "not_decided"


async def test_a_call_off_closes_the_coordinators_question_unsent(house: House) -> None:
    event_id = await two_pickups_decided(house)
    assert len(await sent(house, "roles_to_choose")) == 1
    await act(house, calls.cancel_call, house["Alex"], event_id)
    channel = RecordingChannel()
    await house.drain(channel)
    assert "roles_to_choose" not in channel.kinds  # "Tap Assign roles" must never arrive after "It's called off"


async def test_a_rerun_clears_everything_the_coordinator_set(house: House) -> None:
    from dinnerbell.services import call_admin

    event_id = await two_pickups_decided(house)
    assert (await house.event(event_id)).roles_due_at is not None
    await act(house, call_admin.rerun, house["Alex"], event_id)
    fresh = await house.event(event_id)
    assert (fresh.roles_due_at, fresh.tie_how, fresh.shuffles, fresh.shuffle_note) == (None, None, 0, None)


# Shuffle the picks -----------------------------------------------------------------------------------


async def shuffle(house: House, who: str, event_id: int, **kwargs: Any) -> coordinator.Shuffled:
    result: coordinator.Shuffled = await act(house, coordinator.shuffle, house[who], event_id, **kwargs)
    return result


async def with_many_places(house: House, *eaters: str, **kwargs: Any) -> int:
    """Eleven places that can be picked up, so a shuffle has somewhere to go."""
    await stock(house, 9)
    return await to_voting(house, *eaters, **kwargs)


async def test_a_shuffle_with_no_votes_redraws_every_ranked_pick(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    ctx = await house.context(event_id)
    before = names(ctx)
    assert len(before) == 3

    result = await shuffle(house, "Alex", event_id, run=1)
    after = await house.context(event_id)
    assert (result.replaced, result.kept, result.shuffles_left) == (3, 0, 2)
    assert len(names(after)) == 3 and set(names(after)).isdisjoint(before)  # never one just shuffled out
    assert len({p.item_id for p in after.proposals}) == 3  # a place is one pick (#77)
    assert (after.event.shuffles, after.event.shuffle_note) == (1, "Alex shuffled the picks (3 replaced).")
    assert all(p.source is ProposalSource.SYSTEM for p in after.proposals)


async def test_the_next_draw_is_the_seeded_one_for_that_shuffle(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    ctx = await house.context(event_id)
    out = cc.shuffle_picks(ctx)
    async with house.sessionmaker() as db:
        group = await group_preferences(db, house.household.id, ctx.veto_people)
        taken = frozenset(p.item_id for p in out if p.item_id is not None)
        snapshot = await lifecycle.ranking_snapshot(db, ctx, group, taken=taken, shuffle=1)
    assert (
        snapshot.seed
        == ranking_seed(house.household.id, event_id, 1, 1)
        != ranking_seed(house.household.id, event_id, 1)
    )
    expected = [r.item_id for r in rank(replace(snapshot, limit=3))]
    assert expected == [r.item_id for r in rank(replace(snapshot, limit=3))]  # the same counter, the same draw

    await shuffle(house, "Alex", event_id)
    after = await house.context(event_id)
    assert [p.item_id for p in sorted(after.proposals, key=lambda p: p.rank)] == expected


async def test_a_pick_with_a_vote_stays_and_the_others_are_redrawn(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    ctx = await house.context(event_id)
    voted, *rest = sorted(ctx.proposals, key=lambda p: p.rank)
    await vote(house, "Sam", event_id, voted.id)
    result = await shuffle(house, "Alex", event_id)
    after = await house.context(event_id)
    assert (result.replaced, result.kept) == (2, 1)
    assert voted.id in {p.id for p in after.proposals}  # the same row, with its vote
    assert after.votes[voted.id] == [house["Sam"].id]
    assert {p.item_id for p in rest}.isdisjoint({p.item_id for p in after.proposals})  # row IDs can be reused
    assert set(names(after)).isdisjoint({cc.item_info(ctx, p).name for p in rest})
    assert sorted(p.rank for p in after.proposals) == sorted(p.rank for p in ctx.proposals)  # same places in the order


async def test_a_late_joiners_uncounted_vote_still_keeps_its_pick(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    ctx = await house.context(event_id)
    voted = min(ctx.proposals, key=lambda p: p.rank)
    await act(house, calls.vote, house["Kim"], event_id, voted.id, join_late=True)
    before = await house.context(event_id)
    assert before.counted(voted.id) == frozenset() and before.votes[voted.id]  # shown, not counted
    result = await shuffle(house, "Alex", event_id)
    assert result.replaced == 2 and voted.id in {p.id for p in (await house.context(event_id)).proposals}


async def test_added_picks_stay_and_so_do_picks_a_member_added_after_the_draw(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    ctx = await house.context(event_id)
    free = next(
        i for n, i in house.items.items() if n.startswith("Place") and i.id not in {p.item_id for p in ctx.proposals}
    )
    added = await act(house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, item_id=free.id)
    result = await shuffle(house, "Alex", event_id)
    after = await house.context(event_id)
    assert result.replaced == 3 and added.id in {p.id for p in after.proposals}
    assert len(after.proposals) == 4 and not any(p.id in {q.id for q in ctx.proposals} for p in after.proposals)


async def test_suggested_picks_stay(house: House) -> None:
    await stock(house, 9)
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    wanted = house.items["Place 4"]
    await act(house, calls.save_details, house["Sam"], event_id, Details(suggestion_item_ids=(wanted.id,)))
    await act(house, calls.claim_coordinator, house["Alex"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    before = await house.context(event_id)
    assert "Place 4" in names(before)  # a suggestion is always proposed (#83)
    result = await shuffle(house, "Alex", event_id)
    after = await house.context(event_id)
    assert result.replaced == 2 and "Place 4" in names(after)
    assert (
        next(p for p in after.proposals if p.item_id == wanted.id).id
        == next(p for p in before.proposals if p.item_id == wanted.id).id
    )


async def test_offer_picks_stay(house: House) -> None:
    await stock(house, 9)
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(
        house,
        offers.post_offer,
        house["Kim"],
        offers.OfferInput(
            slot=Slot.DINNER,
            day=DAY,
            mode=Mode.PICKUP,
            at=time(18, 30),
            capacity=3,
            item_id=house.items["Place 6"].id,
        ),
    )
    await act(house, calls.claim_coordinator, house["Alex"], event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    before = await house.context(event_id)
    offered = next(p for p in before.proposals if p.proposed_by == house["Kim"].id)
    result = await shuffle(house, "Alex", event_id)
    after = await house.context(event_id)
    assert offered.id in {p.id for p in after.proposals} and result.replaced == len(before.proposals) - 1


async def test_a_shuffle_never_duplicates_a_place_over_all_its_shuffles(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    seen: list[list[str]] = []
    for _ in range(MAX_SHUFFLES):
        await shuffle(house, "Alex", event_id)
        picks = names(await house.context(event_id))
        assert len(picks) == len(set(picks)) == 3
        seen.append(picks)
    assert len({tuple(p) for p in seen}) == MAX_SHUFFLES


async def test_a_shuffle_holds_to_every_hard_filter(house: House) -> None:
    """Kim vetoes Thai and is in; nobody offered to cook; Breakfast Burrito is for breakfast; and
    Delivery Only can't be fetched. None of them is ever drawn, whichever shuffle it is."""
    await stock(house, 9)  # Siam Garden and Place 0 are Thai
    await house.add_item("Casserole", Mode.COOK, cuisine="Home")
    await house.add_item("Breakfast Burrito", Mode.PICKUP, cuisine="Mexican", meals=frozenset({Meal.BREAKFAST}))
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, house.tags["Thai"].id, Stance(veto=True))
    event_id = await to_voting(house, "Sam", "Kim")
    thai = {house.items[n].id for n in ("Siam Garden", "Place 0")}
    forbidden = thai | {house.items["Casserole"].id, house.items["Breakfast Burrito"].id}
    assert not forbidden & {p.item_id for p in (await house.context(event_id)).proposals}
    for _ in range(MAX_SHUFFLES):
        await shuffle(house, "Alex", event_id)
        assert not forbidden & {p.item_id for p in (await house.context(event_id)).proposals}


async def test_a_late_joiners_guest_needs_filter_the_redraw_but_their_stances_do_not(house: House) -> None:
    await house.tag("Vegetarian", TagKind.DIETARY)
    for n in range(6):
        await house.add_item(f"Green {n}", Mode.PICKUP, cuisine=CUISINES[n], tags=("Vegetarian",))
    await stock(house, 6)  # none of them vegetarian
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, house.tags["Thai"].id, Stance(veto=True))
    event_id = await to_voting(house, "Sam")
    await act(house, calls.join_late, house["Kim"], event_id)
    kim = (await house.context(event_id)).response_of(house["Kim"].id)
    assert kim is not None
    async with house.sessionmaker() as db:  # details are closed while voting: save the answer as a restored one is
        await ResponseRepository(db).save(
            house.household.id,
            event_id,
            replace(kim, guests=1, guest_needs=frozenset({house.tags["Vegetarian"].id})),
            house.clock(),
        )
        await db.commit()
    ctx = await house.context(event_id)
    assert house["Kim"].id not in ctx.veto_people  # her stances never filter (FR-60)
    for _ in range(MAX_SHUFFLES):
        await shuffle(house, "Alex", event_id)
        ctx = await house.context(event_id)
        veg = house.tags["Vegetarian"].id
        assert all(veg in ctx.items[p.item_id].tag_ids for p in ctx.proposals if p.item_id), names(ctx)  # type: ignore[index]


async def test_at_most_three_shuffles_a_call(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    lefts = [(await shuffle(house, "Alex", event_id)).shuffles_left for _ in range(MAX_SHUFFLES)]
    assert lefts == [2, 1, 0]
    ctx = await house.context(event_id)
    assert ctx.event.shuffles == 3 and cc.shuffles_left(ctx) == 0
    with pytest.raises(EventRuleViolation) as refused:
        await shuffle(house, "Alex", event_id)
    assert refused.value.code == "shuffle_limit" and (await house.event(event_id)).shuffles == 3
    updated = cc.proposals_updated(ctx)
    assert updated.shuffles_left == 0
    assert "Shuffle picks" not in json.dumps(render(updated, CTX).payload())  # the button is gone
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    assert not view.can.shuffle


async def test_the_button_counts_down_and_everyone_sees_the_note_but_nobody_is_mentioned(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    ctx = await house.context(event_id)
    assert json.dumps(render(cc.proposals_updated(ctx), CTX).payload()).count("Shuffle picks") == 1
    await shuffle(house, "Alex", event_id)
    updates = await sent(house, "proposals_updated")
    assert updates[-1].event.shuffle_note == "Alex shuffled the picks (3 replaced)." and updates[-1].shuffles_left == 2
    rendered = render(
        updates[-1], RenderContext("https://dinnerbell.example.com", Directory({"Alex": "U01ALEX"}))
    ).payload()
    note_block = next(b for b in rendered["blocks"] if "Alex shuffled the picks" in json.dumps(b))
    assert "<@" not in json.dumps(note_block)  # plain names: an edit adds no mention (#92)
    (said,) = await sent(house, "picks_shuffled")  # and a short note in the call's thread
    assert (said.by.name, said.replaced, said.count) == ("Alex", 3, 1)
    assert said.thread_key == f"event:{event_id}:run1:call" and said.recipient_id is None
    assert "<@" not in json.dumps(
        render(said, RenderContext("https://dinnerbell.example.com", Directory({"Alex": "U01ALEX"}))).payload()
    )
    # Anyone with the proposals open on the web sees the new set on refresh.
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    assert [p.info.item.name for p in view.proposals] == names(await house.context(event_id))
    assert view.info.shuffle_note == "Alex shuffled the picks (3 replaced)."


async def test_the_note_stays_on_later_edits_and_goes_when_voting_closes(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    await shuffle(house, "Alex", event_id)
    first = (await house.context(event_id)).proposals[0]
    await vote(house, "Sam", event_id, first.id)  # a vote edits the picks message again
    assert "shuffled the picks" in json.dumps(render((await sent(house, "proposals_updated"))[-1], CTX).payload())
    await at_deadline(house, event_id)
    assert "shuffled the picks" not in json.dumps(render((await sent(house, "proposals_updated"))[-1], CTX).payload())


async def test_a_shuffle_that_would_change_nothing_is_refused_and_not_counted(house: House) -> None:
    """Siam Garden and Taqueria are the only places that can be fetched: nothing else fits."""
    event_id = await to_voting(house, "Sam")
    before = names(await house.context(event_id))
    with pytest.raises(EventRuleViolation) as refused:
        await shuffle(house, "Alex", event_id)
    assert refused.value.code == "nothing_else_fits"
    ctx = await house.context(event_id)
    assert (names(ctx), ctx.event.shuffles) == (before, 0)
    assert await sent(house, "picks_shuffled") == []


async def test_with_every_pick_voted_for_there_is_nothing_to_shuffle(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    for pick in (await house.context(event_id)).proposals:
        await vote(house, "Sam", event_id, pick.id)
    with pytest.raises(EventRuleViolation) as refused:
        await shuffle(house, "Alex", event_id)
    assert refused.value.code == "nothing_to_shuffle" and (await house.event(event_id)).shuffles == 0
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    assert not view.can.shuffle  # the button isn't offered


async def test_the_places_just_drawn_away_come_back_only_when_nothing_else_fits(house: House) -> None:
    """Four places can be fetched and three are picks: the fourth is the only new one, so one pick
    is replaced and the others come back from those just shuffled out."""
    await stock(house, 2)  # Siam Garden, Taqueria, Place 0, Place 1
    event_id = await to_voting(house, "Sam")
    before = await house.context(event_id)
    assert len(before.proposals) == 3
    unpicked = ({"Siam Garden", "Taqueria", "Place 0", "Place 1"} - set(names(before))).pop()
    result = await shuffle(house, "Alex", event_id)
    after = await house.context(event_id)
    assert (result.replaced, len(after.proposals)) == (1, 3)
    assert unpicked in names(after) and len(set(names(after)) & set(names(before))) == 2


async def test_who_may_shuffle(house: House) -> None:
    event_id = await with_many_places(house, "Sam", "Lee", coordinator_name="Sam")
    for who in ("Lee", "Alex", "Kim"):  # an admin doesn't override a coordinator
        with pytest.raises(EventRuleViolation) as refused:
            await shuffle(house, who, event_id)
        assert refused.value.code == "not_coordinator", who
        view = await act(house, event_views.event_view, house[who], event_id)
        assert not view.can.shuffle and not view.can.break_tie
    with pytest.raises(StaleRun):
        await shuffle(house, "Sam", event_id, run=3)
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    assert view.can.shuffle and view.shuffles_left == 3
    await shuffle(house, "Sam", event_id, run=1)
    assert (await house.event(event_id)).shuffles == 1


async def test_an_admin_shuffles_when_nobody_coordinates_or_a_guest_does(house: House) -> None:
    event_id = await with_many_places(house, "Sam", coordinator_name=None)
    with pytest.raises(EventRuleViolation) as refused:
        await shuffle(house, "Sam", event_id)
    assert refused.value.code == "not_coordinator"
    await shuffle(house, "Alex", event_id)
    (audit,) = [a for a in await house.audits() if a.action == "event.shuffled"]
    assert "'as': 'admin'" in str(audit.payload) or '"as": "admin"' in str(audit.payload)


async def test_a_guest_coordinator_cannot_shuffle_but_an_admin_can(house: House) -> None:
    pat = await add_guest(house, "Pat")
    await stock(house, 9)
    event_id = await open_dinner(house)
    for name in ("Alex", "Sam"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Alex"], event_id, RoleOffer.PICKUP, True)
    await act(house, guests.invite, house["Sam"], event_id, guest_id=pat.id)
    await act(house, calls.respond, pat, event_id, Status.IN)
    await act(house, calls.claim_coordinator, pat, event_id)
    house.clock.now = RESPOND_BY
    await house.tick()
    with pytest.raises(EventRuleViolation) as refused:
        await act(house, coordinator.shuffle, pat, event_id)
    assert refused.value.code == "guests_dont"
    await shuffle(house, "Alex", event_id)


async def test_no_shuffling_once_voting_has_closed(house: House) -> None:
    event_id = await with_many_places(house, "Sam")
    await at_deadline(house, event_id)
    with pytest.raises(EventRuleViolation) as refused:
        await shuffle(house, "Alex", event_id)
    assert refused.value.code == "voting_closed"


async def test_a_shuffle_is_audited_and_logged_with_its_counts(house: House, caplog: pytest.LogCaptureFixture) -> None:
    event_id = await with_many_places(house, "Sam")
    first = min((await house.context(event_id)).proposals, key=lambda p: p.rank)
    await vote(house, "Sam", event_id, first.id)
    caplog.set_level(logging.INFO)
    caplog.clear()
    await shuffle(house, "Alex", event_id)
    (audit,) = [a for a in await house.audits() if a.action == "event.shuffled"]
    payload = audit.payload if isinstance(audit.payload, dict) else json.loads(audit.payload)
    assert payload == {"replaced": 2, "kept": 1, "shuffle": 1, "voted": True, "as": "coordinator"}
    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == activity.log.name]
    (line,) = [line for line in lines if line["msg"] == "shuffle"]
    assert (line["replaced"], line["kept"], line["shuffle"], line["voted"]) == (2, 1, 1, True)
    assert (line["event_id"], line["member"], line["capacity"]) == (event_id, "Alex", "coordinator")
    assert line["state_before"] == line["state_after"] == "voting"


async def test_refusals_say_why_with_reason_codes_in_the_log(house: House, caplog: pytest.LogCaptureFixture) -> None:
    event_id = await with_many_places(house, "Sam", coordinator_name="Sam")
    caplog.set_level(logging.INFO)
    for who in ("Lee", "Alex"):
        try:
            await shuffle(house, who, event_id)
        except EventRuleViolation as exc:
            activity.refused(exc, str(exc), house[who])
    reasons = [r.reason for r in caplog.records if getattr(r, "reason", None)]  # type: ignore[attr-defined]
    assert reasons == ["not_coordinator", "not_coordinator"]


# What stays private (FR-29) --------------------------------------------------------------------------


async def test_what_a_shuffle_says_never_reveals_a_veto(house: House) -> None:
    """Kim vetoed Taqueria, so nothing else can be fetched. The refusal is the same words it would be
    for any other reason nothing fits, and names no stance and no one."""
    await act(house, set_my_preference, house["Kim"], TargetType.ITEM, house.items["Taqueria"].id, Stance(veto=True))
    event_id = await to_voting(house, "Sam", "Kim")
    with pytest.raises(EventRuleViolation) as refused:
        await shuffle(house, "Alex", event_id)
    assert str(refused.value) == coordinator.NOTHING_ELSE_FITS
    assert not {"veto", "kim", "taqueria", "prefer"} & set(coordinator.NOTHING_ELSE_FITS.lower().split())


async def test_a_shuffle_with_a_veto_in_play_sends_nothing_that_names_it(house: House) -> None:
    await stock(house, 9)
    await act(house, set_my_preference, house["Kim"], TargetType.TAG, house.tags["Thai"].id, Stance(veto=True))
    event_id = await to_voting(house, "Sam", "Kim")
    await shuffle(house, "Alex", event_id)
    (said,) = await sent(house, "picks_shuffled")
    text = json.dumps(note_payload(said)).lower()
    assert "veto" not in text and "kim" not in text and "thai" not in text
    (update,) = [u for u in await sent(house, "proposals_updated") if u.event.shuffle_note]
    assert update.event.shuffle_note == "Alex shuffled the picks (3 replaced)."
    assert all(not p.warnings and p.voters == () for p in update.proposals)  # nothing about anyone's stance


def note_payload(notification: Any) -> dict[str, Any]:
    return note.to_payload(notification)


# The web ---------------------------------------------------------------------------------------------


def web_voting(admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings) -> int:
    """Alex (admin, nobody coordinating) and Kim are in, both can pick up, and there are places to
    shuffle to. Returns the event with its picks drawn."""
    for n in range(8):
        add_item(admin, f"Place {n}", cuisines=CUISINES[n % len(CUISINES)])
    invite(admin, "kim@example.com", "Kim")
    event_id = start(admin)
    for who in ("admin", "kim@example.com"):
        if who != "admin":
            sign_in_as(admin, provider, who)
        assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
        assert submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    admin.cookies.clear()
    from tests.conftest import sign_in

    sign_in(admin, provider)
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    return event_id


def test_web_shuffle(admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings) -> None:
    event_id = web_voting(admin, provider, clock, migrated)
    before = proposal_ids(migrated)
    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="/events/{event_id}/shuffle"' in html and "Shuffle picks" in html and "3 left" in html

    assert submit(admin, f"/events/{event_id}/shuffle", {}).status_code == 400  # it names the run
    stale = submit(admin, f"/events/{event_id}/shuffle", {"run": "9"})
    assert stale.status_code == 409 and "can't happen anymore" in page(stale)
    assert proposal_ids(migrated) == before
    done = submit(admin, f"/events/{event_id}/shuffle", {"run": "1"})
    assert done.status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert "shuffled the picks (3 replaced)." in html and "2 left" in html
    (row,) = list(rows(migrated, EventRow))
    assert row.shuffles == 1
    assert [a.action for a in rows(migrated, AuditRow) if a.action == "event.shuffled"] == ["event.shuffled"]

    sign_in_as(admin, provider, "kim@example.com")  # not the coordinator, and not an admin
    html = page(admin.get(f"/events/{event_id}"))
    assert "Shuffle picks" not in html and "shuffled the picks (3 replaced)." in html  # everyone sees the note
    refused = submit(admin, f"/events/{event_id}/shuffle", {"run": "1"})
    assert refused.status_code == 409 and "Only the coordinator" in page(refused)
    assert rows(migrated, EventRow)[0].shuffles == 1


def test_web_break_the_tie(admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings) -> None:
    event_id = web_voting(admin, provider, clock, migrated)
    first, second, _third = proposal_ids(migrated)
    assert "Break the tie" not in page(admin.get(f"/events/{event_id}"))
    assert submit(admin, f"/events/{event_id}/proposals/{first}/vote", {}).status_code == 303
    sign_in_as(admin, provider, "kim@example.com")
    assert submit(admin, f"/events/{event_id}/proposals/{second}/vote", {}).status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert "Break the tie" not in html  # she isn't the coordinator
    refused = submit(admin, f"/events/{event_id}/proposals/{second}/break-tie", {"run": "1"})
    assert refused.status_code == 409 and "Only the coordinator" in page(refused)

    admin.cookies.clear()
    from tests.conftest import sign_in

    sign_in(admin, provider)  # an admin, with nobody coordinating
    html = page(admin.get(f"/events/{event_id}"))
    assert html.count("Break the tie: this one") == 2 and "Tied for the most votes" in html
    third = proposal_ids(migrated)[2]
    assert submit(admin, f"/events/{event_id}/proposals/{third}/break-tie", {"run": "1"}).status_code == 409  # not tied
    assert submit(admin, f"/events/{event_id}/proposals/{second}/break-tie", {}).status_code == 400
    assert submit(admin, f"/events/{event_id}/proposals/{second}/break-tie", {"run": "1"}).status_code == 303
    (row,) = rows(migrated, EventRow)
    assert (row.state, row.tie_how) == ("decided", "chose") and row.locked_by is None
    html = page(admin.get(f"/events/{event_id}"))
    assert "Tied; " in html and "chose." in html


def test_web_assign_roles(admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings) -> None:
    event_id = web_voting(admin, provider, clock, migrated)
    first, *_ = proposal_ids(migrated)
    assert submit(admin, f"/events/{event_id}/proposals/{first}/lock", {}).status_code == 303
    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="/events/{event_id}/roles"' in html and '<summary class="small">Assign roles</summary>' in html
    kim = member(migrated, "kim@example.com")
    assert submit(admin, f"/events/{event_id}/roles", {"mode": "pickup", "member_id": str(kim.id)}).status_code == 400
    bad = submit(admin, f"/events/{event_id}/roles", {"mode": "teleport", "member_id": str(kim.id), "run": "1"})
    assert bad.status_code == 409 and "Pick how to get it" in page(bad)
    assert (
        submit(admin, f"/events/{event_id}/roles", {"mode": "pickup", "member_id": str(kim.id), "run": "1"}).status_code
        == 303
    )
    assert [(a.role, a.member_id) for a in rows(migrated, AssignmentRow) if a.role == "pickup"] == [("pickup", kim.id)]
    assert "Picking up: Kim" in page(admin.get(f"/events/{event_id}"))

    sign_in_as(admin, provider, "kim@example.com")
    assert f"/events/{event_id}/roles" not in page(admin.get(f"/events/{event_id}"))  # not hers to change
    refused = submit(admin, f"/events/{event_id}/roles", {"mode": "pickup", "member_id": str(kim.id), "run": "1"})
    assert refused.status_code == 409 and "Only the coordinator or an admin" in page(refused)


def test_the_time_to_choose_roles_is_set_on_admin_ranking(admin: TestClient, migrated: Settings) -> None:
    from dinnerbell.db.models import HouseholdRow

    html = admin.get("/admin/schedule").text
    assert 'name="roles_choice_minutes" value="15"' in html
    bad = submit(admin, "/admin/schedule", {**ranking_form(), "roles_choice_minutes": "0"})
    assert bad.status_code == 400 and "time to choose roles runs 1 to 120" in page(bad)
    assert submit(admin, "/admin/schedule", ranking_form(roles_choice_minutes="40")).status_code == 303
    assert rows(migrated, HouseholdRow)[0].settings["roles_choice_minutes"] == 40
    assert 'name="roles_choice_minutes" value="40"' in admin.get("/admin/schedule").text


# Slack -----------------------------------------------------------------------------------------------


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def test_slack_shuffle_is_the_coordinators_and_says_what_it_did(slack: Slack, house: House) -> None:
    await stock(house, 9)
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    before = names(await house.context(event_id))
    value = ids.card_value(event_id, 1)
    await slack.click("Lee", ids.SHUFFLE, value)
    assert slack.ephemerals("Lee")[-1] == SHUFFLERS
    await slack.click("Alex", ids.SHUFFLE, value)  # an admin doesn't override a coordinator
    assert slack.ephemerals("Alex")[-1] == SHUFFLERS
    await slack.click("Sam", ids.SHUFFLE, ids.card_value(event_id, 2))  # a message from an earlier run
    assert slack.ephemerals("Sam")[-1] == STALE
    assert names(await house.context(event_id)) == before

    await slack.click("Sam", ids.SHUFFLE, value)
    assert slack.ephemerals("Sam")[-1] == "Shuffled: 3 replaced. Everyone sees the new set."
    after = await house.context(event_id)
    assert set(names(after)).isdisjoint(before) and after.event.shuffle_note == "Sam shuffled the picks (3 replaced)."
    await slack.click("Sam", ids.SHUFFLE, "not-a-value")
    assert "out of date" in slack.ephemerals("Sam")[-1]
    await slack.drain()
    posted = [c for c in slack.fake.calls if c.method in {"chat.postMessage", "chat.update"}]
    texts = json.dumps([c.args.get("blocks") for c in posted])
    assert "Sam shuffled the picks (3 replaced)." in texts and "Shuffle picks" in texts


async def test_slack_break_the_tie_is_the_coordinators(slack: Slack, house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    picks = await pick_ids(house, event_id)
    await vote(house, "Alex", event_id, picks["Siam Garden"])
    await vote(house, "Lee", event_id, picks["Taqueria"])
    value = ids.proposal_value(event_id, picks["Taqueria"])
    await slack.click("Lee", ids.BREAK_TIE, value)
    assert slack.ephemerals("Lee")[-1] == TIE_BREAKERS
    assert (await house.event(event_id)).state is EventState.VOTING
    await slack.click("Sam", ids.BREAK_TIE, value)
    event = await house.event(event_id)
    assert (event.state, event.tie_how, event.tie_by) == (EventState.DECIDED, TieHow.CHOSE, house["Sam"].id)
    await slack.drain()
    decision = [c for c in slack.fake.of("chat.postMessage") if "Tied; Sam (coordinator) chose" in json.dumps(c.args)]
    assert decision, "the decision message says how the tie was broken"


async def test_slack_assign_roles_opens_a_modal_for_the_coordinator_and_saves_the_choice(
    slack: Slack, house: House
) -> None:
    event_id = await two_pickups_decided(house)
    value = ids.card_value(event_id, 1)
    await slack.click("Lee", ids.ASSIGN_ROLES, value)
    assert not slack.fake.of("views.open") and "Only the coordinator" in slack.ephemerals("Lee")[-1]
    await slack.click("Alex", ids.ASSIGN_ROLES, ids.card_value(event_id, 2))
    assert not slack.fake.of("views.open") and slack.ephemerals("Alex")[-1] == STALE

    await slack.click("Alex", ids.ASSIGN_ROLES, value)
    (opened,) = slack.fake.of("views.open")
    view = opened.args["view"]
    assert view["callback_id"] == ids.ASSIGN_VIEW and ids.ViewOrigin.decode(view["private_metadata"]).run == 1
    blocks = {b["block_id"]: b for b in view["blocks"] if b["type"] == "input"}
    assert {o["value"] for o in blocks["mode"]["element"]["options"]} == {"pickup", "delivery"}
    who = blocks["who"]["element"]
    assert {o["text"]["text"] for o in who["options"]} == {"Alex", "Lee", "Sam"}  # whoever is in
    assert who["initial_option"]["text"]["text"] == "Alex"  # a volunteer, preselected

    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    form = state(
        mode={"selected_option": {"value": "pickup"}}, who={"selected_option": {"value": str(house["Lee"].id)}}
    )
    await slack.submit("Alex", ids.ASSIGN_VIEW, form, event_id, origin=origin)
    assert slack.ephemerals("Alex")[-1] == "Assigned. Lee is told."
    assert assigned(await house.context(event_id))[AssignmentRole.PICKUP] == house["Lee"].id
    await slack.drain()
    told = [c for c in slack.fake.of("chat.postMessage") if c.args["channel"] == "U04LEE"]
    assert told and "Alex assigned you this" in json.dumps(told[-1].args)


async def test_slack_assign_roles_modal_checks_its_fields_and_its_run(slack: Slack, house: House) -> None:
    event_id = await two_pickups_decided(house)
    origin = ids.ViewOrigin(event_id, CHANNEL, run=1)
    nobody = state(mode={"selected_option": {"value": "pickup"}})
    response = await slack.submit("Alex", ids.ASSIGN_VIEW, nobody, event_id, origin=origin)
    assert response.body and "Pick who does it" in response.body
    nothing = state(who={"selected_option": {"value": str(house["Sam"].id)}})
    response = await slack.submit("Alex", ids.ASSIGN_VIEW, nothing, event_id, origin=origin)
    assert response.body and "Pick how to get it" in response.body
    full = state(
        mode={"selected_option": {"value": "pickup"}}, who={"selected_option": {"value": str(house["Sam"].id)}}
    )
    await slack.submit("Alex", ids.ASSIGN_VIEW, full, event_id, origin=ids.ViewOrigin(event_id, CHANNEL))  # no run
    assert (await house.event(event_id)).roles_due_at is not None  # nothing happened
    await slack.submit("Alex", ids.ASSIGN_VIEW, full, event_id, origin=replace(origin, run=2))
    assert slack.ephemerals("Alex")[-1] == STALE and (await house.event(event_id)).roles_due_at is not None
    await slack.submit("Lee", ids.ASSIGN_VIEW, full, event_id, origin=origin)
    assert "Only the coordinator" in slack.ephemerals("Lee")[-1]


async def test_the_picks_buttons_are_for_members_only(slack: Slack, house: House) -> None:
    from dinnerbell.channels.slack.interactions import GUEST_TAPS

    assert ids.ASSIGN_ROLES in GUEST_TAPS  # a guest coordinating assigns roles (FR-58)
    assert not {ids.SHUFFLE, ids.BREAK_TIE} & GUEST_TAPS
