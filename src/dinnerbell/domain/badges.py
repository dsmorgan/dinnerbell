# SPDX-License-Identifier: AGPL-3.0-or-later
"""Badges (spec 12, FR-39, FR-43): thirteen badges that share one ladder of five tiers.

The ladder is the same for every badge and its counts are the household's setting
(``badge_thresholds``), 1, 10, 50, 100, and 200 by default. A badge is earned once per member and
tier, and stays earned if the admin later raises a threshold.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any

from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.meals import week_start

SETTINGS_KEY = "badge_thresholds"
MAX_THRESHOLD = 10_000


class Tier(StrEnum):
    FIRST = "first"
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    DIAMOND = "diamond"

    @property
    def label(self) -> str:
        return self.value.capitalize()


TIERS = tuple(Tier)
DEFAULT_THRESHOLDS = (1, 10, 50, 100, 200)


class Announcement(StrEnum):
    PENDING = "pending"
    """Earned, and the channel hasn't been told yet."""
    ANNOUNCED = "announced"
    SKIPPED = "skipped"
    """Earned quietly, so it is never announced: the backfill, and a threshold the admin lowered."""


class BadgeKey(StrEnum):
    CHEF = "chef"
    ROAD_RUNNER = "road_runner"
    ORDER_UP = "order_up"
    QUARTERBACK = "quarterback"
    FULL_TABLE = "full_table"
    EARLY_BIRD = "early_bird"
    BRIGHT_IDEA = "bright_idea"
    SCOUT = "scout"
    EXPLORER = "explorer"
    GOOD_HOST = "good_host"
    FOOD_CRITIC = "food_critic"
    ON_A_ROLL = "on_a_roll"
    GAMBLER = "gambler"


@dataclass(frozen=True, slots=True)
class Badge:
    key: BadgeKey
    name: str
    earned_by: str
    """What the Stats page says each step counts, such as "meals cooked"."""
    kind: Kind | None
    """The contribution it counts; None for On a Roll, which counts weeks (``longest_streak``)."""


BADGES = (
    Badge(BadgeKey.CHEF, "Chef", "meals cooked", Kind.COOKED),
    Badge(BadgeKey.ROAD_RUNNER, "Road Runner", "meals picked up", Kind.PICKED_UP),
    Badge(BadgeKey.ORDER_UP, "Order Up", "meals ordered", Kind.ORDERED),
    Badge(BadgeKey.QUARTERBACK, "Quarterback", "meals coordinated", Kind.COORDINATED),
    Badge(BadgeKey.FULL_TABLE, "Full Table", "meals shared", Kind.PARTICIPATED),
    Badge(BadgeKey.EARLY_BIRD, "Early Bird", "quick answers", Kind.RESPONDED_ON_TIME),
    Badge(BadgeKey.BRIGHT_IDEA, "Bright Idea", "ideas that won", Kind.SUGGESTED),
    Badge(BadgeKey.SCOUT, "Scout", "catalog finds that were chosen", Kind.ADDED_ITEM),
    Badge(BadgeKey.EXPLORER, "Explorer", "new places or cuisines that won", Kind.EXPLORED),
    Badge(BadgeKey.GOOD_HOST, "Good Host", "meals with guests", Kind.HOSTED),
    Badge(BadgeKey.FOOD_CRITIC, "Food Critic", "meals you gave feedback on", Kind.GAVE_FEEDBACK),
    Badge(BadgeKey.ON_A_ROLL, "On a Roll", "weeks in a row of pitching in", None),
    Badge(BadgeKey.GAMBLER, "Gambler", "calls where you left the choice to others", Kind.GAMBLED),
)
BY_KEY = {b.key: b for b in BADGES}
QUIET_BADGES = frozenset({BadgeKey.FOOD_CRITIC})
"""Badges the household never hears about: Food Critic counts feedback, and who gave feedback is
as private as what they said (FR-49). They are earned and shown to the member, but never announced
in the channel, recapped, or listed among the household's newest badges."""
ROLL_KINDS = frozenset({Kind.COOKED, Kind.PICKED_UP, Kind.ORDERED})
"""Pitching in for On a Roll: a week counts when the member cooked, picked up, or ordered."""


