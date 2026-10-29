# SPDX-License-Identifier: AGPL-3.0-or-later
"""Counting contributions and awarding badges when a call is done (M4, FR-38, FR-39, FR-43),
against a migrated database: each rule through the real lifecycle, once and only once, the
admin's thresholds, the backfill, the stats a member and the household see, and two finishes at
once."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import fields, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, text

from dinnerbell.config import Settings
from dinnerbell.db.contributions import BadgeRepository, ContributionRepository
from dinnerbell.db.events import EventRepository, ResponseRepository
from dinnerbell.db.models import (
    AssignmentRow,
    BadgeAwardRow,
    ContributionRow,
    EventRow,
    HouseholdRow,
    MealLogRow,
    ResponseRow,
)
from dinnerbell.domain.badges import (
    DEFAULT_THRESHOLDS,
    Announcement,
    BadgeKey,
    InvalidThresholds,
    Tier,
)
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import EventState, RoleOffer, Status, with_status
from dinnerbell.domain.feedback import FeedbackInput, Rating
from dinnerbell.domain.guests import GuestInput
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member, Role
from dinnerbell.services import calls, contributions, feedback, guests, offers, stats
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import Forbidden
from dinnerbell.services.offers import OfferInput
from tests.support.calls import DONE_AT, OPENS, RESPOND_BY, VOTE_BY, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 22)
DST_ENDS = date(2026, 11, 1)
MORNING = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
LUNCH_AT = datetime(2026, 10, 22, 17, 30, tzinfo=UTC)
MEAL_TIME = datetime(2026, 10, 22, 23, 30, tzinfo=UTC)
QUICK = timedelta(minutes=5)
SLOW = timedelta(minutes=40)


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


async def dinner(
    house: House,
    *,
    n: int = 0,
    ins: tuple[str, ...] = ("Alex", "Sam"),
    answer_in: timedelta = QUICK,
    pickup: str | None = "Alex",
    done: bool = True,
    vote_for: str | None = None,
    while_answering: Callable[[int], Awaitable[None]] | None = None,
    while_voting: Callable[[int], Awaitable[None]] | None = None,
) -> int:
    """Day ``n`` of the week's dinners, from the call to done: everyone in ``ins`` answers In
    ``answer_in`` after it opens, ``pickup`` offers to pick up, Sam coordinates, and the vote and
    the clock do the rest. Returns the event's ID."""
    shift = timedelta(days=n)
    day = DAY + shift
    if day >= DST_ENDS:  # Chicago's clocks went back, so 15:00 there is an hour later in UTC
        shift += timedelta(hours=1)
    house.clock.now = OPENS + shift
    await house.tick()
    event_id = await dinner_id(house, day)
    house.clock.now = OPENS + shift + answer_in
    for name in ins:
        await act(house, calls.respond, house[name], event_id, Status.IN)
    if pickup is not None:
        await act(house, calls.offer_role, house[pickup], event_id, RoleOffer.PICKUP, True)
    await act(house, calls.claim_coordinator, house["Sam"], event_id)
    if while_answering is not None:
        await while_answering(event_id)
    house.clock.now = RESPOND_BY + shift
    await house.tick()
    if while_voting is not None:
        await while_voting(event_id)
    ctx = await house.context(event_id)
    picks = [p for p in ctx.proposals if vote_for is None or p.proposed_by == house[vote_for].id]
    await act(house, calls.vote, house["Sam"], event_id, picks[0].id)
    house.clock.now = VOTE_BY + shift
    await house.tick()
    if done:
        house.clock.now = DONE_AT + shift
        await house.tick()
        assert (await house.event(event_id)).state is EventState.DONE
    return event_id


async def counts(house: House, name: str) -> dict[Kind, int]:
    async with house.sessionmaker() as db:
        return await stats_counts(db, house, name)


async def stats_counts(db: Any, house: House, name: str) -> dict[Kind, int]:
    return (await stats.my_stats(db, house[name])).counts


async def awards(house: House, name: str | None = None) -> list[tuple[str, BadgeKey, Tier, Announcement]]:
    async with house.sessionmaker() as db:
        rows = await db.scalars(select(BadgeAwardRow).order_by(BadgeAwardRow.id))
        names = {m.id: n for n, m in house.people.items()}
        return [
            (names[r.member_id], BadgeKey(r.badge_key), Tier(r.tier), Announcement(r.announcement))
            for r in rows
            if name is None or names[r.member_id] == name
        ]


async def rows(house: House, model: Any = ContributionRow) -> int:
    async with house.sessionmaker() as db:
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)


# Counting at done ------------------------------------------------------------------------------


async def test_a_done_call_counts_who_pitched_in_and_who_was_there(house: House) -> None:
    await dinner(house)
    alex, sam, kim = await counts(house, "Alex"), await counts(house, "Sam"), await counts(house, "Kim")
    assert alex[Kind.PICKED_UP] == 1 and alex[Kind.PARTICIPATED] == 1 and alex[Kind.RESPONDED_ON_TIME] == 1
    assert sam[Kind.COORDINATED] == 1 and sam[Kind.PARTICIPATED] == 1
    assert Kind.COOKED not in alex and Kind.PICKED_UP not in sam
    assert kim == {}  # never answered, never in


async def test_nothing_is_counted_until_the_call_is_done(house: House) -> None:
    await dinner(house, done=False)
    assert await rows(house) == 0 and await rows(house, BadgeAwardRow) == 0


async def test_a_slow_answer_is_in_but_not_on_time(house: House) -> None:
    await dinner(house, answer_in=SLOW)
    alex = await counts(house, "Alex")
    assert alex[Kind.PARTICIPATED] == 1 and Kind.RESPONDED_ON_TIME not in alex


