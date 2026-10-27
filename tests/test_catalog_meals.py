# SPDX-License-Identifier: AGPL-3.0-or-later
"""The meals each catalog item is for (#72): the domain rules, the catalog file, the Catalog page,
and the item lists on Add a proposal and Make an offer, on the web and in Slack. Ranking's meal
filter and top-up are in test_ranking.py, and the migration in test_migrations.py."""

from __future__ import annotations

import re
from dataclasses import replace
from itertools import combinations

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import preview as samples
from dinnerbell.channels.slack.views import offer_modal, proposal_modal
from dinnerbell.config import Settings
from dinnerbell.db.catalog import meals_from_bits, meals_to_bits
from dinnerbell.db.models import CatalogItemRow, ProposalRow
from dinnerbell.domain.catalog import (
    DEFAULT_MEALS,
    MEAL_ORDER,
    CatalogItem,
    InvalidCatalogInput,
    ItemFilter,
    ItemInput,
    ItemKind,
    Meal,
    Mode,
    Tag,
    TagKind,
    clean_item,
    for_meal_first,
    meal_hint,
    meal_words,
)
from dinnerbell.domain.catalog_export import export_catalog
from dinnerbell.domain.catalog_file import parse_catalog
from dinnerbell.domain.catalog_import import CatalogSnapshot, FieldChange, plan_import
from dinnerbell.domain.meals import Slot
from tests.conftest import page, sign_in
from tests.support.blockkit import check_view
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, submit
from tests.test_auth_flow import rows
from tests.test_events_web import start, to_voting

THAI = Tag(1, 1, TagKind.CUISINE, "Thai")
BREAKFAST, LUNCH, DINNER = (frozenset({m}) for m in MEAL_ORDER)


def catalog_item(item_id: int, name: str, meals: frozenset[Meal] = DEFAULT_MEALS) -> CatalogItem:
    return CatalogItem(
        item_id, 1, ItemKind.RESTAURANT, name, frozenset({Mode.PICKUP}), frozenset({THAI.id}), frozenset(), meals=meals
    )


# Domain -------------------------------------------------------------------------------------------


def test_every_slot_is_a_meal_and_breakfast_is_ready_for_58() -> None:
    assert {slot.meal for slot in Slot} == {Meal.LUNCH, Meal.DINNER}
    assert all(slot.meal.value == slot.value for slot in Slot)
    assert MEAL_ORDER == (Meal.BREAKFAST, Meal.LUNCH, Meal.DINNER)
    assert {Meal.LUNCH, Meal.DINNER} == DEFAULT_MEALS
    assert catalog_item(1, "Siam").meals == DEFAULT_MEALS  # a new item's default


def test_an_item_needs_at_least_one_meal() -> None:
    data = ItemInput(kind=ItemKind.RESTAURANT, name="Bagels", modes=frozenset({Mode.PICKUP}), tag_ids={THAI.id})
    assert clean_item(data, [THAI], []).meals == DEFAULT_MEALS
    assert clean_item(replace(data, meals=BREAKFAST), [THAI], []).meals == BREAKFAST
    home = replace(data, kind=ItemKind.HOME, meals=frozenset(Meal))
    assert clean_item(home, [THAI], []).meals == frozenset(Meal)
    with pytest.raises(InvalidCatalogInput, match="Pick at least one meal"):
        clean_item(replace(data, meals=frozenset()), [THAI], [])


@pytest.mark.parametrize("size", [1, 2, 3])
def test_meals_are_stored_as_bits_that_round_trip(size: int) -> None:
    for meals in map(frozenset, combinations(MEAL_ORDER, size)):
        bits = meals_to_bits(meals)
        assert 1 <= bits <= 7 and meals_from_bits(bits) == meals
    assert meals_to_bits(DEFAULT_MEALS) == 6  # the migration's default


