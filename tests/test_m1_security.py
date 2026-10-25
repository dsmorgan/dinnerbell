# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSRF, Origin, and CSP rules on every M1 route and page."""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, CatalogItemRow, FeedbackRow, MealLogRow, PreferenceRow, TagRow
from tests.conftest import csrf_from, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.pages import inline_code
from tests.support.web import add_item, htmx, submit
from tests.test_architecture import audit_routes
from tests.test_auth_flow import member, rows
from tests.test_catalog_web import item_row

M1_PREFIXES = ("/catalog", "/preferences", "/history")


def m1_posts(client: TestClient) -> list[str]:
    paths = [
        route.path
        for route in audit_routes(client.app.routes)[0]  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and "POST" in route.methods and route.path.startswith(M1_PREFIXES)
    ]
    return [re.sub(r"\{[^}]+\}", "1", path) for path in paths]


@pytest.fixture
def populated(client: TestClient, provider: FakeProvider, migrated: Settings) -> Settings:
    """One item, two tags, one stance, and one meal, all with ID 1 where it matters."""
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai, Lao")
    item = item_row(migrated, "Siam Garden")
    htmx(client, "/preferences/stance", {"target": f"item:{item.id}", "level": "1"})
    admin = member(migrated, "admin@example.com").id
    submit(
        client,
        "/history/meals",
        {"day": "2026-10-22", "slot": "dinner", "item_id": str(item.id), "participant_ids": [str(admin)]},
    )
    return migrated


def snapshot(settings: Settings) -> tuple[int, ...]:
    models = (CatalogItemRow, TagRow, PreferenceRow, MealLogRow, FeedbackRow, AuditRow)
    return tuple(len(rows(settings, model)) for model in models)


def test_every_m1_post_route_is_enumerated(client: TestClient) -> None:
    assert len(m1_posts(client)) == 15  # 14 from M1, and feedback on a meal (M3)


def test_every_m1_post_refuses_a_missing_or_wrong_csrf_token(client: TestClient, populated: Settings) -> None:
    before = snapshot(populated)
    tags = {t.name: t for t in rows(populated, TagRow)}
    body = {"name": "Changed", "new_cuisines": "Greek", "kind": "other", "into": str(tags["Lao"].id), "level": "2"}
    for path in m1_posts(client):
        assert client.post(path, data=body).status_code == 403, path
        assert client.post(path, data=body | {"csrf_token": "0" * 64}).status_code == 403, path
        assert client.post(path, data=body, headers={"X-CSRF-Token": "nope"}).status_code == 403, path
    assert snapshot(populated) == before


def test_every_m1_post_refuses_a_foreign_origin(client: TestClient, populated: Settings) -> None:
    before = snapshot(populated)
    token = csrf_from(client)
    for path in m1_posts(client):
        response = client.post(path, data={"csrf_token": token}, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403, path
    assert snapshot(populated) == before


def test_m1_pages_have_no_inline_script_or_style(client: TestClient, populated: Settings) -> None:
    item = item_row(populated, "Siam Garden")
    meal = rows(populated, MealLogRow)[0]
    paths = [
        "/catalog",
        "/catalog/new?kind=restaurant",
        "/catalog/new?kind=home",
        f"/catalog/items/{item.id}",
        "/catalog/tags",
        "/catalog/import",
        "/preferences",
        "/history",
        f"/history/meals/{meal.id}",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.text
        assert inline_code(html)[1] == [], path
        assert 'name="viewport" content="width=device-width' in html


def test_user_content_is_escaped(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "<script>alert(1)</script>", cuisines="<b>bold</b>", notes="<img src=x onerror=alert(1)>")
    for path, shown in (
        ("/catalog", "&lt;script&gt;"),
        ("/preferences", "&lt;script&gt;"),
        ("/catalog/tags", "&lt;b&gt;"),
    ):
        html = client.get(path).text
        assert "<script>alert(1)" not in html and "<img src=x" not in html and "<b>bold</b>" not in html, path
        assert shown in html, path


def test_import_preview_escapes_file_content(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    content = "version: 1\nrestaurants:\n  - {name: '</textarea><script>x</script>', cuisine: [Thai]}\n"
    html = submit(client, "/catalog/import/preview", {"content": content}).text
    assert "</textarea><script>" not in html
