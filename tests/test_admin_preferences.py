# SPDX-License-Identifier: AGPL-3.0-or-later
"""An admin sees and changes another member's preferences from Admin > Members (#181): every
kind of preference, audited with the admin and the member, refused to anyone else and for
anyone outside the household, and never touching the member's own sign-in."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow, PreferenceRow, SessionRow, TagRow
from tests.conftest import BASE_URL, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.households import other_household
from tests.support.web import add_item, htmx, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows
from tests.test_catalog_web import item_row
from tests.test_preferences import checked_level, stances, thai

SAM = {"sub": "subject-sam", "email": "sam@example.com"}


@pytest.fixture
def house(client: TestClient, provider: FakeProvider, migrated: Settings) -> dict[str, Any]:
    """The admin's household: a catalog with a dietary tag, and Sam, who has signed in once and
    vetoes Thai. The admin is signed in."""
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    submit(client, "/catalog/tags", {"kind": "dietary", "name": "Vegetarian"})
    invite(client, "sam@example.com", "Sam", can_cook=True)
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    htmx(client, "/preferences/stance", {"target": f"tag:{thai(migrated)}", "level": "veto"})
    sign_in_as(client, provider, "subject-admin", "admin@example.com")
    veg = next(t.id for t in rows(migrated, TagRow) if t.name == "Vegetarian")
    return {
        "sam": member(migrated, "sam@example.com").id,
        "admin": member(migrated, "admin@example.com").id,
        "thai": thai(migrated),
        "item": item_row(migrated, "Siam Garden").id,
        "veg": veg,
    }


@pytest.fixture
def sam_client(client: TestClient, provider: FakeProvider, house: dict[str, Any]) -> TestClient:
    """Sam, signed in on a device of their own while the admin works."""
    other = TestClient(client.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    assert sign_in(other, provider, claims=SAM).status_code == 303
    return other


def base(house: dict[str, Any], who: str = "sam") -> str:
    return f"/admin/members/{house[who]}/preferences"


ACTIONS = ("member.preference_set", "member.settings_updated", "member.needs_updated", "member.fun_mode_set")


def admin_audit(settings: Settings, action: str) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action == action]


def any_admin_audit(settings: Settings) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action in ACTIONS]


def test_members_links_to_each_members_preferences(client: TestClient, house: dict[str, Any]) -> None:
    members = client.get("/admin/members").text
    assert f'href="{base(house)}"' in members
    assert 'href="/preferences">Preferences</a>' in members  # the admin's own row: their own page
    assert f'href="{base(house)}"' in client.get(f"/admin/members/{house['sam']}").text


def test_admin_sees_a_members_preferences(client: TestClient, house: dict[str, Any]) -> None:
    response = client.get(base(house))
    assert response.status_code == 200
    text = page(response)
    assert "You're editing Sam's preferences." in text and "<h1>Sam's preferences</h1>" in text
    assert 'href="/admin/members#member-' in response.text  # the way back
    assert checked_level(response.text, f"tag:{house['thai']}") == "veto"
    assert 'name="can_cook" checked' in response.text  # Sam's, not the admin's
    # Every form posts to Sam's preferences, never the admin's own.
    for path in ("/stance", "/settings", "/needs", "/fun"):
        assert f'action="{base(house)}{path}"' in response.text, path
    assert f'hx-post="{base(house)}/stance"' in response.text
    for own in ('action="/preferences/', 'action="/me"', 'action="/me/fun"', 'hx-post="/preferences'):
        assert own not in response.text, own
    # The admin's own page is unchanged, and doesn't show the banner.
    mine = client.get("/preferences")
    assert checked_level(mine.text, f"tag:{house['thai']}") == "0"
    assert "You're editing" not in page(mine) and 'action="/preferences/settings"' in mine.text


def test_admin_changes_each_kind_of_preference(
    client: TestClient, sam_client: TestClient, migrated: Settings, house: dict[str, Any]
) -> None:
    thai_target, item_target = f"tag:{house['thai']}", f"item:{house['item']}"
    # A tag and an item level, by HTMX and without JavaScript.
    row = htmx(client, f"{base(house)}/stance", {"target": thai_target, "level": "1"})
    assert row.status_code == 200 and "Saved" in row.text
    assert f'id="pref-tag-{house["thai"]}"' in row.text and f'hx-post="{base(house)}/stance"' in row.text
    plain = submit(client, f"{base(house)}/stance", {"target": item_target, "level": "-2"})
    assert plain.headers["location"] == f"{base(house)}?notice=saved#pref-item-{house['item']}"
    assert stances(migrated, "sam@example.com") == {
        ("tag", house["thai"]): (False, 1),
        ("item", house["item"]): (False, -2),
    }
    assert stances(migrated, "admin@example.com") == {}
    # I can usually and reminders.
    settings = submit(client, f"{base(house)}/settings", {"can_order": "on", "mute_dm_reminders": "on"})
    assert settings.headers["location"] == f"{base(house)}?notice=settings"
    sam = member(migrated, "sam@example.com")
    assert (sam.can_cook, sam.can_pickup, sam.can_order, sam.mute_dm_reminders, sam.feedback_reminder) == (
        False,
        False,
        True,
        True,
        False,
    )
    # Dietary needs and fun mode.
    needs = submit(client, f"{base(house)}/needs", {"needs": [str(house["veg"])]})
    assert needs.headers["location"] == f"{base(house)}?notice=settings"
    fun = submit(client, f"{base(house)}/fun", {})  # the checkbox unchecked sends nothing: off
    assert fun.headers["location"] == f"{base(house)}?notice=fun#fun-mode"
    sam = member(migrated, "sam@example.com")
    assert sam.dietary_needs == [house["veg"]] and not sam.fun_mode
    admin = member(migrated, "admin@example.com")
    assert admin.dietary_needs == [] and admin.fun_mode and not admin.can_order  # the admin's own: untouched

    # Sam's own page shows what the admin saved, and Sam is still signed in on their device.
    mine = sam_client.get("/preferences")
    assert mine.status_code == 200 and "You're editing" not in page(mine)
    assert checked_level(mine.text, thai_target) == "1" and checked_level(mine.text, item_target) == "-2"
    assert 'name="can_order" checked' in mine.text and 'name="mute_dm_reminders" checked' in mine.text
    assert f'value="{house["veg"]}" checked' in mine.text
    assert 'name="fun" value="on"' in mine.text and 'name="fun" value="on" checked' not in mine.text
    assert any(s.member_id == house["sam"] for s in rows(migrated, SessionRow))
    assert (sam.email, sam.oidc_subject) == ("sam@example.com", "subject-sam")


def test_each_change_is_audited_with_the_admin_and_the_member(
    client: TestClient, migrated: Settings, house: dict[str, Any]
) -> None:
    target = f"member:{house['sam']}"
    htmx(client, f"{base(house)}/stance", {"target": f"tag:{house['thai']}", "level": "0"})
    submit(client, f"{base(house)}/settings", {"can_cook": "on", "feedback_reminder": "on"})
    submit(client, f"{base(house)}/needs", {"needs": [str(house["veg"])]})
    submit(client, f"{base(house)}/fun", {"fun": "off"})
    expected = {
        "member.preference_set": {"target": f"tag:{house['thai']}", "name": "Thai", "from": "veto", "to": "0"},
        "member.settings_updated": {"feedback_reminder": True},
        "member.needs_updated": {"dietary_needs": [house["veg"]]},
        "member.fun_mode_set": {"from": True, "to": False},
    }
    for action, payload in expected.items():
        (row,) = admin_audit(migrated, action)
        assert (row.actor_member_id, row.target, row.payload) == (house["admin"], target, payload), action


def test_the_stance_row_keeps_its_admin_url_when_a_save_fails(
    client: TestClient, migrated: Settings, house: dict[str, Any]
) -> None:
    bad = htmx(client, f"{base(house)}/stance", {"target": f"tag:{house['thai']}", "level": "5"})
    assert bad.status_code == 200 and "Not saved." in bad.text
    assert checked_level(bad.text, f"tag:{house['thai']}") == "veto"  # Sam's stored stance
    assert f'hx-post="{base(house)}/stance"' in bad.text
    gone = htmx(client, f"{base(house)}/stance", {"target": "tag:999", "level": "1"})
    assert gone.status_code == 200 and "removed from the catalog" in gone.text
    assert htmx(client, f"{base(house)}/stance", {"target": "member:1", "level": "1"}).status_code == 400
    assert submit(client, f"{base(house)}/fun", {"fun": "maybe"}).status_code == 400
    assert stances(migrated, "sam@example.com") == {("tag", house["thai"]): (True, 0)}
    assert not admin_audit(migrated, "member.preference_set")


def test_a_member_is_refused(
    client: TestClient, provider: FakeProvider, migrated: Settings, house: dict[str, Any]
) -> None:
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    path = base(house, "admin")
    assert client.get(path).status_code == 403
    assert htmx(client, f"{path}/stance", {"target": f"tag:{house['thai']}", "level": "veto"}).status_code == 403
    for suffix, data in (
        ("/settings", {"can_cook": "on"}),
        ("/needs", {"needs": [str(house["veg"])]}),
        ("/fun", {"fun": "off"}),
    ):
        assert submit(client, f"{path}{suffix}", data).status_code == 403, suffix
    admin = member(migrated, "admin@example.com")
    assert stances(migrated, "admin@example.com") == {}
    assert (admin.can_cook, admin.dietary_needs, admin.fun_mode) == (False, [], True)
    assert not any_admin_audit(migrated)


def test_writes_need_the_csrf_token(client: TestClient, migrated: Settings, house: dict[str, Any]) -> None:
    for suffix, data in (
        ("/stance", {"target": f"tag:{house['thai']}", "level": "2"}),
        ("/settings", {"can_order": "on"}),
        ("/needs", {"needs": [str(house["veg"])]}),
        ("/fun", {"fun": "off"}),
    ):
        assert client.post(f"{base(house)}{suffix}", data=data).status_code == 403, suffix
    sam = member(migrated, "sam@example.com")
    assert stances(migrated, "sam@example.com") == {("tag", house["thai"]): (True, 0)}
    assert (sam.can_order, sam.dietary_needs, sam.fun_mode) == (False, [], True)


def eve(settings: Settings) -> int:
    """A member of another household."""
    ids = other_household(settings)
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO member (household_id, display_name, email, role, can_cook, can_pickup, can_order, "
                    "mute_dm_reminders, feedback_reminder, active, created_at, updated_at) "
                    "VALUES (:h, 'Eve', 'eve@example.com', 'member', 0, 0, 0, 0, 0, 1, :t, :t)"
                ),
                {"h": ids["household"], "t": "2026-01-01 00:00:00"},
            )
            eve_id: int = c.execute(text("SELECT max(id) FROM member")).scalar_one()
    finally:
        engine.dispose()
    return eve_id


def test_another_households_member_is_not_found(client: TestClient, migrated: Settings, house: dict[str, Any]) -> None:
    eve_id = eve(migrated)
    path = f"/admin/members/{eve_id}/preferences"
    response = client.get(path)
    assert response.status_code == 404 and "Eve" not in response.text
    assert htmx(client, f"{path}/stance", {"target": f"tag:{house['thai']}", "level": "veto"}).status_code == 404
    for suffix, data in (
        ("/settings", {"can_cook": "on"}),
        ("/needs", {"needs": [str(house["veg"])]}),
        ("/fun", {"fun": "off"}),
    ):
        assert submit(client, f"{path}{suffix}", data).status_code == 404, suffix
    evil = next(m for m in rows(migrated, MemberRow) if m.id == eve_id)
    assert (evil.can_cook, evil.dietary_needs, evil.fun_mode) == (False, [], True)
    assert not [p for p in rows(migrated, PreferenceRow) if p.member_id == eve_id]
    assert not [a for a in rows(migrated, AuditRow) if a.target == f"member:{eve_id}"]


def test_guests_and_missing_members_have_no_preferences_page(
    client: TestClient, migrated: Settings, house: dict[str, Any]
) -> None:
    assert submit(client, "/admin/guests", {"display_name": "Pat"}).status_code == 303
    pat = int(re.findall(r'id="guest-(\d+)"', client.get("/admin/members").text)[-1])
    for member_id in (pat, 99999):
        path = f"/admin/members/{member_id}/preferences"
        assert client.get(path).status_code == 404, member_id
        assert submit(client, f"{path}/fun", {"fun": "off"}).status_code == 404, member_id
    assert f"/admin/members/{pat}/preferences" not in client.get("/admin/members").text
    for bad in ("0", "-3", "9" * 25):
        assert client.get(f"/admin/members/{bad}/preferences").status_code == 404, bad


def test_the_admins_own_id_goes_to_their_own_page(client: TestClient, house: dict[str, Any]) -> None:
    response = client.get(base(house, "admin"))
    assert (response.status_code, response.headers["location"]) == (303, "/preferences")


def test_a_members_own_changes_stay_unaudited(
    client: TestClient, provider: FakeProvider, migrated: Settings, house: dict[str, Any]
) -> None:
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    htmx(client, "/preferences/stance", {"target": f"tag:{house['thai']}", "level": "2"})
    submit(client, "/preferences/settings", {"can_order": "on"})
    submit(client, "/me/fun", {"fun": "off"})
    assert not any_admin_audit(migrated)
