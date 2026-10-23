# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ranking: a deterministic, explainable choice of proposals for a call (spec section 7, ADR 0005).

``rank`` is a pure function over a ``RankingSnapshot``. The snapshot carries only combined
preferences for the people eating, never one member's stances, so nothing ranking produces can
say who vetoed or disliked what (FR-29).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from dinnerbell.domain.catalog import MODE_ORDER, Mode
from dinnerbell.domain.events import Coverage
from dinnerbell.domain.schedule import Weights

TAG_SCORE_FACTOR = 0.5
DEFAULT_MODE_BONUS = 0.5
VOLUNTEER_FIT_BONUS = 0.5
REASONS_SHOWN = 2
SCORE_PLACES = 6
"""Scores are compared after rounding, so 0.1 + 0.2 ties with 0.3 the way a person expects."""


class Component(StrEnum):
    SOFT_PREFERENCE = "soft_preference"
    CUISINE_VARIETY = "cuisine_variety"
    NOVELTY = "novelty"
    SUGGESTED = "suggested"
    DEFAULT_MODE = "default_mode"
    VOLUNTEER_FIT = "volunteer_fit"


COMPONENT_ORDER = tuple(Component)


@dataclass(frozen=True, slots=True)
class RankItem:
    """An active catalog item, as ranking sees it."""

    id: int
    name: str
    modes: frozenset[Mode]
    tag_ids: frozenset[int]
    cuisine_ids: frozenset[int]
    cook_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class GroupPreferences:
    """Preferences combined across the people eating. Vetoes are a union; scores are sums."""

    vetoed_tags: frozenset[int] = frozenset()
    vetoed_items: frozenset[int] = frozenset()
    tag_scores: Mapping[int, int] = field(default_factory=dict)
    item_scores: Mapping[int, int] = field(default_factory=dict)

    def vetoes(self, item: RankItem) -> bool:
        return item.id in self.vetoed_items or bool(item.tag_ids & self.vetoed_tags)

    def soft_score(self, item: RankItem) -> float:
        tags = sum(self.tag_scores.get(tag, 0) for tag in item.tag_ids)
        return self.item_scores.get(item.id, 0) + TAG_SCORE_FACTOR * tags


@dataclass(frozen=True, slots=True)
class RankingSnapshot:
    """Everything ranking reads. Day counts are whole days before the call's date.

    ``recent_items`` and ``recent_cuisines`` hold what was eaten inside the variety window;
    ``last_eaten_days`` holds every item ever eaten. ``suggesters`` and ``member_names`` hold
    names for the reasons; both name only participants, whose suggestions and offers the roster
    already shows.
    """

    items: tuple[RankItem, ...]
    preferences: GroupPreferences
    coverage: Coverage
    weights: Weights
    window_days: int
    guest_needs: frozenset[int] = frozenset()
    default_modes: frozenset[Mode] = frozenset()
    recent_items: Mapping[int, int] = field(default_factory=dict)
    recent_cuisines: Mapping[int, int] = field(default_factory=dict)
    last_eaten_days: Mapping[int, int] = field(default_factory=dict)
    suggesters: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    member_names: Mapping[int, str] = field(default_factory=dict)
    cuisine_names: Mapping[int, str] = field(default_factory=dict)
    limit: int = 3


@dataclass(frozen=True, slots=True)
class Ranked:
    item_id: int
    name: str
    mode: Mode
    score: float
    components: Mapping[Component, float]
    reasons: tuple[str, ...]


def candidates(snapshot: RankingSnapshot) -> list[tuple[RankItem, Mode]]:
    """Steps 1 and 2: every (item, mode) pair that passes the hard filters. Every slot allows
    every mode in version 1; the slot's default modes earn a bonus instead."""
    pairs = [
        (item, mode)
        for item in snapshot.items
        if not snapshot.preferences.vetoes(item) and snapshot.guest_needs <= item.tag_ids
        for mode in MODE_ORDER
        if mode in item.modes and snapshot.coverage.covers(mode, item.cook_ids)
    ]
    fresh = [(item, mode) for item, mode in pairs if item.id not in snapshot.recent_items]
    return fresh or pairs  # a repeat inside the window only when nothing else fits


def _variety(item: RankItem, snapshot: RankingSnapshot) -> float:
    return -sum(
        1 - snapshot.recent_cuisines[tag] / snapshot.window_days
        for tag in item.cuisine_ids
        if tag in snapshot.recent_cuisines
    )


