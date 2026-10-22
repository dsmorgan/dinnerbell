# SPDX-License-Identifier: AGPL-3.0-or-later
"""Malformed IDs in paths, forms, and queries get a 4xx, never a 500."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import CatalogItemRow, MealLogRow, PreferenceRow
from dinnerbell.web.forms import MAX_ID, parse_id
from tests.conftest import BASE_URL, csrf_from, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item
from tests.test_auth_flow import member, rows

BIG = "9" * 25
SUPERSCRIPT_TWO = chr(0x00B2)  # str.isdigit() accepts it; int() does not
ARABIC_INDIC_ONE = chr(0x0661)


@pytest.fixture
def lenient(client: TestClient, provider: FakeProvider, migrated: Settings) -> TestClient:
    """A signed-in client that reports server errors as 500 responses instead of raising."""
    sign_in(client, provider)
    add_item(client, "Mine", cuisines="Thai, Lao")
    other = TestClient(
        client.app,
        base_url=BASE_URL,
        headers={"Origin": BASE_URL},
        follow_redirects=False,
        raise_server_exceptions=False,
    )
    other.cookies = client.cookies
    return other


def test_parse_id() -> None:
    assert parse_id("42") == 42
    assert parse_id(str(MAX_ID)) == MAX_ID
    for bad in (None, "", "0", "-1", " 1", "1.0", SUPERSCRIPT_TWO, ARABIC_INDIC_ONE, BIG, str(MAX_ID + 1), "0x10"):
        assert parse_id(bad) is None, bad


@pytest.mark.parametrize("bad", [SUPERSCRIPT_TWO, BIG, "0", "-3"])
def test_path_ids_are_404(lenient: TestClient, bad: str) -> None:
    token = csrf_from(lenient)
    for path in (f"/catalog/items/{bad}", f"/history/meals/{bad}", f"/admin/members/{bad}"):
        assert lenient.get(path).status_code == 404, path
    for path in (
        f"/catalog/items/{bad}",
        f"/catalog/items/{bad}/deactivate",
        f"/catalog/items/{bad}/reactivate",
        f"/catalog/tags/{bad}/rename",
        f"/catalog/tags/{bad}/merge",
        f"/history/meals/{bad}",
        f"/history/meals/{bad}/delete",
        f"/admin/members/{bad}",
        f"/admin/members/{bad}/deactivate",
    ):
        assert lenient.post(path, data={"csrf_token": token, "name": "x"}).status_code == 404, path


@pytest.mark.parametrize("bad", [SUPERSCRIPT_TWO, BIG])
def test_form_ids_are_ignored_or_refused(lenient: TestClient, migrated: Settings, bad: str) -> None:
    token = csrf_from(lenient)
    admin = str(member(migrated, "admin@example.com").id)
    item = str(rows(migrated, CatalogItemRow)[0].id)
    assert lenient.get(f"/catalog?tag={bad}").status_code == 200
    assert (
        lenient.post(
            "/preferences/stance", data={"target": f"tag:{bad}", "level": "1", "csrf_token": token}
        ).status_code
        == 400
    )
    assert lenient.post("/catalog/tags/1/merge", data={"into": bad, "csrf_token": token}).status_code == 400
    created = lenient.post(
        "/catalog/items",
        data={
            "kind": "restaurant",
            "name": f"Z{len(bad)}",
            "new_cuisines": "Thai",
            "modes": ["pickup"],
            "tag_ids": [bad],
            "csrf_token": token,
        },
    )
    assert created.status_code == 303
    meal = {"day": "2026-10-20", "slot": "dinner", "item_id": item, "participant_ids": [admin], "csrf_token": token}
    assert lenient.post("/history/meals", data=meal | {"participant_ids": [bad]}).status_code == 400
    assert lenient.post("/history/meals", data=meal | {"item_id": bad}).status_code == 400
    assert rows(migrated, PreferenceRow) == [] and rows(migrated, MealLogRow) == []


@pytest.mark.parametrize(("page", "shown"), [("300000", "520"), ("99999999999999999999", "520"), ("-4", "0")])
def test_history_page_is_clamped(lenient: TestClient, page: str, shown: str) -> None:
    response = lenient.get(f"/history?page={page}")
    assert response.status_code == 200
    if shown != "0":
        assert f'href="/history?page={int(shown) - 1}">Newer' in response.text
