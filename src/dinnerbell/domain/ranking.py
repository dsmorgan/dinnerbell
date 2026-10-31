# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ranking: an explainable, seeded choice of proposals for a call (spec section 7, ADR 0005,
ADR 0007).

``rank`` is a pure function over a ``RankingSnapshot``. Hard filters decide what can be picked;
scores decide how likely each place is; the snapshot's seed decides the draw, so the same
snapshot always gives the same picks. The snapshot carries only combined preferences for the
people eating, never one member's stances, so nothing ranking produces can say who vetoed or
disliked what (FR-29). Item scores already include what each member's ratings did to their own
score (FR-55, ADR 0008), and the household's hint for an item is its first reason.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from dinnerbell.domain.catalog import DEFAULT_MEALS, MODE_ORDER, Meal, Mode, meal_words
from dinnerbell.domain.events import Coverage
from dinnerbell.domain.schedule import DEFAULT_TEMPERATURE, Weights

TAG_SCORE_FACTOR = 0.5
DEFAULT_MODE_BONUS = 0.5
VOLUNTEER_FIT_BONUS = 0.5
REASONS_SHOWN = 2
SCORE_PLACES = 6
"""Scores are compared after rounding, so 0.1 + 0.2 ties with 0.3 the way a person expects."""
SAME_CUISINE_FACTOR = 0.3
"""Step 7: once a place is picked, each place sharing one of its cuisines keeps this share of its
weight, so a vote rarely has three taco places when other cuisines fit."""


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
    """Preferences combined across the people eating. Vetoes are a union; scores are sums. An
    item score is each member's stated score plus what their ratings did to it (FR-55), so it
    needn't be whole."""

    vetoed_tags: frozenset[int] = frozenset()
    vetoed_items: frozenset[int] = frozenset()
    tag_scores: Mapping[int, int] = field(default_factory=dict)
    item_scores: Mapping[int, float] = field(default_factory=dict)

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
    temperature: float = DEFAULT_TEMPERATURE
    """How far the draw strays from score order (step 6). 0 takes the best scores in order."""
    seed: int = 0
    """Seeds the draw: ``ranking_seed`` of the call and run, so a call always ranks the same and
    a restart draws again (#78)."""
    hints: Mapping[int, str] = field(default_factory=dict)
    """The household's hint for each item, such as "Mixed reviews · recently late" (FR-55). It's
    the first reason on a proposal and never changes a score."""


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
    equals: int = 1
    """How many candidates in its group (the meal's, or the top-up) had exactly this score, this
    one included. With 4, it was chosen at random among 4 equally good options."""


