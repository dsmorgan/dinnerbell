# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web app's pages (#138, spec 6.15): Admin > Web app, the manifest, the service worker,
**Get the app**, and the Slack, Web app, or Both choice on Preferences, for a member and for an
admin editing one (#181). Off, none of it exists. The outbox and the services are in
``test_web_app_delivery``."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow
from dinnerbell.web.routes.web_app import WORKER_CONTENT_SECURITY_POLICY, WORKER_FILE
from tests.conftest import BASE_URL, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.pages import inline_code
from tests.support.web import submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows

APP_PATHS = ("/manifest.webmanifest", "/sw.js", "/app")
SAM = ("subject-sam", "sam@example.com")
MANIFEST_LINK = '<link rel="manifest" href="/manifest.webmanifest">'


def switch(client: TestClient, on: bool) -> None:
    response = submit(client, "/admin/web-app", {"on": "on"} if on else {})
    assert (response.status_code, response.headers["location"]) == (
        303,
        f"/admin/web-app?notice={'on' if on else 'off'}",
    )


def link_slack(client: TestClient, member_id: int, slack_user_id: str) -> None:
    assert submit(client, f"/admin/members/{member_id}/slack", {"slack_user_id": slack_user_id}).status_code == 303


def card(html: str) -> str:
    return html.split('id="notify-via"', 1)[1].split("</section>", 1)[0]


def audits(settings: Settings, action: str) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action == action]


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    return client


@pytest.fixture
def sam(admin: TestClient, migrated: Settings) -> int:
    assert invite(admin, SAM[1], "Sam").status_code == 303
    return int(member(migrated, SAM[1]).id)


# Off ---------------------------------------------------------------------------------------------


