# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback-informed ranking (FR-55, ADR 0008): what ratings do to scores, and the household's
hint for each item.

**Your adjustment.** Each member has an adjustment for each item, derived from their own
ratings of it, in the order of the meals' dates: each rating moves it toward the rating's score
(``Rating.score``, -2 to +2), halfway, or three-quarters of the way for "Hated it" and "Didn't
like it". It fades by half every 90 days without a new rating, counted from the meal's date. A
stance the member set on Preferences after a rating wins: only ratings set or changed after
their last edit of that item count. Their score for the item is then their stated score plus
the adjustment, kept within -2 to +2. The household's **Feedback** setting, 0 (off), 1, or 2,
limits how far each rating pulls: its score is held within that many points either way.

**The household hint.** Everyone's ratings of an item, each fading by half every 90 days,
give a verdict once two people have rated it: "Mixed reviews", "Not well liked", or "Well
liked". The restaurant issue checkboxes add the most important issue from the last 90 days.
A hint never names anyone, and never includes the "Other" text.

Pure functions: ``today`` comes in, and nothing here reads a clock.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from dinnerbell.domain.feedback import PlaceIssue, Rating
from dinnerbell.domain.preferences import MAX_SCORE, MIN_SCORE, MINUS

HALF_LIFE_DAYS = 90
"""An adjustment, and a rating's weight in a hint, halves every this many days."""
STEP = 0.5
"""How far "It was fine" and better move an adjustment toward the rating's score."""
NEGATIVE_STEP = 0.75
"""How far "Hated it" and "Didn't like it" move it: a bad night counts for more."""
MIN_FEEDBACK = 0
MAX_FEEDBACK = 2
DEFAULT_FEEDBACK = 2
"""The **Feedback** setting: how far ratings can move a member's score. 0 turns it off."""

HINT_MIN_PEOPLE = 2
HINT_MIN_WEIGHT = 1.5
"""A rating verdict needs ratings from 2 people, worth 1.5 after fading: two ratings from the
last 5 weeks, or three from the last 3 months. ADR 0008 explains why not 2, as proposed: two
ratings would fall below 2 the day after the meal, so the verdict would vanish at once."""
PERSON_MIN_WEIGHT = 0.5
"""A person counts toward the two only if their own ratings weigh this much: one from roughly
the last 3 months, so an old rating can't let one person's recent ones make the verdict."""
MIXED_MIN = 0.5
"""Mixed reviews: at least this much positive and this much negative, which is one of each from
roughly the last 3 months."""
ISSUE_DAYS = 90
"""An issue checkbox adds to the hint for this many days after the meal."""
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class Opinion:
    """One member's feedback on one meal, as ranking and hints read it: never the "Other" text
    or a note. ``day`` is the meal's date; ``saved_at`` is when the member last changed the
    rating, which a later stance edit overrides. Re-saving the same rating doesn't move it."""

    member_id: int
    item_id: int
    day: date
    saved_at: datetime
    rating: Rating | None = None
    issues: frozenset[PlaceIssue] = frozenset()


def _fade(value: float, days: int) -> float:
    return value * math.pow(0.5, max(days, 0) / HALF_LIFE_DAYS)


def _counted(opinions: Iterable[Opinion], today: date, edited_at: datetime | None) -> list[tuple[date, int]]:
    """(meal day, score) for each rating that counts toward an adjustment, oldest meal first:
    from meals up to ``today``, saved after the member's last stance edit."""
    kept = [
        (o.day, o.saved_at, o.rating.score)
        for o in opinions
        if o.rating is not None and o.day <= today and (edited_at is None or o.saved_at > edited_at)
    ]
    return [(day, score) for day, _, score in sorted(kept)]


def adjustment(opinions: Iterable[Opinion], *, today: date, cap: int, edited_at: datetime | None = None) -> float:
    """One member's adjustment for one item from their ``opinions`` of it, as of ``today``.
    ``cap`` is the Feedback setting; ``edited_at`` is when the member last set a stance on the
    item, which discards ratings saved before it."""
    counted = _counted(opinions, today, edited_at)
    if cap <= 0 or not counted:
        return 0.0
    value, last = 0.0, counted[0][0]
    for day, score in counted:
        value = _fade(value, (day - last).days)
        value += (NEGATIVE_STEP if score < 0 else STEP) * (max(-cap, min(cap, score)) - value)
        last = day
    return _fade(value, (today - last).days)


def effective(stated: int, adjusted: float) -> float:
    """A member's score for an item: what they set (0 when nothing) plus their adjustment, kept
    within -2 to +2."""
    return max(float(MIN_SCORE), min(float(MAX_SCORE), stated + adjusted))


def has_ratings(opinions: Iterable[Opinion], *, today: date, edited_at: datetime | None = None) -> bool:
    """Whether any of one member's ``opinions`` of one item count toward their adjustment."""
    return bool(_counted(opinions, today, edited_at))


def combined_scores(
    stated: Mapping[tuple[int, int], int],
    opinions: Iterable[Opinion],
    edits: Mapping[tuple[int, int], datetime],
    *,
    today: date,
    cap: int,
) -> dict[int, float]:
    """The group's item scores with ratings: per item, the sum over members of ``effective``.

    ``stated`` maps (member, item) to the score each member set, for the people eating only;
    ``opinions`` must be theirs alone too. ``edits`` maps (member, item) to the member's last
    stance edit. Only sums leave, so the result can't say whose rating moved what (FR-29)."""
    by_pair: dict[tuple[int, int], list[Opinion]] = {}
    for opinion in opinions:
        by_pair.setdefault((opinion.member_id, opinion.item_id), []).append(opinion)
    totals: dict[int, float] = {}
    for (_, item), score in stated.items():
        totals[item] = totals.get(item, 0.0) + score
    for pair, theirs in by_pair.items():
        stance = stated.get(pair, 0)
        moved = effective(stance, adjustment(theirs, today=today, cap=cap, edited_at=edits.get(pair)))
        totals[pair[1]] = totals.get(pair[1], 0.0) + moved - stance
    return {item: round(total, 6) for item, total in totals.items() if round(total, 6) != 0}


