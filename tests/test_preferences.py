# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preferences: each member's own stances and settings, private from everyone else (FR-27 to FR-29)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow, PreferenceRow, TagRow
from dinnerbell.domain.preferences import MINUS
from dinnerbell.logs import JsonFormatter
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, htmx, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows
from tests.test_catalog_web import item_row
from tests.test_logging_and_cli import quiet_loggers  # noqa: F401  (fixture)


def thai(settings: Settings) -> int:
    return next(t.id for t in rows(settings, TagRow) if t.name == "Thai")


def stances(settings: Settings, email: str) -> dict[tuple[str, int], tuple[bool, int]]:
    who = member(settings, email).id
    return {
        ("tag", p.tag_id) if p.tag_id else ("item", p.item_id or 0): (p.veto, p.score)
        for p in rows(settings, PreferenceRow)
        if p.member_id == who
    }


def checked_level(html: str, target: str) -> str | None:
    import re

    row = html.split(f'id="pref-{target.replace(":", "-")}"', 1)[1].split("</li>", 1)[0]
    match = re.search(r'value="([^"]+)" checked', row)
    return match.group(1) if match else None


def test_page_lists_tags_and_items(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden", new_tags="spicy")
    add_item(client, "Tacos", kind="home", cuisines="Mexican")
    response = client.get("/preferences")
    text = page(response)
    assert "Only you can see these." in text
    for heading in ("Cuisine", "Other", "Restaurants", "Home meals"):
        assert f"<h2>{heading}</h2>" in text
    assert checked_level(response.text, f"tag:{thai(migrated)}") == "0"
    assert "Veto" in text and f"{MINUS}2" in text and "+2" in text


def test_empty_catalog(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    assert "nothing to rate yet" in page(client.get("/preferences"))


def test_set_stance_with_htmx(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    target = f"tag:{thai(migrated)}"
    response = htmx(client, "/preferences/stance", {"target": target, "level": "veto"})
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<li") and "Saved" in response.text
    assert checked_level(response.text, target) == "veto"
    assert stances(migrated, "admin@example.com") == {("tag", thai(migrated)): (True, 0)}
    htmx(client, "/preferences/stance", {"target": target, "level": "-1"})
    assert stances(migrated, "admin@example.com") == {("tag", thai(migrated)): (False, -1)}
    htmx(client, "/preferences/stance", {"target": target, "level": "0"})
    assert stances(migrated, "admin@example.com") == {}  # neutral isn't stored
    assert checked_level(client.get("/preferences").text, target) == "0"


def test_set_stance_without_javascript(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    item = item_row(migrated, "Siam Garden")
    response = submit(client, "/preferences/stance", {"target": f"item:{item.id}", "level": "2"})
    assert response.status_code == 303
    assert response.headers["location"] == f"/preferences?notice=saved#pref-item-{item.id}"
    after = client.get(response.headers["location"])
    assert checked_level(after.text, f"item:{item.id}") == "2"
    assert "Preference saved." in page(after)


def test_bad_stances_are_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    target = f"tag:{thai(migrated)}"
    bad_level = htmx(client, "/preferences/stance", {"target": target, "level": "5"})
    assert bad_level.status_code == 200 and "Not saved." in bad_level.text  # the row, with the error
    assert submit(client, "/preferences/stance", {"target": target, "level": "5"}).status_code == 400
    assert htmx(client, "/preferences/stance", {"target": "member:1", "level": "1"}).status_code == 400
    assert htmx(client, "/preferences/stance", {"target": "tag:abc", "level": "1"}).status_code == 400
    for missing in ("tag:999", "item:999"):
        response = htmx(client, "/preferences/stance", {"target": missing, "level": "1"})
        assert response.status_code == 200 and "removed from the catalog" in response.text
        assert submit(client, "/preferences/stance", {"target": missing, "level": "1"}).status_code == 404
    assert rows(migrated, PreferenceRow) == []


def test_inactive_items_are_listed_only_if_rated(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    add_item(client, "Rated")
    add_item(client, "Unrated")
    rated, unrated = item_row(migrated, "Rated"), item_row(migrated, "Unrated")
    htmx(client, "/preferences/stance", {"target": f"item:{rated.id}", "level": "veto"})
    for item in (rated, unrated):
        submit(client, f"/catalog/items/{item.id}/deactivate")
    text = page(client.get("/preferences"))
    assert "Rated" in text and "Unrated" not in text


def test_settings(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    response = submit(client, "/preferences/settings", {"can_cook": "on", "can_order": "on", "feedback_reminder": "on"})
    assert response.status_code == 303
    assert "Settings saved." in page(client.get(response.headers["location"]))
    me = member(migrated, "admin@example.com")
    assert (me.can_cook, me.can_pickup, me.can_order, me.mute_dm_reminders, me.feedback_reminder) == (
        True,
        False,
        True,
        False,
        True,
    )
    submit(client, "/preferences/settings", {"mute_dm_reminders": "on"})
    me = member(migrated, "admin@example.com")
    assert (me.can_cook, me.can_order, me.mute_dm_reminders, me.feedback_reminder) == (False, False, True, False)
    assert 'name="mute_dm_reminders" checked' in client.get("/preferences").text


# Privacy (FR-29) ------------------------------------------------------------------------------------


@pytest.fixture
def sam_vetoes_thai(client: TestClient, provider: FakeProvider, migrated: Settings) -> int:
    """Sam vetoes Thai and scores an item, then the admin signs back in."""
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    invite(client, "sam@example.com", "Sam", can_cook=True)
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    htmx(client, "/preferences/stance", {"target": f"tag:{thai(migrated)}", "level": "veto"})
    item = item_row(migrated, "Siam Garden")
    htmx(client, "/preferences/stance", {"target": f"item:{item.id}", "level": "-2"})
    sign_in_as(client, provider, "subject-admin", "admin@example.com")
    return member(migrated, "sam@example.com").id


def test_admin_never_sees_another_members_preferences(
    client: TestClient, migrated: Settings, sam_vetoes_thai: int
) -> None:
    sam_id = sam_vetoes_thai
    target = f"tag:{thai(migrated)}"
    for path in ("/preferences", f"/preferences?member_id={sam_id}", f"/preferences?member={sam_id}"):
        response = client.get(path)
        assert checked_level(response.text, target) == "0", path
        assert 'value="veto" checked' not in response.text
    # No other page shows a stance either.
    for path in ("/catalog", "/catalog/tags", "/history", f"/admin/members/{sam_id}", "/admin/members"):
        text = page(client.get(path))
        assert "veto" not in text.lower(), path


def test_admin_cannot_write_another_members_preferences(
    client: TestClient, migrated: Settings, sam_vetoes_thai: int
) -> None:
    sam_id = sam_vetoes_thai
    before = stances(migrated, "sam@example.com")
    target = f"tag:{thai(migrated)}"
    for extra in ({"member_id": str(sam_id)}, {"member": str(sam_id)}, {"target": f"member:{sam_id}"}):
        htmx(client, "/preferences/stance", {"target": target, "level": "2"} | extra)
    submit(client, "/preferences/settings", {"member_id": str(sam_id)})
    assert stances(migrated, "sam@example.com") == before
    assert stances(migrated, "admin@example.com") == {("tag", thai(migrated)): (False, 2)}
    sam = member(migrated, "sam@example.com")
    assert sam.can_cook  # the settings post changed only the admin
    assert not [r for r in rows(migrated, MemberRow) if r.id == sam_id and not r.active]


def test_member_sees_only_their_own(
    client: TestClient, provider: FakeProvider, migrated: Settings, sam_vetoes_thai: int
) -> None:
    htmx(client, "/preferences/stance", {"target": f"tag:{thai(migrated)}", "level": "1"})
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    assert checked_level(client.get("/preferences").text, f"tag:{thai(migrated)}") == "veto"


@pytest.mark.usefixtures("quiet_loggers")
def test_preferences_are_not_audited_or_logged(
    client: TestClient, migrated: Settings, sam_vetoes_thai: int, caplog: pytest.LogCaptureFixture
) -> None:
    assert not [a for a in rows(migrated, AuditRow) if "pref" in a.action or "veto" in str(a.payload)]
    with caplog.at_level(logging.DEBUG):
        htmx(client, "/preferences/stance", {"target": f"tag:{thai(migrated)}", "level": "veto"})
    formatted = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "veto" not in formatted


# Failed saves are never silent (HTMX) --------------------------------------------------------


def test_invalid_level_rerenders_the_row_with_the_stored_stance(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    target = f"tag:{thai(migrated)}"
    htmx(client, "/preferences/stance", {"target": target, "level": "-1"})
    response = htmx(client, "/preferences/stance", {"target": target, "level": "7"})
    assert response.status_code == 200  # HTMX swaps only successful responses
    assert response.text.lstrip().startswith("<li")
    assert checked_level(response.text, target) == "-1"
    assert 'role="alert"' in response.text and "Not saved." in page(response)
    assert "Saved" not in response.text.replace("Not saved", "")


def test_removed_target_rerenders_the_row_with_an_error(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai, Siamese")
    siamese = next(t.id for t in rows(migrated, TagRow) if t.name == "Siamese")
    submit(client, f"/catalog/tags/{siamese}/merge", {"into": str(thai(migrated)), "confirm": "on"})
    response = htmx(client, "/preferences/stance", {"target": f"tag:{siamese}", "level": "veto"})
    assert response.status_code == 200
    assert f'id="pref-tag-{siamese}"' in response.text
    assert "Not saved. It was removed from the catalog; reload the page." in page(response)
    assert rows(migrated, PreferenceRow) == []


def test_expired_session_sends_htmx_to_sign_in(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    add_item(client, "Siam Garden")
    token = csrf_from(client)
    client.cookies.clear()
    response = client.post(
        "/preferences/stance",
        data={"target": f"tag:{thai(migrated)}", "level": "veto"},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/"


def test_pages_load_the_save_error_banner(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    html = client.get("/preferences").text
    assert '<script src="/static/app.js' in html
    assert 'id="save-error"' in html and "hidden" in html.split('id="save-error"', 1)[1].split(">", 1)[0]
    script = client.get("/static/app.js").text
    assert "htmx:responseError" in script and "htmx:sendError" in script
    assert "Not saved. Reload the page." in script
