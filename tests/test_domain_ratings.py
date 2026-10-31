# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback-informed ranking (FR-55, ADR 0008): each member's adjustment from their own
ratings, the household hint, what Preferences says, and how ranking uses both. The numbers are
the proposal's tables. No database."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from dinnerbell.domain.catalog import Meal, Mode
from dinnerbell.domain.feedback import PlaceIssue, Rating
from dinnerbell.domain.preferences import MINUS
from dinnerbell.domain.ranking import Component, GroupPreferences, rank
from dinnerbell.domain.ratings import (
    HALF_LIFE_DAYS,
    Hint,
    Opinion,
    Verdict,
    adjustment,
    combined_scores,
    effective,
    has_ratings,
    hint,
    hints,
    ratings_note,
    recent_issue,
    signed,
    verdict,
)
from tests.test_ranking import item, snapshot

TODAY = date(2026, 11, 2)
SAVED = datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
ITEM = 20
H, D, F, LI, LO = Rating.HATED, Rating.DISLIKED, Rating.FINE, Rating.LIKED, Rating.LOVED


def said(
    rating: Rating | None,
    *,
    member: int = 1,
    days_ago: int = 0,
    item_id: int = ITEM,
    saved: datetime | None = None,
    issues: frozenset[PlaceIssue] = frozenset(),
) -> Opinion:
    day = TODAY - timedelta(days=days_ago)
    when = saved or datetime(day.year, day.month, day.day, 23, tzinfo=UTC)
    return Opinion(member, item_id, day, when, rating, issues)


def adjust(*opinions: Opinion, cap: int = 2, edited_at: datetime | None = None, today: date = TODAY) -> float:
    return adjustment(opinions, today=today, cap=cap, edited_at=edited_at)


# One member's adjustment -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rating", "score"), [(H, -1.5), (D, -0.75), (F, 0.0), (LI, 0.5), (LO, 1.0)], ids=lambda v: str(v)
)
def test_one_rating_moves_halfway_or_three_quarters_for_a_negative(rating: Rating, score: float) -> None:
    """The proposal's first table: one person rates a place nobody scored, once."""
    assert adjust(said(rating)) == pytest.approx(score)
    assert effective(0, adjust(said(rating))) == pytest.approx(score)


MIXES = [
    ((LO, LO, LO, LO), (1, 1, 1, 1), 4.0, Verdict.WELL_LIKED),
    ((LO, LO, F, F), (1, 1, 0, 0), 2.0, Verdict.WELL_LIKED),
    ((LO, F, F, F), (1, 0, 0, 0), 1.0, None),
    ((LO, LO, LO, H), (1, 1, 1, -1.5), 1.5, Verdict.MIXED),
    ((LI, LI, D, D), (0.5, 0.5, -0.75, -0.75), -0.5, Verdict.MIXED),
    ((LO, LO, H, H), (1, 1, -1.5, -1.5), -1.0, Verdict.MIXED),
    ((H, F, F, F), (-1.5, 0, 0, 0), -1.5, Verdict.NOT_WELL_LIKED),
    ((H, H, H, H), (-1.5, -1.5, -1.5, -1.5), -6.0, Verdict.NOT_WELL_LIKED),
]


@pytest.mark.parametrize(("ratings", "each", "total", "expected"), MIXES)
def test_all_four_rate_once(
    ratings: tuple[Rating, ...], each: tuple[float, ...], total: float, expected: Verdict | None
) -> None:
    """The proposal's second table: each person's score, the group's total, and the hint."""
    opinions = [said(r, member=m) for m, r in enumerate(ratings, start=1)]
    assert [adjust(o) for o in opinions] == pytest.approx(list(each))
    stated: dict[tuple[int, int], int] = {}
    assert combined_scores(stated, opinions, {}, today=TODAY, cap=2).get(ITEM, 0.0) == pytest.approx(total)
    assert verdict(opinions, today=TODAY) is expected


def test_an_adjustment_halves_every_90_days() -> None:
    loved = said(LO)
    assert adjust(loved, today=TODAY + timedelta(days=HALF_LIFE_DAYS)) == pytest.approx(0.5)
    assert adjust(loved, today=TODAY + timedelta(days=2 * HALF_LIFE_DAYS)) == pytest.approx(0.25)
    assert adjust(loved, today=TODAY + timedelta(days=45)) == pytest.approx(2**-0.5)


