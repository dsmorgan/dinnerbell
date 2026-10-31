# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback-informed ranking end to end (FR-55, ADR 0008): ratings move the rater's own score
while they eat, the Feedback setting caps it, a stance set on Preferences wins over earlier
ratings, and the household's hint is the first reason on a drawn pick, naming nobody."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest

from dinnerbell.config import Settings
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.preferences import PreferenceRepository
from dinnerbell.domain.feedback import FeedbackInput, PlaceIssue, Rating
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.domain.schedule import Weights, feedback_cap
from dinnerbell.services import feedback, meals
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.preferences import group_preferences, set_my_preference, with_ratings
from tests.support.calls import House, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.feedback import done_dinner
from tests.test_calls_services import ranked_with, to_voting

pytestmark = pytest.mark.anyio

MEAL_DAY = date(2026, 10, 10)
"""Twelve days before the dinner call on 2026-10-22, outside its variety window."""
RATED_AT = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    house.clock.now = RATED_AT
    return house


async def log(house: House, item: str, *people: str, day: date = MEAL_DAY) -> int:
    data = MealInput(day, Slot.LUNCH, house.items[item].id, frozenset(house[p].id for p in people))
    async with house.sessionmaker() as db:
        meal = await meals.log_meal(db, house["Alex"], data, today=day, now=house.clock())
    return meal.id


async def rate(house: House, name: str, meal_id: int, rating: Rating | None, **more: object) -> None:
    async with house.sessionmaker() as db:
        await feedback.give(db, house[name], meal_id, FeedbackInput(rating=rating, **more), now=house.clock())  # type: ignore[arg-type]


async def stance(house: House, name: str, item: str, value: Stance) -> None:
    async with house.sessionmaker() as db:
        await set_my_preference(db, house[name], TargetType.ITEM, house.items[item].id, value, now=house.clock())


async def scores(house: House, *names: str, day: date = MEAL_DAY) -> dict[int, float]:
    ids = frozenset(house[n].id for n in names)
    async with house.sessionmaker() as db:
        group = await group_preferences(db, house.household.id, ids)
        cap = feedback_cap(await household_settings(db, house.household.id))
        return dict((await with_ratings(db, house.household.id, ids, group, day=day, cap=cap)).item_scores)


async def set_feedback(house: House, level: int) -> None:
    async with house.sessionmaker() as db:
        values = schedule_service.RankingSettings(Weights(), 3, 7, feedback=level)
        await schedule_service.update_ranking(db, house["Alex"], values, now=house.clock())


async def test_a_rating_moves_only_the_raters_own_score_while_they_eat(house: House) -> None:
    siam = house.items["Siam Garden"].id
    meal = await log(house, "Siam Garden", "Kim", "Sam")  # a logged meal counts
    await rate(house, "Kim", meal, Rating.HATED)
    assert await scores(house, "Kim", "Sam") == {siam: -1.5}
    assert await scores(house, "Sam", "Lee") == {}  # Kim isn't eating: her rating counts for nothing
    await stance(house, "Sam", "Siam Garden", Stance(score=1))
    assert await scores(house, "Kim", "Sam") == {siam: -0.5}
    later = await scores(house, "Kim", "Sam", day=MEAL_DAY + timedelta(days=90))
    assert later == {siam: pytest.approx(1 - 0.75)}  # Kim's -1.5 halved; Sam's +1 is his own setting


async def test_the_feedback_setting_caps_ratings_and_0_turns_them_off(house: House) -> None:
    siam = house.items["Siam Garden"].id
    meal = await log(house, "Siam Garden", "Kim", "Sam")
    await rate(house, "Kim", meal, Rating.LOVED)
    await rate(house, "Sam", meal, Rating.LOVED)
    assert await scores(house, "Kim", "Sam") == {siam: 2.0}
    await set_feedback(house, 1)
    assert await scores(house, "Kim", "Sam") == {siam: 1.0}
    await set_feedback(house, 0)
    assert await scores(house, "Kim", "Sam") == {}
    async with house.sessionmaker() as db:  # the hint shows at any setting
        assert await feedback.hints_on(db, house.household.id, MEAL_DAY) == {siam: "Well liked"}


