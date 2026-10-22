# SPDX-License-Identifier: AGPL-3.0-or-later
"""The import planner: a pure function from a parsed file and a snapshot to a plan."""

from __future__ import annotations

from pathlib import Path

from dinnerbell.domain.catalog import CatalogItem, ItemKind, Mode, Tag, TagKind
from dinnerbell.domain.catalog_file import parse_catalog
from dinnerbell.domain.catalog_import import CatalogSnapshot, FieldChange, ImportPlan, plan_import
from dinnerbell.domain.members import Capabilities, Member, Role

EXAMPLE = Path(__file__).resolve().parents[1] / "seed" / "example.yaml"
HOUSEHOLD = 1


def tag(tag_id: int, kind: TagKind, name: str) -> Tag:
    return Tag(id=tag_id, household_id=HOUSEHOLD, kind=kind, name=name)


def person(member_id: int, email: str, name: str) -> Member:
    return Member(
        id=member_id,
        household_id=HOUSEHOLD,
        display_name=name,
        email=email,
        role=Role.MEMBER,
        capabilities=Capabilities(),
        active=True,
    )


THAI = tag(1, TagKind.CUISINE, "Thai")
MEXICAN = tag(2, TagKind.CUISINE, "Mexican")
SPICY = tag(3, TagKind.OTHER, "spicy")
VEGETARIAN = tag(4, TagKind.PROTEIN, "vegetarian")
CHICKEN = tag(5, TagKind.PROTEIN, "chicken")
DAVID = person(10, "david@example.com", "David")
SAM = person(11, "sam@example.com", "Sam")

SIAM = CatalogItem(
    id=100,
    household_id=HOUSEHOLD,
    kind=ItemKind.RESTAURANT,
    name="Siam Garden",
    modes=frozenset({Mode.PICKUP}),
    tag_ids=frozenset({THAI.id, SPICY.id}),
    cook_ids=frozenset(),
    url="https://example.com/menu",
    notes="Medium spice.",
)
TACOS = CatalogItem(
    id=101,
    household_id=HOUSEHOLD,
    kind=ItemKind.HOME,
    name="Chicken tacos",
    modes=frozenset({Mode.COOK}),
    tag_ids=frozenset({MEXICAN.id, CHICKEN.id}),
    cook_ids=frozenset({DAVID.id}),
    default_servings=6,
)
SNAPSHOT = CatalogSnapshot(tags=(THAI, MEXICAN, SPICY, VEGETARIAN, CHICKEN), items=(SIAM, TACOS), members=(DAVID, SAM))


def plan(source: str, snapshot: CatalogSnapshot = SNAPSHOT) -> ImportPlan:
    return plan_import(parse_catalog(source), snapshot)


def error_strings(result: ImportPlan) -> list[str]:
    return [str(error) for error in result.errors]


def created_tags(result: ImportPlan) -> list[tuple[str, str]]:
    return [(t.kind.value, t.name) for t in result.tags_to_create]


def change(result: ImportPlan, field: str) -> FieldChange:
    [update] = result.items_to_update
    return next(c for c in update.changes if c.field == field)


# The example file ----------------------------------------------------------------------------


def test_example_plans_cleanly_into_an_empty_catalog() -> None:
    result = plan_import(parse_catalog(EXAMPLE.read_bytes()), CatalogSnapshot.empty())
    assert result.ok and result.errors == ()
    assert len(result.items_to_create) == 8
    assert ("cuisine", "Middle Eastern") in created_tags(result)
    assert all(t.declared for t in result.tags_to_create)
    assert result.unknown_cooks == ()


def test_example_is_unchanged_after_its_own_import() -> None:
    """Re-planning a file against the state it produced changes nothing."""
    first = plan_import(parse_catalog(EXAMPLE.read_bytes()), CatalogSnapshot.empty())
    snapshot = simulate_apply(first)
    again = plan_import(parse_catalog(EXAMPLE.read_bytes()), snapshot)
    assert again.ok and not again.has_changes
    assert len(again.unchanged) == 8