async def test_finishing_the_same_call_again_counts_it_once(house: House) -> None:
    event_id = await dinner(house)
    before, badges = await rows(house), await awards(house)
    assert before > 0 and badges
    event = await house.event(event_id)
    for _ in range(3):
        async with house.sessionmaker() as db:
            assert await contributions.record_done(db, event, now=house.clock()) == []
            await db.commit()
    assert await rows(house) == before and await awards(house) == badges


async def test_a_call_finished_again_with_new_answers_adds_only_what_is_new(house: House) -> None:
    """An admin re-run or a late fix finishes a call twice: whoever is now in counts, and no one twice."""
    event_id = await dinner(house)
    first = await rows(house)
    async with house.sessionmaker() as db:
        await ResponseRepository(db).save(
            house.household.id, event_id, with_status(None, house["Kim"].id, Status.IN, house.clock()), house.clock()
        )
        await db.commit()
    event = await house.event(event_id)
    async with house.sessionmaker() as db:
        await contributions.record_done(db, event, now=house.clock())
        await db.commit()
    assert await rows(house) > first
    assert (await counts(house, "Kim"))[Kind.PARTICIPATED] == 1
    assert (await counts(house, "Alex"))[Kind.PARTICIPATED] == 1


async def test_two_dinners_count_twice(house: House) -> None:
    await dinner(house, n=0)
    await dinner(house, n=1)
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 2


# Awarding --------------------------------------------------------------------------------------


async def test_the_first_cooked_picked_up_or_coordinated_earns_the_first_tier_pending_announcement(
    house: House,
) -> None:
    await dinner(house)
    found = await awards(house)
    assert ("Alex", BadgeKey.ROAD_RUNNER, Tier.FIRST, Announcement.PENDING) in found
    assert ("Sam", BadgeKey.QUARTERBACK, Tier.FIRST, Announcement.PENDING) in found
    assert ("Alex", BadgeKey.FULL_TABLE, Tier.FIRST, Announcement.PENDING) in found
    assert ("Alex", BadgeKey.EARLY_BIRD, Tier.FIRST, Announcement.PENDING) in found
    assert {a[3] for a in found} == {Announcement.PENDING}
    assert not [a for a in found if a[0] == "Kim"]
    async with house.sessionmaker() as db:
        row = await db.scalar(select(BadgeAwardRow).where(BadgeAwardRow.badge_key == "road_runner"))
        assert row is not None and row.awarded_at == DONE_AT and row.announced_at is None and row.event_id is not None


async def test_a_badge_tier_is_awarded_once_each_time_the_count_crosses_it(house: House) -> None:
    """With a ladder of 1, 2, 3, 4, 5, the tiers come one dinner at a time, each exactly once."""
    async with house.sessionmaker() as db:
        await contributions.set_thresholds(db, house["Alex"], [1, 2, 3, 4, 5], now=house.clock())
    seen: list[list[Tier]] = []
    for n in range(5):
        await dinner(house, n=n)
        seen.append([t for who, badge, t, _ in await awards(house) if (who, badge) == ("Alex", BadgeKey.ROAD_RUNNER)])
    assert seen == [list(Tier)[: n + 1] for n in range(5)]
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 5


async def test_a_tier_below_a_threshold_is_not_awarded_one_count_short(house: House) -> None:
    async with house.sessionmaker() as db:
        await contributions.set_thresholds(db, house["Alex"], [2, 3, 4, 5, 6], now=house.clock())
    await dinner(house, n=0)
    assert not [a for a in await awards(house, "Alex") if a[1] is BadgeKey.ROAD_RUNNER]
    await dinner(house, n=1)
    assert [a[2] for a in await awards(house, "Alex") if a[1] is BadgeKey.ROAD_RUNNER] == [Tier.FIRST]


async def test_on_a_roll_counts_weeks_in_a_row(house: House) -> None:
    async with house.sessionmaker() as db:
        await contributions.set_thresholds(db, house["Alex"], [1, 2, 3, 4, 5], now=house.clock())
    for n in (0, 7, 14):  # Thursday, then each week after
        await dinner(house, n=n)
    rolls = [a[2] for a in await awards(house, "Alex") if a[1] is BadgeKey.ON_A_ROLL]
    assert rolls == [Tier.FIRST, Tier.BRONZE, Tier.SILVER]
    await dinner(house, n=28)  # a week skipped: the streak starts over, and what was earned stays
    assert [a[2] for a in await awards(house, "Alex") if a[1] is BadgeKey.ON_A_ROLL] == rolls
    mine = await act_stats(house, "Alex")
    roll = next(p for p in mine.progress if p.badge.key is BadgeKey.ON_A_ROLL)
    assert (roll.count, roll.tier, roll.next_tier) == (3, Tier.SILVER, Tier.GOLD)


async def act_stats(house: House, name: str) -> stats.MemberStats:
    async with house.sessionmaker() as db:
        return await stats.my_stats(db, house[name])


async def test_the_admin_sets_the_thresholds_and_it_is_audited(house: House) -> None:
    assert await act_thresholds(house) == DEFAULT_THRESHOLDS
    async with house.sessionmaker() as db:
        await contributions.set_thresholds(db, house["Alex"], [3, 6, 9, 12, 15], now=house.clock())
    assert await act_thresholds(house) == (3, 6, 9, 12, 15)
    (row,) = await house.audits("household.badge_thresholds")
    assert row.actor_member_id == house["Alex"].id
    assert row.payload == {"from": [1, 10, 50, 100, 200], "to": [3, 6, 9, 12, 15], "caught_up": 0}
    async with house.sessionmaker() as db:  # the same values again change nothing and write nothing
        await contributions.set_thresholds(db, house["Alex"], [3, 6, 9, 12, 15], now=house.clock())
    assert len(await house.audits("household.badge_thresholds")) == 1


async def act_thresholds(house: House) -> tuple[int, ...]:
    async with house.sessionmaker() as db:
        return await contributions.thresholds(db, house.household.id)


