# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback rules (spec 5.9, FR-45, FR-46, FR-49): the 7-day window, the one rating, which
parts a meal takes, and what thank-yous reveal. Nobody ever sees who gave which. The household
hints that replace the loved counts are in test_domain_ratings.py."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.feedback import (
    COOK_NOTE_MAX,
    ISSUE_NOTE_MAX,
    CookThanks,
    Feedback,
    FeedbackInput,
    InvalidFeedback,
    MealFacts,
    PlaceIssue,
    Rating,
    clean,
    closes_on,
    is_open,
    thanks_for,
)
from dinnerbell.domain.meals import MemberRef, Slot

DAY = date(2026, 10, 22)
SAVED_AT = datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
SAM, KIM = MemberRef(2, "Sam"), MemberRef(3, "Kim")
HOME = MealFacts(1, DAY, Slot.DINNER, 11, "Sam's Tacos", ItemKind.HOME, Mode.COOK, SAM, (SAM, KIM), 7)
PLACE = MealFacts(2, DAY, Slot.DINNER, 12, "Siam Garden", ItemKind.RESTAURANT, Mode.PICKUP, None, (SAM, KIM), 8)


# Rules ------------------------------------------------------------------------------------------


def test_feedback_stays_open_for_seven_days() -> None:
    assert closes_on(DAY) == date(2026, 10, 29)
    assert is_open(DAY, DAY) and is_open(DAY, date(2026, 10, 29))
    assert not is_open(DAY, date(2026, 10, 30))
    assert not is_open(DAY, date(2026, 10, 21))


def test_ratings_go_from_hated_to_loved_with_scores_for_ranking() -> None:
    assert [(r.value, r.label, r.score) for r in Rating] == [
        ("hated", "Hated it", -2),
        ("disliked", "Didn't like it", -1),
        ("fine", "It was fine", 0),
        ("liked", "Liked it", 1),
        ("loved", "Loved it", 2),
    ]


def test_parts_follow_the_meal() -> None:
    assert clean(FeedbackInput(rating=Rating.LIKED), HOME, KIM.id).rating is Rating.LIKED  # any meal
    assert clean(FeedbackInput(rating=Rating.HATED), PLACE, KIM.id).rating is Rating.HATED
    with pytest.raises(InvalidFeedback, match="only for restaurants"):
        clean(FeedbackInput(place_issues=frozenset({PlaceIssue.COLD})), HOME, KIM.id)
    with pytest.raises(InvalidFeedback, match="only for restaurants"):
        clean(FeedbackInput(issue_note="Cold"), HOME, KIM.id)
    with pytest.raises(InvalidFeedback, match="no cook"):
        clean(FeedbackInput(cook_thanks=True), PLACE, KIM.id)
    with pytest.raises(InvalidFeedback, match="You cooked"):
        clean(FeedbackInput(cook_thanks=True), HOME, SAM.id)
    no_cook = MealFacts(3, DAY, Slot.DINNER, 11, "Sam's Tacos", ItemKind.HOME, None, None, (SAM, KIM))
    with pytest.raises(InvalidFeedback, match="no cook"):
        clean(FeedbackInput(cook_note="Yum"), no_cook, KIM.id)


def test_a_note_comes_with_the_thank_you_and_is_trimmed() -> None:
    answers = clean(FeedbackInput(cook_note="  So good!\r\nAgain soon.  "), HOME, KIM.id)
    assert answers == FeedbackInput(cook_thanks=True, cook_note="So good!\nAgain soon.")
    assert clean(FeedbackInput(cook_thanks=True, cook_note="   "), HOME, KIM.id).cook_note is None
    assert clean(FeedbackInput(cook_note="x" * COOK_NOTE_MAX), HOME, KIM.id).cook_note == "x" * COOK_NOTE_MAX
    with pytest.raises(InvalidFeedback, match="200 characters"):
        clean(FeedbackInput(cook_note="x" * (COOK_NOTE_MAX + 1)), HOME, KIM.id)
    assert FeedbackInput().is_empty and not FeedbackInput(cook_thanks=True).is_empty


def test_what_went_wrong_comes_with_other_and_is_trimmed() -> None:
    answers = clean(FeedbackInput(issue_note="  No forks\r\nagain  "), PLACE, KIM.id)
    assert answers == FeedbackInput(place_issues=frozenset({PlaceIssue.OTHER}), issue_note="No forks\nagain")
    late = frozenset({PlaceIssue.LATE})
    assert clean(FeedbackInput(place_issues=late, issue_note="   "), PLACE, KIM.id) == FeedbackInput(place_issues=late)
    other = frozenset({PlaceIssue.OTHER})
    assert clean(FeedbackInput(place_issues=other), PLACE, KIM.id).issue_note is None  # Other alone is fine
    assert clean(FeedbackInput(issue_note="x" * ISSUE_NOTE_MAX), PLACE, KIM.id).issue_note == "x" * ISSUE_NOTE_MAX
    with pytest.raises(InvalidFeedback, match="200 characters"):
        clean(FeedbackInput(issue_note="x" * (ISSUE_NOTE_MAX + 1)), PLACE, KIM.id)
    assert not FeedbackInput(issue_note="Cold").is_empty
    every = FeedbackInput(place_issues=frozenset(PlaceIssue))
    assert every.issues == (PlaceIssue.WRONG_ORDER, PlaceIssue.LATE, PlaceIssue.COLD, PlaceIssue.OTHER)


def _saved(member_id: int, item_id: int, answers: FeedbackInput, cook: MemberRef | None = None) -> Feedback:
    kind = ItemKind.HOME if cook else ItemKind.RESTAURANT
    return Feedback(member_id, 1, 1, member_id, answers, item_id, "x", kind, None, cook, (), SAVED_AT, SAVED_AT)


def test_only_the_cook_sees_who_thanked_them_and_their_notes() -> None:
    given = [
        _saved(3, 11, FeedbackInput(cook_thanks=True, cook_note="Best tacos yet"), cook=SAM),
        _saved(1, 11, FeedbackInput(cook_thanks=True), cook=SAM),
        _saved(4, 11, FeedbackInput(rating=Rating.HATED), cook=SAM),
    ]
    names = {1: "Alex", 3: "Kim"}
    assert thanks_for(given, SAM.id, names, SAM) == CookThanks(2, SAM, (("Alex", None), ("Kim", "Best tacos yet")))
    assert thanks_for(given, KIM.id, names, SAM) == CookThanks(2, SAM)
    assert thanks_for([], SAM.id, names, SAM) == CookThanks(0, SAM)
    assert thanks_for(given, SAM.id, names, None) == CookThanks(0, None, (("Alex", None), ("Kim", "Best tacos yet")))


def test_each_cook_sees_only_the_notes_written_to_them() -> None:
    """When the cook changes after someone thanked them, as when the admin corrects who
    cooked, each thank-you stays with the cook it named."""
    lee = MemberRef(4, "Lee")
    given = [
        _saved(3, 11, FeedbackInput(cook_thanks=True, cook_note="For Sam"), cook=SAM),
        _saved(1, 11, FeedbackInput(cook_thanks=True, cook_note="For Lee"), cook=lee),
    ]
    names = {1: "Alex", 3: "Kim"}
    assert thanks_for(given, SAM.id, names, lee) == CookThanks(1, lee, (("Kim", "For Sam"),))
    assert thanks_for(given, lee.id, names, lee) == CookThanks(1, lee, (("Alex", "For Lee"),))
    assert thanks_for(given, KIM.id, names, lee) == CookThanks(1, lee)