def simulate_apply(result: ImportPlan) -> CatalogSnapshot:
    tags = [tag(i + 1, new.kind, new.name) for i, new in enumerate(result.tags_to_create)]
    ids = {t.key: t.id for t in tags}
    items = [
        CatalogItem(
            id=1000 + i,
            household_id=HOUSEHOLD,
            kind=c.state.kind,
            name=c.state.name,
            modes=c.state.modes,
            tag_ids=frozenset(ids[k] for k in c.state.tag_keys),
            cook_ids=c.state.cook_ids,
            url=c.state.url,
            order_url=c.state.order_url,
            notes=c.state.notes,
            default_servings=c.state.default_servings,
            active=c.state.active,
        )
        for i, c in enumerate(result.items_to_create)
    ]
    return CatalogSnapshot(tags=tuple(tags), items=tuple(items), members=())


# Creating ------------------------------------------------------------------------------------


def test_new_restaurant_gets_defaults() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Luigi's, cuisine: [Italian]}\n")
    [create] = result.items_to_create
    assert create.location == "restaurants[0]"
    state = create.state
    assert (state.kind, state.name, state.modes, state.active) == (
        ItemKind.RESTAURANT,
        "Luigi's",
        frozenset({Mode.PICKUP}),
        True,
    )
    assert state.tag_keys == frozenset({"italian"})
    assert created_tags(result) == [("cuisine", "Italian")]
    assert result.tags_to_create[0].declared is False


def test_new_home_meal_is_cooked_and_matches_cooks() -> None:
    result = plan(
        "version: 1\nhome_meals:\n  - name: Stir-fry\n    cuisine: [Chinese]\n    servings: 4\n"
        "    cooks: [DAVID@example.com, sam@example.com]\n"
    )
    [create] = result.items_to_create
    assert create.state.modes == frozenset({Mode.COOK})
    assert create.state.cook_ids == frozenset({DAVID.id, SAM.id})
    assert create.state.default_servings == 4


def test_unknown_cooks_are_reported_and_skipped() -> None:
    result = plan(
        "version: 1\nhome_meals:\n  - {name: Pie, cuisine: [American], cooks: [ghost@example.com, sam@example.com]}\n"
    )
    assert result.ok
    [unknown] = result.unknown_cooks
    assert (unknown.location, unknown.email) == ("home_meals[0].cooks[0]", "ghost@example.com")
    assert result.items_to_create[0].state.cook_ids == frozenset({SAM.id})


def test_new_item_can_start_inactive() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Closed Place, cuisine: [Thai], active: false}\n")
    assert result.items_to_create[0].state.active is False


# Tag matching ----------------------------------------------------------------------------------


def test_tags_match_existing_ignoring_case_and_keep_stored_spelling() -> None:
    result = plan(
        "version: 1\ntags:\n  cuisine: [THAI]\nrestaurants:\n  - {name: New Thai, cuisine: [thai], tags: [SPICY]}\n"
    )
    assert result.tags_to_create == ()
    assert result.items_to_create[0].state.tag_keys == frozenset({"thai", "spicy"})


def test_first_spelling_in_the_file_is_kept() -> None:
    result = plan(
        "version: 1\n"
        "restaurants:\n  - {name: A, cuisine: [Lao], tags: [crispy]}\n"
        "home_meals:\n  - {name: B, cuisine: [LAO], tags: [Crispy]}\n",
        CatalogSnapshot.empty(),
    )
    assert created_tags(result) == [("cuisine", "Lao"), ("other", "crispy")]


def test_declared_spelling_beats_later_uses() -> None:
    result = plan(
        "version: 1\ntags:\n  dietary: [Nut-free]\nrestaurants:\n  - {name: A, cuisine: [Thai], tags: [NUT-FREE]}\n"
    )
    assert created_tags(result) == [("dietary", "Nut-free")]


def test_undeclared_tags_in_cuisine_lists_are_cuisines_others_are_other() -> None:
    result = plan(
        "version: 1\nrestaurants:\n  - {name: A, cuisine: [Ethiopian], tags: [injera, late-night]}\n",
        CatalogSnapshot.empty(),
    )
    assert created_tags(result) == [("cuisine", "Ethiopian"), ("other", "injera"), ("other", "late-night")]


def test_undeclared_tag_used_as_cuisine_anywhere_becomes_cuisine() -> None:
    result = plan(
        "version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai], tags: [Fusion]}\n  - {name: B, cuisine: [fusion]}\n",
        CatalogSnapshot.empty(),
    )
    assert ("cuisine", "Fusion") in created_tags(result)
    assert error_strings(result) == ['restaurants[0].tags[0]: "Fusion" is a cuisine tag; list it under cuisine']