async def test_a_stance_set_on_preferences_wins_over_earlier_ratings(house: House) -> None:
    siam = house.items["Siam Garden"].id
    meal = await log(house, "Siam Garden", "Kim", "Sam")
    await rate(house, "Kim", meal, Rating.HATED)
    house.clock.now += timedelta(minutes=5)
    await stance(house, "Kim", "Siam Garden", Stance(score=1))
    assert await scores(house, "Kim") == {siam: 1.0}
    house.clock.now += timedelta(minutes=5)
    await stance(house, "Kim", "Siam Garden", Stance())  # back to 0: the row goes, the edit stays
    assert await scores(house, "Kim") == {}
    async with house.sessionmaker() as db:
        edits = await PreferenceRepository(db).item_edits(house.household.id, [house["Kim"].id])
    assert edits == {(house["Kim"].id, siam): house.clock()}

    house.clock.now += timedelta(days=1)
    again = await log(house, "Siam Garden", "Kim", "Sam", day=MEAL_DAY + timedelta(days=1))
    await rate(house, "Kim", again, Rating.LOVED)  # rated after the edit: it counts
    assert await scores(house, "Kim", day=MEAL_DAY + timedelta(days=1)) == {siam: 1.0}
    await stance(house, "Kim", "Taqueria", Stance(veto=True))  # another item's edit changes nothing here
    assert await scores(house, "Kim", day=MEAL_DAY + timedelta(days=1)) == {siam: 1.0}


async def test_re_saving_feedback_doesnt_bring_back_an_overridden_rating(house: House) -> None:
    """Only a changed rating counts after a stance edit: a thank-you added later, or the same
    rating submitted again, leaves the stance in charge."""
    tacos = house.items["Sam's Tacos"].id
    event_id = await done_dinner(house)  # Sam cooked; Kim ate
    async with house.sessionmaker() as db:
        meal = await feedback.meal_for_event(db, house["Kim"], event_id)
    day = (await house.event(event_id)).day
    await rate(house, "Kim", meal, Rating.HATED)
    house.clock.now += timedelta(minutes=5)
    await stance(house, "Kim", "Sam's Tacos", Stance(score=1))
    house.clock.now += timedelta(minutes=5)
    await rate(house, "Kim", meal, Rating.HATED, cook_thanks=True)  # only a thank-you added
    assert await scores(house, "Kim", day=day) == {tacos: 1.0}
    house.clock.now += timedelta(minutes=5)
    await rate(house, "Kim", meal, Rating.HATED, cook_thanks=True)  # the same answers, submitted again
    assert await scores(house, "Kim", day=day) == {tacos: 1.0}
    house.clock.now += timedelta(minutes=5)
    await rate(house, "Kim", meal, Rating.LOVED)  # a new rating counts
    assert await scores(house, "Kim", day=day) == {tacos: 2.0}


async def test_the_hint_is_the_first_reason_on_a_drawn_pick_and_names_nobody(house: House) -> None:
    siam = house.items["Siam Garden"].id
    meal = await log(house, "Siam Garden", "Alex", "Sam", "Kim")
    await rate(house, "Kim", meal, Rating.LOVED)
    await rate(house, "Sam", meal, Rating.LOVED)
    await rate(house, "Alex", meal, Rating.HATED, place_issues=frozenset({PlaceIssue.OTHER}), issue_note="Qz8 soggy")
    async with house.sessionmaker() as db:
        opinions = await FeedbackRepository(db).opinions(house.household.id)
    assert "Qz8" not in repr(opinions)  # ranking never even reads what went wrong

    event_id = await to_voting(house)  # Alex and Sam eat on 2026-10-22; Alex picks up
    ctx = await house.context(event_id)
    (pick,) = [p for p in ctx.proposals if p.item_id == siam]
    assert pick.reasons[0] == "Mixed reviews · a recent problem."
    shown = " ".join(" ".join(p.reasons) for p in ctx.proposals)
    assert "Qz8" not in shown and not any(name in pick.reasons[0] for name in ("Alex", "Sam", "Kim", "Lee"))
    # Kim isn't eating, so only Alex's -1.5 and Sam's +1 count, each 12 days faded.
    snapshot = await ranked_with(house, ctx)
    assert snapshot.preferences.item_scores[siam] == pytest.approx(-0.5 * 0.5 ** (12 / 90), abs=1e-6)
    assert snapshot.hints == {siam: "Mixed reviews · a recent problem"}
    ready = next(e for e in await house.outbox() if e.kind == "proposals_ready")
    assert "Mixed reviews · a recent problem." in str(ready.payload) and "Qz8" not in str(ready.payload)