def test_the_worked_example() -> None:
    """Alex loves a new place and Sam hates it: +1 and -1.5, Mixed reviews. Three months later,
    +0.5 and -0.75, and no hint, because the ratings are old."""
    alex, sam = said(LO, member=1), said(H, member=2)
    later = TODAY + timedelta(days=90)
    assert (adjust(alex), adjust(sam)) == pytest.approx((1.0, -1.5))
    assert (adjust(alex, today=later), adjust(sam, today=later)) == pytest.approx((0.5, -0.75))
    assert verdict([alex, sam], today=TODAY) is Verdict.MIXED
    assert verdict([alex, sam], today=later) is None


def test_each_rating_moves_from_the_faded_value_in_meal_date_order() -> None:
    hated = said(H, days_ago=91, saved=SAVED)  # saved last, but the older meal
    loved = said(LO, days_ago=1, saved=SAVED - timedelta(days=1))
    faded = -1.5 * 0.5
    expected = faded + 0.5 * (2 - faded)
    assert adjust(hated, loved, today=TODAY - timedelta(days=1)) == pytest.approx(expected)
    assert adjust(loved, hated) == pytest.approx(expected * 0.5 ** (1 / 90))


def test_repeated_ratings_approach_the_rating_but_stay_within_the_cap() -> None:
    assert adjust(said(LO), said(LO)) == pytest.approx(1.5)
    many = [said(LO, days_ago=0) for _ in range(30)]
    assert 1.99 < adjust(*many) <= 2.0
    assert -2.0 <= adjust(*[said(H) for _ in range(30)]) < -1.99


@pytest.mark.parametrize(
    ("cap", "loved", "hated", "most"), [(0, 0.0, 0.0, 0.0), (1, 0.5, -0.75, 1.0), (2, 1.0, -1.5, 2.0)]
)
def test_the_feedback_setting_caps_the_adjustment(cap: int, loved: float, hated: float, most: float) -> None:
    assert adjust(said(LO), cap=cap) == pytest.approx(loved)
    assert adjust(said(H), cap=cap) == pytest.approx(hated)
    assert abs(adjust(*[said(LO) for _ in range(40)], cap=cap)) == pytest.approx(most, abs=1e-6)


def test_a_score_stays_within_minus_2_and_plus_2() -> None:
    assert effective(2, 1.0) == 2.0 and effective(-2, -1.5) == -2.0
    assert effective(1, 1.5) == 2.0 and effective(1, -0.75) == pytest.approx(0.25)


def test_a_stance_edit_wins_over_earlier_ratings() -> None:
    edited = SAVED
    before = said(H, saved=edited - timedelta(minutes=1))
    after = said(LO, days_ago=3, saved=edited + timedelta(minutes=1))  # an older meal, rated after the edit
    assert adjust(before, edited_at=edited) == 0.0
    assert not has_ratings([before], today=TODAY, edited_at=edited)
    assert adjust(before, after, edited_at=edited) == pytest.approx(1.0 * 0.5 ** (3 / 90))
    assert has_ratings([before, after], today=TODAY, edited_at=edited)
    assert adjust(said(H, saved=edited), edited_at=edited) == 0.0  # saved at the same instant: the edit wins


def test_no_rating_and_future_meals_change_nothing() -> None:
    assert adjust(said(None, issues=frozenset({PlaceIssue.LATE}))) == 0.0
    assert adjust(said(LO, days_ago=-1)) == 0.0
    assert adjust() == 0.0


def test_combined_scores_add_each_members_ratings_to_their_own_score_only() -> None:
    siam, tacos, curry = 1, 2, 3
    stated = {(1, siam): 2, (2, siam): -1, (3, tacos): 1, (1, curry): 1, (2, curry): -1}
    opinions = [
        said(LO, member=1, item_id=siam),  # already +2: stays +2
        said(LO, member=2, item_id=siam),  # -1 + 1 = 0
        said(H, member=4, item_id=tacos),  # no stance: -1.5
    ]
    edits = {(2, siam): SAVED + timedelta(days=1)}  # member 2 set -1 after rating: the rating goes
    assert combined_scores(stated, opinions, {}, today=TODAY, cap=2) == {siam: 2.0, tacos: -0.5}
    assert combined_scores(stated, opinions, edits, today=TODAY, cap=2) == {siam: 1.0, tacos: -0.5}
    assert combined_scores(stated, opinions, {}, today=TODAY, cap=0) == {siam: 1.0, tacos: 1.0}


# What Preferences says ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "score", "note"),
    [
        (None, 1.0, "From your ratings: about +1"),
        (None, 0.5, "From your ratings: about +0.5"),
        (None, -0.75, f"From your ratings: about {MINUS}1"),
        (None, -1.5, f"From your ratings: about {MINUS}1.5"),
        (None, 0.2, None),  # rounds to 0: nothing worth saying
        (2, 0.5, "With your ratings: about +0.5"),
        (-1, -2.0, f"With your ratings: about {MINUS}2"),
        (1, 1.1, None),  # rounds to what they set
    ],
)
def test_the_preferences_note(stated: int | None, score: float, note: str | None) -> None:
    assert ratings_note(stated, score) == note


