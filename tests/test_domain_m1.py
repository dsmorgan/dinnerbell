# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure rules for catalog items, tag merges, preferences, meals, and the variety window."""

from __future__ import annotations

from datetime import date

import pytest

from dinnerbell.domain.catalog import (
    CatalogItem,
    CatalogRuleViolation,
    InvalidCatalogInput,
    ItemFilter,
    ItemInput,
    ItemKind,
    Mode,
    Status,
    Tag,
    TagKind,
    check_merge,
    check_modes,
    clean_item,
    normalize_notes,
    normalize_url,
)
from dinnerbell.domain.meals import (
    InvalidMeal,
    MealInput,
    MealLog,
    MemberRef,
    Slot,
    TagRef,
    by_week,
    can_change,
    check_meal,
    week_start,
)
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.domain.preferences import MINUS, NEUTRAL, InvalidPreference, Stance, merged_stance
from dinnerbell.domain.variety import summarize, variety_window_days, window_start

THAI = Tag(1, 1, TagKind.CUISINE, "Thai")
SPICY = Tag(2, 1, TagKind.OTHER, "spicy")
CHICKEN = Tag(3, 1, TagKind.PROTEIN, "chicken")
TAGS = [THAI, SPICY, CHICKEN]


def item_input(**overrides: object) -> ItemInput:
    values: dict[str, object] = {
        "kind": ItemKind.RESTAURANT,
        "name": "  Siam   Garden ",
        "modes": frozenset({Mode.PICKUP}),
        "tag_ids": frozenset({THAI.id}),
    }
    values.update(overrides)
    return ItemInput(**values)  # type: ignore[arg-type]


# Items ------------------------------------------------------------------------------------------


def test_clean_item_normalizes() -> None:
    clean = clean_item(item_input(url=" https://example.com ", notes="  hi \r\n there "), TAGS, [10])
    assert clean.name == "Siam Garden"
    assert clean.url == "https://example.com"
    assert clean.notes == "hi \n there"
    assert clean.tags_to_create == ()


def test_clean_item_requires_a_cuisine() -> None:
    with pytest.raises(InvalidCatalogInput, match="cuisine"):
        clean_item(item_input(tag_ids=frozenset({SPICY.id})), TAGS, [])


def test_new_cuisine_counts_and_matches_existing_ignoring_case() -> None:
    clean = clean_item(item_input(tag_ids=frozenset(), new_cuisines=("thai", "Lao", "LAO", " ")), TAGS, [])
    assert clean.tag_ids == frozenset({THAI.id})
    assert clean.tags_to_create == ((TagKind.CUISINE, "Lao"),)


def test_new_tag_with_wrong_kind_is_refused() -> None:
    with pytest.raises(InvalidCatalogInput, match="already a protein tag"):
        clean_item(item_input(new_cuisines=("Chicken",)), TAGS, [])
    with pytest.raises(InvalidCatalogInput, match="both"):
        clean_item(item_input(new_cuisines=("Fusion",), new_tags=("fusion",)), TAGS, [])


def test_new_other_tags_take_the_chosen_kind() -> None:
    clean = clean_item(item_input(new_tags=("tofu",), new_tags_kind=TagKind.PROTEIN), TAGS, [])
    assert clean.tags_to_create == ((TagKind.PROTEIN, "tofu"),)
    with pytest.raises(InvalidCatalogInput, match="already an other tag"):
        clean_item(item_input(new_tags=("Spicy",), new_tags_kind=TagKind.DIETARY), TAGS, [])


def test_unknown_tag_or_cook_ids_are_refused() -> None:
    with pytest.raises(InvalidCatalogInput, match="no longer exists"):
        clean_item(item_input(tag_ids=frozenset({99})), TAGS, [])
    with pytest.raises(InvalidCatalogInput, match="isn't a member"):
        clean_item(item_input(kind=ItemKind.HOME, cook_ids=frozenset({99})), TAGS, [10])


def test_home_meals_are_cooked_and_drop_restaurant_fields() -> None:
    clean = clean_item(
        item_input(
            kind=ItemKind.HOME,
            modes=frozenset({Mode.DELIVERY}),
            order_url="https://order.example.com",
            cook_ids=frozenset({10}),
            default_servings=4,
        ),
        TAGS,
        [10],
    )
    assert clean.modes == frozenset({Mode.COOK})
    assert clean.order_url is None
    assert (clean.cook_ids, clean.default_servings) == (frozenset({10}), 4)


def test_restaurants_drop_home_fields() -> None:
    clean = clean_item(item_input(cook_ids=frozenset({10}), default_servings=4), TAGS, [10])
    assert (clean.cook_ids, clean.default_servings) == (frozenset(), None)