def _volunteer_fit(item: RankItem, mode: Mode, snapshot: RankingSnapshot) -> bool:
    return mode is Mode.COOK and any(cook in item.cook_ids for cook in snapshot.coverage.cooks)


def components(item: RankItem, mode: Mode, snapshot: RankingSnapshot) -> dict[Component, float]:
    """Step 3: each component's raw value times its weight."""
    w = snapshot.weights
    raw = {
        Component.SOFT_PREFERENCE: snapshot.preferences.soft_score(item),
        Component.CUISINE_VARIETY: _variety(item, snapshot),
        Component.NOVELTY: 0.0 if item.id in snapshot.last_eaten_days else 1.0,
        Component.SUGGESTED: float(len(snapshot.suggesters.get(item.id, ()))),
        Component.DEFAULT_MODE: DEFAULT_MODE_BONUS if mode in snapshot.default_modes else 0.0,
        Component.VOLUNTEER_FIT: VOLUNTEER_FIT_BONUS if _volunteer_fit(item, mode, snapshot) else 0.0,
    }
    return {name: value * getattr(w, name.value) for name, value in raw.items()}


def _order_key(
    item: RankItem, mode: Mode, score: float, snapshot: RankingSnapshot
) -> tuple[float, float, str, int, int]:
    """Step 4: score descending, then fewest days since last eaten, then name. Mode order and
    item ID only make the order total, so equal inputs always give equal output."""
    since = snapshot.last_eaten_days.get(item.id)
    return (
        -round(score, SCORE_PLACES),
        float("inf") if since is None else since,
        item.name.casefold(),
        MODE_ORDER.index(mode),
        item.id,
    )


def rank(snapshot: RankingSnapshot) -> list[Ranked]:
    """The top ``snapshot.limit`` proposals, best first, each with its reasons."""
    scored = []
    for item, mode in candidates(snapshot):
        parts = components(item, mode, snapshot)
        score = sum(parts.values())
        scored.append((_order_key(item, mode, score, snapshot), item, mode, score, parts))
    scored.sort(key=lambda entry: entry[0])
    return [
        Ranked(
            item_id=item.id,
            name=item.name,
            mode=mode,
            score=round(score, SCORE_PLACES),
            components=parts,
            reasons=explain(item, mode, parts, snapshot),
        )
        for _, item, mode, score, parts in scored[: max(snapshot.limit, 0)]
    ]


# Reasons (step 5) ------------------------------------------------------------------------------


def join_names(names: tuple[str, ...] | list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _days_ago(days: int) -> str:
    if days == 0:
        return "today"
    return "yesterday" if days == 1 else f"{days} days ago"


def _sentence(component: Component, item: RankItem, mode: Mode, value: float, snapshot: RankingSnapshot) -> str:
    if component is Component.SUGGESTED:
        return f"Suggested by {join_names(snapshot.suggesters.get(item.id, ()))}."
    if component is Component.SOFT_PREFERENCE:
        return "A favorite with tonight's group." if value > 0 else "Not everyone's favorite."
    if component is Component.NOVELTY:
        return "New to the household."
    if component is Component.DEFAULT_MODE:
        return f"{mode.label} is a usual choice."
    if component is Component.VOLUNTEER_FIT:
        cooks = [snapshot.member_names.get(c, "A volunteer") for c in snapshot.coverage.cooks if c in item.cook_ids]
        return f"{join_names(cooks)} offered to cook and knows this one."
    recent = [t for t in item.cuisine_ids if t in snapshot.recent_cuisines]
    latest = min(recent, key=lambda t: (snapshot.recent_cuisines[t], snapshot.cuisine_names.get(t, "")))
    name = snapshot.cuisine_names.get(latest, "This cuisine")
    return f"Had {name} {_days_ago(snapshot.recent_cuisines[latest])}."


def explain(item: RankItem, mode: Mode, parts: Mapping[Component, float], snapshot: RankingSnapshot) -> tuple[str, ...]:
    """The top two positive components and every penalty, as sentences. A pair with no
    standout component still gets a reason, because it passed every filter."""
    positive = sorted(
        (c for c in COMPONENT_ORDER if parts[c] > 0), key=lambda c: (-parts[c], COMPONENT_ORDER.index(c))
    )[:REASONS_SHOWN]
    penalties = [c for c in COMPONENT_ORDER if parts[c] < 0]
    sentences = tuple(_sentence(c, item, mode, parts[c], snapshot) for c in [*positive, *penalties])
    return sentences or ("Works for everyone who's in.",)
