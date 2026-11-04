# SPDX-License-Identifier: AGPL-3.0-or-later
"""Answers that close early (FR-56, #193): once everyone has answered, or by **Start the vote
now**, voting opens at once but keeps the call's decision time, also for a call posted the day
before (FR-3). Only outs end a call early, and a maybe the early close turned out gets a
counting vote if they join late."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dinnerbell.config import Settings
from dinnerbell.domain.events import (
    EARLY_CLOSE_QUIET,
    EndReason,
    EventState,
    RoleOffer,
    Status,
    Step,
    Window,
    counts_vote,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.services import call_admin, calls
from dinnerbell.services.database import Database, open_database
from tests.support.calls import OPENS, House, furnish, make_house
from tests.support.clock import FakeClock
from tests.test_post_now import LUNCH_BY, LUNCH_POSTS, LUNCH_REMIND, TOMORROW, act, schedule_lunch

pytestmark = pytest.mark.anyio

CHICAGO = ZoneInfo("America/Chicago")


def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CHICAGO).astimezone(UTC)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    house.clock.now = OPENS  # Thursday, 3 PM in Chicago
    return house


LUNCH_DECIDES = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)
"""Tomorrow's lunch vote deadline: its response deadline plus 30 minutes to vote."""


async def everyone_in(house: House, event_id: int) -> None:
    for name in ("Alex", "Sam", "Kim", "Lee"):
        await act(house, calls.respond, house[name], event_id, Status.IN)
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)


async def test_everyone_answering_a_call_posted_early_votes_until_that_days_decision_time(house: House) -> None:
    """Everyone answering opens voting at once (FR-56), and voting runs until the call's decision
    time, which for a call posted early is on its day (FR-3), so it isn't decided the day before.
    Nothing else from that day fires early, late, or twice."""
    event_id = await schedule_lunch(house)
    await act(house, calls.post_now, house["Kim"], event_id)
    await everyone_in(house, event_id)
    house.clock.now = OPENS + timedelta(minutes=11)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.respond_deadline, event.vote_deadline) == (
        EventState.VOTING,
        house.clock(),
        LUNCH_DECIDES,
    )

    steps: list[Step] = []
    for at in (OPENS + timedelta(hours=1), LUNCH_POSTS, LUNCH_REMIND, LUNCH_BY, LUNCH_DECIDES - timedelta(seconds=1)):
        house.clock.now = at
        steps += await house.tick()  # today's dinner opens and expires meanwhile
    event = await house.event(event_id)
    assert (event.state, event.reminder_sent_at) == (EventState.VOTING, None)
    assert Step.CLOSE_VOTING not in steps and Step.REMIND not in steps
    house.clock.now = LUNCH_DECIDES
    assert await house.tick() == [Step.CLOSE_VOTING]
    event = await house.event(event_id)
    assert (event.state, event.decided_at) == (EventState.DECIDED, LUNCH_DECIDES)


async def test_everyone_answering_on_the_day_votes_until_the_decision_time(house: House) -> None:
    """Members can vote for several picks, so voting doesn't end while they're still looking:
    everyone answering opens voting at once, and it closes at the call's decision time (FR-56).
    The same goes for an ad-hoc call for today."""
    event_id = await schedule_lunch(house)
    house.clock.now = LUNCH_POSTS
    assert await house.tick() == [Step.OPEN]
    await everyone_in(house, event_id)
    house.clock.now += timedelta(minutes=10)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.respond_deadline, event.vote_deadline) == (
        EventState.VOTING,
        house.clock(),
        LUNCH_DECIDES,
    )
    first = (await house.context(event_id)).proposals[0].id
    for name in ("Alex", "Sam", "Kim", "Lee"):  # everyone voting doesn't decide it either
        await act(house, calls.vote, house[name], event_id, first)
    house.clock.now = LUNCH_DECIDES - timedelta(seconds=1)
    assert await house.tick() == []
    house.clock.now = LUNCH_DECIDES
    assert await house.tick() == [Step.CLOSE_VOTING]

    # It's lunch's day now. Today's dinner call, started at 11 AM, opens at once.
    dinner = await act(house, calls.start_call, house["Sam"], Slot.DINNER, TOMORROW)
    assert (dinner.state, dinner.opens_at) == (EventState.COLLECTING, house.clock())
    await everyone_in(house, dinner.id)
    house.clock.now += timedelta(minutes=10)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(dinner.id)
    assert (event.state, event.respond_deadline) == (EventState.VOTING, house.clock())
    assert event.vote_deadline == dinner.respond_deadline + timedelta(minutes=dinner.vote_minutes)


