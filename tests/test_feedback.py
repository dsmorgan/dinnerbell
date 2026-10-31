# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback after meals (spec 5.9, FR-45 to FR-49): the rules, the services, the reminder, and
who may see what. Nobody ever sees who gave which rating."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from dinnerbell.config import Settings
from dinnerbell.db.events import AssignmentRepository
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.models import FeedbackRow
from dinnerbell.db.repositories import MemberRepository
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

    saved = await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.LOVED, cook_note="Best tacos Zq7x"))
    assert saved is not None and saved.answers.cook_thanks
    assert (saved.item_id, saved.item_kind, saved.mode) == (house.items["Sam's Tacos"].id, ItemKind.HOME, Mode.COOK)
    assert saved.cook == MemberRef(house["Sam"].id, "Sam")
    assert {p.name for p in saved.participants} == {"Alex", "Kim", "Sam"}

    house.clock.now = DONE_AT + timedelta(days=6)
    answers = FeedbackInput(rating=Rating.FINE)
    async with house.sessionmaker() as db:
        await feedback.give_for_event(db, house["Kim"], event_id, answers, run=1, now=house.clock())
        form = await feedback.form(db, house["Kim"], meal_id, now=house.clock())
    assert form.mine == answers
    (row,) = await rows(house)
    assert (row.rating, row.cook_thanks, row.cook_note, row.created_at) == ("fine", False, None, saved.created_at)
    assert row.updated_at == house.clock()


async def test_who_can_give_feedback_and_until_when(house: House) -> None:
    event_id = await done_dinner(house)
    meal_id = await meal_of(house, event_id)
    with pytest.raises(InvalidFeedback, match="Only people who ate"):
        await give(house, "Lee", meal_id, FeedbackInput(rating=Rating.LOVED))
    with pytest.raises(InvalidFeedback, match="You cooked"):
        await give(house, "Sam", meal_id, FeedbackInput(cook_thanks=True))
    with pytest.raises(InvalidFeedback, match="only for restaurants"):
        await give(house, "Kim", meal_id, FeedbackInput(issue_note="Cold"))
    async with house.sessionmaker() as db:
        with pytest.raises(StaleRun):
            await feedback.form_for_event(db, house["Kim"], event_id, run=2, now=house.clock())
        with pytest.raises(NotFound):
            await feedback.form(db, house["Kim"], 999, now=house.clock())
    house.clock.now = DONE_AT + timedelta(days=8)  # Oct 30 in Chicago: the window closed on the 29th
    with pytest.raises(InvalidFeedback, match="has closed"):
        await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.LOVED))
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
    await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.HATED))
    assert len(await rows(house)) == 1
    assert await give(house, "Kim", meal_id, FeedbackInput()) is None
    assert await rows(house) == []
    assert await give(house, "Kim", meal_id, FeedbackInput()) is None  # nothing to take back


async def test_a_restaurant_meal_takes_issues_and_what_went_wrong_and_no_thanks(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    meal_id = await meal_of(house, await done_dinner(house, "Siam Garden", Mode.PICKUP))
    issues = frozenset({PlaceIssue.COLD, PlaceIssue.LATE})
    answers = FeedbackInput(rating=Rating.HATED, place_issues=issues, issue_note=" No forks Qz8 ")
    caplog.set_level("DEBUG", logger="dinnerbell")
    saved = await give(house, "Kim", meal_id, answers)
    assert saved is not None and saved.cook is None and saved.mode is Mode.PICKUP
    (row,) = await rows(house)
    assert (row.rating, row.place_issues, row.issue_note) == ("hated", ["cold", "late", "other"], "No forks Qz8")
    assert "Qz8" not in " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    async with house.sessionmaker() as db:  # what went wrong is Kim's alone, like her rating
        meal = await meals.get_meal(db, house["Alex"], meal_id)
        alex = (await feedback.for_meals(db, house["Alex"], [meal], today=DAY))[meal_id]
        kim = (await feedback.for_meals(db, house["Kim"], [meal], today=DAY))[meal_id]
    assert alex.mine is None and "Qz8" not in repr(alex)
    assert kim.mine is not None and kim.mine.issue_note == "No forks Qz8"
    with pytest.raises(InvalidFeedback, match="no cook"):
        await give(house, "Kim", meal_id, FeedbackInput(cook_thanks=True))


async def test_a_logged_meal_takes_feedback_from_the_people_named_on_it(house: House) -> None:
    house.clock.now = DONE_AT
    meal_id = await log(house, "Sam's Tacos", "Alex", "Lee")
    saved = await give(house, "Lee", meal_id, FeedbackInput(rating=Rating.LOVED))
    assert saved is not None and saved.mode is None and saved.cook is None
    with pytest.raises(InvalidFeedback, match="no cook"):
        await give(house, "Lee", meal_id, FeedbackInput(cook_thanks=True))
    with pytest.raises(InvalidFeedback, match="Only people who ate"):
        await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.LOVED))
    async with house.sessionmaker() as db:
        assert await feedback.awaiting(db, house["Alex"], now=house.clock()) == []  # Home lists calls only


async def test_deleting_a_meal_deletes_its_feedback(house: House) -> None:
    house.clock.now = DONE_AT
    meal_id = await log(house, "Taqueria", "Alex")
    await give(house, "Alex", meal_id, FeedbackInput(rating=Rating.FINE))
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
    await give(house, "Kim", waiting.meal_id, FeedbackInput(rating=Rating.LOVED))
    async with house.sessionmaker() as db:
        assert await feedback.awaiting(db, house["Kim"], now=house.clock()) == []
        assert len(await feedback.awaiting(db, house["Alex"], now=house.clock())) == 1
    house.clock.now = DONE_AT + timedelta(days=8)
    async with house.sessionmaker() as db:
        assert await feedback.awaiting(db, house["Alex"], now=house.clock()) == []