async def test_only_an_admin_sets_thresholds_and_a_bad_ladder_is_refused(house: House) -> None:
    async with house.sessionmaker() as db:
        with pytest.raises(Forbidden):
            await contributions.set_thresholds(db, house["Sam"], [1, 2, 3, 4, 5], now=house.clock())
        with pytest.raises(InvalidThresholds):
            await contributions.set_thresholds(db, house["Alex"], [5, 4, 3, 2, 1], now=house.clock())
    assert await act_thresholds(house) == DEFAULT_THRESHOLDS
    assert await house.audits("household.badge_thresholds") == []


async def test_lowering_a_threshold_awards_quietly_and_raising_one_takes_nothing_away(house: House) -> None:
    async with house.sessionmaker() as db:
        await contributions.set_thresholds(db, house["Alex"], [2, 3, 4, 5, 6], now=house.clock())
    await dinner(house, n=0)
    assert not [a for a in await awards(house) if a[1] is BadgeKey.ROAD_RUNNER]
    async with house.sessionmaker() as db:  # back to 1 for the first tier: Alex has it already
        await contributions.set_thresholds(db, house["Alex"], [1, 3, 4, 5, 6], now=house.clock())
    found = [a for a in await awards(house) if a[1] is BadgeKey.ROAD_RUNNER]
    assert found == [("Alex", BadgeKey.ROAD_RUNNER, Tier.FIRST, Announcement.SKIPPED)]
    audit = (await house.audits("household.badge"))[-1]
    assert audit.payload["caught_up"] >= 1
    async with house.sessionmaker() as db:  # raising it again leaves the badge earned
        await contributions.set_thresholds(db, house["Alex"], [5, 6, 7, 8, 9], now=house.clock())
    assert [a for a in await awards(house) if a[1] is BadgeKey.ROAD_RUNNER] == found
    mine = await act_stats(house, "Alex")
    runner = next(p for p in mine.progress if p.badge.key is BadgeKey.ROAD_RUNNER)
    assert runner.tier is Tier.FIRST and runner.next_at == 6


# Who counts: guests, late joiners, and offers --------------------------------------------------


async def test_a_guest_who_picks_up_earns_it_and_the_member_who_brought_them_hosted(house: House) -> None:
    riley: list[Member] = []

    async def invite(event_id: int) -> None:
        invited = await act(house, guests.invite, house["Sam"], event_id, new_guest=GuestInput("Riley"))
        riley.append(invited.guest)
        await act(house, guests.answer_for, house["Sam"], event_id, riley[0].id, role=RoleOffer.PICKUP)

    await dinner(house, pickup=None, ins=("Alex", "Sam"), while_answering=invite)
    async with house.sessionmaker() as db:
        by_kind = await ContributionRepository(db).counts(house.household.id, riley[0].id)
        assert by_kind == {Kind.PICKED_UP: 1}  # at the table, but that is for members to earn
        held = await BadgeRepository(db).for_member(house.household.id, riley[0].id)
        assert {(a.badge, a.tier) for a in held} == {
            (BadgeKey.ROAD_RUNNER, Tier.FIRST),
            (BadgeKey.ON_A_ROLL, Tier.FIRST),
        }
    assert (await counts(house, "Sam"))[Kind.HOSTED] == 1
    assert Kind.HOSTED not in await counts(house, "Alex")
    with pytest.raises(Forbidden):  # the member view is for members; a guest has guest_stats
        async with house.sessionmaker() as db:
            await stats.my_stats(db, riley[0])
    async with house.sessionmaker() as db:
        # A stray count a guest can't earn (say, from before they were demoted) is not shown.
        event_id = await db.scalar(select(EventRow.id))
        db.add(
            ContributionRow(
                household_id=house.household.id,
                member_id=riley[0].id,
                kind=Kind.PARTICIPATED.value,
                day=DAY,
                event_id=event_id,
                created_at=DONE_AT,
            )
        )
        await db.commit()
        assert (await ContributionRepository(db).counts(house.household.id, riley[0].id))[Kind.PARTICIPATED] == 1
        own = await stats.guest_stats(db, riley[0])
        assert {p.badge.key for p in own.progress} == {
            BadgeKey.CHEF, BadgeKey.ROAD_RUNNER, BadgeKey.ORDER_UP, BadgeKey.QUARTERBACK, BadgeKey.ON_A_ROLL,
        }  # fmt: skip
        assert own.counts == {Kind.PICKED_UP: 1}
    with pytest.raises(Forbidden):
        async with house.sessionmaker() as db:
            await stats.guest_stats(db, house["Alex"])


async def test_a_late_joiner_who_takes_part_is_a_participant_but_not_early(house: House) -> None:
    async def join(event_id: int) -> None:
        await act(house, calls.join_late, house["Kim"], event_id)

    await dinner(house, while_voting=join)
    kim = await counts(house, "Kim")
    assert kim == {Kind.PARTICIPATED: 1}


async def test_a_member_who_sat_out_earns_nothing_for_the_meal(house: House) -> None:
    await dinner(house, ins=("Alex", "Sam"))
    async with house.sessionmaker() as db:
        assert await ContributionRepository(db).counts(house.household.id, house["Lee"].id) == {}
    assert [a for a in await awards(house) if a[0] == "Lee"] == []


async def test_a_winning_pick_counts_for_who_made_it_and_a_new_cuisine_and_place_for_exploring(house: House) -> None:
    """Kim adds Pho House, a place and cuisine the household has never eaten, and it wins. The
    place joins the catalog under her name, so her find was chosen too."""
    vietnamese = await house.tag("Vietnamese")

    async def pitch(event_id: int) -> None:
        await act(
            house,
            calls.add_proposal,
            house["Kim"],
            event_id,
            Mode.PICKUP,
            free_text="Pho House",
            cuisine_id=vietnamese.id,
        )

    await dinner(house, ins=("Alex", "Sam", "Kim"), while_voting=pitch, vote_for="Kim")
    kim = await counts(house, "Kim")
    assert kim[Kind.SUGGESTED] == 1 and kim[Kind.EXPLORED] == 1 and kim[Kind.ADDED_ITEM] == 1
    found = {(b, t) for who, b, t, _ in await awards(house, "Kim")}
    assert {(BadgeKey.BRIGHT_IDEA, Tier.FIRST), (BadgeKey.EXPLORER, Tier.FIRST), (BadgeKey.SCOUT, Tier.FIRST)} <= found
    assert Kind.SUGGESTED not in await counts(house, "Alex")


