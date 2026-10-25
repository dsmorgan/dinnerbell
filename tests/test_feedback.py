# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback after meals (spec 5.9, FR-45 to FR-49): the rules, the services, the reminder, and
who may see what. Nobody ever sees who chose "not again"."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.events import AssignmentRepository
from dinnerbell.db.models import FeedbackRow
from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.events import AssignmentRole, StaleRun
from dinnerbell.domain.feedback import (
    CookThanks,
    Feedback,
    FeedbackInput,
    InvalidFeedback,
    ItemCounts,
    MealFacts,
    PlaceIssue,
    Rating,
)
from dinnerbell.domain.meals import MealInput, MemberRef, Slot
from dinnerbell.services import feedback, meals
from dinnerbell.services.database import Database, open_database
from dinnerbell.services.errors import NotFound
from tests.support.calls import DONE_AT, OPENS, House, dinner_id, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.feedback import done_dinner, remind

DAY = date(2026, 10, 22)
SAM, KIM = MemberRef(2, "Sam"), MemberRef(3, "Kim")
HOME = MealFacts(1, DAY, Slot.DINNER, 11, "Sam's Tacos", ItemKind.HOME, Mode.COOK, SAM, (SAM, KIM), 7)
PLACE = MealFacts(2, DAY, Slot.DINNER, 12, "Siam Garden", ItemKind.RESTAURANT, Mode.PICKUP, None, (SAM, KIM), 8)


# Services ---------------------------------------------------------------------------------------

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    return await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))


async def meal_of(house: House, event_id: int) -> int:
    (meal,) = [m for m in await house.meals() if m.event_id == event_id]
    return meal.id


async def give(house: House, name: str, meal_id: int, answers: FeedbackInput) -> Feedback | None:
    async with house.sessionmaker() as db:
        return await feedback.give(db, house[name], meal_id, answers, now=house.clock())


async def log(house: House, item: str, *people: str, day: date = DAY) -> int:
    data = MealInput(day, Slot.LUNCH, house.items[item].id, frozenset(house[p].id for p in people))
    async with house.sessionmaker() as db:
        meal = await meals.log_meal(db, house["Alex"], data, today=day, now=house.clock())
    return meal.id


async def rows(house: House) -> list[FeedbackRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(FeedbackRow).order_by(FeedbackRow.id)))


async def test_feedback_on_a_home_meal_snapshots_it_and_an_edit_replaces_it(house: House) -> None:
    event_id = await done_dinner(house)
    meal_id = await meal_of(house, event_id)
    async with house.sessionmaker() as db:
        form = await feedback.form_for_event(db, house["Kim"], event_id, run=1, now=house.clock())
    assert form.facts.item_kind is ItemKind.HOME and form.facts.cook == MemberRef(house["Sam"].id, "Sam")
    assert form.thanks_cook and form.mine is None and form.closes_on == date(2026, 10, 29)

    saved = await give(house, "Kim", meal_id, FeedbackInput(food=Rating.LOVED, cook_note="Best tacos yet"))
    assert saved is not None and saved.answers.cook_thanks
    assert (saved.item_id, saved.item_kind, saved.mode) == (house.items["Sam's Tacos"].id, ItemKind.HOME, Mode.COOK)
    assert saved.cook == MemberRef(house["Sam"].id, "Sam")
    assert {p.name for p in saved.participants} == {"Alex", "Kim", "Sam"}

    house.clock.now = DONE_AT + timedelta(days=6)
    answers = FeedbackInput(food=Rating.FINE)
    async with house.sessionmaker() as db:
        await feedback.give_for_event(db, house["Kim"], event_id, answers, run=1, now=house.clock())
        form = await feedback.form(db, house["Kim"], meal_id, now=house.clock())
    assert form.mine == answers
    (row,) = await rows(house)
    assert (row.food, row.cook_thanks, row.cook_note, row.created_at) == ("fine", False, None, saved.created_at)
    assert row.updated_at == house.clock()


