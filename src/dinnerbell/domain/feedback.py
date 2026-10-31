# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback after meals (spec 5.9, FR-45 to FR-49).

Each member who ate a meal can say how it was, once, and change it for 7 days. Every part is
optional: one rating of the meal, from "Hated it" to "Loved it"; for a restaurant, what went
wrong (issue chips, and a few words for "Other"); and for a home meal with a known cook who
isn't them, a thank-you with a note. It's stored with a snapshot of the meal as it was, for a
later milestone to use (#18); in version 1 it doesn't affect ranking.

Privacy (FR-49): a member's rating, issues, and "Other" note are shown only to them. Everyone
else sees only how many loved an item: no other rating and no issue is ever shown, not even as
a count. A cook sees their thank-yous and notes; other members see only how many thank-yous
there were.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.meals import MemberRef, Slot
from dinnerbell.domain.members import Member

FEEDBACK_DAYS = 7
"""Feedback stays open through this many days after the meal's date (FR-45)."""
COOK_NOTE_MAX = 200
ISSUE_NOTE_MAX = 200
"""The most characters in what went wrong, for the "Other" issue."""


class Rating(StrEnum):
    """How a member found a meal, lowest first, which is the order forms show."""

    HATED = "hated"
    DISLIKED = "disliked"
    FINE = "fine"
    LIKED = "liked"
    LOVED = "loved"

    @property
    def label(self) -> str:
        return _RATING_LABELS[self]

    @property
    def score(self) -> int:
        """-2 for "Hated it" to +2 for "Loved it", for a later milestone's ranking (#18)."""
        return _RATING_SCORES[self]


_RATING_LABELS = {
    Rating.HATED: "Hated it",
    Rating.DISLIKED: "Didn't like it",
    Rating.FINE: "It was fine",
    Rating.LIKED: "Liked it",
    Rating.LOVED: "Loved it",
}
_RATING_SCORES = {Rating.HATED: -2, Rating.DISLIKED: -1, Rating.FINE: 0, Rating.LIKED: 1, Rating.LOVED: 2}


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
NO_PLACE = "What went wrong is only for restaurants."
NO_COOK = "There's no cook to thank for this meal."
OWN_COOKING = "You cooked this one, so there's nobody to thank. Thanks for cooking!"
NOTE_TOO_LONG = f"Keep the note to {COOK_NOTE_MAX} characters."
ISSUE_NOTE_TOO_LONG = f"Keep what went wrong to {ISSUE_NOTE_MAX} characters."


@dataclass(frozen=True, slots=True)
class FeedbackInput:
    """A member's answers. Every part is optional. ``issue_note`` says what went wrong, and
    comes with the "Other" issue; ``cook_note`` comes with the thank-you."""

    rating: Rating | None = None
    place_issues: frozenset[PlaceIssue] = frozenset()
    issue_note: str | None = None
    cook_thanks: bool = False
    cook_note: str | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.rating is None
            and not self.place_issues
            and not self.issue_note
            and not self.cook_thanks
            and not self.cook_note
        )

    @property
    def issues(self) -> tuple[PlaceIssue, ...]:
        """``place_issues`` in the order forms show them, so "Other" comes last."""
        return tuple(i for i in PlaceIssue if i in self.place_issues)


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


def _trimmed(text: str | None, most: int, too_long: str) -> str | None:
    found = (text or "").replace("\r\n", "\n").strip() or None
    if found is not None and len(found) > most:
        raise InvalidFeedback(too_long)
    return found


def clean(data: FeedbackInput, facts: MealFacts, member_id: int) -> FeedbackInput:
    """``data`` checked against the meal, with the notes trimmed. Each note implies what it comes
    with: what went wrong implies "Other", and a note to the cook the thank-you. Raises
    InvalidFeedback."""
    note = _trimmed(data.cook_note, COOK_NOTE_MAX, NOTE_TOO_LONG)
    issue_note = _trimmed(data.issue_note, ISSUE_NOTE_MAX, ISSUE_NOTE_TOO_LONG)
    issues = frozenset(data.place_issues) | ({PlaceIssue.OTHER} if issue_note is not None else set())
    if issues and not facts.has_place:
        raise InvalidFeedback(NO_PLACE)
    thanks = data.cook_thanks or note is not None
    if thanks and not facts.thanks_cook(member_id):
        own = facts.cook is not None and facts.cook.id == member_id
        raise InvalidFeedback(OWN_COOKING if own else NO_COOK)
    return FeedbackInput(
        rating=data.rating, place_issues=issues, issue_note=issue_note, cook_thanks=thanks, cook_note=note
    )


# What people may see (FR-49) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemCounts:
    """Feedback on one catalog item as others may see it: how many loved it, and nothing else.
    Every other rating and the issues are stored for a later milestone's ranking but never
    counted for display, because in a small household any count of them could name the one
    person who gave it."""

    loved: int = 0


def count(feedback: Iterable[Feedback]) -> dict[int, ItemCounts]:
    """How many loved each catalog item, from each feedback's snapshot of the item. Items
    nobody loved are left out."""
    loved: dict[int, int] = {}
    for f in feedback:
        if f.answers.rating is Rating.LOVED:
            loved[f.item_id] = loved.get(f.item_id, 0) + 1
    return {item: ItemCounts(n) for item, n in loved.items()}


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