def test_off_by_default_nothing_exists(admin: TestClient, sam: int, migrated: Settings) -> None:
    """A new household is off: the app's routes are the usual 404 signed in and out, the choice
    isn't on Preferences, and no page links the manifest or Get the app."""
    for path in APP_PATHS:
        response = admin.get(path)
        assert response.status_code == 404, path
        assert "That page doesn't exist." in page(response), path
    for path in ("/", "/preferences", f"/admin/members/{sam}/preferences", "/admin/web-app", "/catalog"):
        html = admin.get(path).text
        assert MANIFEST_LINK not in html and 'id="notify-via"' not in html and 'href="/app"' not in html, path
    for path, data in (("/preferences/notify", {"via": "web"}), (f"/admin/members/{sam}/preferences/notify", {})):
        assert submit(admin, path, data).status_code == 404, path
    assert member(migrated, SAM[1]).notify_via is None and member(migrated, "admin@example.com").notify_via is None
    signed_out = TestClient(admin.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    for path in APP_PATHS:
        assert signed_out.get(path).status_code == 404, path
    home = signed_out.get("/").text
    assert MANIFEST_LINK not in home and 'href="/app"' not in home


OFF_PAGES = ("/", "/preferences", "/catalog", "/history", "/stats", "/schedule", "/admin/members", "/admin/schedule")


def test_off_pages_are_exactly_as_before(admin: TestClient, sam: int) -> None:
    """Turning it on and off again leaves every page as it was, an admin's own included. (Only
    Admin's tabs differ from a release without the web app: they list **Web app**.)"""
    paths = (*OFF_PAGES, f"/admin/members/{sam}/preferences")
    before = {path: admin.get(path).text for path in paths}
    signed_out = TestClient(admin.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    home = signed_out.get("/").text
    switch(admin, True)
    assert {path: admin.get(path).text for path in paths} != before
    switch(admin, False)
    assert {path: admin.get(path).text for path in paths} == before
    assert signed_out.get("/").text == home


# Admin > Web app ---------------------------------------------------------------------------------


def test_the_admin_turns_it_on_and_off_and_each_change_is_audited(admin: TestClient, migrated: Settings) -> None:
    html = admin.get("/admin/members").text
    assert '<a href="/admin/web-app">Web app</a>' in html
    settings_page = admin.get("/admin/web-app")
    assert settings_page.status_code == 200
    assert 'name="on" value="on" aria-describedby="web-app-about">' in settings_page.text  # unchecked
    assert inline_code(settings_page.text)[1] == []
    assert 'id="not-built-yet"' not in settings_page.text  # push and code sign-in both exist now
    note = page(settings_page).split('id="web-app-pushes">', 1)[1].split("</p>", 1)[0]
    assert "get push notifications" in note and "sign in with a code" in note
    assert "nothing changes for members" in page(settings_page)
    switch(admin, True)
    on = page(admin.get("/admin/web-app?notice=on"))
    assert "The web app is on." in on and 'value="on" aria-describedby="web-app-about" checked>' in on
    assert '<a href="/admin/web-app" aria-current="page">Web app</a>' in on
    switch(admin, True)  # unchanged: no second row
    switch(admin, False)
    assert "The web app is off." in page(admin.get("/admin/web-app?notice=off"))
    admin_id = member(migrated, "admin@example.com").id
    assert [(a.actor_member_id, a.payload) for a in audits(migrated, "household.web_app_updated")] == [
        (admin_id, {"on": True}),
        (admin_id, {"on": False}),
    ]


def test_only_an_admin_with_a_csrf_token_reaches_the_switch(
    admin: TestClient, provider: FakeProvider, sam: int, migrated: Settings
) -> None:
    assert admin.post("/admin/web-app", data={"on": "on"}).status_code == 403  # no token
    sign_in_as(admin, provider, *SAM)
    assert admin.get("/admin/web-app").status_code == 403
    assert submit(admin, "/admin/web-app", {"on": "on"}).status_code == 403
    assert '<a href="/admin/web-app">' not in admin.get("/").text
    assert audits(migrated, "household.web_app_updated") == []
    signed_out = TestClient(admin.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    assert signed_out.get("/admin/web-app").status_code == 303
    assert signed_out.post("/admin/web-app", data={"on": "on"}).status_code == 303
    sign_in_as(admin, provider, "subject-admin", "admin@example.com")
    assert (
        admin.post("/admin/web-app", data={"on": "on"}, headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert audits(migrated, "household.web_app_updated") == []


# The installable app -----------------------------------------------------------------------------


def test_the_manifest_names_dinnerbell_and_its_icons(admin: TestClient) -> None:
    switch(admin, True)
    response = admin.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/manifest+json"
    body = json.loads(response.text)
    version = body["icons"][0]["src"].split("?v=", 1)[1]
    assert body == {
        "id": "/",
        "name": "Dinnerbell",
        "short_name": "Dinnerbell",
        "lang": "en",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#faf6f0",
        "theme_color": "#faf6f0",
        "icons": [
            {"src": f"/static/icon-{n}.png?v={version}", "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any"}
            for n in (192, 512)
        ],
    }
    for icon in body["icons"]:
        served = admin.get(icon["src"])
        assert (served.status_code, served.headers["content-type"]) == (200, "image/png"), icon["src"]


@pytest.mark.parametrize("settings_overrides", [{"instance_label": "Test"}])
def test_a_labeled_instance_has_its_label_and_the_test_mascot(admin: TestClient) -> None:
    """On a phone with both installed, test and production differ by name and icon."""
    title = '<meta name="apple-mobile-web-app-title" content="Dinnerbell">'
    assert title in admin.get("/").text  # off: as before
    switch(admin, True)
    body = json.loads(admin.get("/manifest.webmanifest").text)
    assert (body["name"], body["short_name"]) == ("Dinnerbell Test", "Dinnerbell Test")
    assert [icon["src"].split("?")[0] for icon in body["icons"]] == [
        "/static/icon-test-192.png",
        "/static/icon-test-512.png",
    ]
    assert '<meta name="apple-mobile-web-app-title" content="Dinnerbell Test">' in admin.get("/").text
    worker = admin.get("/sw.js").text
    assert 'var ICON = "/static/icon-test-192.png";' in worker and "/static/icon-192.png" not in worker


def test_signed_out_the_app_follows_the_installs_household(admin: TestClient) -> None:
    """The browser fetches the manifest without cookies, and a member without Slack installs
    the app before they can sign in, so a visitor with no session gets the install's household."""
    signed_out = TestClient(admin.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, follow_redirects=False)
    switch(admin, True)
    for path in APP_PATHS:
        assert signed_out.get(path).status_code == 200, path
    home = signed_out.get("/").text
    assert MANIFEST_LINK in home and '<p class="small"><a href="/app">Get the app</a></p>' in home
    switch(admin, False)
    for path in APP_PATHS:
        assert signed_out.get(path).status_code == 404, path


def test_the_service_worker_has_its_own_policy_and_caches_nothing(admin: TestClient) -> None:
    switch(admin, True)
    response = admin.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"
    policy = response.headers["content-security-policy"]
    assert policy == WORKER_CONTENT_SECURITY_POLICY
    directives = [part.strip() for part in policy.split(";")]
    assert "connect-src 'self'" in directives and "default-src 'none'" in directives
    assert "sandbox" not in policy and "http" not in policy and "*" not in policy
    assert all(d.split()[1:] in (["'self'"], ["'none'"]) for d in directives)  # nothing outside the origin
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "service-worker-allowed" not in response.headers  # its scope is the root it's served from
    worker = response.text
    assert worker == WORKER_FILE.read_text()
    assert 'var ICON = "/static/icon-192.png";' in worker
    assert worker.splitlines()[0] == "// SPDX-License-Identifier: AGPL-3.0-or-later"
    for handler in ('addEventListener("push"', 'addEventListener("notificationclick"', "openWindow"):
        assert handler in worker
    for caching in ('addEventListener("fetch"', "caches.", "importScripts"):
        assert caching not in worker  # it doesn't cache pages or load anything


def test_get_the_app_shows_the_steps_and_every_page_links_the_manifest(admin: TestClient) -> None:
    switch(admin, True)
    response = admin.get("/app")
    assert response.status_code == 200
    html = page(response)
    assert "<h1>Get the app</h1>" in html
    assert 'id="iphone"' in html and "Add to Home Screen" in html
    assert 'id="android"' in html and "Add to Home screen" in html
    assert 'class="card stack in-app" id="installed"' in html  # where Turn on notifications goes
    assert inline_code(response.text)[1] == []
    for path in ("/", "/preferences", "/catalog", "/app", "/admin/web-app"):
        assert admin.get(path).text.count(MANIFEST_LINK) == 1, path
    assert MANIFEST_LINK not in admin.get("/catalog", headers={"HX-Request": "true"}).text
    # The icons and theme color every page had already stay as they were.
    home = admin.get("/").text
    assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=' in home
    assert 'name="theme-color"' in home


# Each member's choice ----------------------------------------------------------------------------


def test_a_member_without_slack_has_only_the_web_app(admin: TestClient, migrated: Settings) -> None:
    switch(admin, True)
    prefs = admin.get("/preferences").text
    assert 'name="via"' not in card(prefs)
    assert "Without a linked Slack account, Dinnerbell can reach you only in the web app." in page(
        admin.get("/preferences")
    )
    assert '<a href="/app">Get the app</a>' in card(prefs)
    assert inline_code(prefs)[1] == []
    for via in ("slack", "both"):
        response = submit(admin, "/preferences/notify", {"via": via})
        assert response.status_code == 400, via
        assert "Without a Slack account, Dinnerbell can reach you only in the web app." in page(response)
    assert submit(admin, "/preferences/notify", {"via": "web"}).status_code == 303
    assert member(migrated, "admin@example.com").notify_via is None  # what counts already: nothing stored


def test_a_member_with_slack_starts_on_both_and_chooses(admin: TestClient, migrated: Settings) -> None:
    switch(admin, True)
    me = member(migrated, "admin@example.com").id
    link_slack(admin, me, "U01ADMIN")
    prefs = admin.get("/preferences").text
    assert 'action="/preferences/notify"' in card(prefs)
    assert 'name="via" value="both" checked' in card(prefs) and card(prefs).count(" checked") == 1
    assert card(prefs).index('value="slack"') < card(prefs).index('value="web"') < card(prefs).index('value="both"')
    response = submit(admin, "/preferences/notify", {"via": "web"})
    assert (response.status_code, response.headers["location"]) == (303, "/preferences?notice=notify#notify-via")
    saved = admin.get(response.headers["location"]).text
    assert '<p class="alert ok" role="status">Saved.</p>' in card(saved)
    assert 'name="via" value="web" checked' in card(saved)
    assert member(migrated, "admin@example.com").notify_via == "web"
    assert audits(migrated, "member.notify_via_set") == []  # your own choice isn't audited
    for data in ({}, {"via": ""}, {"via": "Slack"}, {"via": "push"}):
        assert submit(admin, "/preferences/notify", data).status_code == 400, data
    assert submit(admin, "/preferences/notify", {"via": "slack-x"}).status_code == 400  # too long to be one
    assert admin.post("/preferences/notify", data={"via": "both"}).status_code == 403  # no token
    assert member(migrated, "admin@example.com").notify_via == "web"
    # Unlinked, they're on the web app whatever they chose; linked again, their choice is back.
    link_slack(admin, me, "")
    assert 'name="via"' not in card(admin.get("/preferences").text)
    link_slack(admin, me, "U01ADMIN")
    assert 'name="via" value="web" checked' in card(admin.get("/preferences").text)


def test_preferences_turns_on_this_devices_notifications(admin: TestClient, migrated: Settings) -> None:
    """Inside the installed app, Turn on notifications is on Preferences, beside the choice, and a
    banner on every page points there until the member answers; a member on Slack is told why
    their device gets nothing."""
    switch(admin, True)
    me = member(migrated, "admin@example.com").id
    link_slack(admin, me, "U01ADMIN")
    assert submit(admin, "/preferences/notify", {"via": "slack"}).status_code == 303  # Both by default
    prefs = admin.get("/preferences").text
    device = card(prefs).split('id="notifications"', 1)[1]
    assert "hidden" in device.split(">", 1)[0] and "Turn on notifications" in device
    assert 'id="push-install" hidden' in device and '<a href="/app">Get the app</a> shows how.' in device
    assert 'id="push-slack"' in device  # on Slack: this device gets nothing
    assert 'id="push-nudge"' not in admin.get("/").text
    assert inline_code(prefs)[1] == []
    assert submit(admin, "/preferences/notify", {"via": "both"}).status_code == 303
    prefs = admin.get("/preferences").text
    assert 'id="push-slack"' not in prefs and 'id="notifications"' in card(prefs)
    today = admin.get("/").text
    nudge = today.split('id="push-nudge"', 1)[1].split("</p>", 1)[0]
    assert nudge.startswith(" hidden>") and 'href="/preferences#notify-via"' in nudge
    app = admin.get("/app").text  # the same controls, without a link to the page they're on
    assert 'id="push-install" hidden' in app and "shows how." not in app
    switch(admin, False)
    assert 'id="push-nudge"' not in admin.get("/").text and 'id="notifications"' not in admin.get("/preferences").text


def test_an_admin_never_sees_another_members_device_controls(admin: TestClient, sam: int, migrated: Settings) -> None:
    switch(admin, True)
    html = admin.get(f"/admin/members/{sam}/preferences").text
    assert 'id="notify-via"' in html and 'id="notifications"' not in html


def test_an_admin_sets_a_members_choice_and_it_is_audited(
    admin: TestClient, provider: FakeProvider, sam: int, migrated: Settings
) -> None:
    switch(admin, True)
    base = f"/admin/members/{sam}/preferences"
    html = page(admin.get(base))
    assert "Without a linked Slack account, Dinnerbell can reach them only in the web app." in html
    assert submit(admin, f"{base}/notify", {"via": "slack"}).status_code == 400
    link_slack(admin, sam, "U02SAM")
    theirs = admin.get(base).text
    assert f'action="{base}/notify"' in card(theirs) and 'name="via" value="both" checked' in card(theirs)
    response = submit(admin, f"{base}/notify", {"via": "slack"})
    assert (response.status_code, response.headers["location"]) == (303, f"{base}?notice=notify#notify-via")
    assert 'name="via" value="slack" checked' in card(admin.get(response.headers["location"]).text)
    assert member(migrated, SAM[1]).notify_via == "slack"
    admin_id = member(migrated, "admin@example.com").id
    assert [(a.actor_member_id, a.target, a.payload) for a in audits(migrated, "member.notify_via_set")] == [
        (admin_id, f"member:{sam}", {"from": "both", "to": "slack"})
    ]
    # Sam isn't an admin: their own choice is theirs, and another member's is refused.
    sign_in_as(admin, provider, *SAM)
    assert submit(admin, f"{base}/notify", {"via": "web"}).status_code == 403
    assert 'name="via" value="slack" checked' in card(admin.get("/preferences").text)
    assert member(migrated, SAM[1]).notify_via == "slack"


def test_guests_get_no_choice(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    switch(admin, True)
    assert submit(admin, "/admin/guests", {"display_name": "Pat"}).status_code == 303
    guest = next(m for m in rows(migrated, MemberRow) if m.display_name == "Pat")
    assert admin.get(f"/admin/members/{guest.id}/preferences").status_code == 404
    assert submit(admin, f"/admin/members/{guest.id}/preferences/notify", {"via": "web"}).status_code == 404


def test_the_choice_is_hidden_again_once_it_is_off(admin: TestClient, migrated: Settings) -> None:
    switch(admin, True)
    link_slack(admin, member(migrated, "admin@example.com").id, "U01ADMIN")
    assert submit(admin, "/preferences/notify", {"via": "slack"}).status_code == 303
    switch(admin, False)
    assert 'id="notify-via"' not in admin.get("/preferences").text
    assert submit(admin, "/preferences/notify", {"via": "web"}).status_code == 404
    assert member(migrated, "admin@example.com").notify_via == "slack"  # kept for when it's on again


def test_a_page_renders_without_the_app_when_the_switch_cant_be_read(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    switch(admin, True)

    async def broken(*_: object) -> bool:
        raise RuntimeError("database is locked")

    monkeypatch.setattr("dinnerbell.web.render.web_app_enabled_for", broken)
    response = admin.get("/catalog")
    assert response.status_code == 200 and MANIFEST_LINK not in response.text
    assert "could not read the web app switch" in caplog.text


@pytest.mark.parametrize("value", ["", "yes", "1", "true", "On", "off"])
def test_only_the_checkboxs_own_value_turns_it_on(admin: TestClient, migrated: Settings, value: str) -> None:
    response = submit(admin, "/admin/web-app", {"on": value})
    assert (response.status_code, response.headers["location"]) == (303, "/admin/web-app?notice=off")
    assert admin.get("/app").status_code == 404
    assert audits(migrated, "household.web_app_updated") == []