async def test_who_can_give_feedback_and_until_when(house: House) -> None:
    event_id = await done_dinner(house)
    meal_id = await meal_of(house, event_id)
    with pytest.raises(InvalidFeedback, match="Only people who ate"):
        await give(house, "Lee", meal_id, FeedbackInput(food=Rating.LOVED))
    with pytest.raises(InvalidFeedback, match="You cooked"):
        await give(house, "Sam", meal_id, FeedbackInput(cook_thanks=True))
    with pytest.raises(InvalidFeedback, match="only for restaurants"):
        await give(house, "Kim", meal_id, FeedbackInput(place=Rating.LOVED))
    async with house.sessionmaker() as db:
        with pytest.raises(StaleRun):
            await feedback.form_for_event(db, house["Kim"], event_id, run=2, now=house.clock())
        with pytest.raises(NotFound):
            await feedback.form(db, house["Kim"], 999, now=house.clock())
    house.clock.now = DONE_AT + timedelta(days=8)  # Oct 30 in Chicago: the window closed on the 29th
    with pytest.raises(InvalidFeedback, match="has closed"):
        await give(house, "Kim", meal_id, FeedbackInput(food=Rating.LOVED))
    assert await rows(house) == []


async def test_a_call_not_done_yet_takes_no_feedback(house: House) -> None:
    house.clock.now = OPENS
    await house.tick()
    event_id = await dinner_id(house)
    async with house.sessionmaker() as db:
        with pytest.raises(InvalidFeedback, match="once the meal is done"):
            await feedback.form_for_event(db, house["Kim"], event_id, run=1, now=house.clock())
        with pytest.raises(NotFound):
            await feedback.form_for_event(db, house["Kim"], 999, run=1, now=house.clock())


async def test_empty_feedback_takes_it_back(house: House) -> None:
    meal_id = await meal_of(house, await done_dinner(house))
    await give(house, "Kim", meal_id, FeedbackInput(food=Rating.NOT_AGAIN))
    assert len(await rows(house)) == 1
    assert await give(house, "Kim", meal_id, FeedbackInput()) is None
    assert await rows(house) == []
    assert await give(house, "Kim", meal_id, FeedbackInput()) is None  # nothing to take back


async def test_a_restaurant_meal_takes_place_feedback_and_no_thanks(house: House) -> None:
    meal_id = await meal_of(house, await done_dinner(house, "Siam Garden", Mode.PICKUP))
    issues = frozenset({PlaceIssue.COLD, PlaceIssue.LATE})
    saved = await give(house, "Kim", meal_id, FeedbackInput(place=Rating.NOT_AGAIN, place_issues=issues))
    assert saved is not None and saved.cook is None and saved.mode is Mode.PICKUP
    (row,) = await rows(house)
    assert row.place_issues == ["cold", "late"]
    with pytest.raises(InvalidFeedback, match="no cook"):
        await give(house, "Kim", meal_id, FeedbackInput(cook_thanks=True))


async def test_a_logged_meal_takes_feedback_from_the_people_named_on_it(house: House) -> None:
    house.clock.now = DONE_AT
    meal_id = await log(house, "Sam's Tacos", "Alex", "Lee")
    saved = await give(house, "Lee", meal_id, FeedbackInput(food=Rating.LOVED))
    assert saved is not None and saved.mode is None and saved.cook is None
    with pytest.raises(InvalidFeedback, match="no cook"):
        await give(house, "Lee", meal_id, FeedbackInput(cook_thanks=True))
    with pytest.raises(InvalidFeedback, match="Only people who ate"):
        await give(house, "Kim", meal_id, FeedbackInput(food=Rating.LOVED))
    async with house.sessionmaker() as db:
        assert await feedback.awaiting(db, house["Alex"], now=house.clock()) == []  # Home lists calls only


async def test_deleting_a_meal_deletes_its_feedback(house: House) -> None:
    house.clock.now = DONE_AT
    meal_id = await log(house, "Taqueria", "Alex")
    await give(house, "Alex", meal_id, FeedbackInput(food=Rating.FINE))
    async with house.sessionmaker() as db:
        await meals.delete_meal(db, house["Alex"], meal_id, now=house.clock())
    assert await rows(house) == []


async def test_meals_await_feedback_until_given_or_closed(house: House) -> None:
    event_id = await done_dinner(house)
    async with house.sessionmaker() as db:
        (waiting,) = await feedback.awaiting(db, house["Kim"], now=house.clock())
        assert await feedback.awaiting(db, house["Lee"], now=house.clock()) == []  # didn't eat
    assert (waiting.event_id, waiting.run, waiting.item_name) == (event_id, 1, "Sam's Tacos")
    assert waiting.closes_on == date(2026, 10, 29)
    await give(house, "Kim", waiting.meal_id, FeedbackInput(food=Rating.LOVED))
    async with house.sessionmaker() as db:
        assert await feedback.awaiting(db, house["Kim"], now=house.clock()) == []
        assert len(await feedback.awaiting(db, house["Alex"], now=house.clock())) == 1
    house.clock.now = DONE_AT + timedelta(days=8)
    async with house.sessionmaker() as db:
        assert await feedback.awaiting(db, house["Alex"], now=house.clock()) == []