async def test_a_pick_the_system_made_credits_no_idea_and_an_old_cuisine_is_not_exploring(house: House) -> None:
    await dinner(house, n=0)
    await dinner(house, n=1)
    for name in ("Alex", "Sam"):
        kinds = await counts(house, name)
        assert Kind.SUGGESTED not in kinds and Kind.EXPLORED not in kinds


async def test_an_offer_counts_the_offerer_for_the_mode_and_those_who_joined(house: House) -> None:
    """Sam offers tacos for lunch; Kim joins with a guest. Sam cooked, Kim hosted, and no one
    answered a call quickly, because an offer has no opening."""
    house.clock.now = MORNING
    data = OfferInput(
        slot=Slot.LUNCH,
        day=DAY,
        mode=Mode.COOK,
        at=time(12, 30),
        capacity=3,
        item_id=house.items["Sam's Tacos"].id,
    )
    posted = await act(house, offers.post_offer, house["Sam"], data)
    await act(house, offers.add_guest, house["Kim"], posted.event_id)
    house.clock.now = LUNCH_AT
    await house.tick()
    house.clock.now = LUNCH_AT + timedelta(hours=2)
    await house.tick()
    assert (await house.event(posted.event_id)).state is EventState.DONE
    sam, kim = await counts(house, "Sam"), await counts(house, "Kim")
    assert sam[Kind.COOKED] == 1 and Kind.COORDINATED not in sam
    assert kim[Kind.PARTICIPATED] == 1 and kim[Kind.HOSTED] == 1
    assert Kind.RESPONDED_ON_TIME not in sam and Kind.RESPONDED_ON_TIME not in kim
    assert ("Sam", BadgeKey.CHEF, Tier.FIRST, Announcement.PENDING) in await awards(house)


# Feedback --------------------------------------------------------------------------------------


async def give(house: House, name: str, meal_id: int, food: Rating | None = Rating.LOVED) -> None:
    await act(house, feedback.give, house[name], meal_id, FeedbackInput(food=food))


async def test_feedback_counts_once_per_meal_however_often_it_changes(house: House) -> None:
    await dinner(house)
    (meal,) = await house.meals()
    await give(house, "Alex", meal.id)
    assert (await counts(house, "Alex"))[Kind.GAVE_FEEDBACK] == 1
    await give(house, "Alex", meal.id, Rating.FINE)  # changing it
    await give(house, "Alex", meal.id, None)  # taking it back
    await give(house, "Alex", meal.id, Rating.NOT_AGAIN)  # and giving it again
    assert (await counts(house, "Alex"))[Kind.GAVE_FEEDBACK] == 1
    assert ("Alex", BadgeKey.FOOD_CRITIC, Tier.FIRST, Announcement.PENDING) in await awards(house)
    assert Kind.GAVE_FEEDBACK not in await counts(house, "Sam")


async def test_empty_feedback_counts_for_nothing(house: House) -> None:
    await dinner(house)
    (meal,) = await house.meals()
    await give(house, "Sam", meal.id, None)
    assert Kind.GAVE_FEEDBACK not in await counts(house, "Sam")


# Announcements ---------------------------------------------------------------------------------


async def test_pending_awards_are_announced_once(house: House) -> None:
    await dinner(house)
    async with house.sessionmaker() as db:
        pending = await contributions.pending_announcements(db, house.household.id)
    assert pending and {a.announcement for a in pending} == {Announcement.PENDING}
    ids = [a.id for a in pending]
    house.clock.now = DONE_AT + timedelta(minutes=1)
    async with house.sessionmaker() as db:
        assert await contributions.mark_announced(db, house.household.id, ids, now=house.clock()) == len(ids)
        await db.commit()
    async with house.sessionmaker() as db:
        assert await contributions.pending_announcements(db, house.household.id) == []
        assert await contributions.mark_announced(db, house.household.id, ids, now=house.clock()) == 0
        await db.commit()
        rows = await db.scalars(select(BadgeAwardRow))
        assert {(r.announcement, r.announced_at) for r in rows} == {("announced", house.clock.now)}


# Backfill --------------------------------------------------------------------------------------


async def snapshot(house: House) -> list[tuple[Any, ...]]:
    async with house.sessionmaker() as db:
        rows = await db.scalars(select(ContributionRow).order_by(ContributionRow.id))
        return sorted((r.member_id, r.kind, r.day, r.event_id, r.meal_id, r.item_id) for r in rows)


async def forget_everything(house: House) -> None:
    """The database as an upgrade from before M4 leaves it: the meals and calls are there, the
    counts and the marker are not."""
    async with house.sessionmaker() as db:
        await db.execute(text("DELETE FROM badge_award"))
        await db.execute(text("DELETE FROM contribution"))
        stored = (await db.scalar(select(HouseholdRow.settings))) or {}
        await db.execute(
            text("UPDATE household SET settings = :s"),
            {"s": json.dumps({k: v for k, v in stored.items() if k != contributions.BACKFILLED_KEY})},
        )
        await db.commit()


