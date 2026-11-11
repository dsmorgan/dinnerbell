# SPDX-License-Identifier: AGPL-3.0-or-later
"""What **Confirm** does on the assistant's catalog edits and meal-log drafts (#254, spec 6.14,
**Drafts**): editing a catalog item, taking one off the catalog or putting it back (FR-23), and
logging, editing, or deleting a meal the member logged (spec 5.6).

A catalog edit is a before-and-after diff: one line for each field **Confirm** changes, and
nothing for a field it keeps. A person goes in a line as ``person`` of their member ID, never a
name, and an item's link, order link, and notes as they are now go in as ``held``: the card shows
its member those, and the model reads labels and that the old value isn't shown to it. So a stored
title or line holds no name, and no note or link the model never read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from dinnerbell.domain.assistant_coordinating import held, person, plain
from dinnerbell.domain.catalog import MODE_ORDER, CatalogItem, CleanItem, Meal, meal_words, name_key
from dinnerbell.domain.meals import Slot

EDIT_FIELDS = {
    "name": "Name",
    "modes": "Ways",
    "meals": "Meals",
    "tag_ids": "Tags",
    "cook_ids": "Cooks",
    "url": "Link",
    "order_url": "Order link",
    "notes": "Notes",
    "default_servings": "Servings",
}
"""Each field a catalog edit can change, by its key in ``item_snapshot``, and the word its line
starts with on the card."""
HELD_FIELDS = frozenset({"url", "order_url", "notes"})
"""Fields whose value now the model never reads: ``look_up_catalog_items`` doesn't return them."""
NOT_SHOWN = "(not shown to you)"
"""What the model reads in place of a held value."""
UNCHANGED = "Nothing else about it changes."
NEW_SNAPSHOT = "It takes a new snapshot of the item's cuisines and the people who ate, as History's Save does."


def item_snapshot(item: CatalogItem) -> dict[str, Any]:
    """Everything about ``item`` a catalog edit reads or writes, as JSON: a draft keeps it, and
    **Confirm** refuses when the item isn't the same any more."""
    return {
        "kind": item.kind.value,
        "name": item.name,
        "modes": [m.value for m in MODE_ORDER if m in item.modes],
        "meals": [m.value for m in Meal if m in item.meals],
        "tag_ids": sorted(item.tag_ids),
        "cook_ids": sorted(item.cook_ids),
        "url": item.url,
        "order_url": item.order_url,
        "notes": item.notes,
        "default_servings": item.default_servings,
        "active": item.active,
    }


def clean_snapshot(clean: CleanItem) -> dict[str, Any]:
    """What ``update_item`` saves from ``clean``, keyed as ``item_snapshot`` is."""
    return {
        "name": clean.name,
        "modes": [m.value for m in MODE_ORDER if m in clean.modes],
        "meals": [m.value for m in Meal if m in clean.meals],
        "tag_ids": sorted(clean.tag_ids),
        "cook_ids": sorted(clean.cook_ids),
        "url": clean.url,
        "order_url": clean.order_url,
        "notes": clean.notes,
        "default_servings": clean.default_servings,
    }


def changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """The ``EDIT_FIELDS`` keys whose value ``after`` changes, in that order."""
    return [key for key in EDIT_FIELDS if before.get(key) != after.get(key)]


def _list(names: Iterable[str]) -> str:
    shown = list(names)
    if len(shown) <= 2:
        return " and ".join(shown)
    return f"{', '.join(shown[:-1])}, and {shown[-1]}"


def _quoted(value: object) -> str:
    return "none" if value is None else f"“{plain(str(value))}”"


def _modes(values: Iterable[str]) -> str:
    chosen = set(values)
    return _list(m.value for m in MODE_ORDER if m.value in chosen) or "none"


def _changes(added: list[str], removed: list[str]) -> str:
    parts = ([f"adds {_list(added)}"] if added else []) + ([f"takes off {_list(removed)}"] if removed else [])
    return "; ".join(parts)


def held_value(before: Mapping[str, Any]) -> Callable[[str], str]:
    """How the card writes each ``held`` field: as it is now, from the draft's ``before``."""
    return lambda field: _quoted(before.get(field)) if field in HELD_FIELDS else NOT_SHOWN


