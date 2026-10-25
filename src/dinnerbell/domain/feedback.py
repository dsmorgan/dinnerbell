# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback after meals (spec 5.9, FR-45 to FR-49).

Each member who ate a meal can say how it was, once, and change it for 7 days. Feedback has up
to three subjects, each optional: the food, the place (restaurants only), and the cook (home
meals with a known cook only). It's stored with a snapshot of the meal as it was, for a later
milestone to use; in version 1 it doesn't affect ranking.

Privacy (FR-49): nobody ever sees who chose "not again". Food and place ratings are shown only
to the member who gave them and, to everyone else, only as counts per catalog item. A cook sees
their thank-yous and notes; other members see only how many thank-yous there were.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.meals import MemberRef, Slot
from dinnerbell.domain.members import Member

FEEDBACK_DAYS = 7
"""Feedback stays open through this many days after the meal's date (FR-45)."""
COOK_NOTE_MAX = 200


class Rating(StrEnum):
    LOVED = "loved"
    FINE = "fine"
    NOT_AGAIN = "not_again"

    @property
    def label(self) -> str:
        return {"loved": "Loved it", "fine": "Fine", "not_again": "Not again"}[self.value]


class PlaceIssue(StrEnum):
    LATE = "late"
    WRONG_ORDER = "wrong_order"
    COLD = "cold"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {"late": "Late", "wrong_order": "Wrong order", "cold": "Cold", "other": "Other"}[self.value]


class InvalidFeedback(ValueError):
    """Feedback that can't be saved, or a meal that can't take feedback. The message is safe to show."""


NOT_A_PARTICIPANT = "Only people who ate this meal can give feedback on it."
CLOSED = f"Feedback is open for {FEEDBACK_DAYS} days after a meal, and this one has closed."
NO_PLACE = "Place feedback is only for restaurants."
NO_COOK = "There's no cook to thank for this meal."
OWN_COOKING = "You cooked this one, so there's nobody to thank. Thanks for cooking!"
NOTE_TOO_LONG = f"Keep the note to {COOK_NOTE_MAX} characters."


@dataclass(frozen=True, slots=True)
class FeedbackInput:
    """A member's answers. Every subject is optional; a note comes with the thank-you."""

    food: Rating | None = None
    place: Rating | None = None
    place_issues: frozenset[PlaceIssue] = frozenset()
    cook_thanks: bool = False
    cook_note: str | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.food is None
            and self.place is None
            and not self.place_issues
            and not self.cook_thanks
            and not self.cook_note
        )


@dataclass(frozen=True, slots=True)
class MealFacts:
    """What feedback is about: the meal as it is now, which a saved feedback snapshots. ``mode``
    and ``cook`` are known only for meals a call decided; a logged meal has neither."""

    meal_id: int
    day: date
    slot: Slot
    item_id: int
    item_name: str
    item_kind: ItemKind
    mode: Mode | None
    cook: MemberRef | None
    participants: tuple[MemberRef, ...]
    event_id: int | None = None

    @property
    def has_place(self) -> bool:
        return self.item_kind is ItemKind.RESTAURANT

    def thanks_cook(self, member_id: int) -> bool:
        """Whether ``member_id`` gets the cook subject: a home meal with a cook who isn't them."""
        return self.item_kind is ItemKind.HOME and self.cook is not None and self.cook.id != member_id

    def ate(self, member_id: int) -> bool:
        return any(p.id == member_id for p in self.participants)


@dataclass(frozen=True, slots=True)
class Feedback:
    """One member's saved feedback on one meal, with the snapshot taken when they saved it."""

    id: int
    household_id: int
    meal_id: int
    member_id: int
    answers: FeedbackInput
    item_id: int
    item_name: str
    item_kind: ItemKind
    mode: Mode | None
    cook: MemberRef | None
    participants: tuple[MemberRef, ...]
    created_at: datetime
    updated_at: datetime


def closes_on(day: date) -> date:
    """The last day feedback on a meal from ``day`` can be given or changed."""
    return day + timedelta(days=FEEDBACK_DAYS)


def is_open(day: date, today: date) -> bool:
    """``today`` is in the household's time zone."""
    return day <= today <= closes_on(day)


def can_give(member: Member, participants: Iterable[MemberRef], day: date, today: date) -> bool:
    """Whether ``member`` ate a meal from ``day`` and can still give or change feedback on it."""
    return member.active and any(p.id == member.id for p in participants) and is_open(day, today)