class InvalidThresholds(ValueError):
    """Thresholds that can't be saved. The message is safe to show."""


def thresholds_from(settings: Mapping[str, Any]) -> tuple[int, ...]:
    """The ladder from the household's settings JSON, or the defaults when it isn't a valid one."""
    value = settings.get(SETTINGS_KEY)
    try:
        return check_thresholds(value) if isinstance(value, list | tuple) else DEFAULT_THRESHOLDS
    except InvalidThresholds:
        return DEFAULT_THRESHOLDS


def check_thresholds(values: Sequence[object]) -> tuple[int, ...]:
    """``values`` as a ladder: one count per tier, each a whole number above the one before."""
    if len(values) != len(TIERS):
        raise InvalidThresholds(f"Give {len(TIERS)} counts, one for each tier.")
    counts: list[int] = []
    for tier, value in zip(TIERS, values, strict=True):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_THRESHOLD:
            raise InvalidThresholds(f"The {tier.label} count must be a whole number from 1 to {MAX_THRESHOLD}.")
        counts.append(value)
    if any(a >= b for a, b in pairwise(counts)):
        raise InvalidThresholds("Each tier needs a higher count than the one before it.")
    return tuple(counts)


def tiers_reached(count: int, thresholds: Sequence[int]) -> list[Tier]:
    """Every tier ``count`` has reached, lowest first."""
    return [tier for tier, needed in zip(TIERS, thresholds, strict=True) if count >= needed]


@dataclass(frozen=True, slots=True)
class Award:
    id: int
    household_id: int
    member_id: int
    badge: BadgeKey
    tier: Tier
    awarded_at: datetime
    announcement: Announcement
    announced_at: datetime | None = None
    event_id: int | None = None
    """The call whose completion earned it; None for a catch-up."""


@dataclass(frozen=True, slots=True)
class Progress:
    """One member's standing on one badge."""

    badge: Badge
    count: int
    tier: Tier | None
    """The highest tier reached, or None before the first."""
    next_tier: Tier | None
    next_at: int | None
    remaining: int | None


def progress(badge: Badge, count: int, thresholds: Sequence[int], *, earned: Iterable[Tier] = ()) -> Progress:
    """``badge`` at ``count``. A tier earned earlier counts as reached even if the admin has
    since raised its threshold, so progress never goes backward."""
    reached = set(tiers_reached(count, thresholds)) | set(earned)
    top = max(reached, key=TIERS.index, default=None)
    step = 0 if top is None else TIERS.index(top) + 1
    ahead = TIERS[step] if step < len(TIERS) else None
    if ahead is None:
        return Progress(badge, count, top, None, None, None)
    needed = thresholds[TIERS.index(ahead)]
    return Progress(badge, count, top, ahead, needed, max(needed - count, 0))


def longest_streak(days: Iterable[date]) -> int:
    """The most calendar weeks (Monday to Sunday) in a row that hold at least one of ``days``."""
    weeks = sorted({week_start(d) for d in days})
    best = run = 0
    previous: date | None = None
    for week in weeks:
        run = run + 1 if previous is not None and week - previous == timedelta(weeks=1) else 1
        best, previous = max(best, run), week
    return best


def next_up(steps: Iterable[Progress]) -> Progress | None:
    """The badge to point a member at: among those they have started, the one closest to its next
    tier (ties go to the earlier badge in ``BADGES``); with none started, the first badge. None
    once every badge is at the top tier."""
    open_steps = [p for p in steps if p.next_tier is not None]
    started = [p for p in open_steps if p.count > 0]
    if started:
        return min(started, key=lambda p: p.remaining or 0)  # min keeps the first of equals
    return open_steps[0] if open_steps else None