async def open_lunch(house: House, *, early: bool) -> int:
    """Tomorrow's lunch, posted now (``early``) or opened by the tick at its call time."""
    event_id = await schedule_lunch(house)
    if early:
        await act(house, calls.post_now, house["Kim"], event_id)
    else:
        house.clock.now = LUNCH_POSTS
        assert await house.tick() == [Step.OPEN]
    return event_id


async def answer(house: House, event_id: int, *answers: tuple[str, Status]) -> None:
    for name, status in answers:
        await act(house, calls.respond, house[name], event_id, status)


EARLY_OR_NOT = pytest.mark.parametrize("early", [True, False], ids=["posted early", "on its day"])


@EARLY_OR_NOT
async def test_everyone_answering_in_out_or_maybe_votes_until_the_decision_time(house: House, early: bool) -> None:
    """In, Out, and Maybe all count as answers once two are in (FR-56). Closing turns the maybe
    out, voting opens at once, and the call decides at its decision time, not before."""
    event_id = await open_lunch(house, early=early)
    await answer(house, event_id, ("Alex", Status.IN), ("Sam", Status.IN), ("Kim", Status.MAYBE), ("Lee", Status.OUT))
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now += EARLY_CLOSE_QUIET
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.respond_deadline, event.vote_deadline) == (
        EventState.VOTING,
        house.clock(),
        LUNCH_DECIDES,
    )
    house.clock.now = LUNCH_DECIDES - timedelta(seconds=1)
    await house.tick()  # today's dinner expires, for a call posted early
    assert (await house.event(event_id)).state is EventState.VOTING
    house.clock.now = LUNCH_DECIDES
    assert await house.tick() == [Step.CLOSE_VOTING]
    event = await house.event(event_id)
    assert (event.state, event.decided_at) == (EventState.DECIDED, LUNCH_DECIDES)


@EARLY_OR_NOT
async def test_everyone_out_ends_the_call_at_once(house: House, early: bool) -> None:
    """Too few to eat together has no timing to keep, so the call ends on-your-own without a vote
    at once, even the day before (FR-56)."""
    event_id = await open_lunch(house, early=early)
    await answer(house, event_id, ("Alex", Status.OUT), ("Sam", Status.OUT), ("Kim", Status.OUT), ("Lee", Status.OUT))
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.end_reason, event.vote_deadline) == (EventState.ABANDONED, EndReason.TOO_FEW, None)


@EARLY_OR_NOT
async def test_all_out_but_a_lone_in_ends_the_call_after_the_quiet_spell(house: House, early: bool) -> None:
    """A lone in gets the quiet spell to add a guest, which would make two, then the call ends
    without a vote."""
    event_id = await open_lunch(house, early=early)
    await answer(house, event_id, ("Alex", Status.OUT), ("Sam", Status.OUT), ("Kim", Status.OUT), ("Lee", Status.IN))
    assert await house.tick() == []
    house.clock.now += EARLY_CLOSE_QUIET
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.TOO_FEW)


@EARLY_OR_NOT
async def test_all_out_but_one_who_hasnt_answered_waits_for_them(house: House, early: bool) -> None:
    """They could still come in with a guest, which makes two (``head_count``), so the call
    isn't hopeless: it waits for them, or for its deadline."""
    event_id = await open_lunch(house, early=early)
    await answer(house, event_id, ("Alex", Status.OUT), ("Sam", Status.OUT), ("Kim", Status.OUT))
    house.clock.now += timedelta(minutes=30)
    await house.tick()  # today's dinner opens, for a call posted early
    assert (await house.event(event_id)).state is EventState.COLLECTING


async def test_start_the_vote_now_keeps_the_decision_time_and_decide_now_ends_it(house: House) -> None:
    """**Start the vote now** opens voting at once, and voting still closes at the call's decision
    time, on its day for a call posted early. Only the coordinator or an admin ends voting
    sooner: **Decide now**, or an admin's shorter deadline."""
    event_id = await schedule_lunch(house)
    await act(house, calls.post_now, house["Kim"], event_id)
    await everyone_in(house, event_id)
    await act(house, calls.close_window, house["Alex"], event_id)
    event = await house.event(event_id)
    assert (event.state, event.respond_deadline, event.vote_deadline) == (
        EventState.VOTING,
        OPENS,
        LUNCH_DECIDES,
    )
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.VOTE, -15)
    assert (await house.event(event_id)).vote_deadline == LUNCH_DECIDES - timedelta(minutes=15)
    await act(house, calls.close_window, house["Alex"], event_id)
    event = await house.event(event_id)
    assert (event.state, event.decided_at) == (EventState.DECIDED, OPENS)