def test_signed() -> None:
    assert [signed(v) for v in (0, 0.5, 1, -1.5, -0.0)] == ["0", "+0.5", "+1", f"{MINUS}1.5", "0"]


# The household hint ------------------------------------------------------------------------------


def test_a_verdict_needs_two_people() -> None:
    assert verdict([said(LO), said(LO), said(LO)], today=TODAY) is None  # one person, three times
    assert verdict([said(LO, member=1), said(LO, member=2)], today=TODAY) is Verdict.WELL_LIKED


def test_an_old_rating_doesnt_make_a_second_person() -> None:
    """Each of the two people needs their own rating from roughly the last 3 months: a
    two-year-old rating can't let one person's two recent ones make the verdict."""
    recent = [said(LO, member=1), said(LO, member=1, days_ago=1)]
    assert verdict([*recent, said(LO, member=2, days_ago=730)], today=TODAY) is None
    assert verdict([*recent, said(LO, member=2, days_ago=91)], today=TODAY) is None  # weighs just under 0.5
    assert verdict([*recent, said(LO, member=2, days_ago=90)], today=TODAY) is Verdict.WELL_LIKED
    two_old = [said(F, member=2, days_ago=200), said(F, member=2, days_ago=200), said(F, member=2, days_ago=200)]
    assert verdict([*recent, *two_old], today=TODAY) is Verdict.WELL_LIKED  # together they weigh 0.64


def test_a_verdict_needs_recent_enough_ratings() -> None:
    """Two ratings weigh 1.5 or more for about 5 weeks; three for 3 months."""
    two = [said(LO, member=1, days_ago=37), said(LO, member=2, days_ago=37)]
    assert verdict(two, today=TODAY) is Verdict.WELL_LIKED
    stale = [replace(o, day=o.day - timedelta(days=2)) for o in two]
    assert verdict(stale, today=TODAY) is None
    three = [said(LO, member=m, days_ago=90) for m in (1, 2, 3)]
    assert verdict(three, today=TODAY) is Verdict.WELL_LIKED
    assert verdict([said(LO, member=1, days_ago=-3), said(LO, member=2, days_ago=-3)], today=TODAY) is None


def test_one_of_each_from_the_last_3_months_is_mixed() -> None:
    """The owner's rule: a positive and a negative weighing 0.5 or more each make Mixed reviews,
    however many like it."""
    fans = [said(LO, member=m) for m in range(1, 11)]
    assert verdict([*fans, said(H, member=11)], today=TODAY) is Verdict.MIXED
    assert verdict([*fans[:2], said(D, member=3, days_ago=90)], today=TODAY) is Verdict.MIXED
    assert verdict([*fans[:2], said(D, member=3, days_ago=100)], today=TODAY) is Verdict.WELL_LIKED
    assert verdict([said(LO, member=1, days_ago=100), said(H, member=2), said(H, member=3)], today=TODAY) is (
        Verdict.NOT_WELL_LIKED
    )


def test_each_branch_of_the_verdict() -> None:
    assert verdict([said(D, member=1), said(F, member=2), said(F, member=3)], today=TODAY) is Verdict.NOT_WELL_LIKED
    assert verdict([said(LI, member=1), said(F, member=2)], today=TODAY) is Verdict.WELL_LIKED  # half positive
    assert verdict([said(LI, member=1), said(F, member=2), said(F, member=3)], today=TODAY) is None
    assert verdict([said(F, member=1), said(F, member=2)], today=TODAY) is None
    issues_only = [said(None, member=m, issues=frozenset({PlaceIssue.COLD})) for m in (1, 2, 3)]
    assert verdict(issues_only, today=TODAY) is None


@pytest.mark.parametrize(
    ("checked", "expected"),
    [
        ({PlaceIssue.OTHER, PlaceIssue.COLD, PlaceIssue.LATE, PlaceIssue.WRONG_ORDER}, PlaceIssue.WRONG_ORDER),
        ({PlaceIssue.OTHER, PlaceIssue.COLD, PlaceIssue.LATE}, PlaceIssue.LATE),
        ({PlaceIssue.OTHER, PlaceIssue.COLD}, PlaceIssue.COLD),
        ({PlaceIssue.OTHER}, PlaceIssue.OTHER),
    ],
)
def test_the_most_important_issue_wins(checked: set[PlaceIssue], expected: PlaceIssue) -> None:
    spread = [said(None, member=n, issues=frozenset({i})) for n, i in enumerate(sorted(checked), start=1)]
    assert recent_issue(spread, today=TODAY) is expected
    assert list(PlaceIssue) == [PlaceIssue.WRONG_ORDER, PlaceIssue.LATE, PlaceIssue.COLD, PlaceIssue.OTHER]