async def test_the_backfill_counts_the_meal_log_without_announcing_anything(house: House) -> None:
    for n in range(3):
        await dinner(house, n=n)
    (first, *_) = await house.meals()
    await give(house, "Alex", first.id)
    counted, earned = await snapshot(house), await awards(house)
    assert counted and earned
    await forget_everything(house)
    assert await snapshot(house) == [] and await awards(house) == []

    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert await snapshot(house) == counted  # the same counts the live path gave
    backfilled = await awards(house)
    assert sorted(a[:3] for a in backfilled) == sorted(a[:3] for a in earned)  # the same badges
    assert {a[3] for a in backfilled} == {Announcement.SKIPPED}
    async with house.sessionmaker() as db:
        assert await contributions.pending_announcements(db, house.household.id) == []


async def test_the_backfill_runs_once_and_later_calls_still_count(house: House) -> None:
    async with house.sessionmaker() as db:
        await contributions.set_thresholds(db, house["Alex"], [1, 2, 3, 4, 5], now=house.clock())
    await dinner(house, n=0)
    await forget_everything(house)
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is False
    before = len(await snapshot(house))
    async with house.sessionmaker() as db:
        stored = await db.scalar(select(HouseholdRow.settings))
    assert stored is not None and stored[contributions.BACKFILLED_KEY] == house.clock.now.isoformat()
    await dinner(house, n=1)
    assert len(await snapshot(house)) > before
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 2
    assert ("Alex", BadgeKey.ROAD_RUNNER, Tier.BRONZE, Announcement.PENDING) in await awards(house)  # live again


async def test_the_backfill_of_an_empty_household_does_nothing_but_remember_it(house: House) -> None:
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert await snapshot(house) == [] and await awards(house) == []


# What members see ------------------------------------------------------------------------------


async def test_a_member_sees_their_own_counts_and_all_thirteen_badges(house: House) -> None:
    await dinner(house)
    mine = await act_stats(house, "Alex")
    assert mine.member_id == house["Alex"].id
    assert [p.badge.key for p in mine.progress] == list(BadgeKey)
    runner = next(p for p in mine.progress if p.badge.key is BadgeKey.ROAD_RUNNER)
    assert (runner.count, runner.tier, runner.next_tier, runner.remaining) == (1, Tier.FIRST, Tier.BRONZE, 9)
    chef = next(p for p in mine.progress if p.badge.key is BadgeKey.CHEF)
    assert (chef.count, chef.tier, chef.next_tier, chef.remaining) == (0, None, Tier.FIRST, 1)
    assert {(a.badge, a.tier) for a in mine.earned} >= {(BadgeKey.ROAD_RUNNER, Tier.FIRST)}
    assert mine.counts == await counts(house, "Alex") and mine.counts != await counts(house, "Sam")


async def test_the_household_view_has_totals_and_recent_badges_and_no_member_counts(house: House) -> None:
    await dinner(house, n=0)
    await dinner(house, n=1)
    async with house.sessionmaker() as db:
        view = await stats.household_stats(db, house["Kim"])
    assert view.meals == 2
    assert view.totals[Kind.PICKED_UP] == 2 and view.totals[Kind.PARTICIPATED] == 4
    assert view.badges_earned == len(await awards(house))
    names = {r.member_name for r in view.recent}
    assert names <= {"Alex", "Sam"} and len(view.recent) <= stats.RECENT_BADGES
    stamps = [r.award.awarded_at for r in view.recent]
    assert stamps == sorted(stamps, reverse=True)  # newest first, never best or worst first
    assert {f.name for f in fields(view)} == {"meals", "totals", "badges_earned", "recent"}


async def test_nothing_in_the_stats_carries_anyone_elses_stances_or_feedback(house: House) -> None:
    """Sam rates the meal "not again" and Alex looks at everything the stats give him."""
    await dinner(house)
    (meal,) = await house.meals()
    await give(house, "Sam", meal.id, Rating.NOT_AGAIN)
    async with house.sessionmaker() as db:
        mine = await stats.my_stats(db, house["Alex"])
        household = await stats.household_stats(db, house["Alex"])
    seen = repr(mine) + repr(household)
    for secret in ("not_again", "NOT_AGAIN", "loved", "veto", "score", "stance", "preference"):
        assert secret not in seen
    assert {f.name for f in fields(mine)} == {"member_id", "counts", "progress", "earned"}
    assert {c.name for c in ContributionRow.__table__.columns} == {
        "id", "household_id", "member_id", "kind", "day", "event_id", "meal_id", "item_id", "created_at",
    }  # fmt: skip
    with pytest.raises(Forbidden):  # nor does a guest or anyone but a member reach them
        async with house.sessionmaker() as db:
            await stats.household_stats(db, replace(house["Kim"], role=Role.GUEST))


# Two finishes at once --------------------------------------------------------------------------


async def test_two_mark_done_taps_at_once_count_the_call_once(house: House) -> None:
    event_id = await dinner(house, done=False)
    assert (await house.event(event_id)).state is EventState.DECIDED
    house.clock.now = MEAL_TIME
    results = await asyncio.gather(
        act(house, calls.mark_done, house["Alex"], event_id),
        act(house, calls.mark_done, house["Sam"], event_id),
        return_exceptions=True,
    )
    assert sum(r is None for r in results) == 1  # the second finds it done
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 1
    assert await rows(house) == len(await snapshot(house))
    found = await awards(house)
    assert len(found) == len(set(found)) and found


async def test_two_recordings_racing_for_the_same_call_award_each_badge_once(house: House) -> None:
    event_id = await dinner(house)
    await forget_everything(house)
    event = await house.event(event_id)

    async def record() -> list[Any]:
        async with house.sessionmaker() as db:
            made = await contributions.record_done(db, event, now=house.clock())
            await db.commit()
            return made

    first, second = await asyncio.gather(record(), record())
    assert len(first) + len(second) == len(await awards(house))  # each award was made by exactly one of them
    keys = [(a[0], a[1], a[2]) for a in await awards(house)]
    assert len(keys) == len(set(keys)) and keys
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 1


# Logs ------------------------------------------------------------------------------------------