async def test_an_admin_shortening_answers_keeps_or_moves_the_decision_time(house: House) -> None:
    """A shorter answer window still ahead moves the decision time as much. One shortened into
    the past closes answers now, like **Start the vote now**, and keeps the decision time."""
    event_id = await open_lunch(house, early=False)
    await everyone_in(house, event_id)
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, -15)
    assert (await house.event(event_id)).respond_deadline == LUNCH_BY - timedelta(minutes=15)
    await act(house, call_admin.move_deadline, house["Alex"], event_id, Window.RESPOND, -240)
    event = await house.event(event_id)
    assert (event.state, event.respond_deadline, event.vote_deadline) == (
        EventState.VOTING,
        house.clock(),
        LUNCH_DECIDES - timedelta(minutes=15),
    )


# Maybes ------------------------------------------------------------------------------------------


@EARLY_OR_NOT
@pytest.mark.parametrize("first", [Status.MAYBE, Status.IN], ids=["all maybe", "one in"])
async def test_a_maybe_with_fewer_than_two_in_keeps_answers_open(house: House, early: bool, first: Status) -> None:
    """Only outs end a call early: a maybe might still make it two, so answers stay open until
    the deadline, which turns the maybes out."""
    event_id = await open_lunch(house, early=early)
    await answer(house, event_id, ("Alex", first), ("Sam", Status.MAYBE), ("Kim", Status.MAYBE), ("Lee", Status.MAYBE))
    house.clock.now += timedelta(minutes=30)
    await house.tick()  # today's dinner opens, for a call posted early
    assert (await house.event(event_id)).state is EventState.COLLECTING
    house.clock.now = LUNCH_BY
    assert Step.CLOSE_RESPONSES in await house.tick()
    event = await house.event(event_id)
    assert (event.state, event.end_reason) == (EventState.ABANDONED, EndReason.TOO_FEW)


@EARLY_OR_NOT
async def test_a_maybe_answers_closing_early_turned_out_gets_a_counting_vote(house: House, early: bool) -> None:
    """They answered on time, so joining late, their vote counts. Their guests are cleared, as
    for anyone who joins late."""
    event_id = await open_lunch(house, early=early)
    await answer(house, event_id, ("Alex", Status.IN), ("Sam", Status.IN), ("Kim", Status.MAYBE), ("Lee", Status.OUT))
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now += EARLY_CLOSE_QUIET
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    assert await act(house, calls.join_late, house["Kim"], event_id)
    kim = (await house.context(event_id)).response_of(house["Kim"].id)
    assert kim is not None and (kim.status, kim.late, kim.vote_counts, kim.guests) == (Status.IN, True, True, 0)
    assert counts_vote(kim)


async def test_a_maybe_start_the_vote_now_turned_out_gets_a_counting_vote(house: House) -> None:
    event_id = await open_lunch(house, early=True)
    await answer(house, event_id, ("Alex", Status.IN), ("Sam", Status.IN), ("Kim", Status.MAYBE))  # Lee hasn't
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.close_window, house["Alex"], event_id)
    assert await act(house, calls.join_late, house["Kim"], event_id)
    kim = (await house.context(event_id)).response_of(house["Kim"].id)
    assert kim is not None and (kim.late, kim.vote_counts) == (True, True)


async def test_start_the_vote_now_with_one_in_ends_the_call_without_a_decision_time(house: House) -> None:
    """Fewer than two in: the call ends on-your-own, and keeps no vote deadline."""
    event_id = await open_lunch(house, early=False)
    await answer(house, event_id, ("Alex", Status.IN), ("Sam", Status.OUT), ("Kim", Status.OUT), ("Lee", Status.OUT))
    await act(house, calls.close_window, house["Alex"], event_id)
    event = await house.event(event_id)
    assert (event.state, event.end_reason, event.vote_deadline) == (EventState.ABANDONED, EndReason.TOO_FEW, None)


async def test_a_maybe_the_deadline_turned_out_joins_late_without_a_counting_vote(house: House) -> None:
    """At the deadline, the coordinator decides whether a late joiner's vote counts (#76)."""
    event_id = await open_lunch(house, early=False)
    await answer(house, event_id, ("Alex", Status.IN), ("Sam", Status.IN), ("Kim", Status.MAYBE))  # Lee hasn't
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    house.clock.now = LUNCH_BY
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    assert await act(house, calls.join_late, house["Kim"], event_id)
    kim = (await house.context(event_id)).response_of(house["Kim"].id)
    assert kim is not None and (kim.late, kim.vote_counts) == (True, False)


# Re-runs -----------------------------------------------------------------------------------------