def ranking_seed(household_id: int, event_id: int, run: int, shuffle: int = 0) -> int:
    """The draw's seed for one run of a call. A digest, not ``hash()``, so it's the same in every
    process and on every restart of the app. ``shuffle`` counts the coordinator's shuffles (#87):
    0, the first draw, keeps the seed it always had, and each shuffle draws with the next."""
    text = f"dinnerbell ranking {household_id} {event_id} {run}"
    digest = hashlib.sha256((text if shuffle == 0 else f"{text} shuffle {shuffle}").encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _eligible(snapshot: RankingSnapshot) -> list[tuple[RankItem, Mode]]:
    """Every (item, mode) pair that passes the hard filters other than the meal, for any meal.
    The variety window applies once, across every meal: a repeat only when nothing fresh fits
    for any meal (FR-31), so a fresh place for another meal beats a repeat for this one."""
    pairs = [
        (item, mode)
        for item in snapshot.items
        if item.id not in snapshot.taken
        and not snapshot.preferences.vetoes(item)
        and snapshot.guest_needs <= item.tag_ids
        for mode in MODE_ORDER
        if mode in item.modes and snapshot.coverage.covers(mode, item.cook_ids)
    ]
    fresh = [(item, mode) for item, mode in pairs if item.id not in snapshot.recent_items]
    return fresh or pairs  # a repeat inside the window only when nothing else fits


def candidates(snapshot: RankingSnapshot, *, for_meal: bool = True) -> list[tuple[RankItem, Mode]]:
    """Steps 1 and 2: every (item, mode) pair that passes the hard filters, leaving out items
    already taken. Every slot allows every mode in version 1; the slot's default modes earn a
    bonus instead.

    The meal filter comes last, after the variety window: it keeps the items for the call's
    meal, and ``for_meal=False`` keeps the others instead, the top-up's candidates (step 8)."""
    return [(item, mode) for item, mode in _eligible(snapshot) if (snapshot.meal in item.meals) is for_meal]


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


def _mode_key(mode: Mode, parts: Mapping[Component, float]) -> tuple[float, int]:
    """Step 4: how one way of getting an item compares with another, by the mode's own components,
    best first, then ``MODE_ORDER``."""
    return (-round(sum(parts[c] for c in MODE_COMPONENTS), SCORE_PLACES), MODE_ORDER.index(mode))


_Scored = tuple[RankItem, Ranked]
"""A candidate with its score: the item, for its cuisines, and the proposal it would be."""


def _scored(snapshot: RankingSnapshot, *, for_meal: bool) -> list[_Scored]:
    """Steps 3, 4, and 9 over one group, every candidate, by item ID. Each item is proposed once,
    in its best mode (``_mode_key``), so the picks are different places (#77). The ID order only
    fixes which random number goes to which candidate; it never favors one."""
    best: dict[int, tuple[RankItem, Mode, dict[Component, float]]] = {}
    for item, mode in candidates(snapshot, for_meal=for_meal):
        parts = components(item, mode, snapshot)
        kept = best.get(item.id)
        if kept is None or _mode_key(mode, parts) < _mode_key(kept[1], kept[2]):
            best[item.id] = (item, mode, parts)
    scores = {item_id: round(sum(parts.values()), SCORE_PLACES) for item_id, (_, _, parts) in best.items()}
    equals = Counter(scores.values())
    return [
        (
            item,
            Ranked(
                item_id=item.id,
                name=item.name,
                mode=mode,
                score=scores[item.id],
                components=parts,
                reasons=explain(item, mode, parts, snapshot, for_meal=for_meal),
                for_meal=for_meal,
                equals=equals[scores[item.id]],
            ),
        )
        for item, mode, parts in (best[item_id] for item_id in sorted(best))
    ]


def _log_weight(score: float, best: float, temperature: float) -> float:
    """The log of ``exp((score - best) / temperature)``, so no weight overflows or rounds to 0.
    At temperature 0 only the best score has any weight: score order, ties drawn at random."""
    if temperature > 0:
        return (score - best) / temperature
    return 0.0 if score == best else -math.inf


def _exponential(rng: random.Random) -> float:
    """A draw from the exponential distribution with rate 1, ``-log(1 - u)`` for ``u`` in [0, 1).
    It's what ``rng.expovariate(1)`` computes, but built on ``random()``, which Python guarantees
    to be reproducible for a seed, so picks don't depend on the Python version's ``expovariate``."""
    return -math.log(1.0 - rng.random())


def _draw(pool: list[_Scored], picks: list[_Scored], limit: int, temperature: float, rng: random.Random) -> None:
    """Step 6: weighted sampling without replacement, adding to ``picks`` until it holds ``limit``
    or ``pool`` runs out. Each draw is one step of the Efraimidis-Spirakis method: every place
    left gets a key ``u ** (1 / weight)`` and the highest key wins. Keys are compared as logs,
    ``log(weight) - log(E)`` with ``E = -log(u)``, which orders them the same way without
    overflow. Weights are relative to the best score left, which changes nothing above 0 and
    keeps the next best in play at 0. A place with no weight (a worse score at temperature 0) is
    never drawn while another is left, even when ``E`` is exactly 0. Keys are drawn afresh for
    each pick, because step 7 changes the weights."""
    left = list(pool)
    while left and len(picks) < limit:
        best = max(ranked.score for _, ranked in left)
        keys: dict[int, float] = {}
        for index, (item, ranked) in enumerate(left):
            shared = sum(1 for picked, _ in picks if picked.cuisine_ids & item.cuisine_ids)
            log_weight = _log_weight(ranked.score, best, temperature) + shared * math.log(SAME_CUISINE_FACTOR)
            exponential = _exponential(rng)  # drawn for every place, so skipping one shifts no one else's number
            if log_weight == -math.inf:
                continue
            keys[index] = log_weight - math.log(exponential) if exponential > 0 else math.inf  # u = 1: a sure win
        chosen = max(keys, key=keys.__getitem__)
        picks.append(left.pop(chosen))


def rank(snapshot: RankingSnapshot) -> list[Ranked]:
    """Up to ``snapshot.limit`` proposals, each with its reasons, in the order drawn: items for
    the call's meal, then, only when too few of those fit, a top-up of items for other meals
    (#72). Within each group, places suggested for the call come first, but at most ``limit - 1``
    of them across both groups, then a weighted draw from the rest, so the last place in the vote
    is not reserved for a suggestion (at temperature 0 it is the best-scoring place left, which
    can be a suggestion). The hard filters apply to both groups, so no pick is ever a veto, an
    unmet guest need, an uncovered mode, or a needless repeat; chance only orders what passed
    them."""
    limit = max(snapshot.limit, 0)
    guaranteed = max(limit - 1, 0)  # step 5: suggestions can't fill the whole vote
    rng = random.Random(snapshot.seed)  # noqa: S311 - a reproducible draw, not a secret
    picks: list[_Scored] = []
    for for_meal in (True, False):  # step 8: the top-up, drawn after every pick for the meal
        if len(picks) >= limit:
            break
        pool = _scored(snapshot, for_meal=for_meal)
        suggested = [entry for entry in pool if snapshot.suggesters.get(entry[0].id)]
        before = len(picks)
        _draw(suggested, picks, min(limit, before + guaranteed), 0.0, rng)  # the best of them when too many
        guaranteed -= len(picks) - before
        drawn = {ranked.item_id for _, ranked in picks}
        _draw([entry for entry in pool if entry[1].item_id not in drawn], picks, limit, snapshot.temperature, rng)
    return [ranked for _, ranked in picks]


# Reasons (step 9) ------------------------------------------------------------------------------


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
    """The household's hint, a top-up's meal note, then the top two positive components and every
    penalty, as sentences. A pair with no standout component still gets a reason, because it
    passed every filter."""
    positive = sorted(
        (c for c in COMPONENT_ORDER if parts[c] > 0), key=lambda c: (-parts[c], COMPONENT_ORDER.index(c))
    )[:REASONS_SHOWN]
    penalties = [c for c in COMPONENT_ORDER if parts[c] < 0]
    sentences = tuple(_sentence(c, item, mode, parts[c], snapshot) for c in [*positive, *penalties])
    hint = snapshot.hints.get(item.id)
    first = (f"{hint}.",) if hint else ()
    if not for_meal:
        return (*first, other_meal(item.meals), *sentences)
    return (*first, *sentences) or ("Works for everyone who's in.",)
