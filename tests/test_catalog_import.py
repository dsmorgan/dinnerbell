# SPDX-License-Identifier: AGPL-3.0-or-later
"""Importing catalog files from the Catalog page: preview, apply, re-plan, all or nothing (FR-26)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.config import Settings
from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.models import AuditRow, CatalogItemRow, TagRow
from dinnerbell.domain.catalog_file import MAX_FILE_BYTES
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.database import open_database
from tests.conftest import invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import rows

EXAMPLE = (Path(__file__).resolve().parents[1] / "seed" / "example.yaml").read_text()
SMALL = "version: 1\nrestaurants:\n  - name: Siam Garden\n    cuisine: [Thai]\n    modes: [pickup, delivery]\n"


def token_in(response: httpx.Response) -> str | None:
    match = re.search(r'name="token" value="([^"]+)"', response.text)
    return match.group(1) if match else None


def preview(client: TestClient, content: str) -> httpx.Response:
    return submit(client, "/catalog/import/preview", {"content": content})


def apply(client: TestClient, content: str, token: str | None) -> httpx.Response:
    return submit(client, "/catalog/import/apply", {"content": content, "token": token or ""})


def names(settings: Settings) -> set[str]:
    return {i.name for i in rows(settings, CatalogItemRow)}


def test_import_page(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    text = page(client.get("/catalog/import"))
    assert "Import a catalog file" in text and 'enctype="multipart/form-data"' in text
    assert "Apply" not in text


def test_preview_saves_nothing_then_apply_imports(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    response = preview(client, EXAMPLE)
    assert response.status_code == 200
    text = page(response)
    assert "Add 25 tags" in text and "Add 8 items" in text
    assert "Example Thai Kitchen" in text and "Middle Eastern" in text
    token = token_in(response)
    assert token
    assert rows(migrated, CatalogItemRow) == [] and rows(migrated, TagRow) == []

    applied = apply(client, EXAMPLE, token)
    assert applied.status_code == 303
    assert applied.headers["location"] == "/catalog?notice=imported&created=8&updated=0&tags_created=25"
    assert "Imported: 8 added, 0 updated, 25 new tags." in page(client.get(applied.headers["location"]))
    assert len(names(migrated)) == 8 and len(rows(migrated, TagRow)) == 25
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "catalog.imported"]
    assert audit.payload["items_created"] == 8 and len(audit.payload["sha256"]) == 64

    again = preview(client, EXAMPLE)
    assert "already matches this file" in page(again)
    assert token_in(again) is None


def test_preview_by_upload(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    response = submit(
        client,
        "/catalog/import/preview",
        {"content": "ignored"},
        files={"upload": ("catalog.yaml", SMALL.encode(), "application/yaml")},
    )
    assert response.status_code == 200
    assert "Siam Garden" in page(response)
    assert "\nversion: 1\nrestaurants:" in page(response)  # the upload fills the textarea
    assert token_in(response)


def test_upload_limits(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    big = submit(
        client,
        "/catalog/import/preview",
        files={"upload": ("big.yaml", b"version: 1\n" + b"#" * MAX_FILE_BYTES, "application/yaml")},
    )
    assert big.status_code == 400 and "larger than 1 MB" in page(big)
    latin1 = submit(
        client,
        "/catalog/import/preview",
        files={"upload": ("x.yaml", "version: 1\n# café".encode("latin-1"), "text/plain")},
    )
    assert latin1.status_code == 400 and "isn't UTF-8" in page(latin1)


def test_hostile_yaml_gets_a_clean_400(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    deep = "version: 1\nrestaurants: " + "[" * 100_000 + "]" * 100_000 + "\n"
    response = preview(client, deep)
    assert response.status_code == 400
    assert "<code>line 2</code> values are nested more than 20 levels deep" in page(response)
    bomb = "version: 1\na: &a [x, x, x]\nb: [*a, *a, *a]\n"
    response = preview(client, bomb)
    assert response.status_code == 400
    assert "<code>line 3</code> anchors and aliases (*name) are not supported" in page(response)


def test_empty_submission(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    assert "Paste a catalog file" in page(preview(client, "   "))


def test_errors_block_apply(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    bad = SMALL + "  - name: Luigi's\n    cuisine: [Italian]\n    modes: [drive-thru]\n"
    response = preview(client, bad)
    assert response.status_code == 400
    text = page(response)
    assert '<code>restaurants[1].modes[0]</code> "drive-thru" is not one of pickup, delivery' in text
    assert "(Luigi's)" in text
    assert token_in(response) is None and "formaction" not in response.text
    assert rows(migrated, CatalogItemRow) == []


def test_unknown_cooks_are_listed(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    content = (
        "version: 1\nhome_meals:\n  - {name: Pie, cuisine: [American], cooks: [ghost@example.com, admin@example.com]}\n"
    )
    response = preview(client, content)
    text = page(response)
    assert "Cooks that will be skipped" in text and "ghost@example.com" in text
    assert apply(client, content, token_in(response)).status_code == 303
    from dinnerbell.db.models import CatalogItemCookRow

    assert len(rows(migrated, CatalogItemCookRow)) == 1


def test_update_preview_shows_field_changes(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    apply(client, SMALL, token_in(preview(client, SMALL)))
    changed = SMALL.replace("[pickup, delivery]", "[delivery]") + "    active: false\n"
    text = page(preview(client, changed))
    assert "Update 1 item" in text
    assert "<dt>modes</dt><dd><del>pickup, delivery</del> → <ins>delivery</ins></dd>" in text
    assert "<dt>active</dt><dd><del>yes</del> → <ins>no</ins></dd>" in text


def test_apply_refuses_content_that_differs_from_the_preview(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    token = token_in(preview(client, SMALL))
    edited = SMALL.replace("Siam Garden", "Siam Palace")
    response = apply(client, edited, token)
    assert response.status_code == 409
    text = page(response)
    assert "The file changed or the preview expired" in text and "Siam Palace" in text
    assert token_in(response)  # a fresh preview of what was sent
    assert rows(migrated, CatalogItemRow) == []


def test_apply_accepts_textarea_line_endings(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    token = token_in(preview(client, SMALL))
    assert apply(client, SMALL.replace("\n", "\r\n"), token).status_code == 303
    assert names(migrated) == {"Siam Garden"}


def test_apply_refuses_forged_and_expired_tokens(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    sign_in(client, provider)
    assert apply(client, SMALL, "forged.token").status_code == 409
    assert apply(client, SMALL, None).status_code == 409
    token = token_in(preview(client, SMALL))
    clock.advance(timedelta(minutes=31))
    assert apply(client, SMALL, token).status_code == 409
    assert rows(migrated, CatalogItemRow) == []


def test_token_is_bound_to_the_member(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    token = token_in(preview(client, SMALL))
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    assert apply(client, SMALL, token).status_code == 409
    assert rows(migrated, CatalogItemRow) == []


def test_apply_replans_against_current_state(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    token = token_in(preview(client, SMALL))
    add_item(client, "siam garden", modes=["pickup"])  # someone adds it meanwhile
    response = apply(client, SMALL, token)
    assert response.status_code == 409
    text = page(response)
    assert "The catalog changed since your preview" in text
    assert "Update 1 item" in text  # the new preview reflects the current catalog
    [item] = rows(migrated, CatalogItemRow)
    assert item.modes == ["pickup"]  # nothing from the import was saved
    assert apply(client, SMALL, token_in(response)).status_code == 303
    [item] = rows(migrated, CatalogItemRow)
    assert sorted(item.modes) == ["delivery", "pickup"]


def test_apply_rejects_a_file_that_became_invalid(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    content = "version: 1\nrestaurants:\n  - {name: A, cuisine: [Thai], tags: [spicy]}\n"
    token = token_in(preview(client, content))
    submit(client, "/catalog/tags", {"name": "Spicy", "kind": "cuisine"})
    response = apply(client, content, token)
    assert response.status_code == 400
    assert '"spicy" is a cuisine tag; list it under cuisine' in page(response)
    assert rows(migrated, CatalogItemRow) == []


def test_any_member_can_import(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    assert apply(client, SMALL, token_in(preview(client, SMALL))).status_code == 303
    assert names(migrated) == {"Siam Garden"}


# Service level: all or nothing ----------------------------------------------------------------------------

pytestmark_anyio = pytest.mark.anyio


@pytest.fixture
async def db(migrated: Settings, clock: FakeClock) -> AsyncIterator[AsyncSession]:
    database = open_database(migrated.async_database_url)
    async with database.sessionmaker() as session:
        await bootstrap(session, household_name="Home", timezone="UTC", admin_email=migrated.admin_email, now=clock())
        yield session
    await database.close()


@pytest.mark.anyio
async def test_failure_midway_saves_nothing(
    db: AsyncSession, migrated: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = CatalogRepository.add_item

    async def flaky(self: CatalogRepository, *args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("disk full")
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CatalogRepository, "add_item", flaky)
    with pytest.raises(RuntimeError, match="disk full"):
        await catalog_service.apply_import(db, 1, None, EXAMPLE, now=clock())
    assert calls == 3
    assert rows(migrated, CatalogItemRow) == [] and rows(migrated, TagRow) == []
    assert [a for a in rows(migrated, AuditRow) if a.action.startswith("catalog.")] == []


@pytest.mark.anyio
async def test_lost_race_on_a_unique_name_saves_nothing(
    db: AsyncSession, migrated: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    async def clash(self: CatalogRepository, *args: object, **kwargs: object) -> object:
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(CatalogRepository, "update_item", clash)
    await catalog_service.apply_import(db, 1, None, SMALL, now=clock())
    with pytest.raises(catalog_service.ImportChanged):
        await catalog_service.apply_import(db, 1, None, SMALL.replace("[pickup, delivery]", "[pickup]"), now=clock())
    [item] = rows(migrated, CatalogItemRow)
    assert sorted(item.modes) == ["delivery", "pickup"]


@pytest.mark.anyio
async def test_apply_refuses_errors_and_stale_fingerprints(db: AsyncSession, clock: FakeClock) -> None:
    with pytest.raises(catalog_service.ImportRejected) as rejected:
        await catalog_service.apply_import(db, 1, None, "version: 2\n", now=clock())
    assert not rejected.value.plan.ok
    with pytest.raises(catalog_service.ImportChanged):
        await catalog_service.apply_import(db, 1, None, SMALL, now=clock(), expected_fingerprint="0" * 64)


@pytest.mark.anyio
async def test_example_imports_cleanly(db: AsyncSession, migrated: Settings, clock: FakeClock) -> None:
    plan = await catalog_service.apply_import(db, 1, None, EXAMPLE, now=clock())
    assert plan.ok and plan.summary()["items_created"] == 8
    again = await catalog_service.preview_import(db, 1, EXAMPLE)
    assert again.ok and not again.has_changes
    kinds = {t.name: t.kind for t in rows(migrated, TagRow)}
    assert kinds["Thai"] == "cuisine" and kinds["gluten-free option"] == "dietary" and kinds["beef"] == "protein"


def test_upload_size_is_checked_in_the_browser(client: TestClient, provider: FakeProvider) -> None:
    """Oversized uploads usually end in a TCP reset, so the browser would never show the 413;
    app.js checks the chosen file's size before sending it."""
    sign_in(client, provider)
    html = client.get("/catalog/import").text
    field = html.split('name="upload"', 1)[1].split(">", 1)[0]
    assert f'data-max-bytes="{MAX_FILE_BYTES}"' in field and 'data-size-error="upload-error"' in field
    assert '<p id="upload-error" class="save-error small" role="alert" hidden></p>' in html
    script = client.get("/static/app.js").text
    assert "Catalog files can be up to 1 MB." in script
    assert "data-max-bytes" in script and "textContent" in script and "innerHTML" not in script
