# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light, dark, or auto (#231): each person's choice, where they make it, and what every page's
``<html>``, ``color-scheme``, and ``theme-color`` carry with each choice. The admin's path and a
guest's page are in ``test_admin_preferences`` and ``test_guests_web``."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow
from tests.conftest import ADMIN_EMAIL, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.web import submit
from tests.test_auth_flow import member, rows

AUTO_HEAD = (
    '<meta name="color-scheme" content="light dark">\n'
    '  <meta name="theme-color" content="#fbf7f2" media="(prefers-color-scheme: light)">\n'
    '  <meta name="theme-color" content="#1b1714" media="(prefers-color-scheme: dark)">\n'
)
FORCED_HEAD = {
    "light": '<meta name="color-scheme" content="light">\n  <meta name="theme-color" content="#FAF6F0">\n',
    "dark": '<meta name="color-scheme" content="dark">\n  <meta name="theme-color" content="#1C1714">\n',
}
SIGNED_IN = ("/", "/preferences", "/catalog", "/history", "/stats", "/schedule", "/admin/members")


def card(html: str) -> str:
    return html.split('id="color-mode"', 1)[1].split("</form>", 1)[0]


def assert_auto(html: str, where: str = "") -> None:
    assert '<html lang="en">' in html and "data-theme" not in html, where
    assert AUTO_HEAD in html and html.count('name="theme-color"') == 2, where


def test_auto_is_the_default_and_the_page_follows_the_device(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    assert member(migrated, ADMIN_EMAIL).color_mode == "auto"
    for path in SIGNED_IN:
        assert_auto(client.get(path).text, path)
    prefs = client.get("/preferences").text
    assert 'action="/me/color-mode" class="card stack" id="color-mode"' in prefs
    assert 'name="mode" value="auto" checked' in card(prefs)
    assert card(prefs).count(" checked") == 1
    # Auto first, said to follow the device, and the card sits beside fun mode.
    assert card(prefs).index('value="auto"') < card(prefs).index('value="light"') < card(prefs).index('value="dark"')
    assert "Auto matches your device" in card(prefs)
    assert prefs.index('id="fun-mode"') < prefs.index('id="color-mode"') < prefs.index("<h2>How it works</h2>")


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_light_or_dark_sets_the_page_and_its_metas(
    client: TestClient, provider: FakeProvider, migrated: Settings, mode: str
) -> None:
    sign_in(client, provider)
    before, audits = client.get("/catalog").text, len(rows(migrated, AuditRow))
    response = submit(client, "/me/color-mode", {"mode": mode})
    assert (response.status_code, response.headers["location"]) == (303, "/preferences?notice=color#color-mode")
    saved = page(client.get(response.headers["location"]))
    assert '<p class="alert ok" role="status">Saved.</p>' in card(saved)  # the notice is at the card
    assert "Saved." not in saved.split('id="color-mode"', 1)[0] and "Settings saved." not in saved
    assert f'name="mode" value="{mode}" checked' in card(saved) and card(saved).count(" checked") == 1
    assert member(migrated, ADMIN_EMAIL).color_mode == mode
    for path in SIGNED_IN:
        html = client.get(path).text
        assert f'<html lang="en" data-theme="{mode}">' in html, path
        assert FORCED_HEAD[mode] in html and html.count('name="theme-color"') == 1, path
        assert "prefers-color-scheme" not in html, path
    assert len(rows(migrated, AuditRow)) == audits  # a personal setting, not audited

    # Back to auto: the page is exactly as it was.
    assert submit(client, "/me/color-mode", {"mode": "auto"}).status_code == 303
    assert client.get("/catalog").text == before
    assert member(migrated, ADMIN_EMAIL).color_mode == "auto"


def test_it_changes_only_your_own(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    assert invite(client, "sam@example.com", "Sam").status_code == 303
    sam = str(member(migrated, "sam@example.com").id)
    assert submit(client, "/me/color-mode", {"mode": "dark", "member_id": sam}).status_code == 303
    assert member(migrated, ADMIN_EMAIL).color_mode == "dark"
    assert member(migrated, "sam@example.com").color_mode == "auto"


def test_the_choice_needs_csrf_and_a_known_mode(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    assert client.post("/me/color-mode", data={"mode": "dark"}).status_code == 403
    assert member(migrated, ADMIN_EMAIL).color_mode == "auto"
    for data in ({}, {"mode": ""}, {"mode": "Dark"}, {"mode": "night"}, {"mode": "on"}):
        response = submit(client, "/me/color-mode", data)
        assert response.status_code == 400, data
        assert "Not a light, dark, or auto setting." in page(response)
        assert_auto(response.text, str(data))
    assert submit(client, "/me/color-mode", {"mode": "auto-dark"}).status_code == 400  # too long to be one
    assert member(migrated, ADMIN_EMAIL).color_mode == "auto"


def test_signed_out_pages_follow_the_device(client: TestClient, provider: FakeProvider) -> None:
    assert_auto(client.get("/").text, "sign-in")
    missing = client.get("/no-such-page")
    assert missing.status_code == 404
    assert_auto(missing.text, "error")
    response = client.post("/me/color-mode", data={"mode": "dark"})
    assert (response.status_code, response.headers["location"]) == (303, "/")
    # A choice belongs to the person, not the browser: signed out, the page is on auto again.
    sign_in(client, provider)
    submit(client, "/me/color-mode", {"mode": "dark"})
    assert 'data-theme="dark"' in client.get("/").text
    submit(client, "/auth/logout")
    assert_auto(client.get("/").text, "after sign-out")


def test_a_signed_in_error_page_keeps_the_choice(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    submit(client, "/me/color-mode", {"mode": "light"})
    missing = client.get("/events/99999")
    assert missing.status_code == 404
    assert '<html lang="en" data-theme="light">' in missing.text


def test_the_den_follows_the_choice_too() -> None:
    """fun.css keys the den's dark colors on the same attribute app.css does (#230)."""
    css = (Path(__file__).parents[1] / "src/dinnerbell/web/static/fun.css").read_text()
    assert ':root:not([data-theme="light"]) body[data-fun]' in css
    assert ':root[data-theme="dark"] body[data-fun]' in css