def test_tag_names_are_unique_across_kinds_within_the_file() -> None:
    result = plan("version: 1\ntags:\n  protein: [tofu]\n  other: [Tofu]\n", CatalogSnapshot.empty())
    assert error_strings(result) == [
        'tags.other[0]: "Tofu" is already a protein tag; tag names are unique across kinds'
    ]


def test_tag_names_are_unique_across_kinds_against_existing_tags() -> None:
    result = plan("version: 1\ntags:\n  dietary: [Vegetarian]\n")
    assert error_strings(result) == [
        'tags.dietary[0]: "Vegetarian" is already a protein tag; tag names are unique across kinds'
    ]


def test_cuisine_list_must_hold_cuisines() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai, vegetarian]}\n")
    assert error_strings(result) == ['restaurants[0].cuisine[1]: "vegetarian" is a protein tag, not a cuisine']


def test_tags_list_must_not_hold_cuisines() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai], tags: [mexican]}\n")
    assert error_strings(result) == ['restaurants[0].tags[0]: "mexican" is a cuisine tag; list it under cuisine']


def test_same_tag_declared_twice_in_one_kind_is_fine() -> None:
    result = plan("version: 1\ntags:\n  other: [late, LATE]\n", CatalogSnapshot.empty())
    assert result.ok and created_tags(result) == [("other", "late")]


def test_declared_tags_are_created_even_if_unused() -> None:
    result = plan("version: 1\ntags:\n  other: [healthy]\n")
    assert created_tags(result) == [("other", "healthy")]
    assert result.tags_to_create[0].declared


# Duplicate names -----------------------------------------------------------------------------------


def test_duplicate_names_in_one_kind_are_errors() -> None:
    result = plan(
        "version: 1\nrestaurants:\n  - {name: Luigi's, cuisine: [Italian]}\n  - {name: LUIGI'S, cuisine: [Italian]}\n"
    )
    assert error_strings(result) == ['restaurants[1].name: "LUIGI\'S" appears more than once among restaurants']


def test_same_name_in_different_kinds_is_fine() -> None:
    result = plan(
        "version: 1\nrestaurants:\n  - {name: Tacos, cuisine: [Mexican]}\n"
        "home_meals:\n  - {name: tacos, cuisine: [Mexican]}\n"
    )
    assert result.ok and len(result.items_to_create) == 2


# Updating -----------------------------------------------------------------------------------------


def test_matching_name_updates_only_fields_present() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: SIAM GARDEN, cuisine: [Thai], modes: [pickup, delivery]}\n")
    [update] = result.items_to_update
    assert update.item_id == SIAM.id
    assert update.location == "restaurants[0]"
    assert update.state.name == "Siam Garden"  # the stored spelling stays
    assert [c.field for c in update.changes] == ["modes"]
    assert change(result, "modes") == FieldChange("modes", "pickup", "pickup, delivery")
    # Omitted fields keep their stored values.
    assert update.state.url == SIAM.url and update.state.notes == SIAM.notes
    assert update.state.tag_keys == frozenset({"thai", "spicy"})


def test_identical_entry_is_unchanged() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai]}\n")
    assert result.items_to_update == () and result.items_to_create == ()
    assert [(u.kind, u.name) for u in result.unchanged] == [(ItemKind.RESTAURANT, "Siam Garden")]
    assert not result.has_changes


def test_cuisine_replaces_only_cuisine_tags() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai, Lao]}\n")
    assert created_tags(result) == [("cuisine", "Lao")]
    assert change(result, "cuisine") == FieldChange("cuisine", "Thai", "Lao, Thai")
    assert result.items_to_update[0].state.tag_keys == frozenset({"thai", "lao", "spicy"})


def test_tags_replace_other_tags_when_present() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai], tags: [vegetarian]}\n")
    assert change(result, "tags") == FieldChange("tags", "spicy", "vegetarian")
    assert result.items_to_update[0].state.tag_keys == frozenset({"thai", "vegetarian"})


def test_empty_tags_list_clears_other_tags() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai], tags: []}\n")
    assert change(result, "tags") == FieldChange("tags", "spicy", "none")


def test_null_clears_optional_fields() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai], url: null, notes: ''}\n")
    assert change(result, "url") == FieldChange("url", "https://example.com/menu", "none")
    assert change(result, "notes") == FieldChange("notes", "Medium spice.", "none")


