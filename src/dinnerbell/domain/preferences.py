# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preferences: each member's veto or soft score for a tag or an item (FR-27 to FR-29).

Preferences are private. Nothing in this module or the services that use it takes a member ID
from anyone but the signed-in member, so no one, admins included, can read or write another
member's preferences. Ranking (M2) sees them only in aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

MIN_SCORE = -2
MAX_SCORE = 2
VETO = "veto"
LEVELS = (VETO, "-2", "-1", "0", "1", "2")
MINUS = "\u2212"  # the typographic minus sign the page shows
LEVEL_LABELS = {VETO: "Veto", "-2": f"{MINUS}2", "-1": f"{MINUS}1", "0": "0", "1": "+1", "2": "+2"}


class TargetType(StrEnum):
    TAG = "tag"
    ITEM = "item"


class InvalidPreference(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Stance:
    """A veto, or a score from -2 to +2. The neutral stance (score 0, no veto) is the default
    and is never stored."""

    veto: bool = False
    score: int = 0

    def __post_init__(self) -> None:
        if not MIN_SCORE <= self.score <= MAX_SCORE:
            raise InvalidPreference(f"Scores run from {MINUS}2 to +2.")
        if self.veto and self.score:
            raise InvalidPreference("A veto has no score.")

    @classmethod
    def parse(cls, value: str) -> Stance:
        text = value.strip().replace(MINUS, "-").removeprefix("+")
        if text == VETO:
            return cls(veto=True)
        if text not in LEVELS:
            raise InvalidPreference(f"Choose veto or a score from {MINUS}2 to +2.")
        return cls(score=int(text))

    @property
    def level(self) -> str:
        return VETO if self.veto else str(self.score)

    @property
    def is_neutral(self) -> bool:
        return not self.veto and self.score == 0


NEUTRAL = Stance()


@dataclass(frozen=True, slots=True)
class Preference:
    member_id: int
    target_type: TargetType
    target_id: int
    stance: Stance


def merged_stance(kept: Stance, merged: Stance) -> Stance:
    """The stance a member keeps when two tags they rated become one.

    A veto wins, because it's a hard constraint the member chose. Otherwise the lower score
    wins: a dislike matters more than a like when choosing for a group.
    """
    if kept.veto or merged.veto:
        return Stance(veto=True)
    return Stance(score=min(kept.score, merged.score))