async def test_the_reminder_goes_once_to_those_who_turned_it_on(house: House) -> None:
    await remind(house, "Kim", "Lee")  # Lee doesn't eat, so gets nothing
    event_id = await done_dinner(house)
    sent = [e for e in await house.outbox() if e.kind == "feedback_reminder"]
    assert [(e.idempotency_key, e.payload["member"]["name"], e.payload["item"], e.payload["closes"]) for e in sent] == [
        (f"event:{event_id}:run1:feedback_reminder:member{house['Kim'].id}", "Kim", "Sam's Tacos", "2026-10-29")
    ]


# Who sees what (FR-49) -------------------------------------------------------------------------


async def test_nobody_sees_who_chose_not_again(house: House) -> None:
    meal_id = await meal_of(house, await done_dinner(house))
    await give(house, "Kim", meal_id, FeedbackInput(food=Rating.NOT_AGAIN, cook_note="Thanks anyway"))
    await give(house, "Alex", meal_id, FeedbackInput(food=Rating.LOVED, cook_thanks=True))
    sam = MemberRef(house["Sam"].id, "Sam")
    async with house.sessionmaker() as db:
        meal = await meals.get_meal(db, house["Alex"], meal_id)
        seen = {
            name: (await feedback.for_meals(db, house[name], [meal], today=DAY + timedelta(days=1)))[meal_id]
            for name in ("Sam", "Alex", "Lee")
        }
        counts = await feedback.item_counts(db, house["Lee"])
    # Sam, the cook, sees the thank-yous with names and notes, but nobody's rating.
    assert seen["Sam"].thanks == CookThanks(2, sam, (("Kim", "Thanks anyway"), ("Alex", None)))
    assert seen["Sam"].mine is None
    assert seen["Alex"].mine == FeedbackInput(food=Rating.LOVED, cook_thanks=True)  # only their own answers
    assert seen["Alex"].thanks == CookThanks(2, sam) and seen["Alex"].can_give
    assert seen["Lee"].mine is None and seen["Lee"].thanks == CookThanks(2, sam) and not seen["Lee"].can_give
    assert counts[house.items["Sam's Tacos"].id] == ItemCounts(food_loved=1)
    for view in seen.values():
        assert "not_again" not in repr(view) and "NOT_AGAIN" not in repr(view)


async def test_saving_feedback_logs_and_audits_no_rating(house: House, caplog: pytest.LogCaptureFixture) -> None:
    meal_id = await meal_of(house, await done_dinner(house))
    caplog.set_level("DEBUG", logger="dinnerbell")
    await give(house, "Kim", meal_id, FeedbackInput(food=Rating.NOT_AGAIN, cook_note="Too salty"))
    await give(house, "Kim", meal_id, FeedbackInput())
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "feedback saved" in logged and "feedback removed" in logged
    assert "not_again" not in logged and "Not again" not in logged and "Too salty" not in logged
    assert await house.audits("feedback") == []


async def test_a_new_cook_never_sees_thanks_written_to_the_old_one(house: House) -> None:
    event_id = await done_dinner(house)
    meal_id = await meal_of(house, event_id)
    await give(house, "Kim", meal_id, FeedbackInput(cook_note="For Sam"))
    async with house.sessionmaker() as db:  # the admin corrects who cooked
        await AssignmentRepository(db).assign(
            house.household.id, event_id, AssignmentRole.COOK, house["Lee"].id, house.clock()
        )
        await db.commit()
    await give(house, "Alex", meal_id, FeedbackInput(cook_note="For Lee"))
    lee = MemberRef(house["Lee"].id, "Lee")
    async with house.sessionmaker() as db:
        meal = await meals.get_meal(db, house["Alex"], meal_id)
        seen = {
            name: (await feedback.for_meals(db, house[name], [meal], today=DAY))[meal_id].thanks
            for name in ("Sam", "Lee", "Kim")
        }
    assert seen["Lee"] == CookThanks(1, lee, (("Alex", "For Lee"),))
    assert seen["Sam"] == CookThanks(1, lee, (("Kim", "For Sam"),))
    assert seen["Kim"] == CookThanks(1, lee)