def check_can_give(member: Member, facts: MealFacts, today: date) -> None:
    if not (member.active and facts.ate(member.id)):
        raise InvalidFeedback(NOT_A_PARTICIPANT)
    if not is_open(facts.day, today):
        raise InvalidFeedback(CLOSED)


def clean(data: FeedbackInput, facts: MealFacts, member_id: int) -> FeedbackInput:
    """``data`` checked against the meal, with the note trimmed. A note implies the thank-you it
    comes with. Raises InvalidFeedback."""
    note = (data.cook_note or "").replace("\r\n", "\n").strip() or None
    if note is not None and len(note) > COOK_NOTE_MAX:
        raise InvalidFeedback(NOTE_TOO_LONG)
    if (data.place is not None or data.place_issues) and not facts.has_place:
        raise InvalidFeedback(NO_PLACE)
    thanks = data.cook_thanks or note is not None
    if thanks and not facts.thanks_cook(member_id):
        own = facts.cook is not None and facts.cook.id == member_id
        raise InvalidFeedback(OWN_COOKING if own else NO_COOK)
    return FeedbackInput(data.food, data.place, frozenset(data.place_issues), thanks, note)


# What people may see (FR-49) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemCounts:
    """Feedback on one catalog item as counts only: never who gave which."""

    food: Mapping[Rating, int] = field(default_factory=dict)
    place: Mapping[Rating, int] = field(default_factory=dict)
    place_issues: Mapping[PlaceIssue, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.food.values()) + sum(self.place.values())

    @staticmethod
    def line(counts: Mapping[Rating, int]) -> list[tuple[Rating, int]]:
        """The non-zero counts in a fixed order: loved, fine, not again."""
        return [(rating, counts[rating]) for rating in Rating if counts.get(rating)]


def count(feedback: Iterable[Feedback]) -> dict[int, ItemCounts]:
    """Counts per catalog item, from each feedback's snapshot of the item."""
    food: dict[int, dict[Rating, int]] = {}
    place: dict[int, dict[Rating, int]] = {}
    issues: dict[int, dict[PlaceIssue, int]] = {}
    for f in feedback:
        if f.answers.food is not None:
            bucket = food.setdefault(f.item_id, {})
            bucket[f.answers.food] = bucket.get(f.answers.food, 0) + 1
        if f.answers.place is not None:
            bucket = place.setdefault(f.item_id, {})
            bucket[f.answers.place] = bucket.get(f.answers.place, 0) + 1
        for issue in f.answers.place_issues:
            chips = issues.setdefault(f.item_id, {})
            chips[issue] = chips.get(issue, 0) + 1
    items = food.keys() | place.keys() | issues.keys()
    return {i: ItemCounts(food.get(i, {}), place.get(i, {}), issues.get(i, {})) for i in items}


@dataclass(frozen=True, slots=True)
class CookThanks:
    """The thank-yous on one meal as a viewer may see them. Everyone sees ``count``, the
    thank-yous to the meal's cook as it is now; a viewer sees ``notes`` only for thank-yous
    addressed to them."""

    count: int
    cook: MemberRef | None = None
    """The meal's cook now, whom ``count`` is about."""
    notes: tuple[tuple[str, str | None], ...] = ()
    """(name, note) for each thank-you addressed to the viewer. Empty for anyone else."""


def thanks_for(
    feedback: Iterable[Feedback], viewer_id: int, names: Mapping[int, str], cook: MemberRef | None
) -> CookThanks:
    """The thank-yous among one meal's ``feedback``, as ``viewer_id`` may see them. ``cook`` is
    the meal's cook now; ``names`` maps member IDs to display names.

    Each feedback is judged on its own snapshot: a thank-you's name and note reach only the cook
    it was addressed to. So if the cook changed after someone thanked them (a corrected
    assignment), the new cook never sees what was written to the old one, and the old one's
    thank-yous no longer count toward the meal's public count.
    """
    thanked = sorted((f for f in feedback if f.answers.cook_thanks and f.cook is not None), key=lambda f: f.id)
    public = sum(1 for f in thanked if cook is not None and f.cook is not None and f.cook.id == cook.id)
    notes = tuple(
        (names.get(f.member_id, "A former member"), f.answers.cook_note)
        for f in thanked
        if f.cook is not None and f.cook.id == viewer_id
    )
    return CookThanks(public, cook, notes)