@pytest.mark.parametrize("servings", [0, 100])
def test_servings_range(servings: int) -> None:
    with pytest.raises(InvalidCatalogInput, match="Servings"):
        clean_item(item_input(kind=ItemKind.HOME, default_servings=servings), TAGS, [])


def test_modes_rules() -> None:
    with pytest.raises(InvalidCatalogInput, match="at least one"):
        check_modes(ItemKind.RESTAURANT, [])
    with pytest.raises(InvalidCatalogInput, match="supports only: pickup, delivery"):
        check_modes(ItemKind.RESTAURANT, [Mode.COOK])


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "ftp://x.com", "https://", "http://a b", "x" * 2001, "http://[::1"]
)
def test_bad_urls(url: str) -> None:
    with pytest.raises(InvalidCatalogInput):
        normalize_url(url)


def test_notes_rules() -> None:
    assert normalize_notes("   ") is None
    assert normalize_notes(None) is None
    with pytest.raises(InvalidCatalogInput, match="control"):
        normalize_notes("bell\x07")
    with pytest.raises(InvalidCatalogInput, match="500"):
        normalize_notes("x" * 501)


def test_names_refuse_control_characters() -> None:
    with pytest.raises(InvalidCatalogInput, match="control"):
        clean_item(item_input(name="Tab​name\x00"), TAGS, [])


def test_check_merge() -> None:
    lao = Tag(4, 1, TagKind.CUISINE, "Lao")
    check_merge(lao, THAI)
    check_merge(SPICY, THAI)  # an other tag can become a cuisine
    with pytest.raises(CatalogRuleViolation, match="different tag"):
        check_merge(THAI, THAI)
    with pytest.raises(CatalogRuleViolation, match="cuisine only into another cuisine"):
        check_merge(THAI, SPICY)


def test_item_filter() -> None:
    siam = CatalogItem(
        id=1,
        household_id=1,
        kind=ItemKind.RESTAURANT,
        name="Siam Garden",
        modes=frozenset({Mode.PICKUP}),
        tag_ids=frozenset({THAI.id, SPICY.id}),
        cook_ids=frozenset(),
        notes="Ask for medium",
    )
    names = {t.id: t.name for t in TAGS}
    assert ItemFilter().matches(siam, names)
    assert ItemFilter(query="  SIAM ").matches(siam, names)
    assert ItemFilter(query="medium").matches(siam, names)
    assert ItemFilter(query="spicy").matches(siam, names)
    assert not ItemFilter(query="pizza").matches(siam, names)
    assert not ItemFilter(kind=ItemKind.HOME).matches(siam, names)
    assert not ItemFilter(mode=Mode.DELIVERY).matches(siam, names)
    assert ItemFilter(tag_id=SPICY.id).matches(siam, names)
    assert not ItemFilter(tag_id=CHICKEN.id).matches(siam, names)
    assert not ItemFilter(status=Status.INACTIVE).matches(siam, names)
    assert ItemFilter(status=Status.ALL).matches(siam, names)
    retired = CatalogItem(**{**{s: getattr(siam, s) for s in siam.__slots__}, "active": False})  # type: ignore[attr-defined]
    assert not ItemFilter().matches(retired, names)
    assert ItemFilter(status=Status.INACTIVE).matches(retired, names)
    assert ItemFilter().is_default and not ItemFilter(query="x").is_default


# Preferences ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "stance"),
    [
        ("veto", Stance(veto=True)),
        ("-2", Stance(score=-2)),
        (f"{MINUS}1", Stance(score=-1)),
        ("+2", Stance(score=2)),
        ("0", NEUTRAL),
    ],
)
def test_stance_parse(text: str, stance: Stance) -> None:
    assert Stance.parse(text) == stance
    assert Stance.parse(stance.level) == stance


@pytest.mark.parametrize("text", ["3", "-3", "", "maybe", "1.5"])
def test_stance_parse_rejects(text: str) -> None:
    with pytest.raises(InvalidPreference):
        Stance.parse(text)


def test_stance_invariants() -> None:
    with pytest.raises(InvalidPreference):
        Stance(score=5)
    with pytest.raises(InvalidPreference):
        Stance(veto=True, score=1)
    assert NEUTRAL.is_neutral and not Stance(veto=True).is_neutral


def test_merged_stance_veto_wins_then_lower_score() -> None:
    assert merged_stance(Stance(score=2), Stance(veto=True)) == Stance(veto=True)
    assert merged_stance(Stance(veto=True), Stance(score=2)) == Stance(veto=True)
    assert merged_stance(Stance(score=2), Stance(score=-1)) == Stance(score=-1)
    assert merged_stance(Stance(score=-2), Stance(score=1)) == Stance(score=-2)


# Meals ------------------------------------------------------------------------------------------


def person(member_id: int, *, admin: bool = False, active: bool = True) -> Member:
    return Member(
        id=member_id,
        household_id=1,
        display_name=f"M{member_id}",
        email=f"m{member_id}@example.com",
        role=Role.ADMIN if admin else Role.MEMBER,
        capabilities=Capabilities(),
        active=active,
    )


