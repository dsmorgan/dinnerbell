# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Catalog page: browse, search, add, edit, and deactivate items (FR-23, FR-24)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, CatalogItemRow, TagRow
from dinnerbell.web.routes.catalog import last_eaten_label
from tests.conftest import invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.pages import inline_code
from tests.support.web import add_item, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows

LUNCH_AND_DINNER = ["lunch", "dinner"]
"""What the item form starts with ticked, so what a browser sends (#72)."""


def item_row(settings: Settings, name: str) -> CatalogItemRow:
    return next(i for i in rows(settings, CatalogItemRow) if i.name == name)


def tag_names(settings: Settings) -> dict[int, str]:
    return {t.id: t.name for t in rows(settings, TagRow)}


def item_tags(settings: Settings, name: str) -> set[str]:
    from dinnerbell.db.models import CatalogItemTagRow

    item = item_row(settings, name)
    names = tag_names(settings)
    return {names[link.tag_id] for link in rows(settings, CatalogItemTagRow) if link.item_id == item.id}


def actions(settings: Settings) -> list[str]:
    return [a.action for a in rows(settings, AuditRow)]


def test_signed_out_visitors_are_sent_to_sign_in(client: TestClient) -> None:
    for path in ("/catalog", "/catalog/new", "/catalog/tags", "/catalog/import", "/preferences", "/history"):
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/"