# What Preferences shows a member about their own ratings ---------------------------------------


def _about(value: float) -> float:
    """``value`` to the nearest half point, halves away from zero."""
    return math.copysign(math.floor(abs(value) * 2 + 0.5) / 2, value)


def signed(value: float) -> str:
    """``value`` as Preferences shows a score: +1, +0.5, 0, or -1.5 with a typographic minus."""
    if value == 0:
        return "0"
    text = f"{abs(value):g}"
    return f"+{text}" if value > 0 else f"{MINUS}{text}"


def ratings_note(stated: int | None, score: float) -> str | None:
    """What Preferences says beside an item a member has rated since they last set a stance.
    ``stated`` is their stance's score, or None when they set none; ``score`` is ``effective``.
    Nothing when the ratings change nothing worth saying, after rounding to half a point."""
    shown = _about(score)
    if stated is None or stated == 0:
        return f"From your ratings: about {signed(shown)}" if shown != 0 else None
    return f"With your ratings: about {signed(shown)}" if shown != stated else None


# The household hint ----------------------------------------------------------------------------


class Verdict(StrEnum):
    WELL_LIKED = "Well liked"
    MIXED = "Mixed reviews"
    NOT_WELL_LIKED = "Not well liked"


ISSUE_PRIORITY = tuple(PlaceIssue)
"""Wrong order, Late, Cold, then Other: the order forms list them in."""
_ISSUE_PHRASES = {
    PlaceIssue.WRONG_ORDER: "a recent wrong order",
    PlaceIssue.LATE: "recently late",
    PlaceIssue.COLD: "recently cold",
    PlaceIssue.OTHER: "a recent problem",
}


@dataclass(frozen=True, slots=True)
class Hint:
    """The household's hint for one item: a verdict, an issue, or both."""

    verdict: Verdict | None = None
    issue: PlaceIssue | None = None

    @property
    def text(self) -> str:
        """ "Mixed reviews · recently late", "Well liked", or "Recently late"."""
        if self.issue is None:
            return self.verdict.value if self.verdict is not None else ""
        phrase = _ISSUE_PHRASES[self.issue]
        if self.verdict is None:
            return phrase[0].upper() + phrase[1:]
        return f"{self.verdict.value} · {phrase}"


def verdict(opinions: Iterable[Opinion], *, today: date) -> Verdict | None:
    """The household's verdict on one item from everyone's ratings of it, or None.

    Each rating weighs 1, halving every 90 days since the meal. With P, N, and F the weights of
    positive (Liked it, Loved it), negative (Hated it, Didn't like it), and It was fine ratings,
    a verdict needs 2 people whose own ratings weigh at least 0.5 each (a rating from roughly the
    last 3 months), and P + N + F of at least 1.5. Then: Mixed reviews when
    P and N are both at least 0.5; otherwise Not well liked when N is at least a quarter of the
    total; otherwise Well liked when P is at least half of it."""
    people: dict[int, float] = {}
    positive = negative = total = 0.0
    for opinion in opinions:
        if opinion.rating is None or opinion.day > today:
            continue
        weight = _fade(1.0, (today - opinion.day).days)
        people[opinion.member_id] = people.get(opinion.member_id, 0.0) + weight
        total += weight
        if opinion.rating.score > 0:
            positive += weight
        elif opinion.rating.score < 0:
            negative += weight
    counted = sum(1 for weight in people.values() if weight >= PERSON_MIN_WEIGHT - _EPSILON)
    if counted < HINT_MIN_PEOPLE or total < HINT_MIN_WEIGHT - _EPSILON:
        return None
    if positive >= MIXED_MIN - _EPSILON and negative >= MIXED_MIN - _EPSILON:
        return Verdict.MIXED
    if negative >= total / 4 - _EPSILON:
        return Verdict.NOT_WELL_LIKED
    if positive >= total / 2 - _EPSILON:
        return Verdict.WELL_LIKED
    return None


def recent_issue(opinions: Iterable[Opinion], *, today: date) -> PlaceIssue | None:
    """The most important issue anyone checked on the item's meals in the last 90 days."""
    checked: set[PlaceIssue] = set()
    for opinion in opinions:
        if 0 <= (today - opinion.day).days < ISSUE_DAYS:
            checked |= opinion.issues
    return next((issue for issue in ISSUE_PRIORITY if issue in checked), None)


def hint(opinions: Iterable[Opinion], *, today: date) -> Hint | None:
    """The hint for one item from everyone's ``opinions`` of it, or None when there's none."""
    found = list(opinions)
    shown = Hint(verdict(found, today=today), recent_issue(found, today=today))
    return shown if shown.verdict is not None or shown.issue is not None else None


def hints(opinions: Iterable[Opinion], *, today: date) -> dict[int, Hint]:
    """``hint`` for every item in ``opinions``, leaving out items with none."""
    by_item: dict[int, list[Opinion]] = {}
    for opinion in opinions:
        by_item.setdefault(opinion.item_id, []).append(opinion)
    found = {item: hint(theirs, today=today) for item, theirs in by_item.items()}
    return {item: h for item, h in found.items() if h is not None}
