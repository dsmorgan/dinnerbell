# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every M1 read and write stays inside the signed-in member's household (spec 13.3)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import CatalogItemRow, CatalogItemTagRow, MealLogRow, PreferenceRow, TagRow
from tests.conftest import page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.households import other_household
from tests.support.web import add_item, htmx, submit
from tests.test_auth_flow import member, rows
from tests.test_catalog_web import item_row


@pytest.fixture
def two(client: TestClient, provider: FakeProvider, migrated: Settings) -> dict[str, Any]:
    """The signed-in admin's household with one item, and a second household's IDs."""
    sign_in(client, provider)
    add_item(client, "Mine", cuisines="Thai")
    ids: dict[str, Any] = other_household(migrated)
    ids["mine"] = item_row(migrated, "Mine").id
    ids["thai"] = next(t.id for t in rows(migrated, TagRow) if t.name == "Thai")
    ids["admin"] = member(migrated, "admin@example.com").id
    return ids


def untouched(settings: Settings, ids: dict[str, Any]) -> None:
    evil = next(i for i in rows(settings, CatalogItemRow) if i.id == ids["item"])
    assert (evil.name, evil.active, evil.household_id) == ("EvilItem", True, ids["household"])
    evil_tag = next(t for t in rows(settings, TagRow) if t.id == ids["tag"])
    assert (evil_tag.name, evil_tag.household_id) == ("EvilCuisine", ids["household"])
    assert [link.tag_id for link in rows(settings, CatalogItemTagRow) if link.item_id == ids["item"]] == [ids["tag"]]
    assert [m.id for m in rows(settings, MealLogRow) if m.household_id == ids["household"]] == [ids["meal"]]
    assert rows(settings, PreferenceRow) == []


def test_foreign_items_are_not_found(client: TestClient, migrated: Settings, two: dict[str, Any]) -> None:
    item = two["item"]
    assert client.get(f"/catalog/items/{item}").status_code == 404
    edit = {"name": "pwn", "new_cuisines": "X", "modes": ["pickup"]}
    assert submit(client, f"/catalog/items/{item}", edit).status_code == 404
    assert submit(client, f"/catalog/items/{item}/deactivate").status_code == 404
    assert submit(client, f"/catalog/items/{item}/reactivate").status_code == 404
    untouched(migrated, two)


def test_foreign_tags_are_not_found(client: TestClient, migrated: Settings, two: dict[str, Any]) -> None:
    tag, thai = two["tag"], two["thai"]
    assert submit(client, f"/catalog/tags/{tag}/rename", {"name": "pwn"}).status_code == 404
    assert submit(client, f"/catalog/tags/{tag}/merge", {"into": str(thai), "confirm": "on"}).status_code == 404
    assert submit(client, f"/catalog/tags/{thai}/merge", {"into": str(tag), "confirm": "on"}).status_code == 404
    untouched(migrated, two)
    assert any(t.id == thai for t in rows(migrated, TagRow))


def test_foreign_tag_ids_in_item_forms_are_refused(client: TestClient, migrated: Settings, two: dict[str, Any]) -> None:
    tags = [str(two["tag"]), str(two["thai"])]
    created = submit(
        client,
        "/catalog/items",
        {"kind": "restaurant", "name": "X2", "tag_ids": tags, "modes": ["pickup"], "meals": ["dinner"]},
    )
    assert created.status_code == 400 and "no longer exists" in page(created)
    edited = submit(client, f"/catalog/items/{two['mine']}", {"name": "Mine", "tag_ids": tags, "modes": ["pickup"]})
    assert edited.status_code == 400
    untouched(migrated, two)


def test_foreign_preference_targets_are_refused(client: TestClient, migrated: Settings, two: dict[str, Any]) -> None:
    for target in (f"item:{two['item']}", f"tag:{two['tag']}"):
        response = htmx(client, "/preferences/stance", {"target": target, "level": "veto"})
        assert response.status_code == 200 and "removed from the catalog" in page(response)
        assert "Evil" not in response.text
        assert submit(client, "/preferences/stance", {"target": target, "level": "veto"}).status_code == 404
    untouched(migrated, two)


def test_foreign_meals_are_not_found(client: TestClient, migrated: Settings, two: dict[str, Any]) -> None:
    meal = two["meal"]
    edit = {"day": "2026-10-01", "slot": "dinner", "item_id": str(two["mine"]), "participant_ids": [str(two["admin"])]}
    assert client.get(f"/history/meals/{meal}").status_code == 404
    assert submit(client, f"/history/meals/{meal}", edit).status_code == 404
    assert submit(client, f"/history/meals/{meal}/delete").status_code == 404
    logged = submit(client, "/history/meals", edit | {"item_id": str(two["item"])})
    assert logged.status_code == 400 and "Pick something from the catalog." in page(logged)
    untouched(migrated, two)


def test_foreign_data_never_shows(client: TestClient, migrated: Settings, two: dict[str, Any]) -> None:
    for path in ("/catalog?status=all", "/catalog/tags", "/preferences", "/history", f"/catalog/items/{two['mine']}"):
        assert "Evil" not in client.get(path).text, path
    assert "Evil" not in client.get(f"/catalog?tag={two['tag']}&status=all").text


def test_import_never_touches_another_household(client: TestClient, migrated: Settings, two: dict[str, Any]) -> None:
    import re

    content = "version: 1\nrestaurants:\n  - {name: EvilItem, cuisine: [EvilCuisine], modes: [delivery]}\n"
    preview = submit(client, "/catalog/import/preview", {"content": content})
    assert "Add 1 item" in page(preview) and "Add 1 tag" in page(preview)
    token = re.search(r'name="token" value="([^"]+)"', preview.text)
    assert token
    assert submit(client, "/catalog/import/apply", {"content": content, "token": token.group(1)}).status_code == 303
    untouched(migrated, two)
    mine = [i for i in rows(migrated, CatalogItemRow) if i.name == "EvilItem" and i.household_id != two["household"]]
    assert len(mine) == 1 and mine[0].modes == ["delivery"]
