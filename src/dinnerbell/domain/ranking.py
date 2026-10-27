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

from dinnerbell.domain.catalog import DEFAULT_MEALS, MODE_ORDER, Meal, Mode, meal_words
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
MODE_COMPONENTS = (Component.DEFAULT_MODE, Component.VOLUNTEER_FIT)
"""The components that depend on the mode. The rest score the item, whichever way it comes."""


@dataclass(frozen=True, slots=True)
class RankItem:
    """An active catalog item, as ranking sees it."""

    id: int
    name: str
    modes: frozenset[Mode]
    tag_ids: frozenset[int]
    cuisine_ids: frozenset[int]
    cook_ids: frozenset[int] = frozenset()
    meals: frozenset[Meal] = DEFAULT_MEALS


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
    already shows. ``meal`` is the call's: items for it rank first (#72).
    """

    items: tuple[RankItem, ...]
    preferences: GroupPreferences
    coverage: Coverage
    weights: Weights
    window_days: int
    meal: Meal
    guest_needs: frozenset[int] = frozenset()
    default_modes: frozenset[Mode] = frozenset()
    recent_items: Mapping[int, int] = field(default_factory=dict)
    recent_cuisines: Mapping[int, int] = field(default_factory=dict)
    last_eaten_days: Mapping[int, int] = field(default_factory=dict)
    suggesters: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    member_names: Mapping[int, str] = field(default_factory=dict)
    cuisine_names: Mapping[int, str] = field(default_factory=dict)
    limit: int = 3
    taken: frozenset[int] = frozenset()
    """Items that are picks on the call already, such as an offer's (FR-5). Ranking leaves them
    out and fills ``limit`` from the rest, since a call has one pick per place (#77)."""


@dataclass(frozen=True, slots=True)
class Ranked:
    item_id: int
    name: str
    mode: Mode
    score: float
    components: Mapping[Component, float]
    reasons: tuple[str, ...]
    for_meal: bool = True
    """False for a top-up: an item for other meals, proposed because too few for this one fit."""


def candidates(snapshot: RankingSnapshot, *, for_meal: bool = True) -> list[tuple[RankItem, Mode]]:
    """Steps 1 and 2: every (item, mode) pair that passes the hard filters, leaving out items
    already taken. Every slot allows every mode in version 1; the slot's default modes earn a
    bonus instead.

    The meal filter keeps the items for the call's meal; ``for_meal=False`` keeps the others
    instead, the top-up's candidates (step 6). The variety window applies to each set apart, so
    a repeat for this meal still ranks before anything for other meals."""
    pairs = [
        (item, mode)
        for item in snapshot.items
        if (snapshot.meal in item.meals) is for_meal
        and item.id not in snapshot.taken
        and not snapshot.preferences.vetoes(item)
        and snapshot.guest_needs <= item.tag_ids
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
    """Step 5: score descending, then the item eaten longest ago (never eaten first), then
    name. Mode order and item ID only make the order total, so equal inputs always give equal
    output."""
    since = snapshot.last_eaten_days.get(item.id)
    return (
        -round(score, SCORE_PLACES),
        float("-inf") if since is None else -since,
        item.name.casefold(),
        MODE_ORDER.index(mode),
        item.id,
    )


def _mode_key(mode: Mode, parts: Mapping[Component, float]) -> tuple[float, int]:
    """Step 4: how one way of getting an item compares with another, by the mode's own components,
    best first, then ``MODE_ORDER``."""
    return (-round(sum(parts[c] for c in MODE_COMPONENTS), SCORE_PLACES), MODE_ORDER.index(mode))


def _ranked(snapshot: RankingSnapshot, *, for_meal: bool) -> list[Ranked]:
    """Steps 3 to 5 and 7 over one candidate set, best first, every candidate. Each item is
    proposed once, in its best mode (``_mode_key``), so the picks are different places (#77)."""
    best: dict[int, tuple[RankItem, Mode, dict[Component, float]]] = {}
    for item, mode in candidates(snapshot, for_meal=for_meal):
        parts = components(item, mode, snapshot)
        kept = best.get(item.id)
        if kept is None or _mode_key(mode, parts) < _mode_key(kept[1], kept[2]):
            best[item.id] = (item, mode, parts)
    scored = []
    for item, mode, parts in best.values():
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
            reasons=explain(item, mode, parts, snapshot, for_meal=for_meal),
            for_meal=for_meal,
        )
        for _, item, mode, score, parts in scored
    ]


def rank(snapshot: RankingSnapshot) -> list[Ranked]:
    """The top ``snapshot.limit`` proposals, best first, each with its reasons: items for the
    call's meal, then, only when too few of those fit, a top-up of items for other meals (#72).
    The hard filters apply to both, so a top-up is never a veto or an uncovered mode."""
    limit = max(snapshot.limit, 0)
    picks = _ranked(snapshot, for_meal=True)[:limit]
    if len(picks) < limit:  # step 6: the top-up, ranked after every pick for the meal
        picks += _ranked(snapshot, for_meal=False)[: limit - len(picks)]
    return picks


# Reasons (step 7) ------------------------------------------------------------------------------


def join_names(names: tuple[str, ...] | list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def other_meal(meals: frozenset[Meal]) -> str:
    """A top-up's first reason, since it's why the pick may not suit (#72)."""
    return f"Usually for {meal_words(meals)}."


def usual_choice(mode: Mode) -> str:
    """The default-mode reason, which stops being true when an offer changes a pick's mode."""
    return f"{mode.label} is a usual choice."


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
        return usual_choice(mode)
    if component is Component.VOLUNTEER_FIT:
        cooks = [snapshot.member_names.get(c, "A volunteer") for c in snapshot.coverage.cooks if c in item.cook_ids]
        return f"{join_names(cooks)} offered to cook and knows this one."
    recent = [t for t in item.cuisine_ids if t in snapshot.recent_cuisines]
    latest = min(recent, key=lambda t: (snapshot.recent_cuisines[t], snapshot.cuisine_names.get(t, "")))
    name = snapshot.cuisine_names.get(latest, "This cuisine")
    return f"Had {name} {_days_ago(snapshot.recent_cuisines[latest])}."


def explain(
    item: RankItem, mode: Mode, parts: Mapping[Component, float], snapshot: RankingSnapshot, *, for_meal: bool = True
) -> tuple[str, ...]:
    """The top two positive components and every penalty, as sentences, after a top-up's meal
    note. A pair with no standout component still gets a reason, because it passed every filter."""
    positive = sorted(
        (c for c in COMPONENT_ORDER if parts[c] > 0), key=lambda c: (-parts[c], COMPONENT_ORDER.index(c))
    )[:REASONS_SHOWN]
    penalties = [c for c in COMPONENT_ORDER if parts[c] < 0]
    sentences = tuple(_sentence(c, item, mode, parts[c], snapshot) for c in [*positive, *penalties])
    if not for_meal:
        return (other_meal(item.meals), *sentences)
    return sentences or ("Works for everyone who's in.",)