def test_issues_count_for_90_days_from_one_person() -> None:
    late = frozenset({PlaceIssue.LATE})
    assert hint([said(None, issues=late, days_ago=89)], today=TODAY) == Hint(None, PlaceIssue.LATE)
    assert hint([said(None, issues=late, days_ago=90)], today=TODAY) is None
    assert hint([said(LO, issues=late, days_ago=-1)], today=TODAY) is None  # a meal after today


@pytest.mark.parametrize(
    ("shown", "text"),
    [
        (Hint(None, PlaceIssue.WRONG_ORDER), "A recent wrong order"),
        (Hint(None, PlaceIssue.LATE), "Recently late"),
        (Hint(None, PlaceIssue.COLD), "Recently cold"),
        (Hint(None, PlaceIssue.OTHER), "A recent problem"),
        (Hint(Verdict.MIXED, PlaceIssue.LATE), "Mixed reviews · recently late"),
        (Hint(Verdict.WELL_LIKED, PlaceIssue.WRONG_ORDER), "Well liked · a recent wrong order"),
        (Hint(Verdict.NOT_WELL_LIKED, PlaceIssue.OTHER), "Not well liked · a recent problem"),
        (Hint(Verdict.WELL_LIKED), "Well liked"),
    ],
)
def test_hint_wording(shown: Hint, text: str) -> None:
    assert shown.text == text


def test_hints_for_every_item_leave_out_items_with_none() -> None:
    opinions = [
        said(LO, member=1, item_id=1, issues=frozenset({PlaceIssue.LATE})),
        said(H, member=2, item_id=1),
        said(LO, member=1, item_id=2),  # one person: no hint
        said(None, member=3, item_id=3, issues=frozenset({PlaceIssue.OTHER})),
    ]
    found = hints(opinions, today=TODAY)
    assert {i: h.text for i, h in found.items()} == {1: "Mixed reviews · recently late", 3: "A recent problem"}


def test_a_hint_can_carry_nothing_private() -> None:
    """Opinions hold no note and no "Other" text, and a hint is a verdict and an issue only, so
    neither can carry a name or what someone wrote."""
    assert {f.name for f in fields(Opinion)} == {"member_id", "item_id", "day", "saved_at", "rating", "issues"}
    assert {f.name for f in fields(Hint)} == {"verdict", "issue"}


# Ranking with ratings and hints ------------------------------------------------------------------


def test_the_hint_is_the_first_reason_and_changes_no_score() -> None:
    siam, curry = item(1, "Siam"), item(2, "Curry", Mode.PICKUP)
    base = snapshot(siam, curry, temperature=0.0)
    hinted = replace(base, hints={1: "Mixed reviews · recently late"})
    plain, with_hint = {r.item_id: r for r in rank(base)}, {r.item_id: r for r in rank(hinted)}
    assert with_hint[1].reasons == ("Mixed reviews · recently late.", *plain[1].reasons)
    assert with_hint[1].score == plain[1].score and with_hint[2].reasons == plain[2].reasons


def test_a_hint_alone_is_the_whole_reason() -> None:
    """Intended: a pick with no standout component says "Works for everyone who's in.", but a
    hint is a reason, so with one it says only the hint."""
    eaten = item(1, "Siam")
    plain = snapshot(eaten, last_eaten_days={1: 30})
    assert rank(plain)[0].reasons == ("Works for everyone who's in.",)
    assert rank(replace(plain, hints={1: "Recently cold"}))[0].reasons == ("Recently cold.",)


def test_a_top_ups_hint_comes_before_its_meal_note() -> None:
    brunch = item(1, "Brunch", meals=frozenset({Meal.BREAKFAST}))
    (ranked,) = rank(snapshot(brunch, meal=Meal.DINNER, hints={1: "Well liked"}))
    assert ranked.reasons[:2] == ("Well liked.", "Usually for breakfast.")


def test_ratings_move_the_score_at_the_weight_of_a_stated_score() -> None:
    siam = item(1, "Siam")
    stated = rank(snapshot(siam, preferences=GroupPreferences(item_scores={1: 1})))[0]
    rated = rank(snapshot(siam, preferences=GroupPreferences(item_scores={1: -1.5})))[0]
    assert stated.components[Component.SOFT_PREFERENCE] == 1.0
    assert rated.components[Component.SOFT_PREFERENCE] == -1.5
    assert stated.score - rated.score == pytest.approx(2.5)
    assert "Not everyone's favorite." in rated.reasons