def test_hints_and_order_for_a_meal() -> None:
    assert meal_words(DINNER) == "dinner"
    assert meal_words({Meal.DINNER, Meal.BREAKFAST}) == "breakfast and dinner"
    assert meal_words(frozenset(Meal)) == "breakfast, lunch, and dinner"
    bagels, siam, steak = (
        catalog_item(1, "Bagels", BREAKFAST),
        catalog_item(2, "Siam"),
        catalog_item(3, "Steak", DINNER),
    )
    assert meal_hint(siam, Meal.LUNCH) is None
    assert meal_hint(steak, Meal.LUNCH) == "usually dinner"
    assert meal_hint(bagels, Meal.DINNER) == "usually breakfast"
    assert [i.name for i in for_meal_first([bagels, siam, steak], Meal.LUNCH)] == ["Siam", "Bagels", "Steak"]
    assert [i.name for i in for_meal_first([bagels, siam, steak], Meal.BREAKFAST)] == ["Bagels", "Siam", "Steak"]


def test_the_catalog_filter_matches_a_meal() -> None:
    bagels, siam = catalog_item(1, "Bagels", BREAKFAST), catalog_item(2, "Siam")
    assert [i.name for i in (bagels, siam) if ItemFilter(meal=Meal.BREAKFAST).matches(i, {})] == ["Bagels"]
    assert [i.name for i in (bagels, siam) if ItemFilter(meal=Meal.DINNER).matches(i, {})] == ["Siam"]
    assert not ItemFilter(meal=Meal.DINNER).is_default


# The catalog file -----------------------------------------------------------------------------------

FILE = """\
version: 1
restaurants:
  - name: Bagel Barn
    cuisine: [American]
    meals: [breakfast]
  - name: Siam Garden
    cuisine: [Thai]
home_meals:
  - name: Pancakes
    cuisine: [American]
    meals: [dinner, breakfast, breakfast]
"""


def _created(source: str, snapshot: CatalogSnapshot | None = None) -> dict[str, frozenset[Meal]]:
    plan = plan_import(parse_catalog(source), snapshot or CatalogSnapshot.empty())
    assert plan.ok, plan.errors
    return {c.state.name: c.state.meals for c in plan.items_to_create}


def test_a_file_says_which_meals_and_leaving_it_out_means_lunch_and_dinner() -> None:
    assert _created(FILE) == {
        "Bagel Barn": BREAKFAST,
        "Siam Garden": DEFAULT_MEALS,  # an older file, without meals, imports as lunch and dinner
        "Pancakes": BREAKFAST | DINNER,
    }


@pytest.mark.parametrize(
    ("meals", "message"),
    [
        ("[]", "restaurants[0].meals: needs at least one value"),
        ("null", "restaurants[0].meals: must be a list"),
        ("[brunch]", 'restaurants[0].meals[0]: "brunch" is not one of breakfast, lunch, dinner'),
        ("dinner", "restaurants[0].meals: must be a list"),
    ],
)
def test_a_file_with_bad_meals_is_refused(meals: str, message: str) -> None:
    source = f"version: 1\nrestaurants:\n  - name: X\n    cuisine: [Thai]\n    meals: {meals}\n"
    assert message in [str(e) for e in plan_import(parse_catalog(source), CatalogSnapshot.empty()).errors]


def test_an_update_replaces_meals_only_when_the_file_has_them() -> None:
    steak = CatalogItem(
        7, 1, ItemKind.RESTAURANT, "Steak", frozenset({Mode.PICKUP}), frozenset({THAI.id}), frozenset(), meals=DINNER
    )
    snapshot = CatalogSnapshot(tags=(THAI,), items=(steak,), members=())
    without = plan_import(parse_catalog("version: 1\nrestaurants:\n  - name: Steak\n    cuisine: [Thai]\n"), snapshot)
    assert [u.location for u in without.unchanged] == ["restaurants[0]"]  # stays dinner only
    source = "version: 1\nrestaurants:\n  - name: Steak\n    cuisine: [Thai]\n    meals: [lunch, dinner]\n"
    (update,) = plan_import(parse_catalog(source), snapshot).items_to_update
    assert update.state.meals == DEFAULT_MEALS
    assert update.changes == (FieldChange("meals", "dinner", "lunch and dinner"),)