@EARLY_OR_NOT
async def test_a_rerun_after_an_early_close_starts_fresh(house: House, early: bool) -> None:
    """A re-run clears the kept decision time, and a maybe again loses the counting vote; closing
    early again keeps the new run's decision time."""
    event_id = await open_lunch(house, early=early)
    await answer(house, event_id, ("Alex", Status.IN), ("Sam", Status.IN), ("Kim", Status.MAYBE), ("Lee", Status.OUT))
    await act(house, calls.offer_role, house["Sam"], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.close_window, house["Alex"], event_id)
    assert (await house.event(event_id)).vote_deadline == LUNCH_DECIDES

    house.clock.now += timedelta(minutes=5)
    await act(house, call_admin.rerun, house["Alex"], event_id)
    event = await house.event(event_id)
    # Posted early, it keeps that day's deadline; on its day, it opens now with a fresh window.
    respond_by = LUNCH_BY if early else house.clock() + timedelta(hours=1)
    assert (event.run, event.state, event.respond_deadline, event.vote_deadline) == (
        2,
        EventState.COLLECTING,
        respond_by,
        None,
    )
    kim = (await house.context(event_id)).response_of(house["Kim"].id)
    assert kim is not None and (kim.status, kim.vote_counts) == (Status.MAYBE, False)
    await act(house, calls.close_window, house["Alex"], event_id)
    event = await house.event(event_id)
    assert (event.state, event.vote_deadline) == (EventState.VOTING, respond_by + timedelta(minutes=30))


async def test_a_call_posted_early_for_the_day_clocks_go_back(house: House) -> None:
    """Sunday, November 1, 2026, Chicago falls back an hour at 2 AM. Lunch posted Saturday, closed
    early, and re-run still decides at noon Sunday, 18:00 UTC rather than 17:00."""
    sunday = date(2026, 11, 1)
    house.clock.now = local(date(2026, 10, 31), 15)
    lunch = await act(house, calls.start_call, house["Sam"], Slot.LUNCH, sunday)
    await act(house, calls.post_now, house["Kim"], lunch.id)
    noon = local(sunday, 12)
    assert noon == datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
    await everyone_in(house, lunch.id)
    house.clock.now += EARLY_CLOSE_QUIET
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    assert (await house.event(lunch.id)).vote_deadline == noon

    await act(house, call_admin.rerun, house["Alex"], lunch.id)
    assert (await house.event(lunch.id)).respond_deadline == local(sunday, 11, 30)
    await act(house, calls.close_window, house["Alex"], lunch.id)
    assert (await house.event(lunch.id)).vote_deadline == noon
    house.clock.now = noon - timedelta(seconds=1)
    await house.tick()  # Saturday's dinner and Sunday's breakfast come and go
    assert (await house.event(lunch.id)).state is EventState.VOTING
    house.clock.now = noon
    await house.tick()
    assert (await house.event(lunch.id)).decided_at == noon


# Late calls --------------------------------------------------------------------------------------


async def late_dinner(house: House, starts: datetime) -> int:
    """Today's dinner started at ``starts``, everyone in, closed at 9:40 PM: 90 minutes to answer
    and 45 to vote."""
    house.clock.now = starts
    dinner = await act(house, calls.start_call, house["Alex"], Slot.DINNER, OPENS.date())
    await everyone_in(house, dinner.id)
    house.clock.now = local(OPENS.date(), 21, 40)
    await act(house, calls.close_window, house["Alex"], dinner.id)
    return int(dinner.id)


async def test_a_late_call_keeps_a_decision_time_of_11_45_pm(house: House) -> None:
    """Started at 9:30 PM, it decides at 11:45 PM, with the meal still on its day."""
    event_id = await late_dinner(house, local(OPENS.date(), 21, 30))
    quarter_to = local(OPENS.date(), 23, 45)
    assert (await house.event(event_id)).vote_deadline == quarter_to
    house.clock.now = quarter_to
    assert await house.tick() == [Step.CLOSE_VOTING, Step.ASSIGN_ROLES]  # roles are due by the meal
    event = await house.event(event_id)
    assert (event.state, event.meal_time) == (EventState.DECIDED, quarter_to)


async def test_a_late_call_too_late_to_decide_on_its_day_votes_its_usual_length(house: House) -> None:
    """Started at 9:31 PM, it would decide at 11:46 PM, and its meal would round up to midnight,
    the next day. So voting gets its usual 45 minutes from the close, as before."""
    event_id = await late_dinner(house, local(OPENS.date(), 21, 31))
    assert (await house.event(event_id)).vote_deadline == local(OPENS.date(), 22, 25)
    house.clock.now = local(OPENS.date(), 22, 25)
    assert await house.tick() == [Step.CLOSE_VOTING]
    assert (await house.event(event_id)).state is EventState.DECIDED
