# SPDX-License-Identifier: AGPL-3.0-or-later
"""The meal log: what the household ate, whether or not Dinnerbell coordinated it (FR-30)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from dinnerbell.domain.catalog import Meal
from dinnerbell.domain.members import Member

MAX_MEAL_AGE = timedelta(days=730)
"""How far back a meal can be logged: enough to backfill, not enough to typo a century."""


class Slot(StrEnum):
    """A meal of the day that has calls, in the order of the day. Breakfast (#58) has no daily
    call unless someone turns its schedule on; members start one when they want it."""

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"

    @property
    def meal(self) -> Meal:
        """The meal catalog items say they're for (#72). Every slot is a meal."""
        return Meal(self.value)


CALL_ORDER = (Slot.DINNER, Slot.BREAKFAST, Slot.LUNCH)
"""How the start buttons and the schedule list slots: dinner, the daily call, first, then the
others in the order of the day."""
_OF_THE_DAY = {slot: n for n, slot in enumerate(Slot)}
PICK_SLOT = "Pick breakfast, lunch, or dinner."
"""What a form says when its meal is missing or unknown."""


class InvalidMeal(ValueError):
    """Meal input that can't be saved. The message is safe to show."""


@dataclass(frozen=True, slots=True)
class TagRef:
    """A cuisine tag as it was when the meal was logged."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class MemberRef:
    """A participant as they were when the meal was logged."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class MealLog:
    id: int
    household_id: int
    day: date
    slot: Slot
    item_id: int
    item_name: str
    cuisines: tuple[TagRef, ...]
    participants: tuple[MemberRef, ...]
    logged_by: int | None
    event_id: int | None = None


@dataclass(frozen=True, slots=True)
class MealInput:
    day: date
    slot: Slot
    item_id: int
    participant_ids: frozenset[int]


def check_meal(
    data: MealInput,
    *,
    today: date,
    active_member_ids: Iterable[int],
    item_exists: bool,
    original_day: date | None = None,
) -> None:
    """Raise InvalidMeal unless the meal can be logged. ``today`` is in the household time zone.

    ``original_day`` is the stored date when editing: a meal already older than the two-year
    floor stays editable as long as its date doesn't change.
    """
    if data.day > today:
        raise InvalidMeal("You can log meals for today or earlier, not the future.")
    if data.day < today - MAX_MEAL_AGE and data.day != original_day:
        raise InvalidMeal("You can log meals from the last two years.")
    if not item_exists:
        raise InvalidMeal("Pick something from the catalog.")
    if not data.participant_ids:
        raise InvalidMeal("Pick at least one person who ate.")
    if not data.participant_ids <= set(active_member_ids):
        raise InvalidMeal("One of the people picked isn't an active member. Reload the page and try again.")


def can_change(actor: Member, meal: MealLog) -> bool:
    """Members edit or delete meals they logged; the admin can change any."""
    return actor.active and (actor.is_admin or meal.logged_by == actor.id)


def newest_first(meal: MealLog) -> tuple[date, int, int]:
    """A sort key that, reversed, puts the newest meal first: later days, then later meals of
    the day, then the one logged last."""
    return meal.day, _OF_THE_DAY[meal.slot], meal.id


def week_start(day: date) -> date:
    """The Monday that starts ``day``'s week."""
    return day - timedelta(days=day.weekday())


def by_week(meals: Iterable[MealLog]) -> list[tuple[date, list[MealLog]]]:
    """Group meals by week, newest week first; within a week, newest meal first: dinner, then
    lunch, then breakfast on the same day."""
    weeks: dict[date, list[MealLog]] = {}
    ordered = sorted(meals, key=newest_first, reverse=True)
    for meal in ordered:
        weeks.setdefault(week_start(meal.day), []).append(meal)
    return list(weeks.items())