def test_an_export_writes_meals_and_imports_back_the_same() -> None:
    american = Tag(2, 1, TagKind.CUISINE, "American")
    items = [catalog_item(1, "Bagel Barn", BREAKFAST), catalog_item(2, "Siam Garden")]
    items[0] = replace(items[0], tag_ids=frozenset({american.id}))
    exported = export_catalog([THAI, american], items).text
    assert "    meals: [breakfast]\n" in exported and "    meals: [lunch, dinner]\n" in exported
    assert _created(exported) == {"Bagel Barn": BREAKFAST, "Siam Garden": DEFAULT_MEALS}
    same = CatalogSnapshot(tags=(THAI, american), items=tuple(items), members=())
    plan = plan_import(parse_catalog(exported), same)
    assert plan.ok and not plan.has_changes


# The Catalog page -----------------------------------------------------------------------------------


def checked_meals(html: str) -> set[str]:
    return set(re.findall(r'name="meals" value="(\w+)" checked', html))


def test_the_item_form_starts_on_lunch_and_dinner_and_needs_a_meal(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    form = page(client.get("/catalog/new?kind=restaurant"))
    assert "Meals it's for" in form and checked_meals(form) == {"lunch", "dinner"}
    assert 'name="meals" value="breakfast">' in form

    refused = add_item(client, "Bagel Barn", meals=[])
    assert refused.status_code == 400 and "Pick at least one meal it" in page(refused)
    assert checked_meals(page(refused)) == set()  # the form keeps what was posted
    assert add_item(client, "Bagel Barn", meals=["brunch"]).status_code == 400
    bogus = add_item(client, "Bagel Barn", meals=["lunch", "bogus"])  # refused, not saved as lunch
    assert bogus.status_code == 400 and "Pick breakfast, lunch, dinner, or a mix." in page(bogus)
    assert not [i for i in rows(migrated, CatalogItemRow) if i.name == "Bagel Barn"]

    assert add_item(client, "Bagel Barn", meals=["breakfast"]).status_code == 303
    assert (
        add_item(client, "Pancakes", kind="home", cuisines="American", meals=["breakfast", "lunch"]).status_code == 303
    )
    assert add_item(client, "Siam Garden").status_code == 303
    by_name = {i.name: i for i in rows(migrated, CatalogItemRow)}
    assert {n: meals_from_bits(i.meals) for n, i in by_name.items()} == {
        "Bagel Barn": BREAKFAST,
        "Pancakes": BREAKFAST | LUNCH,
        "Siam Garden": DEFAULT_MEALS,
    }

    bagels = by_name["Bagel Barn"].id
    assert checked_meals(page(client.get(f"/catalog/items/{bagels}"))) == {"breakfast"}
    edited = submit(
        client,
        f"/catalog/items/{bagels}",
        {"name": "Bagel Barn", "new_cuisines": "Thai", "modes": ["pickup"], "meals": ["breakfast", "lunch"]},
    )
    assert edited.status_code == 303, page(edited)
    assert meals_from_bits(next(i for i in rows(migrated, CatalogItemRow) if i.id == bagels).meals) == BREAKFAST | LUNCH
    empty = submit(
        client, f"/catalog/items/{bagels}", {"name": "Bagel Barn", "new_cuisines": "Thai", "modes": ["pickup"]}
    )
    assert empty.status_code == 400 and "Pick at least one meal it" in page(empty)


def test_the_catalog_shows_each_items_meals_and_filters_by_meal(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    add_item(client, "Bagel Barn", meals=["breakfast"])
    add_item(client, "Siam Garden")
    listing = page(client.get("/catalog"))
    assert '<a class="chip meal" href="/catalog?meal=breakfast">Breakfast</a>' in listing
    assert listing.count('class="chip meal"') == 3  # Breakfast; Lunch and Dinner
    assert '<option value="breakfast">Breakfast</option>' in listing
    breakfast = page(client.get("/catalog?meal=breakfast"))
    assert "Bagel Barn" in breakfast and "Siam Garden" not in breakfast and "1 match" in breakfast
    assert '<option value="breakfast" selected>Breakfast</option>' in breakfast
    assert "Siam Garden" in page(client.get("/catalog?meal=dinner")) and "Bagel" not in page(
        client.get("/catalog?meal=dinner")
    )
    assert "Bagel Barn" in page(client.get("/catalog?meal=brunch"))  # an unknown meal filters nothing


# Add a proposal and Make an offer -----------------------------------------------------------------


def select_options(html: str, name: str) -> list[str]:
    select = re.search(rf'<select name="{name}">(.*?)</select>', html, re.S)
    assert select is not None
    return [re.sub(r"\s+", " ", o).strip() for o in re.findall(r"<option[^>]*>(.*?)</option>", select.group(1))]


def test_add_a_proposal_lists_the_calls_meal_first_and_marks_the_rest(
    client: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """A dinner call: two dinner places fit, so a breakfast or lunch place tops up the vote and
    says so. Add a proposal lists dinner places first, and still offers the rest, marked."""
    sign_in(client, provider)
    add_item(client, "Bagel Barn", meals=["breakfast"])
    add_item(client, "Deli", meals=["lunch"])
    add_item(client, "Siam Garden")
    add_item(client, "Taqueria", cuisines="Mexican", meals=["dinner"])
    event_id = start(client)
    to_voting(client, clock, event_id)
    by_id = {i.id: i.name for i in rows(migrated, CatalogItemRow)}
    ranked = sorted(rows(migrated, ProposalRow), key=lambda p: p.rank)
    names = [by_id[p.item_id] for p in ranked]
    assert sorted(names[:2]) == ["Siam Garden", "Taqueria"]  # in a seeded random order (#78)
    usually = {"Bagel Barn": "breakfast", "Deli": "lunch"}
    topped = names[2]
    assert ranked[2].reasons[0] == f"Usually for {usually[topped]}."

    html = page(client.get(f"/events/{event_id}"))
    assert f"Usually for {usually[topped]}." in html
    marked = {name: f"{name} (usually {meal})" for name, meal in usually.items()}
    assert select_options(html, "item_id") == [
        "Something new instead",
        "Siam Garden (already a pick)",
        "Taqueria (already a pick)",
        *(f"{marked[name]} (already a pick)" if name == topped else marked[name] for name in usually),
    ]
    other = next(i for i, name in by_id.items() if name in usually and name != topped)
    added = submit(client, f"/events/{event_id}/proposals", {"mode": "pickup", "item_id": str(other)})
    assert added.status_code == 303, page(added)  # people know better than the catalog
    assert other in {p.item_id for p in rows(migrated, ProposalRow)}


def test_the_offer_form_lists_its_meal_first_and_marks_the_rest(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)  # 13:00 in Chicago, past lunch's meal time, so the form starts on dinner
    add_item(client, "Bagel Barn", meals=["breakfast"])
    add_item(client, "Deli", meals=["lunch"])
    add_item(client, "Siam Garden")
    html = page(client.get("/"))
    assert '<option value="dinner" selected>Dinner</option>' in html
    assert select_options(html, "item_id") == [
        "Something new instead",
        "Siam Garden",
        "Bagel Barn (usually breakfast)",
        "Deli (usually lunch)",
    ]


def _labels(view: dict[str, object]) -> list[str]:
    (block,) = [b for b in view["blocks"] if b.get("block_id") == "item"]  # type: ignore[attr-defined]
    return [o["text"]["text"] for g in block["element"]["option_groups"] for o in g["options"]]


def test_slack_lists_mark_items_for_other_meals() -> None:
    """The modals keep the order the service gives (the meal's items first) and mark the rest,
    alongside "already a pick", clipping the name so the label still fits."""
    from dinnerbell.channels.slack import ids

    long_name = "L" * 75
    siam, bagels, deli = (
        catalog_item(1, "Siam Garden"),
        catalog_item(12, "Bagel Barn", BREAKFAST),
        catalog_item(3, long_name, LUNCH),
    )
    items = (siam, bagels, deli)
    modal = proposal_modal(replace(samples.VOTING_VIEW, items=items), samples.ORIGIN)  # a dinner call; 12 is a pick
    check_view(modal)
    labels = _labels(modal)
    assert labels[:2] == ["Siam Garden", "Bagel Barn (usually breakfast) (already a pick)"]
    assert labels[2].endswith("\N{HORIZONTAL ELLIPSIS} (usually lunch)") and len(labels[2]) == 75

    offer = offer_modal(replace(samples.OFFER_FORM, items=items), ids.FormOrigin("C0DINNER"))  # starts on lunch
    check_view(offer)
    assert _labels(offer) == ["Siam Garden", "Bagel Barn (usually breakfast)", long_name]
