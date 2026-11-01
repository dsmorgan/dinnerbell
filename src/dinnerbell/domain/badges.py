# SPDX-License-Identifier: AGPL-3.0-or-later
"""Badges (spec 12, FR-39, FR-43): thirteen badges, five tiers each, on three ladders (#130).

Each badge climbs one ladder: Frequent (1, 10, 50, 100, 200 by default) for the badges that count
meals, Occasional (1, 5, 20, 50, 100) for ideas and guests, and Rare (1, 3, 10, 25, 50) for the
ones capped by novelty or by the calendar. The counts are the household's setting
(``badge_thresholds``). A badge is earned once per member and tier, and stays earned if the admin
later raises a threshold.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
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


class Ladder(StrEnum):
    FREQUENT = "frequent"
    OCCASIONAL = "occasional"
    RARE = "rare"

    @property
    def label(self) -> str:
        return self.value.capitalize()


LADDERS = tuple(Ladder)
Ladders = Mapping[Ladder, tuple[int, ...]]
"""Each ladder's five counts, one per tier."""
DEFAULT_LADDERS: Ladders = MappingProxyType(
    {
        Ladder.FREQUENT: (1, 10, 50, 100, 200),
        Ladder.OCCASIONAL: (1, 5, 20, 50, 100),
        Ladder.RARE: (1, 3, 10, 25, 50),
    }
)


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
    ladder: Ladder
    """The ladder whose counts its tiers need."""


BADGES = (
    Badge(BadgeKey.CHEF, "Chef", "meals cooked", Kind.COOKED, Ladder.FREQUENT),
    Badge(BadgeKey.ROAD_RUNNER, "Road Runner", "meals picked up", Kind.PICKED_UP, Ladder.FREQUENT),
    Badge(BadgeKey.ORDER_UP, "Order Up", "meals ordered", Kind.ORDERED, Ladder.FREQUENT),
    Badge(BadgeKey.QUARTERBACK, "Quarterback", "meals coordinated", Kind.COORDINATED, Ladder.FREQUENT),
    Badge(BadgeKey.FULL_TABLE, "Full Table", "meals shared", Kind.PARTICIPATED, Ladder.FREQUENT),
    Badge(BadgeKey.EARLY_BIRD, "Early Bird", "quick answers", Kind.RESPONDED_ON_TIME, Ladder.FREQUENT),
    Badge(BadgeKey.BRIGHT_IDEA, "Bright Idea", "ideas that won", Kind.SUGGESTED, Ladder.OCCASIONAL),
    Badge(BadgeKey.SCOUT, "Scout", "catalog finds that were chosen", Kind.ADDED_ITEM, Ladder.RARE),
    Badge(BadgeKey.EXPLORER, "Explorer", "new places or cuisines that won", Kind.EXPLORED, Ladder.RARE),
    Badge(BadgeKey.GOOD_HOST, "Good Host", "meals with guests", Kind.HOSTED, Ladder.OCCASIONAL),
    Badge(BadgeKey.FOOD_CRITIC, "Food Critic", "meals you gave feedback on", Kind.GAVE_FEEDBACK, Ladder.FREQUENT),
    Badge(BadgeKey.ON_A_ROLL, "On a Roll", "weeks in a row of pitching in", None, Ladder.RARE),
    Badge(BadgeKey.GAMBLER, "Gambler", "calls where you left the choice to others", Kind.GAMBLED, Ladder.FREQUENT),
)
BY_KEY = {b.key: b for b in BADGES}
SILENT_BADGES = frozenset({BadgeKey.FOOD_CRITIC})
"""Earned and shown, but never announced by itself, because a post would interrupt the channel each
time someone gives feedback. The announcer marks these awards ``announced`` without queuing a post;
the recap, the household's newest badges, Stats, and the art treat them like any other badge."""
RARITY = (Ladder.RARE, Ladder.OCCASIONAL, Ladder.FREQUENT)
"""The ladders from hardest to climb to easiest: a list of news names a Rare badge before a Frequent one."""
_CATALOG = {b.key: n for n, b in enumerate(BADGES)}


