# SPDX-License-Identifier: AGPL-3.0-or-later
"""What **Confirm** does on the assistant's drafts of the member's own data (#253, spec 6.14,
**Drafts**): a level on Preferences, feedback on a meal they ate, and their away days.

The services work out the before and after with the use cases' own rules; these say it in the
words the Preferences page and the feedback form use. Each line is about the member alone:
never who else ate, who cooked, or what anyone else said.
"""

from __future__ import annotations

from datetime import date, timedelta

from dinnerbell.domain.assistant_drafts import day_words
from dinnerbell.domain.away import Away
from dinnerbell.domain.feedback import FeedbackInput, PlaceIssue
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.preferences import LEVEL_LABELS, Stance


def level_label(stance: Stance) -> str:
    """A level as Preferences shows it: "Veto", a score from minus 2 to "+2", or "0"."""
    return LEVEL_LABELS[stance.level]


def when_words(day: date, today: date) -> str:
    """``day`` as a member reads it, with "yesterday" too, since feedback is about meals past."""
    if day == today - timedelta(days=1):
        return "yesterday"
    return day_words(day, today)


def span_words(away: Away, today: date) -> str:
    """An away window in words, such as "from today through Sat, Oct 24"."""
    if away.first == away.last:
        return f"on {when_words(away.first, today)}"
    return f"from {when_words(away.first, today)} through {when_words(away.last, today)}"


# Levels on Preferences (FR-27, FR-28) ---------------------------------------------------------------


def stance_title(name: str, after: Stance) -> str:
    if after.veto:
        return f"Veto {name}"
    if after.is_neutral:
        return f"Set {name} back to 0"
    return f"Set {name} to {level_label(after)}"


def stance_lines(*, target: str, tag: bool, before: Stance, after: Stance, resets_ratings: bool) -> tuple[str, ...]:
    """What saving a level does (``preferences.set_my_preference``). ``target`` names it, such
    as "the tag Mushrooms". ``resets_ratings``: the member's earlier ratings of the item move its
    score now, and a level they set wins over them (FR-55)."""
    lines = [f"Your level for {target} on Preferences goes from {level_label(before)} to {level_label(after)}."]
    if after.veto:
        lines.append(
            "Ranking leaves out everything with that tag on every call you're in."
            if tag
            else "Ranking leaves it out of every call you're in."
        )
    elif before.veto:
        lines.append("It's no longer vetoed, so calls you're in can propose it again.")
    if resets_ratings:
        lines.append("Your earlier ratings of it stop moving your score: the level you set wins.")
    return tuple(lines)


# Feedback (FR-45 to FR-47) -----------------------------------------------------------------------


def feedback_words(answers: FeedbackInput) -> str:
    """A member's own answers in a few words, such as "Hated it; went wrong: cold"."""
    parts: list[str] = []
    if answers.rating is not None:
        parts.append(answers.rating.label)
    wrong = [i.label.lower() for i in answers.issues if i is not PlaceIssue.OTHER]
    if PlaceIssue.OTHER in answers.place_issues:
        wrong.append(f'other ("{answers.issue_note}")' if answers.issue_note else "other")
    if wrong:
        parts.append(f"went wrong: {', '.join(wrong)}")
    if answers.cook_thanks:
        parts.append(f'thanks to the cook ("{answers.cook_note}")' if answers.cook_note else "thanks to the cook")
    return "; ".join(parts) or "no answers"


def feedback_title(item: str, slot: Slot, when: str, answers: FeedbackInput) -> str:
    if answers.is_empty:
        return f"Take back your feedback on {item}"
    return f"Feedback on {item}, {slot.value} {when}"


def feedback_lines(
    *, item: str, slot: Slot, when: str, answers: FeedbackInput, before: FeedbackInput | None
) -> tuple[str, ...]:
    """What saving feedback does (``feedback.give``): it replaces any the member gave on the
    meal, and empty answers take theirs back. The privacy line is the feedback form's."""
    meal = f"{item} ({slot.value}, {when})"
    if answers.is_empty:
        return (f"It takes back your feedback on {meal}: {feedback_words(before or answers)}.",)
    lines = [f"It saves your feedback on {meal}: {feedback_words(answers)}."]
    if before is not None:
        lines.append(f"It replaces what you said before: {feedback_words(before)}.")
    if answers.cook_thanks:
        lines.append(
            "The cook sees your thank-you and your note." if answers.cook_note else "The cook sees your thank-you."
        )
    lines.append(
        "Only you see your rating and what went wrong. The household sees a summary of everyone's, never who said what."
    )
    if answers.rating is not None:
        lines.append("Your rating can also move your own score for it.")
    return tuple(lines)


# Away days (FR-63) -------------------------------------------------------------------------------


def away_title(away: Away | None, today: date) -> str:
    if away is None:
        return "Clear your away days"
    return f"Away {span_words(away, today)}"


def away_lines(*, away: Away | None, before: Away | None, today: date) -> tuple[str, ...]:
    """What setting a window does (``away.set_my_away``), or, with ``away`` None, ending the one
    the member has (``away.clear_my_away``)."""
    if away is None:
        if before is None:  # pragma: no cover - a draft that ends nothing is refused first
            return ()
        if before.under_way(today):
            return (
                f"Your away days, {span_words(before, today)}, end now.",
                "Calls still taking answers ask you again.",
            )
        return (f"Your away days, {span_words(before, today)}, are cleared.",)
    lines = [
        f"You're away {span_words(away, today)}, and back {when_words(away.back, today)}.",
        "Calls on those days answer you Out, marked as away, unless you've answered them. You can still answer In.",
    ]
    if before is not None:
        lines.append(f"It replaces your away days {span_words(before, today)}.")
    return tuple(lines)
