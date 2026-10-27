# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ranking (spec section 7, ADR 0005): worked examples, then property-style checks over
hundreds of seeded random snapshots. No database."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from dinnerbell.domain.catalog import DEFAULT_MEALS, MODE_ORDER, Meal, Mode
from dinnerbell.domain.events import Coverage
from dinnerbell.domain.ranking import (
    MODE_COMPONENTS,
    Component,
    GroupPreferences,
    RankingSnapshot,
    RankItem,
    candidates,
    components,
    join_names,
    rank,
)
from dinnerbell.domain.schedule import Weights

THAI, MEXICAN, ETHIOPIAN, VEGAN, GLUTEN_FREE = 1, 2, 3, 4, 5
NAMES = {THAI: "Thai", MEXICAN: "Mexican", ETHIOPIAN: "Ethiopian"}


def item(
    item_id: int,
    name: str,
    *modes: Mode,
    cuisine: int = THAI,
    tags: frozenset[int] = frozenset(),
    cooks: frozenset[int] = frozenset(),
    meals: frozenset[Meal] = DEFAULT_MEALS,
) -> RankItem:
    return RankItem(
        id=item_id,
        name=name,
        modes=frozenset(modes or (Mode.PICKUP,)),
        tag_ids=frozenset({cuisine}) | tags,
        cuisine_ids=frozenset({cuisine}),
        cook_ids=cooks,
        meals=meals,
    )