async def test_the_reminder_goes_once_to_those_who_turned_it_on(house: House) -> None:
    await remind(house, "Kim", "Lee", "Alex")  # Lee doesn't eat, so gets nothing
    async with house.sessionmaker() as db:  # Alex muted direct messages, which wins
        await MemberRepository(db).update(house.household.id, house["Alex"].id, house.clock(), mute_dm_reminders=True)
        await db.commit()
    event_id = await done_dinner(house)
    sent = [e for e in await house.outbox() if e.kind == "feedback_reminder"]
    assert [(e.idempotency_key, e.payload["member"]["name"], e.payload["item"], e.payload["closes"]) for e in sent] == [
        (f"event:{event_id}:run1:feedback_reminder:member{house['Kim'].id}", "Kim", "Sam's Tacos", "2026-10-29")
    ]


# Who sees what (FR-49) -------------------------------------------------------------------------


async def test_nobody_sees_who_gave_which_rating_or_what_went_wrong(house: House) -> None:
    meal_id = await meal_of(house, await done_dinner(house))
    await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.HATED, cook_note="Thanks anyway"))
    await give(house, "Alex", meal_id, FeedbackInput(rating=Rating.LOVED, cook_thanks=True))
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
    assert seen["Alex"].mine == FeedbackInput(rating=Rating.LOVED, cook_thanks=True)  # only their own answers
    assert seen["Alex"].thanks == CookThanks(2, sam) and seen["Alex"].can_give
    assert seen["Lee"].mine is None and seen["Lee"].thanks == CookThanks(2, sam) and not seen["Lee"].can_give
    assert counts[house.items["Sam's Tacos"].id] == ItemCounts(loved=1)
    for view in seen.values():
        assert "hated" not in repr(view) and "HATED" not in repr(view)


async def test_saving_feedback_logs_and_audits_no_rating(house: House, caplog: pytest.LogCaptureFixture) -> None:
    meal_id = await meal_of(house, await done_dinner(house))
    caplog.set_level("DEBUG", logger="dinnerbell")
    await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.HATED, cook_note="Too salty"))
    await give(house, "Kim", meal_id, FeedbackInput())
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "feedback saved" in logged and "feedback removed" in logged
    assert "hated" not in logged and "Hated it" not in logged and "Too salty" not in logged
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


async def test_the_window_closes_at_midnight_in_the_households_time_zone(house: House) -> None:
    """The household is in Chicago. A dinner on Thursday, Oct 22 takes feedback through Thursday,
    Oct 29 local time, hours after that day has ended in UTC."""
    meal_id = await meal_of(house, await done_dinner(house))
    chicago = ZoneInfo("America/Chicago")
    house.clock.now = datetime(2026, 10, 29, 23, 30, tzinfo=chicago).astimezone(UTC)  # Oct 30 04:30 UTC
    assert await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.LOVED)) is not None
    house.clock.now += timedelta(hours=1)  # 00:30 on Oct 30 in Chicago
    with pytest.raises(InvalidFeedback, match="has closed"):
        await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.FINE))
    (row,) = await rows(house)
    assert row.rating == "loved"


async def test_a_double_submit_that_loses_the_insert_race_replaces_the_winner(
    house: House, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first saves at once: the second finds no row, its insert hits the unique
    constraint, and it saves again as an edit, so there's still one row, with its answer."""
    meal_id = await meal_of(house, await done_dinner(house))
    await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.FINE))
    real = FeedbackRepository._row
    missed: list[bool] = []

    async def miss_once(self: FeedbackRepository, *args: Any) -> FeedbackRow | None:
        if not missed:
            missed.append(True)
            return None  # as if the other submit hadn't committed yet
        return await real(self, *args)

    monkeypatch.setattr(FeedbackRepository, "_row", miss_once)
    saved = await give(house, "Kim", meal_id, FeedbackInput(rating=Rating.LOVED))
    assert missed and saved is not None and saved.answers.rating is Rating.LOVED
    (row,) = await rows(house)
    assert row.rating == "loved"


async def test_editing_a_logged_meal_drops_feedback_that_no_longer_fits(house: House) -> None:
    house.clock.now = DONE_AT
    meal_id = await log(house, "Siam Garden", "Alex", "Kim", "Lee")
    for name in ("Alex", "Kim", "Lee"):
        await give(house, name, meal_id, FeedbackInput(rating=Rating.LOVED, issue_note="Late"))

    async def edit(item: str, *people: str) -> None:
        data = MealInput(DAY, Slot.LUNCH, house.items[item].id, frozenset(house[p].id for p in people))
        async with house.sessionmaker() as db:
            await meals.update_meal(db, house["Alex"], meal_id, data, today=DAY, now=house.clock())

    await edit("Siam Garden", "Alex", "Kim")  # Lee didn't eat it after all
    assert sorted(r.member_id for r in await rows(house)) == [house["Alex"].id, house["Kim"].id]
    audit = (await house.audits("meal.updated"))[-1]
    assert audit.payload["feedback_removed"] == 1 and "loved" not in str(audit.payload)

    await edit("Taqueria", "Alex", "Kim")  # a different place: every answer was about Siam Garden
    assert await rows(house) == []
    async with house.sessionmaker() as db:
        assert await feedback.item_counts(db, house["Alex"]) == {}