def test_navigation_links_every_page(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    header = client.get("/catalog").text.split("<main")[0]
    for path in ("/catalog", "/preferences", "/history", "/admin/members"):
        assert f'href="{path}"' in header
    assert 'href="/catalog" aria-current="page"' in header


def test_empty_catalog(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    text = page(client.get("/catalog"))
    assert "The catalog is empty" in text
    assert 'href="/catalog/import"' in text


def test_add_restaurant(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    assert "Add a restaurant" in page(client.get("/catalog/new?kind=restaurant"))
    response = add_item(
        client,
        " Siam   Garden ",
        cuisines="Thai, Lao",
        modes=["pickup", "delivery"],
        new_tags="spicy",
        url="https://example.com/menu",
        order_url="https://order.example.com/siam",
        notes="Medium spice.",
    )
    assert response.status_code == 303, page(response)
    siam = item_row(migrated, "Siam Garden")
    assert (siam.kind, sorted(siam.modes), siam.active) == ("restaurant", ["delivery", "pickup"], True)
    assert siam.created_by == member(migrated, "admin@example.com").id
    assert item_tags(migrated, "Siam Garden") == {"Thai", "Lao", "spicy"}
    kinds = {t.name: t.kind for t in rows(migrated, TagRow)}
    assert kinds == {"Thai": "cuisine", "Lao": "cuisine", "spicy": "other"}
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "catalog.item_created"]
    assert audit.target == f"item:{siam.id}"
    assert sorted(audit.payload["tags_created"]) == ["Lao", "Thai", "spicy"]

    listing = client.get(response.headers["location"])
    text = page(listing)
    assert "Saved to the catalog." in text
    assert "Siam Garden" in text and "Never eaten" in text
    assert 'href="https://example.com/menu" rel="noopener noreferrer external" target="_blank">Menu' in listing.text
    assert 'href="https://order.example.com/siam"' in listing.text and "Order online" in text


def test_add_home_meal_with_cooks(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    form = page(client.get("/catalog/new?kind=home"))
    assert "Who can cook it" in form and "Sam" in form and "Order online" not in form
    response = add_item(
        client,
        "Chicken tacos",
        kind="home",
        cuisines="Mexican",
        servings="6",
        cook_ids=[str(sam.id)],
        order_url="https://ignored.example.com",
        modes=["delivery"],
    )
    assert response.status_code == 303, page(response)
    tacos = item_row(migrated, "Chicken tacos")
    assert (tacos.kind, tacos.modes, tacos.default_servings, tacos.order_url) == ("home", ["cook"], 6, None)
    text = page(client.get("/catalog"))
    assert "Serves 6" in text and "Cooks: Sam" in text and "Recipe" not in text


def test_existing_tags_are_reused_ignoring_case(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "One", cuisines="Thai")
    thai = next(t for t in rows(migrated, TagRow) if t.name == "Thai")
    response = add_item(client, "Two", cuisines="THAI", tag_ids=[str(thai.id)])
    assert response.status_code == 303
    assert len(rows(migrated, TagRow)) == 1
    assert item_tags(migrated, "Two") == {"Thai"}


def test_add_item_validation_keeps_the_form(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    cases = [
        ({"cuisines": ""}, "Pick at least one cuisine."),
        ({"url": "javascript:alert(1)"}, "A link must start with http:// or https://."),
        ({"modes": []}, "Pick at least one way to get it"),
        ({"modes": ["teleport"]}, "Pick pickup, delivery, or both."),
        ({"notes": "x" * 501}, "Notes are at most 500 characters."),
    ]
    for fields, message in cases:
        response = add_item(client, "Draft Place", **fields)
        assert response.status_code == 400, fields
        text = page(response)
        assert message in text, fields
        assert 'value="Draft Place"' in text
    home = add_item(client, "Pie", kind="home", servings="lots")
    assert home.status_code == 400 and "Servings must be a whole number." in page(home)
    assert rows(migrated, CatalogItemRow) == []


def test_duplicate_names_are_refused_per_kind(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Tacos")
    response = add_item(client, "TACOS")
    assert response.status_code == 400
    assert "A restaurant named TACOS already exists." in page(response)
    assert add_item(client, "tacos", kind="home").status_code == 303  # a home meal may share the name
    assert len(rows(migrated, CatalogItemRow)) == 2


def test_tag_kind_conflict_in_form(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    add_item(client, "One", new_tags="vegetarian", new_tags_kind="protein")
    response = add_item(client, "Two", cuisines="Vegetarian")
    assert response.status_code == 400
    assert '"vegetarian" is already a protein tag' in page(response)


def test_edit_item(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden", new_tags="spicy", notes="Old note")
    siam = item_row(migrated, "Siam Garden")
    form = page(client.get(f"/catalog/items/{siam.id}"))
    assert 'value="Siam Garden"' in form and "Old note" in form and "Deactivate" in form
    thai = next(t for t in rows(migrated, TagRow) if t.name == "Thai")
    response = submit(
        client,
        f"/catalog/items/{siam.id}",
        {
            "meals": LUNCH_AND_DINNER,
            "kind": "home",
            "name": "Siam Garden Thai",
            "tag_ids": [str(thai.id)],
            "modes": ["delivery"],
            "notes": "",
        },
    )
    assert response.status_code == 303, page(response)
    updated = item_row(migrated, "Siam Garden Thai")
    assert (updated.kind, updated.modes, updated.notes) == ("restaurant", ["delivery"], None)  # kind can't change
    assert item_tags(migrated, "Siam Garden Thai") == {"Thai"}
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "catalog.item_updated"]
    assert set(audit.payload) == {"name", "modes", "tags", "notes", "tags_created"}
    assert audit.payload["tags"] == ["Thai"]


def test_edit_validation_and_duplicates(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "One")
    add_item(client, "Two")
    one = item_row(migrated, "One")
    response = submit(
        client,
        f"/catalog/items/{one.id}",
        {"meals": LUNCH_AND_DINNER, "name": "two", "new_cuisines": "Thai", "modes": ["pickup"]},
    )
    assert response.status_code == 400 and "already exists" in page(response)
    response = submit(
        client,
        f"/catalog/items/{one.id}",
        {"meals": LUNCH_AND_DINNER, "name": "", "new_cuisines": "Thai", "modes": ["pickup"]},
    )
    assert response.status_code == 400 and "Enter a name." in page(response)
    same = submit(
        client,
        f"/catalog/items/{one.id}",
        {"meals": LUNCH_AND_DINNER, "name": "ONE", "new_cuisines": "Thai", "modes": ["pickup"]},
    )
    assert same.status_code == 303  # renaming to a different case of itself is fine
    assert item_row(migrated, "ONE").id == one.id


def test_deactivate_and_reactivate(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Old Place")
    item = item_row(migrated, "Old Place")
    response = submit(client, f"/catalog/items/{item.id}/deactivate")
    assert response.status_code == 303
    assert not item_row(migrated, "Old Place").active
    assert "hidden from proposals" in page(client.get(response.headers["location"]))
    assert "Old Place" not in page(client.get("/catalog"))
    inactive = page(client.get("/catalog?status=inactive"))
    assert "Old Place" in inactive and "Inactive" in inactive
    assert submit(client, f"/catalog/items/{item.id}/deactivate").status_code == 303  # idempotent
    assert submit(client, f"/catalog/items/{item.id}/reactivate").status_code == 303
    assert item_row(migrated, "Old Place").active
    assert actions(migrated).count("catalog.item_deactivated") == 1
    assert actions(migrated).count("catalog.item_reactivated") == 1


def test_any_member_maintains_the_catalog(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    assert add_item(client, "Sam's Pick").status_code == 303
    item = item_row(migrated, "Sam's Pick")
    assert item.created_by == member(migrated, "sam@example.com").id
    edit = submit(
        client,
        f"/catalog/items/{item.id}",
        {"meals": LUNCH_AND_DINNER, "name": "Sam's Pick", "new_cuisines": "Greek", "modes": ["pickup"]},
    )
    assert edit.status_code == 303
    assert submit(client, f"/catalog/items/{item.id}/deactivate").status_code == 303


def test_unknown_item_is_404(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    assert client.get("/catalog/items/999").status_code == 404
    assert submit(client, "/catalog/items/999/deactivate").status_code == 404
    response = submit(client, "/catalog/items/999", {"name": "x", "new_cuisines": "Thai"})
    assert response.status_code == 404


def test_search_and_filters(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai", modes=["pickup", "delivery"], notes="pad see ew")
    add_item(client, "Luigi's", cuisines="Italian")
    add_item(client, "Chicken tacos", kind="home", cuisines="Mexican", new_tags="chicken", new_tags_kind="protein")
    chicken = next(t for t in rows(migrated, TagRow) if t.name == "chicken")

    def found(query: str) -> set[str]:
        text = page(client.get(f"/catalog?{query}"))
        return {name for name in ("Siam Garden", "Luigi's", "Chicken tacos") if name in text}

    assert found("") == {"Siam Garden", "Luigi's", "Chicken tacos"}
    assert found("q=SEE+EW") == {"Siam Garden"}
    assert found("q=italian") == {"Luigi's"}
    assert found("kind=home") == {"Chicken tacos"}
    assert found("mode=delivery") == {"Siam Garden"}
    assert found("mode=cook") == {"Chicken tacos"}
    assert found(f"tag={chicken.id}") == {"Chicken tacos"}
    assert found("kind=bogus&mode=bogus&tag=x&status=bogus") == {"Siam Garden", "Luigi's", "Chicken tacos"}
    filtered = page(client.get("/catalog?kind=restaurant"))
    assert "2 matches" in filtered and "Clear filters" in filtered
    assert "Nothing matches" in page(client.get("/catalog?q=sushi"))


def test_filter_works_over_htmx(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    response = client.get("/catalog?q=siam", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="results"' in response.text and "Siam Garden" in response.text


def test_catalog_shows_last_eaten(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    item = item_row(migrated, "Siam Garden")
    admin = member(migrated, "admin@example.com")
    logged = submit(
        client,
        "/history/meals",
        {"day": "2026-10-20", "slot": "dinner", "item_id": str(item.id), "participant_ids": [str(admin.id)]},
    )
    assert logged.status_code == 303, page(logged)
    assert "Eaten 2 days ago" in page(client.get("/catalog"))


def test_last_eaten_labels() -> None:
    today = date(2026, 10, 22)
    assert last_eaten_label(None, today) == "Never eaten"
    assert last_eaten_label(today, today) == "Eaten today"
    assert last_eaten_label(date(2026, 10, 21), today) == "Eaten yesterday"
    assert last_eaten_label(date(2026, 10, 12), today) == "Eaten 10 days ago"
    assert last_eaten_label(date(2026, 9, 1), today) == "Last eaten Sep 1"
    assert last_eaten_label(date(2025, 9, 1), today) == "Last eaten Sep 1, 2025"


def test_cooks_must_be_active_but_existing_cooks_stay(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    from dinnerbell.db.models import CatalogItemCookRow

    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    invite(client, "kim@example.com", "Kim")
    sam, kim = member(migrated, "sam@example.com").id, member(migrated, "kim@example.com").id
    add_item(client, "Pie", kind="home", cuisines="American", cook_ids=[str(sam)])
    pie = item_row(migrated, "Pie")
    for who in (sam, kim):
        submit(client, f"/admin/members/{who}/deactivate")
    form = client.get(f"/catalog/items/{pie.id}").text
    assert f'name="cook_ids" value="{sam}" checked' in form  # an existing cook stays listed
    assert f'name="cook_ids" value="{kim}"' not in form
    keep = submit(
        client,
        f"/catalog/items/{pie.id}",
        {"meals": LUNCH_AND_DINNER, "name": "Pie", "new_cuisines": "American", "cook_ids": [str(sam)]},
    )
    assert keep.status_code == 303
    add = submit(
        client,
        f"/catalog/items/{pie.id}",
        {"meals": LUNCH_AND_DINNER, "name": "Pie", "new_cuisines": "American", "cook_ids": [str(kim)]},
    )
    assert add.status_code == 400 and "isn't an active member" in page(add)
    new = add_item(client, "Cake", kind="home", cuisines="American", cook_ids=[str(sam)])
    assert new.status_code == 400
    assert {c.member_id for c in rows(migrated, CatalogItemCookRow)} == {sam}


def test_trailing_slash_is_served_not_redirected(client: TestClient, provider: FakeProvider) -> None:
    """Starlette's slash redirect built http://<Host>/catalog from the Host header (audit #14)."""
    sign_in(client, provider)
    for host in ("testserver", "evil.example"):
        response = client.get("/catalog/", headers={"Host": host}, follow_redirects=False)
        assert response.status_code == 200, host
        assert "location" not in response.headers
        assert "evil.example" not in response.text
    assert client.get("/catalog", follow_redirects=False).status_code == 200
    assert client.get("/static/nope/", follow_redirects=False).status_code == 404


def test_a_post_to_a_slash_path_keeps_csrf(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    headers = {"Origin": "https://dinnerbell.test"}
    bare = client.post("/catalog/tags", data={"name": "Noodles"}, headers=headers, follow_redirects=False)
    slash = client.post("/catalog/tags/", data={"name": "Noodles"}, headers=headers, follow_redirects=False)
    assert bare.status_code == slash.status_code == 403  # no CSRF token
    assert "form expired" in slash.text
    assert "location" not in slash.headers
    served = submit(client, "/catalog/tags/", {"name": "Noodles", "kind": "other"})
    assert served.status_code != 404
    assert submit(client, "/catalog/tags//", {"name": "Soup", "kind": "other"}).status_code == served.status_code


def test_repeated_slashes_are_trimmed(client: TestClient, provider: FakeProvider) -> None:
    assert client.get("//", follow_redirects=False).status_code == 200
    sign_in(client, provider)
    for path in ("/catalog//", "/catalog///", "/catalog/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200, path
        assert "location" not in response.headers


def test_a_slash_path_does_not_dodge_the_auth_rate_limit(client: TestClient) -> None:
    statuses = [client.get("/auth/login/", follow_redirects=False).status_code for _ in range(30)]
    assert 429 in statuses
    assert client.get("/auth/login", follow_redirects=False).status_code == 429


def test_the_catalog_pages_and_keeps_the_search(
    client: TestClient, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dinnerbell.web.routes import catalog

    monkeypatch.setattr(catalog, "ITEMS_PER_PAGE", 2)
    sign_in(client, provider)
    for name in ("Anchor", "Bistro", "Cantina", "Diner", "Eatery"):
        add_item(client, name, cuisines="Thai")
    add_item(client, "Falafel Hut", kind="home", cuisines="Lebanese")
    shown = lambda html: [n for n in ("Anchor", "Bistro", "Cantina", "Diner", "Eatery", "Falafel Hut") if n in html]  # noqa: E731

    first = page(client.get("/catalog"))
    assert shown(first) == ["Anchor", "Bistro"] and "6 active items" in first
    assert "Page 1 of 3" in first and 'rel="next" href="/catalog?page=2"' in first and 'rel="prev"' not in first
    last = page(client.get("/catalog?page=3"))
    assert shown(last) == ["Eatery", "Falafel Hut"] and 'rel="prev" href="/catalog?page=2"' in last
    assert 'rel="next"' not in last

    # Filters carry over to the page links, normalized; bogus values drop out.
    filtered = page(client.get("/catalog?q=a&kind=restaurant&mode=bogus&status=all"))
    assert "5 matches" in filtered and shown(filtered) == ["Anchor", "Bistro"]
    assert 'href="/catalog?q=a&kind=restaurant&status=all&page=2"' in filtered
    assert 'href="/catalog?q=a&kind=restaurant&status=all&page=3"' not in filtered
    second = page(client.get("/catalog?q=a&kind=restaurant&status=all&page=2"))
    assert shown(second) == ["Cantina", "Diner"]
    assert 'rel="prev" href="/catalog?q=a&kind=restaurant&status=all"' in second

    # A page past either end shows the nearest page; too few matches for two pages, no pager.
    assert shown(page(client.get("/catalog?page=99"))) == ["Eatery", "Falafel Hut"]
    assert shown(page(client.get("/catalog?page=-1"))) == ["Anchor", "Bistro"]
    single = page(client.get("/catalog?kind=home&page=5"))
    assert shown(single) == ["Falafel Hut"] and 'class="pager"' not in single and "1 match " in single


def test_catalog_page_links_load_a_whole_page(
    client: TestClient, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain links, not HTMX swaps: a swap loses keyboard focus when the link pressed isn't on the
    next page, as Next isn't on the last."""
    from dinnerbell.web.routes import catalog

    monkeypatch.setattr(catalog, "ITEMS_PER_PAGE", 1)
    sign_in(client, provider)
    add_item(client, "Anchor")
    add_item(client, "Bistro")
    html = client.get("/catalog?q=", headers={"HX-Request": "true"}).text
    pager = html[html.index('<nav class="pager"') :].split("</nav>", 1)[0]
    assert 'href="/catalog?page=2"' in pager and "hx-" not in pager
    assert inline_code(html)[1] == []


def test_adding_or_editing_an_item_returns_to_its_page(
    client: TestClient, provider: FakeProvider, migrated: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dinnerbell.web.routes import catalog

    monkeypatch.setattr(catalog, "ITEMS_PER_PAGE", 2)
    sign_in(client, provider)
    first = add_item(client, "Anchor")
    assert first.headers["location"] == f"/catalog?notice=created#item-{item_row(migrated, 'Anchor').id}"
    add_item(client, "Bistro")
    add_item(client, "Cantina")
    cantina = item_row(migrated, "Cantina").id
    created = add_item(client, "Diner")
    diner = item_row(migrated, "Diner").id
    assert created.headers["location"] == f"/catalog?page=2&notice=created#item-{diner}"
    edit = {"name": "Cantina", "new_cuisines": "Thai", "modes": ["pickup"], "meals": ["dinner"]}
    updated = submit(client, f"/catalog/items/{cantina}", edit)
    assert updated.headers["location"] == f"/catalog?page=2&notice=updated#item-{cantina}"
    assert f'id="item-{cantina}"' in client.get(updated.headers["location"]).text
    # Renamed to sort first, it's on page 1; an inactive item isn't listed, so page 1 too.
    renamed = submit(client, f"/catalog/items/{cantina}", edit | {"name": "Aardvark"})
    assert renamed.headers["location"] == f"/catalog?notice=updated#item-{cantina}"
    submit(client, f"/catalog/items/{diner}/deactivate")
    again = submit(client, f"/catalog/items/{diner}", edit | {"name": "Diner"})
    assert again.headers["location"] == f"/catalog?notice=updated#item-{diner}"