def snapshot(*items: RankItem, **changes: object) -> RankingSnapshot:
    base = RankingSnapshot(
        items=items,
        preferences=GroupPreferences(),
        coverage=Coverage(cooks=(1,), pickups=(1,), orderers=(1,)),
        weights=Weights(),
        window_days=7,
        meal=Meal.DINNER,
        cuisine_names=NAMES,
        member_names={1: "Alex", 2: "Sam"},
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_hard_filters() -> None:
    siam = item(1, "Siam", Mode.PICKUP, Mode.DELIVERY)
    tacos = item(2, "Tacos", Mode.COOK, cuisine=MEXICAN, cooks=frozenset({2}))
    salad = item(3, "Salad", Mode.PICKUP, cuisine=MEXICAN, tags=frozenset({VEGAN}))
    base = snapshot(siam, tacos, salad, coverage=Coverage(cooks=(1,), pickups=(1,)))
    # Tacos: only member 2 can cook it and 2 didn't volunteer. Siam delivery: nobody orders.
    assert {(i.id, m) for i, m in candidates(base)} == {(1, Mode.PICKUP), (3, Mode.PICKUP)}
    vetoed = replace(base, preferences=GroupPreferences(vetoed_tags=frozenset({MEXICAN})))
    assert {(i.id, m) for i, m in candidates(vetoed)} == {(1, Mode.PICKUP)}
    by_item = replace(base, preferences=GroupPreferences(vetoed_items=frozenset({1})))
    assert {(i.id, m) for i, m in candidates(by_item)} == {(3, Mode.PICKUP)}
    vegan_guest = replace(base, guest_needs=frozenset({VEGAN}))
    assert {(i.id, m) for i, m in candidates(vegan_guest)} == {(3, Mode.PICKUP)}


def test_recent_items_drop_out_unless_nothing_else_fits() -> None:
    siam, tacos = item(1, "Siam"), item(2, "Taqueria", cuisine=MEXICAN)
    recent = snapshot(siam, tacos, recent_items={1: 2}, last_eaten_days={1: 2})
    assert [r.item_id for r in rank(recent)] == [2]
    only = snapshot(siam, recent_items={1: 2}, last_eaten_days={1: 2})
    assert [r.item_id for r in rank(only)] == [1]


def test_scores_follow_the_spec_table() -> None:
    cook = item(1, "Pad Thai", Mode.COOK, cooks=frozenset({1}))
    snap = snapshot(
        cook,
        preferences=GroupPreferences(item_scores={1: 2}, tag_scores={THAI: 3}),
        recent_cuisines={THAI: 2},
        suggesters={1: ("Alex", "Sam")},
        default_modes=frozenset({Mode.COOK}),
    )
    (top,) = rank(snap)
    assert top.components == {
        Component.SOFT_PREFERENCE: 2 + 0.5 * 3,
        Component.CUISINE_VARIETY: -(1 - 2 / 7) * 3.0,
        Component.NOVELTY: 1.0,
        Component.SUGGESTED: 2 * 2.0,
        Component.DEFAULT_MODE: 0.5,
        Component.VOLUNTEER_FIT: 0.5,
    }
    assert top.score == round(sum(top.components.values()), 6)
    # The two biggest positives, then every penalty.
    assert top.reasons == ("Suggested by Alex and Sam.", "A favorite with tonight's group.", "Had Thai 2 days ago.")


def test_weights_scale_components() -> None:
    snap = snapshot(
        item(1, "Siam"), weights=Weights(novelty=0.0, default_mode=4.0), default_modes=frozenset({Mode.PICKUP})
    )
    (top,) = rank(snap)
    assert top.components[Component.NOVELTY] == 0.0
    assert top.components[Component.DEFAULT_MODE] == 2.0
    assert top.reasons == ("Pickup is a usual choice.",)


def test_reasons_cover_every_component() -> None:
    cook = item(1, "Curry", Mode.COOK, cooks=frozenset({1, 2}))
    snap = snapshot(
        cook,
        coverage=Coverage(cooks=(1, 2)),
        weights=Weights(soft_preference=0, cuisine_variety=0, novelty=0, suggested=0, default_mode=0),
        last_eaten_days={1: 30},
    )
    assert rank(snap)[0].reasons == ("Alex and Sam offered to cook and knows this one.",)
    novel = snapshot(item(1, "New"), weights=Weights(default_mode=0))
    assert rank(novel)[0].reasons == ("New to the household.",)
    plain = snapshot(item(1, "Old"), last_eaten_days={1: 40})
    assert rank(plain)[0].reasons == ("Works for everyone who's in.",)
    disliked = snapshot(item(1, "Meh"), last_eaten_days={1: 40}, preferences=GroupPreferences(item_scores={1: -1}))
    assert rank(disliked)[0].reasons == ("Not everyone's favorite.",)
    today = snapshot(item(1, "Again", cuisine=MEXICAN), last_eaten_days={1: 40}, recent_cuisines={MEXICAN: 0})
    assert rank(today)[0].reasons == ("Had Mexican today.",)
    yesterday = replace(today, recent_cuisines={MEXICAN: 1})
    assert rank(yesterday)[0].reasons == ("Had Mexican yesterday.",)


def test_ordering_and_tie_breaks() -> None:
    # Equal scores: never-eaten items first, then the one eaten longest ago, then name.
    a, b, c, d = item(1, "Bravo"), item(2, "alpha"), item(3, "Charlie"), item(4, "Delta")
    snap = snapshot(a, b, c, d, weights=Weights(novelty=0), last_eaten_days={1: 20, 2: 20, 3: 10}, limit=4)
    assert [r.item_id for r in rank(snap)] == [4, 2, 1, 3]
    higher = replace(snap, preferences=GroupPreferences(item_scores={4: 1}))
    assert rank(higher)[0].item_id == 4
    assert len(rank(replace(snap, limit=2))) == 2
    assert rank(replace(snap, limit=0)) == []


def test_each_item_is_proposed_once_in_its_best_mode() -> None:
    """#77: one pick per place. The mode with the better mode components wins; a tie goes to
    ``MODE_ORDER``."""
    siam = item(1, "Siam", Mode.DELIVERY, Mode.PICKUP)
    assert [(r.item_id, r.mode) for r in rank(snapshot(siam))] == [(1, Mode.PICKUP)]
    usual = snapshot(siam, default_modes=frozenset({Mode.DELIVERY}))
    assert [(r.item_id, r.mode) for r in rank(usual)] == [(1, Mode.DELIVERY)]
    nobody_drives = snapshot(siam, coverage=Coverage(orderers=(1,)))
    assert [(r.item_id, r.mode) for r in rank(nobody_drives)] == [(1, Mode.DELIVERY)]


def test_places_that_allow_pickup_and_delivery_give_three_different_picks() -> None:
    """#77's report: every restaurant allows both, and both are covered."""
    places = [item(n, f"Place {n}", Mode.PICKUP, Mode.DELIVERY) for n in range(1, 6)]
    ranked = rank(snapshot(*places, default_modes=frozenset({Mode.PICKUP})))
    assert [(r.item_id, r.mode) for r in ranked] == [(1, Mode.PICKUP), (2, Mode.PICKUP), (3, Mode.PICKUP)]


def test_taken_items_are_left_out_and_the_limit_still_filled() -> None:
    """#77: an offer's pick doesn't shrink the ranked picks."""
    places = [item(n, f"Place {n}", Mode.PICKUP, Mode.DELIVERY) for n in range(1, 6)]
    ranked = rank(snapshot(*places, taken=frozenset({1, 3})))
    assert [r.item_id for r in ranked] == [2, 4, 5]
    assert rank(snapshot(*places[:2], taken=frozenset({1, 2}))) == []


DINNER_ONLY = frozenset({Meal.DINNER})


def test_a_lunch_call_never_proposes_a_dinner_place_while_enough_lunch_places_fit() -> None:
    """#72: the meal is a hard filter. A dinner-only place, however well it scores, stays out
    while ``limit`` places for lunch pass every other filter."""
    steakhouse = item(1, "Steakhouse", meals=DINNER_ONLY)
    lunch = [item(n, f"Lunch {n}") for n in range(2, 5)]
    favored = GroupPreferences(item_scores={1: 2})
    snap = snapshot(steakhouse, *lunch, meal=Meal.LUNCH, preferences=favored, suggesters={1: ("Alex",)})
    ranked = rank(snap)
    assert [r.item_id for r in ranked] == [2, 3, 4]
    assert all(r.for_meal for r in ranked)
    assert {i.id for i, _ in candidates(snap)} == {2, 3, 4}
    assert {i.id for i, _ in candidates(snap, for_meal=False)} == {1}
    # For dinner it's in, and it wins.
    assert rank(replace(snap, meal=Meal.DINNER))[0].item_id == 1


def test_a_top_up_fills_the_rest_and_ranks_after_the_meals_own_picks() -> None:
    """Too few lunch places fit, so the best of the rest fill the vote, after every lunch pick
    whatever their scores, each saying what it's usually for."""
    bagels = item(1, "Bagels", meals=frozenset({Meal.BREAKFAST}))
    steakhouse = item(2, "Steakhouse", meals=DINNER_ONLY)
    diner = item(3, "Diner", meals=frozenset({Meal.BREAKFAST, Meal.DINNER}))
    deli = item(4, "Deli", meals=frozenset({Meal.LUNCH}))
    favored = GroupPreferences(item_scores={2: 2, 3: 1})
    snap = snapshot(bagels, steakhouse, diner, deli, meal=Meal.LUNCH, preferences=favored, last_eaten_days={4: 30})
    ranked = rank(snap)
    assert [(r.item_id, r.for_meal) for r in ranked] == [(4, True), (2, False), (3, False)]
    assert ranked[0].score < ranked[1].score  # the lunch place first anyway
    assert ranked[1].reasons == ("Usually for dinner.", "A favorite with tonight's group.", "New to the household.")
    assert ranked[2].reasons[0] == "Usually for breakfast and dinner."
    assert [r.item_id for r in rank(replace(snap, limit=5))] == [4, 2, 3, 1]
    # Nothing fits lunch: the vote is all top-ups.
    assert [r.item_id for r in rank(replace(snap, items=(bagels, steakhouse)))] == [2, 1]


def test_a_top_up_passes_every_hard_filter() -> None:
    """Vetoes, guest needs, and coverage hold for a top-up too."""
    vegan = frozenset({VEGAN})
    deli = item(1, "Deli", tags=vegan, meals=frozenset({Meal.LUNCH}))
    vetoed = item(2, "Vetoed", tags=vegan, meals=DINNER_ONLY)
    not_vegan = item(3, "Not vegan", meals=DINNER_ONLY)
    uncovered = item(4, "Uncovered", Mode.COOK, tags=vegan, cooks=frozenset({2}), meals=DINNER_ONLY)
    fresh = item(5, "Fresh", tags=vegan, meals=DINNER_ONLY)
    snap = snapshot(
        deli,
        vetoed,
        not_vegan,
        uncovered,
        fresh,
        meal=Meal.LUNCH,
        guest_needs=vegan,
        preferences=GroupPreferences(vetoed_items=frozenset({2})),
    )
    assert [(r.item_id, r.for_meal) for r in rank(snap)] == [(1, True), (5, False)]


def test_the_variety_window_applies_once_across_meals_before_the_meal_order() -> None:
    """FR-31 holds as written: a repeat only when nothing else fits, for any meal. So fresh beats
    meal fit, and meal fit beats score."""
    eaten = {"recent_items": {1: 1}, "last_eaten_days": {1: 1}}
    deli = item(1, "Deli", meals=frozenset({Meal.LUNCH}))
    dinners = [item(n, f"Dinner {n}", meals=DINNER_ONLY) for n in (2, 3, 4)]
    # Case A: the only lunch place was eaten yesterday; fresh dinner places fill the vote.
    only = snapshot(deli, *dinners, meal=Meal.LUNCH, **eaten)
    assert [(r.item_id, r.for_meal) for r in rank(only)] == [(2, False), (3, False), (4, False)]
    assert candidates(only) == []
    # Case B: two fresh lunch places and the recent one: the fresh two, then a dinner top-up.
    lunches = [item(n, f"Lunch {n}", meals=frozenset({Meal.LUNCH})) for n in (5, 6)]
    two = snapshot(deli, *lunches, *dinners, meal=Meal.LUNCH, **eaten)
    assert [(r.item_id, r.for_meal) for r in rank(two)] == [(5, True), (6, True), (2, False)]
    # Everything was eaten inside the window: repeats come back, the meal's first.
    recent = dict.fromkeys(range(1, 7), 1)
    again = replace(two, recent_items=recent, last_eaten_days=recent)
    assert [(r.item_id, r.for_meal) for r in rank(again)] == [(1, True), (5, True), (6, True)]
    assert [(r.item_id, r.for_meal) for r in rank(replace(again, limit=4))][-1] == (2, False)


def test_join_names() -> None:
    assert join_names(["A"]) == "A"
    assert join_names(["A", "B"]) == "A and B"
    assert join_names(["A", "B", "C"]) == "A, B, and C"


# Properties ------------------------------------------------------------------------------------

BATCHES = 8
SEEDS_PER_BATCH = 100
TAGS = list(range(1, 13))
CUISINES = [1, 2, 3, 4]
DIETARY = [10, 11, 12]


def random_snapshot(rng: random.Random) -> RankingSnapshot:
    items = []
    for item_id in range(1, rng.randint(1, 14)):
        modes = frozenset(rng.sample(list(Mode), rng.randint(1, 3)))
        cuisine = rng.choice(CUISINES)
        tags = frozenset({cuisine, *rng.sample(TAGS, rng.randint(0, 3))})
        cooks = frozenset(rng.sample([1, 2, 3, 4], rng.randint(0, 2)))
        meals = frozenset(rng.sample(list(Meal), rng.randint(1, 3)))
        items.append(RankItem(item_id, f"Item {rng.randint(0, 5)}", modes, tags, frozenset({cuisine}), cooks, meals))
    ids = [i.id for i in items]
    pick = lambda pool, k: frozenset(rng.sample(pool, min(len(pool), rng.randint(0, k))))  # noqa: E731
    return RankingSnapshot(
        items=tuple(items),
        preferences=GroupPreferences(
            vetoed_tags=pick(TAGS, 2),
            vetoed_items=pick(ids, 2),
            tag_scores={t: rng.randint(-6, 6) for t in pick(TAGS, 5)},
            item_scores={i: rng.randint(-6, 6) for i in pick(ids, 5)},
        ),
        coverage=Coverage(
            cooks=tuple(pick([1, 2, 3, 4], 2)), pickups=tuple(pick([1, 2], 1)), orderers=tuple(pick([1, 3], 1))
        ),
        weights=Weights(*(rng.choice([0.0, 0.5, 1.0, 2.0, 3.0]) for _ in range(6))),
        window_days=rng.randint(1, 14),
        meal=rng.choice(list(Meal)),
        guest_needs=pick(DIETARY, 1),
        default_modes=pick(list(Mode), 2),
        recent_items={i: rng.randint(0, 6) for i in pick(ids, 4)},
        recent_cuisines={c: rng.randint(0, 6) for c in pick(CUISINES, 2)},
        last_eaten_days={i: rng.randint(0, 60) for i in pick(ids, 8)},
        suggesters={i: ("Alex",) * rng.randint(1, 2) for i in pick(ids, 2)},
        member_names={1: "Alex", 2: "Sam", 3: "Kim", 4: "Lee"},
        cuisine_names={c: f"Cuisine {c}" for c in CUISINES},
        limit=rng.randint(1, 5),
        taken=pick(ids, 3),
    )


def by_id(snap: RankingSnapshot) -> dict[int, RankItem]:
    return {i.id: i for i in snap.items}


@pytest.mark.parametrize("batch", range(BATCHES))
def test_properties(batch: int) -> None:
    for seed in range(batch * SEEDS_PER_BATCH, (batch + 1) * SEEDS_PER_BATCH):
        try:
            check_properties(seed)
        except AssertionError as exc:
            raise AssertionError(f"ranking property failed for seed {seed}") from exc


def check_properties(seed: int) -> None:
    snap = random_snapshot(random.Random(seed))  # noqa: S311 - reproducible test data
    items = by_id(snap)
    ranked = rank(snap)
    assert len(ranked) <= snap.limit
    eligible_any = [
        (i, m)
        for i in snap.items
        for m in i.modes
        if i.id not in snap.taken
        and not snap.preferences.vetoes(i)
        and snap.guest_needs <= i.tag_ids
        and snap.coverage.covers(m, i.cook_ids)
    ]
    # Items for the call's meal, then a top-up from the rest (#72), after the variety window,
    # which applies once across every meal (FR-31).
    fresh_exists = any(i.id not in snap.recent_items for i, _ in eligible_any)
    pool = {i.id for i, _ in eligible_any if i.id not in snap.recent_items or not fresh_exists}
    pools = {fm: {i for i in pool if (snap.meal in items[i].meals) is fm} for fm in (True, False)}
    matched = min(snap.limit, len(pools[True]))
    topped = min(snap.limit - matched, len(pools[False]))
    # One pick per place (#77), none of them taken already, and as many as the limit allows.
    assert len({p.item_id for p in ranked}) == len(ranked) == matched + topped
    # The meal's picks come first, and a top-up only when too few of them fit.
    assert [p.for_meal for p in ranked] == [True] * matched + [False] * topped
    assert all(p.item_id in pools[p.for_meal] for p in ranked)
    assert all((snap.meal in items[p.item_id].meals) is p.for_meal for p in ranked)
    for proposal in ranked:
        chosen = items[proposal.item_id]
        assert proposal.item_id not in snap.taken
        # The item's best mode by its mode components, ties going to the earlier mode.
        modes = [m for i, m in eligible_any if i.id == chosen.id]
        mode_score = {m: round(sum(components(chosen, m, snap)[c] for c in MODE_COMPONENTS), 6) for m in modes}
        assert mode_score[proposal.mode] == max(mode_score.values())
        assert all(
            MODE_ORDER.index(proposal.mode) < MODE_ORDER.index(m)
            for m in modes
            if m is not proposal.mode and mode_score[m] == mode_score[proposal.mode]
        )
        # No vetoed item or tag ever appears.
        assert proposal.item_id not in snap.preferences.vetoed_items
        assert not chosen.tag_ids & snap.preferences.vetoed_tags
        # No uncovered mode ever appears, and the item supports the mode.
        assert proposal.mode in chosen.modes
        assert snap.coverage.covers(proposal.mode, chosen.cook_ids)
        # Guest dietary needs are always satisfied.
        assert snap.guest_needs <= chosen.tag_ids
        # A same-window repeat appears only when nothing else is eligible, for any meal.
        if proposal.item_id in snap.recent_items:
            assert not fresh_exists
        # A top-up says what the item is usually for, first.
        assert proposal.for_meal or proposal.reasons[0].startswith("Usually for ")
        # Every proposal explains itself, and reasons never name a veto.
        assert proposal.reasons and all("veto" not in r.lower() for r in proposal.reasons)
    # Something eligible always yields at least one proposal.
    assert bool(ranked) == bool(eligible_any and snap.limit)
    # Deterministic: the same snapshot, with items in any order, ranks the same.
    reorder = random.Random(seed + 1)  # noqa: S311 - reproducible test data
    shuffled = replace(snap, items=tuple(reorder.sample(snap.items, len(snap.items))))
    assert rank(shuffled) == ranked == rank(snap)
    # Ordered by score, descending, within each part.
    for for_meal in (True, False):
        scores = [p.score for p in ranked if p.for_meal is for_meal]
        assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("seed", range(20))
def test_a_repeated_cuisine_never_outranks_an_equivalent_novel_one(seed: int) -> None:
    """ADR 0005's example property: two items alike except that one's cuisine was eaten
    recently; the recent one never ranks above the other."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    weights = Weights(*(rng.choice([0.0, 1.0, 2.0, 3.0]) for _ in range(6)))
    fresh = item(1, "Same", cuisine=THAI)
    repeat = item(2, "Same", cuisine=MEXICAN)
    snap = snapshot(fresh, repeat, weights=weights, recent_cuisines={MEXICAN: rng.randint(0, 6)}, limit=2)
    order = [r.item_id for r in rank(snap)]
    assert order.index(1) < order.index(2)
