# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tag management: any member adds tags; only the admin renames and merges them (FR-25)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, CatalogItemTagRow, MealLogRow, PreferenceRow, TagRow
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, htmx, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows
from tests.test_catalog_web import item_row, item_tags


def tag_id(settings: Settings, name: str) -> int:
    return next(t.id for t in rows(settings, TagRow) if t.name == name)


def test_tags_page_lists_tags_with_usage(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai", new_tags="spicy")
    add_item(client, "Thai Two", cuisines="Thai")
    text = page(client.get("/catalog/tags"))
    assert "Thai" in text and "2 items" in text and "1 item" in text
    assert "No dietary tags yet." in text
    assert "Rename or merge" in text  # the admin sees the controls


def test_member_adds_a_tag(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    response = submit(client, "/catalog/tags", {"name": "  gluten-free   option ", "kind": "dietary"})
    assert response.status_code == 303
    assert "Tag added." in page(client.get(response.headers["location"]))
    [tag] = rows(migrated, TagRow)
    assert (tag.name, tag.kind, tag.name_key) == ("gluten-free option", "dietary", "gluten-free option")
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "catalog.tag_created"]
    assert audit.actor_member_id == member(migrated, "sam@example.com").id


def test_tag_names_are_unique_ignoring_case_and_kind(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    submit(client, "/catalog/tags", {"name": "vegetarian", "kind": "protein"})
    response = submit(client, "/catalog/tags", {"name": "Vegetarian", "kind": "dietary"})
    assert response.status_code == 400
    assert 'There\'s already a protein tag named "vegetarian".' in page(response)
    assert 'value="Vegetarian"' in response.text
    assert submit(client, "/catalog/tags", {"name": "x", "kind": "allergen"}).status_code == 400
    assert submit(client, "/catalog/tags", {"name": "   ", "kind": "other"}).status_code == 400
    assert len(rows(migrated, TagRow)) == 1


def test_members_cannot_rename_or_merge(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "One", cuisines="Thai, Lao")
    invite(client, "sam@example.com", "Sam")
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    assert "Rename or merge" not in page(client.get("/catalog/tags"))
    thai, lao = tag_id(migrated, "Thai"), tag_id(migrated, "Lao")
    assert submit(client, f"/catalog/tags/{thai}/rename", {"name": "Thailand"}).status_code == 403
    assert submit(client, f"/catalog/tags/{thai}/merge", {"into": str(lao), "confirm": "on"}).status_code == 403
    assert {t.name for t in rows(migrated, TagRow)} == {"Thai", "Lao"}
    assert not [a for a in rows(migrated, AuditRow) if a.action in {"catalog.tag_renamed", "catalog.tags_merged"}]


def test_admin_renames_a_tag(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "One", cuisines="thai, Lao")
    thai = tag_id(migrated, "thai")
    response = submit(client, f"/catalog/tags/{thai}/rename", {"name": "Thai"})  # case-only change
    assert response.status_code == 303
    assert "Tag renamed." in page(client.get(response.headers["location"]))
    assert tag_id(migrated, "Thai") == thai
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "catalog.tag_renamed"]
    assert audit.payload == {"from": "thai", "to": "Thai"}
    assert submit(client, f"/catalog/tags/{thai}/rename", {"name": "Thai"}).status_code == 303  # no change
    clash = submit(client, f"/catalog/tags/{thai}/rename", {"name": "LAO"})
    assert clash.status_code == 400 and "Merge the two tags instead." in page(clash)
    assert submit(client, f"/catalog/tags/{thai}/rename", {"name": ""}).status_code == 400
    assert submit(client, "/catalog/tags/999/rename", {"name": "x"}).status_code == 404


def test_admin_merges_tags(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    admin = member(migrated, "admin@example.com")
    add_item(client, "Both", cuisines="Thai, Thai food")
    add_item(client, "Source only", cuisines="Thai food")
    add_item(client, "Target only", cuisines="Thai")
    source, target = tag_id(migrated, "Thai food"), tag_id(migrated, "Thai")
    # The admin rated both tags; Sam rated only the source.
    htmx(client, "/preferences/stance", {"target": f"tag:{source}", "level": "veto"})
    htmx(client, "/preferences/stance", {"target": f"tag:{target}", "level": "2"})
    item = item_row(migrated, "Source only")
    submit(
        client,
        "/history/meals",
        {"day": "2026-10-21", "slot": "dinner", "item_id": str(item.id), "participant_ids": [str(admin.id)]},
    )
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    htmx(client, "/preferences/stance", {"target": f"tag:{source}", "level": "-1"})
    sign_in_as(client, provider, "subject-admin", "admin@example.com")

    response = submit(client, f"/catalog/tags/{source}/merge", {"into": str(target), "confirm": "on"})
    assert response.status_code == 303
    assert "Tags merged." in page(client.get(response.headers["location"]))
    assert {t.name for t in rows(migrated, TagRow)} == {"Thai"}
    for name in ("Both", "Source only", "Target only"):
        assert item_tags(migrated, name) == {"Thai"}
    assert len([link for link in rows(migrated, CatalogItemTagRow) if link.tag_id == target]) == 3
    stances = {(p.member_id, p.tag_id): (p.veto, p.score) for p in rows(migrated, PreferenceRow)}
    sam = member(migrated, "sam@example.com")
    assert stances == {(admin.id, target): (True, 0), (sam.id, target): (False, -1)}  # veto beat +2
    [meal] = rows(migrated, MealLogRow)
    assert meal.cuisine_tags == [{"id": target, "name": "Thai food"}]  # identity moves; logged name stays
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "catalog.tags_merged"]
    assert audit.payload["source"]["name"] == "Thai food" and audit.payload["target"]["id"] == target
    assert (audit.payload["items_retagged"], audit.payload["meals_repointed"]) == (1, 1)
    # FR-29: even a count would tell the admin whether anyone rated the tag.
    assert not any("pref" in key for key in audit.payload)


def test_merge_keeps_the_lower_score(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "One", cuisines="Thai, Siamese")
    source, target = tag_id(migrated, "Siamese"), tag_id(migrated, "Thai")
    htmx(client, "/preferences/stance", {"target": f"tag:{source}", "level": "-2"})
    htmx(client, "/preferences/stance", {"target": f"tag:{target}", "level": "1"})
    submit(client, f"/catalog/tags/{source}/merge", {"into": str(target), "confirm": "on"})
    [pref] = rows(migrated, PreferenceRow)
    assert (pref.tag_id, pref.veto, pref.score) == (target, False, -2)


def test_merge_rules(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "One", cuisines="Thai", new_tags="spicy")
    thai, spicy = tag_id(migrated, "Thai"), tag_id(migrated, "spicy")
    cases = [
        (thai, str(spicy), "Merge a cuisine only into another cuisine"),
        (thai, str(thai), "Pick a different tag to merge into."),
        (thai, "", "Pick a tag to merge into."),
    ]
    for source, into, message in cases:
        response = submit(client, f"/catalog/tags/{source}/merge", {"into": into, "confirm": "on"})
        assert response.status_code == 400 and message in page(response)
    assert submit(client, f"/catalog/tags/{thai}/merge", {"into": "999", "confirm": "on"}).status_code == 404
    assert (
        submit(client, f"/catalog/tags/{spicy}/merge", {"into": str(thai), "confirm": "on"}).status_code == 303
    )  # other → cuisine
    assert item_tags(migrated, "One") == {"Thai"}


def test_merge_survives_a_meal_without_the_source(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    admin = member(migrated, "admin@example.com")
    add_item(client, "One", cuisines="Thai")
    add_item(client, "Two", cuisines="Siamese, Thai")
    for name in ("One", "Two"):
        item = item_row(migrated, name)
        submit(
            client,
            "/history/meals",
            {"day": "2026-10-21", "slot": "dinner", "item_id": str(item.id), "participant_ids": [str(admin.id)]},
        )
    source, target = tag_id(migrated, "Siamese"), tag_id(migrated, "Thai")
    submit(client, f"/catalog/tags/{source}/merge", {"into": str(target), "confirm": "on"})
    snapshots = [m.cuisine_tags for m in rows(migrated, MealLogRow)]
    assert all(len(s) == 1 and s[0]["id"] == target for s in snapshots)


def test_tag_names_are_unique_in_the_database(migrated: Settings) -> None:
    """The unique name_key backs the service check, so a race can't create duplicates."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO household (id, name, timezone, settings, created_at) "
                    "VALUES (1, 'H', 'UTC', '{}', '2026-10-22')"
                )
            )
            insert = text(
                "INSERT INTO tag (household_id, kind, name, name_key, created_at) "
                "VALUES (1, :kind, :name, :key, '2026-10-22')"
            )
            connection.execute(insert, {"kind": "cuisine", "name": "Thai", "key": "thai"})
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(insert, {"kind": "other", "name": "THAI", "key": "thai"})
    finally:
        engine.dispose()


def test_merge_needs_confirmation(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "One", cuisines="Thai, Siamese")
    source, target = tag_id(migrated, "Siamese"), tag_id(migrated, "Thai")
    html = client.get("/catalog/tags").text
    merge_form = html.split(f'action="/catalog/tags/{source}/merge"', 1)[1].split("</form>", 1)[0]
    assert "hx-confirm=" in merge_form and 'hx-boost="true"' in merge_form
    assert 'type="checkbox" name="confirm" required' in merge_form
    response = submit(client, f"/catalog/tags/{source}/merge", {"into": str(target)})
    assert response.status_code == 400
    assert "Tick the box to confirm the merge." in page(response)
    assert {t.name for t in rows(migrated, TagRow)} == {"Thai", "Siamese"}
    assert submit(client, f"/catalog/tags/{source}/merge", {"into": str(target), "confirm": "on"}).status_code == 303
    assert {t.name for t in rows(migrated, TagRow)} == {"Thai"}


def test_boosted_merge_errors_are_swapped_in(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    """HTMX swaps only successful responses, so a boosted merge shows its error with status 200."""
    sign_in(client, provider)
    add_item(client, "One", cuisines="Thai", new_tags="spicy")
    thai, spicy = tag_id(migrated, "Thai"), tag_id(migrated, "spicy")
    response = client.post(
        f"/catalog/tags/{thai}/merge",
        data={"into": str(spicy), "confirm": "on"},
        headers={"HX-Request": "true", "HX-Boosted": "true", "X-CSRF-Token": csrf_from(client)},
    )
    assert response.status_code == 200
    assert "Merge a cuisine only into another cuisine" in page(response)
    plain = submit(client, f"/catalog/tags/{thai}/merge", {"into": str(spicy), "confirm": "on"})
    assert plain.status_code == 400


@pytest.mark.anyio
async def test_services_refuse_members_rename_and_merge(migrated: Settings, clock: FakeClock) -> None:
    """The web layer checks for an admin, and so does the service, in case a route forgets."""
    from dinnerbell.db.repositories import MemberRepository
    from dinnerbell.domain.catalog import TagKind
    from dinnerbell.domain.members import Capabilities, Role
    from dinnerbell.services import catalog as catalog_service
    from dinnerbell.services.bootstrap import bootstrap
    from dinnerbell.services.database import open_database
    from dinnerbell.services.errors import Forbidden

    database = open_database(migrated.async_database_url)
    try:
        async with database.sessionmaker() as db:
            household = await bootstrap(
                db, household_name="H", timezone="UTC", admin_email="a@example.com", now=clock()
            )
            sam = await MemberRepository(db).add(
                household_id=household.id,
                display_name="Sam",
                email="sam@example.com",
                role=Role.MEMBER,
                capabilities=Capabilities(),
                now=clock(),
            )
            await db.commit()
            thai = await catalog_service.create_tag(db, sam, TagKind.CUISINE, "Thai", now=clock())
            lao = await catalog_service.create_tag(db, sam, TagKind.CUISINE, "Lao", now=clock())
            with pytest.raises(Forbidden):
                await catalog_service.rename_tag(db, sam, thai.id, "Thailand", now=clock())
            with pytest.raises(Forbidden):
                await catalog_service.merge_tags(db, sam, lao.id, thai.id, now=clock())
    finally:
        await database.close()
    assert {t.name for t in rows(migrated, TagRow)} == {"Thai", "Lao"}


def test_merge_keeps_the_target_name_in_meal_snapshots(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    admin = member(migrated, "admin@example.com")
    add_item(client, "Both", cuisines="Siamese, Thai")
    item = item_row(migrated, "Both")
    submit(
        client,
        "/history/meals",
        {"day": "2026-10-21", "slot": "dinner", "item_id": str(item.id), "participant_ids": [str(admin.id)]},
    )
    source, target = tag_id(migrated, "Siamese"), tag_id(migrated, "Thai")
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.begin() as connection:  # the source first, so a naive dedupe keeps its name
            snapshot = [{"id": source, "name": "Siamese"}, {"id": target, "name": "Thai"}]
            import json

            connection.execute(text("UPDATE meal_log SET cuisine_tags = :c"), {"c": json.dumps(snapshot)})
    finally:
        engine.dispose()
    submit(client, f"/catalog/tags/{source}/merge", {"into": str(target), "confirm": "on"})
    [meal] = rows(migrated, MealLogRow)
    assert meal.cuisine_tags == [{"id": target, "name": "Thai"}]


@pytest.mark.anyio
async def test_only_unique_violations_read_as_duplicate_names(migrated: Settings, clock: FakeClock) -> None:
    """Any other integrity error is a bug and must surface, not pose as "already exists"."""
    from sqlalchemy.exc import IntegrityError

    from dinnerbell.domain.catalog import TagKind
    from dinnerbell.domain.members import Capabilities, Member, Role
    from dinnerbell.services import catalog as catalog_service
    from dinnerbell.services.bootstrap import bootstrap
    from dinnerbell.services.database import open_database

    database = open_database(migrated.async_database_url)
    try:
        async with database.sessionmaker() as db:
            household = await bootstrap(
                db, household_name="H", timezone="UTC", admin_email="a@example.com", now=clock()
            )
            ghost = Member(
                id=999,  # no such member row, so the audit row's foreign key fails
                household_id=household.id,
                display_name="Ghost",
                email="ghost@example.com",
                role=Role.MEMBER,
                capabilities=Capabilities(),
                active=True,
            )
            with pytest.raises(IntegrityError):
                await catalog_service.create_tag(db, ghost, TagKind.CUISINE, "Thai", now=clock())
    finally:
        await database.close()