def news_order(badge: BadgeKey, tier: Tier) -> tuple[int, int, int]:
    """The sort key for one member's badges in a badge post or the recap (FR-39, FR-40): the
    highest tier first (Diamond to First), then the rarest ladder (``RARITY``), then ``BADGES``
    order. Any list that is cut short ("and 2 more badges") is sorted first, so what's left off
    is the most common."""
    return (-TIERS.index(tier), RARITY.index(BY_KEY[badge].ladder), _CATALOG[badge])


ROLL_KINDS = frozenset({Kind.COOKED, Kind.PICKED_UP, Kind.ORDERED})
"""Pitching in for On a Roll: a week counts when the member cooked, picked up, or ordered."""


class InvalidThresholds(ValueError):
    """Thresholds that can't be saved. The message is safe to show."""


def ladders_from(settings: Mapping[str, Any]) -> dict[Ladder, tuple[int, ...]]:
    """The ladders from the household's settings JSON. A ladder that is missing or isn't a valid
    one takes its default. The setting holds one list per ladder; a single list, which is how
    Dinnerbell stored it before there were three ladders, is the Frequent ladder."""
    value = settings.get(SETTINGS_KEY)
    if isinstance(value, list | tuple):
        value = {Ladder.FREQUENT.value: value}
    stored = value if isinstance(value, Mapping) else {}
    found: dict[Ladder, tuple[int, ...]] = {}
    for ladder in LADDERS:
        counts = stored.get(ladder.value)
        try:
            found[ladder] = check_thresholds(counts) if isinstance(counts, list | tuple) else DEFAULT_LADDERS[ladder]
        except InvalidThresholds:
            found[ladder] = DEFAULT_LADDERS[ladder]
    return found


def ladders_setting(ladders: Ladders) -> dict[str, list[int]]:
    """``ladders`` as the settings JSON stores them."""
    return {ladder.value: list(ladders[ladder]) for ladder in LADDERS}


def check_ladders(values: Mapping[Ladder, Sequence[object]]) -> dict[Ladder, tuple[int, ...]]:
    """``values`` as the three ladders, each checked by ``check_thresholds``. The message names the
    ladder that failed."""
    found: dict[Ladder, tuple[int, ...]] = {}
    for ladder in LADDERS:
        try:
            found[ladder] = check_thresholds(values.get(ladder, ()))
        except InvalidThresholds as exc:
            raise InvalidThresholds(f"{ladder.label} ladder: {exc}") from exc
    return found


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


def highest_tiers(awards: Iterable[Award]) -> dict[BadgeKey, Tier]:
    """The highest tier among ``awards`` of each badge they cover."""
    held: dict[BadgeKey, Tier] = {}
    for award in awards:
        if award.badge not in held or TIERS.index(award.tier) > TIERS.index(held[award.badge]):
            held[award.badge] = award.tier
    return held


def progress(badge: Badge, count: int, ladders: Ladders, *, earned: Iterable[Tier] = ()) -> Progress:
    """``badge`` at ``count``, on its own ladder. A tier earned earlier counts as reached even if
    the admin has since raised its threshold, so progress never goes backward."""
    thresholds = ladders[badge.ladder]
    reached = set(tiers_reached(count, thresholds)) | set(earned)
    top = max(reached, key=TIERS.index, default=None)
    step = 0 if top is None else TIERS.index(top) + 1
    ahead = TIERS[step] if step < len(TIERS) else None
    if ahead is None:
        return Progress(badge, count, top, None, None, None)
    needed = thresholds[TIERS.index(ahead)]
    return Progress(badge, count, top, ahead, needed, max(needed - count, 0))


GRACE = timedelta(weeks=2)
"""The widest step between two active weeks that keeps a run going: one missed week between them."""


def longest_streak(days: Iterable[date]) -> int:
    """The most calendar weeks (Monday to Sunday) in a run that hold at least one of ``days``.

    A run survives one missed week between two active weeks, as often as that happens (a grace
    week, #130), but the missed week doesn't count toward its length. Two missed weeks in a row end
    it."""
    weeks = sorted({week_start(d) for d in days})
    best = run = 0
    previous: date | None = None
    for week in weeks:
        run = run + 1 if previous is not None and week - previous <= GRACE else 1
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