def test_active_false_retires_an_item_and_omitting_active_keeps_it() -> None:
    retired = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai], active: false}\n")
    assert change(retired, "active") == FieldChange("active", "yes", "no")
    inactive = CatalogItem(**{**_fields(SIAM), "active": False})
    snapshot = CatalogSnapshot(tags=SNAPSHOT.tags, items=(inactive, TACOS), members=SNAPSHOT.members)
    stays = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai]}\n", snapshot)
    assert not stays.has_changes
    back = plan("version: 1\nrestaurants:\n  - {name: Siam Garden, cuisine: [Thai], active: true}\n", snapshot)
    assert back.items_to_update[0].state.active is True


def _fields(item: CatalogItem) -> dict[str, object]:
    return {name: getattr(item, name) for name in item.__slots__}  # type: ignore[attr-defined]


def test_home_meal_update_fields() -> None:
    result = plan(
        "version: 1\nhome_meals:\n  - name: chicken TACOS\n    cuisine: [Mexican]\n    servings: 8\n"
        "    cooks: [sam@example.com, nobody@example.com]\n"
    )
    [update] = result.items_to_update
    assert [c.field for c in update.changes] == ["servings", "cooks"]
    assert change(result, "servings") == FieldChange("servings", "6", "8")
    assert change(result, "cooks") == FieldChange("cooks", "David", "Sam")
    assert [u.email for u in result.unknown_cooks] == ["nobody@example.com"]
    assert update.state.tag_keys == frozenset({"mexican", "chicken"})  # tags omitted: kept


def test_omitting_cooks_keeps_them() -> None:
    result = plan("version: 1\nhome_meals:\n  - {name: Chicken tacos, cuisine: [Mexican]}\n")
    assert not result.has_changes


def test_restaurant_name_does_not_match_home_meal() -> None:
    result = plan("version: 1\nrestaurants:\n  - {name: Chicken tacos, cuisine: [Mexican]}\n")
    assert len(result.items_to_create) == 1 and result.items_to_update == ()


# Errors and the plan as a whole -----------------------------------------------------------------------


def test_parse_errors_and_plan_errors_are_reported_together() -> None:
    result = plan(
        "version: 1\nrestaurants:\n"
        "  - {name: A, cuisine: [Thai], modes: [boat]}\n"
        "  - {name: B, cuisine: [vegetarian]}\n"
    )
    assert error_strings(result) == [
        'restaurants[0].modes[0]: "boat" is not one of pickup, delivery',
        'restaurants[1].cuisine[0]: "vegetarian" is a protein tag, not a cuisine',
    ]
    assert not result.ok


def test_fingerprint_tracks_the_plan() -> None:
    source = "version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai]}\n"
    assert plan(source).fingerprint() == plan(source).fingerprint()
    assert plan(source).fingerprint() != plan(source.replace("[Thai]", "[Lao]")).fingerprint()
    # A snapshot change that alters the outcome changes the fingerprint.
    with_a = CatalogSnapshot(
        tags=SNAPSHOT.tags,
        items=(*SNAPSHOT.items, CatalogItem(**{**_fields(SIAM), "id": 200, "name": "A"})),
        members=SNAPSHOT.members,
    )
    assert plan(source).fingerprint() != plan(source, with_a).fingerprint()


def test_summary_counts() -> None:
    result = plan(
        "version: 1\ntags:\n  other: [healthy]\nrestaurants:\n"
        "  - {name: Siam Garden, cuisine: [Thai], modes: [delivery]}\n  - {name: New, cuisine: [Thai]}\n"
    )
    assert result.summary() == {
        "tags_created": 1,
        "items_created": 1,
        "items_updated": 1,
        "items_unchanged": 0,
        "unknown_cooks": 0,
        "errors": 0,
    }


def test_repeated_tags_in_an_entry_count_once() -> None:
    result = plan(
        "version: 1\nrestaurants:\n  - {name: A, cuisine: [Lao, LAO], tags: [x, X, Tofu]}\n", CatalogSnapshot.empty()
    )
    assert result.ok
    assert created_tags(result) == [("cuisine", "Lao"), ("other", "x"), ("other", "Tofu")]
    assert result.items_to_create[0].state.tag_keys == frozenset({"lao", "x", "tofu"})