def meal(
    meal_id: int, day: date, slot: Slot = Slot.DINNER, *, logged_by: int = 1, cuisines: tuple[TagRef, ...] = ()
) -> MealLog:
    return MealLog(
        id=meal_id,
        household_id=1,
        day=day,
        slot=slot,
        item_id=meal_id * 10,
        item_name=f"Item {meal_id}",
        cuisines=cuisines,
        participants=(MemberRef(1, "M1"),),
        logged_by=logged_by,
    )


TODAY = date(2026, 10, 22)  # a Thursday


def test_check_meal() -> None:
    good = MealInput(TODAY, Slot.DINNER, 1, frozenset({1}))
    check_meal(good, today=TODAY, active_member_ids=[1], item_exists=True)
    with pytest.raises(InvalidMeal, match="future"):
        check_meal(
            MealInput(date(2026, 10, 23), Slot.DINNER, 1, frozenset({1})),
            today=TODAY,
            active_member_ids=[1],
            item_exists=True,
        )
    with pytest.raises(InvalidMeal, match="catalog"):
        check_meal(good, today=TODAY, active_member_ids=[1], item_exists=False)
    with pytest.raises(InvalidMeal, match="at least one"):
        check_meal(MealInput(TODAY, Slot.DINNER, 1, frozenset()), today=TODAY, active_member_ids=[1], item_exists=True)
    with pytest.raises(InvalidMeal, match="active member"):
        check_meal(
            MealInput(TODAY, Slot.DINNER, 1, frozenset({2})), today=TODAY, active_member_ids=[1], item_exists=True
        )


def test_can_change() -> None:
    mine = meal(1, TODAY, logged_by=1)
    assert can_change(person(1), mine)
    assert not can_change(person(2), mine)
    assert can_change(person(2, admin=True), mine)
    assert not can_change(person(1, active=False), mine)


def test_by_week_groups_newest_first() -> None:
    meals = [
        meal(1, date(2026, 10, 12)),  # Monday of the week before last
        meal(2, date(2026, 10, 20), Slot.LUNCH),
        meal(3, date(2026, 10, 20), Slot.DINNER),
        meal(4, date(2026, 10, 19)),
        meal(5, date(2026, 10, 18)),  # Sunday, the previous week
    ]
    weeks = by_week(meals)
    assert [w for w, _ in weeks] == [date(2026, 10, 19), date(2026, 10, 12)]
    assert [m.id for m in weeks[0][1]] == [3, 2, 4]
    assert [m.id for m in weeks[1][1]] == [5, 1]
    assert week_start(date(2026, 10, 25)) == date(2026, 10, 19)


# Variety ----------------------------------------------------------------------------------------


def test_window_days_from_settings() -> None:
    assert variety_window_days({}) == 7
    assert variety_window_days({"variety_window_days": 10}) == 10
    for bad in (0, 61, True, "10", None, 3.5):
        assert variety_window_days({"variety_window_days": bad}) == 7


def test_window_start() -> None:
    assert window_start(TODAY, 7) == date(2026, 10, 16)
    assert window_start(TODAY, 1) == TODAY


def test_summarize_window() -> None:
    thai, lao = TagRef(1, "Thai (old name)"), TagRef(2, "Lao")
    meals = [
        meal(1, TODAY, cuisines=(thai,)),
        meal(2, date(2026, 10, 19), cuisines=(thai, lao)),
        meal(3, date(2026, 10, 16), cuisines=(lao,)),  # 6 days ago: inside a 7-day window
        meal(4, date(2026, 10, 15), cuisines=(TagRef(3, "Greek"),)),  # 7 days ago: outside
        meal(5, date(2026, 10, 23), cuisines=(TagRef(4, "Future"),)),  # never counts
    ]
    window = summarize(meals, today=TODAY, days=7, tag_names={1: "Thai"})
    assert [(c.name, c.days_since, c.times) for c in window.cuisines] == [("Thai", 0, 2), ("Lao", 3, 2)]
    assert window.cuisine_ids() == {1, 2}
    assert window.item_ids() == {10, 20, 30}
    assert window.items[0].days_since == 0
    assert (window.start, window.today, window.days) == (date(2026, 10, 16), TODAY, 7)


def test_summarize_counts_repeat_items() -> None:
    meals = [
        meal(1, TODAY),
        MealLog(**{**{s: getattr(meal(1, TODAY), s) for s in MealLog.__slots__}, "id": 9, "day": date(2026, 10, 20)}),
    ]  # type: ignore[attr-defined]
    window = summarize(meals, today=TODAY, days=7)
    [item] = window.items
    assert (item.times, item.last_eaten) == (2, TODAY)