def edit_lines(before: Mapping[str, Any], after: Mapping[str, Any], tag_names: Mapping[int, str]) -> tuple[str, ...]:
    """What confirming a catalog edit does: a line for each changed field, from what it is to
    what it becomes, then that nothing else changes. ``tag_names`` names the household's tags."""
    lines: list[str] = []

    def tag(tag_id: int) -> str:
        return plain(tag_names.get(tag_id, "a tag"))

    for key in changed_fields(before, after):
        label = EDIT_FIELDS[key]
        old: Any = before.get(key)
        new: Any = after.get(key)
        if key == "name":
            lines.append(f"{label}: {plain(str(old))} → {plain(str(new))}.")
        elif key == "modes":
            lines.append(f"{label}: {_modes(old or [])} → {_modes(new or [])}.")
        elif key == "meals":
            lines.append(f"{label}: {meal_words(Meal(m) for m in old or [])} → {meal_words(Meal(m) for m in new)}.")
        elif key in {"tag_ids", "cook_ids"}:
            was, now = {int(i) for i in old or []}, {int(i) for i in new or []}
            if key == "tag_ids":
                came, gone = sorted(map(tag, now - was), key=name_key), sorted(map(tag, was - now), key=name_key)
            else:
                came, gone = [person(i) for i in sorted(now - was)], [person(i) for i in sorted(was - now)]
            lines.append(f"{label}: {_changes(came, gone)}.")
        elif key in HELD_FIELDS:
            lines.append(f"{label}: {held(key)} → {_quoted(new)}.")
        else:
            lines.append(f"{label}: {old if old is not None else 'none'} → {new if new is not None else 'none'}.")
    return (*lines, UNCHANGED)


def active_lines(name: str, *, active: bool) -> tuple[str, ...]:
    """What confirming ``set_item_active`` does (FR-23): deactivating hides an item and keeps its
    history."""
    name = plain(name)
    if active:
        return (f"It puts {name} back on the catalog, so calls can propose it again.",)
    return (
        f"It takes {name} off the catalog: it's hidden, and calls stop proposing it.",
        "Its meals stay in History, and anyone can put it back on the Catalog page.",
    )


def active_title(name: str, *, active: bool) -> str:
    return f"Put {plain(name)} back on the catalog" if active else f"Take {plain(name)} off the catalog"


# The meal log (spec 5.6) ------------------------------------------------------------------------


def past_day_words(day: date, today: date) -> str:
    """A meal's day as a member reads it: "today", "yesterday", or "Tue, Oct 20"."""
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"{day:%a, %b} {day.day}"


def _people(member_ids: Iterable[int]) -> str:
    return _list(person(i) for i in sorted(member_ids))


def log_title(item: str, slot: Slot, when: str) -> str:
    return f"Log {plain(item)} for {slot.value}, {when}"


def log_lines(item: str, *, slot: Slot, when: str, people: Iterable[int]) -> tuple[str, ...]:
    """What confirming ``meals.log_meal`` does: the meal goes in History, and counts toward
    variety, with no role contributions."""
    return (
        f"It logs {plain(item)} for {slot.value} {when} in History.",
        f"Who ate: {_people(people)}.",
        "It counts toward variety, and gives nobody a role contribution.",
    )


@dataclass(frozen=True, slots=True)
class MealShown:
    """A meal as a draft shows it: its day, meal, item's name, and who ate, by member ID."""

    day: date
    slot: Slot
    item_id: int
    item: str
    people: frozenset[int]


def meal_title(meal: MealShown, today: date, *, delete: bool = False) -> str:
    verb = "Delete" if delete else "Edit"
    return f"{verb} {plain(meal.item)}, {meal.slot.value} {past_day_words(meal.day, today)}"


def meal_edit_lines(before: MealShown, after: MealShown, today: date) -> tuple[str, ...]:
    """What confirming ``meals.update_meal`` does: a line for each change, the feedback it drops
    (all of it for another item, and that of anyone taken off), and the new snapshot it takes."""
    lines: list[str] = []
    if after.day != before.day:
        lines.append(f"Day: {past_day_words(before.day, today)} → {past_day_words(after.day, today)}.")
    if after.slot is not before.slot:
        lines.append(f"Meal: {before.slot.value} → {after.slot.value}.")
    if after.item_id != before.item_id:
        lines.append(f"Item: {plain(before.item)} → {plain(after.item)}.")
    added, removed = after.people - before.people, before.people - after.people
    if added or removed:
        came = [person(i) for i in sorted(added)]
        gone = [person(i) for i in sorted(removed)]
        lines.append(f"Who ate: {_changes(came, gone)}.")
    if after.item_id != before.item_id:
        lines.append("Its feedback is deleted, since it was about another item.")
    elif removed:
        lines.append("Feedback from anyone taken off it is deleted.")
    return (*lines, NEW_SNAPSHOT)


def meal_delete_lines(meal: MealShown, today: date) -> tuple[str, ...]:
    """What confirming ``meals.delete_meal`` does: deleting a meal deletes its feedback."""
    return (
        f"It deletes {plain(meal.item)}, {meal.slot.value} {past_day_words(meal.day, today)}, from History.",
        "Its feedback is deleted with it, and that can't be undone.",
    )
