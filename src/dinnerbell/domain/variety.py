# SPDX-License-Identifier: AGPL-3.0-or-later
"""The variety window: which items and cuisines the household ate recently (FR-31, FR-32).

A meal is inside a window of ``days`` days when it was eaten 0 to ``days - 1`` days ago, which
matches the ranking penalty -(1 - days_since / window) in spec section 7: it reaches zero at
exactly ``days``. History shows this summary today; M2 ranking uses the same function.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from dinnerbell.domain.meals import MealLog

DEFAULT_VARIETY_WINDOW_DAYS = 7
MIN_VARIETY_WINDOW_DAYS = 1
MAX_VARIETY_WINDOW_DAYS = 60
SETTINGS_KEY = "variety_window_days"


def variety_window_days(settings: Mapping[str, Any]) -> int:
    """Read the window from the household's settings JSON, falling back to the default."""
    value = settings.get(SETTINGS_KEY)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_VARIETY_WINDOW_DAYS
    if MIN_VARIETY_WINDOW_DAYS <= value <= MAX_VARIETY_WINDOW_DAYS:
        return value
    return DEFAULT_VARIETY_WINDOW_DAYS


def window_start(today: date, days: int) -> date:
    """The earliest date inside the window that ends ``today``."""
    return today - timedelta(days=days - 1)


def days_since(day: date, today: date) -> int:
    return (today - day).days


@dataclass(frozen=True, slots=True)
class CuisineRecency:
    tag_id: int
    name: str
    last_eaten: date
    days_since: int
    times: int


@dataclass(frozen=True, slots=True)
class ItemRecency:
    item_id: int
    name: str
    last_eaten: date
    days_since: int
    times: int


@dataclass(frozen=True, slots=True)
class VarietyWindow:
    days: int
    start: date
    today: date
    cuisines: tuple[CuisineRecency, ...]
    items: tuple[ItemRecency, ...]

    def cuisine_ids(self) -> frozenset[int]:
        return frozenset(c.tag_id for c in self.cuisines)

    def item_ids(self) -> frozenset[int]:
        return frozenset(i.item_id for i in self.items)


def summarize(
    meals: Iterable[MealLog], *, today: date, days: int, tag_names: Mapping[int, str] | None = None
) -> VarietyWindow:
    """Summarize meals inside the window, most recent first.

    Cuisines are identified by tag ID from each meal's snapshot. ``tag_names`` supplies current
    names so a renamed tag shows its new name; tags missing from it keep the snapshot name.
    """
    names = tag_names or {}
    cuisines: dict[int, CuisineRecency] = {}
    items: dict[int, ItemRecency] = {}
    for meal in meals:
        since = days_since(meal.day, today)
        if not 0 <= since < days:
            continue
        for tag in meal.cuisines:
            seen = cuisines.get(tag.id)
            last = meal.day if seen is None else max(seen.last_eaten, meal.day)
            cuisines[tag.id] = CuisineRecency(
                tag_id=tag.id,
                name=names.get(tag.id, tag.name),
                last_eaten=last,
                days_since=days_since(last, today),
                times=1 if seen is None else seen.times + 1,
            )
        seen_item = items.get(meal.item_id)
        last = meal.day if seen_item is None else max(seen_item.last_eaten, meal.day)
        items[meal.item_id] = ItemRecency(
            item_id=meal.item_id,
            name=meal.item_name,
            last_eaten=last,
            days_since=days_since(last, today),
            times=1 if seen_item is None else seen_item.times + 1,
        )
    return VarietyWindow(
        days=days,
        start=window_start(today, days),
        today=today,
        cuisines=tuple(sorted(cuisines.values(), key=lambda c: (c.days_since, c.name.casefold()))),
        items=tuple(sorted(items.values(), key=lambda i: (i.days_since, i.name.casefold()))),
    )