async def test_the_story_of_a_finished_call_names_ids_and_badges_and_no_email(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await dinner(house)
    lines = [r for r in caplog.records if r.getMessage() in {"contributions_recorded", "badge_earned"}]
    recorded = [r for r in lines if r.getMessage() == "contributions_recorded"]
    assert len(recorded) == 1 and recorded[0].event_id and recorded[0].credits > 0  # type: ignore[attr-defined]
    earned = [r for r in lines if r.getMessage() == "badge_earned"]
    assert {(r.member, r.badge, r.tier) for r in earned} >= {("Alex", "road_runner", "first")}  # type: ignore[attr-defined]
    assert all(getattr(r, "member_id", None) for r in earned)
    assert "example.com" not in " ".join(str(r.__dict__) for r in lines)
    caplog.clear()
    event = await house.event(await dinner_id(house))
    async with house.sessionmaker() as db:
        await contributions.record_done(db, event, now=house.clock())
        await db.commit()
    assert [r for r in caplog.records if r.getMessage() in {"contributions_recorded", "badge_earned"}] == []


async def test_the_feedback_log_line_does_not_name_the_meal(house: House, caplog: pytest.LogCaptureFixture) -> None:
    await dinner(house)
    (meal,) = await house.meals()
    caplog.set_level(logging.INFO)
    await give(house, "Alex", meal.id)
    lines = [r for r in caplog.records if r.getMessage() == "contributions_recorded"]
    assert lines and not any(hasattr(r, "meal_id") for r in lines)


# Catching up, and counting that fails -----------------------------------------------------------


async def test_badges_earned_while_catching_up_are_not_news(house: House) -> None:
    await dinner(house)
    await forget_everything(house)
    async with house.sessionmaker() as db:
        await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
        view = await stats.household_stats(db, house["Kim"])
        assert view.recent == () and view.badges_earned > 0  # earned, but not the newest


async def test_a_failure_while_counting_never_blocks_mark_done_and_the_next_start_counts_the_call(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = contributions._award

    async def broken(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        raise RuntimeError("boom")

    async with house.sessionmaker() as db:  # the household has been through the first start already
        await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    monkeypatch.setattr(contributions, "_award", broken)
    event_id = await dinner(house)
    assert (await house.event(event_id)).state is EventState.DONE  # the transition committed
    assert await rows(house) == 0 and await awards(house) == []  # and the savepoint took the half-done counts back
    assert any(r.getMessage() == "counting contributions failed" and r.exc_info for r in caplog.records)

    monkeypatch.undo()
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 1
    found = await awards(house)
    assert found and {a[3] for a in found} == {Announcement.SKIPPED}  # a missed award is never announced late
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is False


async def make_history(house: House, n: int) -> None:
    """``n`` done dinners on as many days, each with a meal, a pickup, and one answer In."""
    first = date(2020, 1, 1)
    sam, alex = house["Sam"].id, house["Alex"].id
    item_ids = [i.id for i in house.items.values()]
    stamp = datetime(2026, 10, 1, tzinfo=UTC)
    async with house.sessionmaker() as db:
        for i in range(n):
            day = first + timedelta(days=i)
            event = EventRow(
                household_id=house.household.id, kind="call", slot="dinner", date=day, state="done", run=1, version=1,
                opens_at=stamp, respond_deadline=stamp, reminder_minutes=15, vote_minutes=45,
                decided_item_id=item_ids[i % 3], decided_mode="pickup", created_at=stamp, updated_at=stamp,
            )  # fmt: skip
            db.add(event)
            await db.flush()
            hid = house.household.id
            db.add(AssignmentRow(household_id=hid, event_id=event.id, member_id=alex, role="pickup", created_at=stamp))
            db.add(
                ResponseRow(
                    household_id=hid, event_id=event.id, member_id=sam, status="in", guests=0, guest_needs=[],
                    offers_cook=False, offers_pickup=False, offers_order=False, maybe_at_deadline=False,
                    responded_at=stamp, updated_at=stamp,
                )
            )  # fmt: skip
            db.add(
                MealLogRow(
                    household_id=hid, date=day, slot="dinner", item_id=item_ids[i % 3], cuisine_tags=[],
                    participants=[], event_id=event.id, created_at=stamp, updated_at=stamp,
                )
            )  # fmt: skip
        await db.commit()


async def counted_events(house: House) -> int:
    async with house.sessionmaker() as db:
        return int(await db.scalar(select(func.count(func.distinct(ContributionRow.event_id)))) or 0)


async def marker(house: House) -> bool:
    async with house.sessionmaker() as db:
        stored = await db.scalar(select(HouseholdRow.settings))
    return contributions.BACKFILLED_KEY in (stored or {})


async def test_a_long_history_backfills_in_linear_time_and_batches(house: House) -> None:
    """2,000 done dinners, each with a meal, a cook, and two answers, are counted with a handful of
    queries per call, not one that rereads the whole log, and the write lock is taken per batch."""
    n = 2_000
    await make_history(house, n)
    statements: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    async with house.sessionmaker() as db:
        engine = db.get_bind()
        sa_event.listen(engine, "before_cursor_execute", count)
        try:
            assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
        finally:
            sa_event.remove(engine, "before_cursor_execute", count)
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == n
    # About 5 reads and 2 inserts per call; rereading the whole log for each call would add 2,000 big reads.
    assert len(statements) < 14 * n
    assert not [s for s in statements if "FROM meal_log" in s and "JOIN" in s][1:]  # the log is read once
    assert sum("BEGIN IMMEDIATE" in s for s in statements) <= n // contributions.BATCH + 2  # a lock per batch


async def test_a_log_line_does_not_outlive_counting_that_was_rolled_back(
    house: House, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The failure comes after the lines were noted, so only rewinding them keeps the log honest."""
    real = contributions.record_done

    async def noted_then_failed(*args: Any, **kwargs: Any) -> Any:
        await real(*args, **kwargs)
        raise RuntimeError("boom")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(contributions, "record_done", noted_then_failed)
    event_id = await dinner(house)
    assert (await house.event(event_id)).state is EventState.DONE and await rows(house) == 0
    told = {r.getMessage() for r in caplog.records}
    assert "counting contributions failed" in told and "done" in told
    assert not told & {"contributions_recorded", "badge_earned"}


async def test_calls_with_nothing_to_credit_settle_after_the_first_start(house: House) -> None:
    async with house.sessionmaker() as db:
        db.add(
            EventRow(
                household_id=house.household.id, kind="call", slot="dinner", date=DAY, state="done", run=1, version=1,
                opens_at=MORNING, respond_deadline=MORNING, reminder_minutes=15, vote_minutes=45,
                created_at=MORNING, updated_at=MORNING,
            )
        )  # fmt: skip
        await db.commit()
    seen = []
    for _ in range(3):
        async with house.sessionmaker() as db:
            seen.append(await contributions.ensure_backfilled(db, house.household.id, now=house.clock()))
    assert seen == [True, False, False]


async def test_a_backfill_killed_halfway_resumes_and_sets_its_marker_last(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_history(house, 30)
    monkeypatch.setattr(contributions, "BATCH", 2)
    real = contributions._count_event
    calls = 0

    async def killed(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise KeyboardInterrupt("killed")
        return await real(*args, **kwargs)

    monkeypatch.setattr(contributions, "_count_event", killed)
    async with house.sessionmaker() as db:
        with pytest.raises(KeyboardInterrupt):
            await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    assert not await marker(house)
    assert await counted_events(house) == 2  # the first batch committed; the second went back

    monkeypatch.setattr(contributions, "_count_event", real)
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert await marker(house) and await counted_events(house) == 30
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 30
    assert {a[3] for a in await awards(house)} == {Announcement.SKIPPED}


async def test_a_call_discarded_while_the_backfill_waits_is_skipped(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_history(house, 5)
    real = EventRepository.done

    async def then_discarded(self: EventRepository, household_id: int, **kw: Any) -> Any:
        found = await real(self, household_id, **kw)
        async with house.sessionmaker() as other:  # a test instance's discard, between the list and the batch
            await EventRepository(other).discard(household_id, found[2].id)
            await other.commit()
        return found

    monkeypatch.setattr(EventRepository, "done", then_discarded)
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert await counted_events(house) == 4 and await marker(house)


# Gambler (#108): every pick or none ---------------------------------------------------------------


async def three_picks(house: House, event_id: int) -> None:
    """Tops the call up to three picks: the catalog fixture draws only two."""
    cuisine = await house.tag("Vietnamese")
    for name in ("Pho House", "Banh Mi Bar"):
        if len((await house.context(event_id)).proposals) < 3:
            await act(
                house, calls.add_proposal, house["Alex"], event_id, Mode.PICKUP, free_text=name, cuisine_id=cuisine.id
            )
    assert len((await house.context(event_id)).proposals) >= 3


async def votes_of(house: House, event_id: int, name: str) -> None:
    """``name`` votes for every pick."""
    for pick in (await house.context(event_id)).proposals:
        await act(house, calls.vote, house[name], event_id, pick.id)


async def test_voting_for_every_pick_or_none_earns_gambler_and_some_does_not(house: House) -> None:
    """Alex votes for nothing, Kim for every pick, and Sam for one. The call has three picks."""

    async def kim_votes_all(event_id: int) -> None:
        await three_picks(house, event_id)
        await votes_of(house, event_id, "Kim")

    await dinner(house, ins=("Alex", "Sam", "Kim"), while_voting=kim_votes_all)
    assert (await counts(house, "Alex"))[Kind.GAMBLED] == 1
    assert (await counts(house, "Kim"))[Kind.GAMBLED] == 1
    assert Kind.GAMBLED not in await counts(house, "Sam")
    assert Kind.GAMBLED not in await counts(house, "Lee")  # never answered: no vote to leave
    found = await awards(house)
    assert ("Alex", BadgeKey.GAMBLER, Tier.FIRST, Announcement.PENDING) in found  # announced like most badges
    assert not [a for a in found if a[0] == "Sam" and a[1] is BadgeKey.GAMBLER]


async def test_a_vote_counts_as_it_stood_when_the_call_was_done_after_an_unvote(house: House) -> None:
    async def vote_then_undo(event_id: int) -> None:
        await three_picks(house, event_id)
        await votes_of(house, event_id, "Kim")  # all of them, then...
        await act(house, calls.unvote, house["Kim"], event_id, (await house.context(event_id)).proposals[0].id)
        first = (await house.context(event_id)).proposals[0].id
        await act(house, calls.vote, house["Alex"], event_id, first)  # a vote taken back leaves none...
        await act(house, calls.unvote, house["Alex"], event_id, first)

    await dinner(house, ins=("Alex", "Sam", "Kim"), while_voting=vote_then_undo)
    assert Kind.GAMBLED not in await counts(house, "Kim")  # ...two of three is a choice
    assert (await counts(house, "Alex"))[Kind.GAMBLED] == 1  # ...and none is not


async def test_a_late_joiner_never_earns_gambler_for_a_call_and_a_guest_never_does(house: House) -> None:
    async def invite(event_id: int) -> None:
        invited = await act(house, guests.invite, house["Sam"], event_id, new_guest=GuestInput("Riley"))
        await act(house, guests.answer_for, house["Sam"], event_id, invited.guest.id, status=Status.IN)

    async def join_and_vote_all(event_id: int) -> None:
        await three_picks(house, event_id)
        await act(house, calls.join_late, house["Kim"], event_id)
        await votes_of(house, event_id, "Kim")

    await dinner(house, while_answering=invite, while_voting=join_and_vote_all)
    assert await counts(house, "Kim") == {Kind.PARTICIPATED: 1}
    async with house.sessionmaker() as db:
        kinds = set(await db.scalars(select(ContributionRow.kind).where(ContributionRow.member_id != house["Alex"].id)))
    assert Kind.GAMBLED not in kinds  # neither Kim, late, nor Riley, a guest
    assert (await counts(house, "Alex"))[Kind.GAMBLED] == 1


async def test_a_call_with_two_picks_gives_no_gambler_for_all_or_none(house: House) -> None:
    async def down_to_two(event_id: int) -> None:
        assert len((await house.context(event_id)).proposals) == 2  # what the fixture draws
        await votes_of(house, event_id, "Kim")

    await dinner(house, ins=("Alex", "Sam", "Kim"), while_voting=down_to_two)
    assert Kind.GAMBLED not in await counts(house, "Alex") and Kind.GAMBLED not in await counts(house, "Kim")


async def test_the_backfill_counts_gambler_for_history(house: House) -> None:
    async def kim_votes_all(event_id: int) -> None:
        await three_picks(house, event_id)
        await votes_of(house, event_id, "Kim")

    await dinner(house, ins=("Alex", "Sam", "Kim"), while_voting=kim_votes_all)
    counted = await snapshot(house)
    assert any(row[1] == Kind.GAMBLED.value for row in counted)
    await forget_everything(house)
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert await snapshot(house) == counted
    assert {a[3] for a in await awards(house) if a[1] is BadgeKey.GAMBLER} == {Announcement.SKIPPED}
    async with house.sessionmaker() as db:
        stored = await db.scalar(select(HouseholdRow.settings))
    assert stored is not None and contributions.GAMBLER_KEY in stored  # a new household needs no catch-up


async def before_gambler(house: House) -> list[tuple[Any, ...]]:
    """The database of a household that ran the 0016 backfill before Gambler: every other count
    and badge is there, and the gambled counts, their badges, and the catch-up marker are not.
    Returns the counts it still has."""
    async with house.sessionmaker() as db:
        await db.execute(text("DELETE FROM badge_award WHERE badge_key = 'gambler'"))
        await db.execute(text("DELETE FROM contribution WHERE kind = 'gambled'"))
        stored = (await db.scalar(select(HouseholdRow.settings))) or {}
        await db.execute(
            text("UPDATE household SET settings = :s"),
            {"s": json.dumps({k: v for k, v in stored.items() if k != contributions.GAMBLER_KEY})},
        )
        await db.commit()
    return await snapshot(house)


async def test_a_household_that_ran_the_backfill_before_gambler_is_caught_up_once_and_quietly(house: House) -> None:
    async def kim_votes_all(event_id: int) -> None:
        await three_picks(house, event_id)
        await votes_of(house, event_id, "Kim")

    await dinner(house, n=0, ins=("Alex", "Sam", "Kim"), while_voting=kim_votes_all)
    await dinner(house, n=1)
    async with house.sessionmaker() as db:  # the household has been through the first start
        await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    full = await snapshot(house)
    others = await before_gambler(house)
    assert len(others) < len(full)
    others_awards = await awards(house)
    async with house.sessionmaker() as db:
        waiting = await contributions.pending_announcements(db, house.household.id)

    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert await snapshot(house) == full  # Gambler is back, and nothing else moved
    found = [a for a in await awards(house) if a[1] is BadgeKey.GAMBLER]
    assert {(a[0], a[2], a[3]) for a in found} == {
        ("Alex", Tier.FIRST, Announcement.SKIPPED),
        ("Kim", Tier.FIRST, Announcement.SKIPPED),
    }
    assert [a for a in await awards(house) if a[1] is not BadgeKey.GAMBLER] == others_awards
    async with house.sessionmaker() as db:
        assert await contributions.pending_announcements(db, house.household.id) == waiting  # nothing new to tell
        stored = await db.scalar(select(HouseholdRow.settings))
        assert stored is not None and stored[contributions.GAMBLER_KEY] == house.clock.now.isoformat()
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is False  # once
    assert await snapshot(house) == full


async def test_the_gambler_catch_up_survives_a_start_that_dies_and_counts_an_uncounted_call_in_full(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    await dinner(house, n=0)
    async with house.sessionmaker() as db:
        await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    full = await snapshot(house)
    await before_gambler(house)

    real = contributions._award

    async def dies(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("killed")

    monkeypatch.setattr(contributions, "_award", dies)
    async with house.sessionmaker() as db:
        with pytest.raises(RuntimeError):
            await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    async with house.sessionmaker() as db:
        stored = await db.scalar(select(HouseholdRow.settings))
    assert stored is not None and contributions.GAMBLER_KEY not in stored  # marked last, so it runs again
    monkeypatch.setattr(contributions, "_award", real)
    async with house.sessionmaker() as db:
        assert await contributions.ensure_backfilled(db, house.household.id, now=house.clock()) is True
    assert await snapshot(house) == full

    # A call with no counts at all, during a catch-up, still gets every kind.
    await dinner(house, n=1)
    await before_gambler(house)
    async with house.sessionmaker() as db:
        await db.execute(text("DELETE FROM contribution WHERE event_id = (SELECT max(id) FROM event)"))
        await db.commit()
        await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    assert (await counts(house, "Alex"))[Kind.PICKED_UP] == 2


async def test_gambler_reads_every_calls_votes_in_one_query_not_one_per_member(house: House) -> None:
    async def kim_votes_all(event_id: int) -> None:
        await three_picks(house, event_id)
        await votes_of(house, event_id, "Kim")

    event_id = await dinner(house, ins=("Alex", "Sam", "Kim", "Lee"), while_voting=kim_votes_all)
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    event = await house.event(event_id)
    engine = house.sessionmaker.kw["bind"].sync_engine
    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        async with house.sessionmaker() as db:
            await contributions.record_done(db, event, now=house.clock())
            await db.commit()
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)
    assert len([s for s in statements if "FROM vote" in s]) == 1
