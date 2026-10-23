# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Members: each member's Slack link, set and cleared by the admin (FR-42)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.test_admin import post, sign_in_as
from tests.test_auth_flow import member, rows


def slack_audits(settings: Settings) -> list[tuple[str, dict[str, object]]]:
    return [(a.action, a.payload) for a in rows(settings, AuditRow) if a.action.startswith("member.slack")]


def test_admin_links_and_unlinks_a_member(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    assert "Slack: not linked yet" in page(client.get("/admin/members"))

    response = post(client, f"/admin/members/{sam.id}/slack", {"slack_user_id": " u02sam "})
    assert (response.status_code, response.headers["location"]) == (303, "/admin/members?notice=slack_set")
    assert member(migrated, "sam@example.com").slack_user_id == "U02SAM"
    listing = page(client.get("/admin/members?notice=slack_set"))
    assert "Slack link saved." in listing and "linked as <code>U02SAM</code> (set by hand)" in listing
    assert "U02SAM" in page(client.get(f"/admin/members/{sam.id}"))

    post(client, f"/admin/members/{sam.id}/slack", {"slack_user_id": "U02SAM"})  # unchanged: no audit row
    response = post(client, f"/admin/members/{sam.id}/slack", {"slack_user_id": ""})
    assert response.headers["location"] == "/admin/members?notice=slack_cleared"
    assert member(migrated, "sam@example.com").slack_user_id is None
    assert "Slack: unlinked (set by hand)" in page(client.get("/admin/members"))
    admin_id = member(migrated, "admin@example.com").id
    assert slack_audits(migrated) == [
        ("member.slack_user_set", {"from": None, "to": "U02SAM"}),
        ("member.slack_user_cleared", {"from": "U02SAM", "to": None}),
    ]
    assert {a.actor_member_id for a in rows(migrated, AuditRow) if a.action.startswith("member.slack")} == {admin_id}


def test_bad_or_taken_slack_ids_are_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    invite(client, "kim@example.com", "Kim")
    sam, kim = member(migrated, "sam@example.com"), member(migrated, "kim@example.com")
    for bad in ("C0CHANNEL", "U", "U02<script>", "x" * 40):
        response = post(client, f"/admin/members/{sam.id}/slack", {"slack_user_id": bad})
        assert response.status_code == 400, bad
        assert "Copy member ID" in page(response)
    post(client, f"/admin/members/{sam.id}/slack", {"slack_user_id": "U02SAM"})
    response = post(client, f"/admin/members/{kim.id}/slack", {"slack_user_id": "U02SAM"})
    assert response.status_code == 400
    assert "Sam is already linked to that Slack user." in page(response)
    assert 'value="U02SAM"' in response.text  # what the admin typed stays in the field
    assert member(migrated, "kim@example.com").slack_user_id is None


def test_only_an_admin_with_a_csrf_token_can_link(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    assert client.post(f"/admin/members/{sam.id}/slack", data={"slack_user_id": "U02SAM"}).status_code == 403
    assert post(client, "/admin/members/999999/slack", {"slack_user_id": "U02SAM"}).status_code == 404
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    response = client.post(
        f"/admin/members/{sam.id}/slack", data={"slack_user_id": "U0HIJACK", "csrf_token": csrf_from(client)}
    )
    assert response.status_code == 403
    assert member(migrated, "sam@example.com").slack_user_id is None
    assert slack_audits(migrated) == []


def test_back_to_automatic_unlocks_the_link(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    assert "Back to automatic" not in page(client.get(f"/admin/members/{sam.id}"))
    post(client, f"/admin/members/{sam.id}/slack", {"slack_user_id": "U02SAM"})
    assert "Back to automatic" in page(client.get(f"/admin/members/{sam.id}"))
    assert client.post(f"/admin/members/{sam.id}/slack/automatic").status_code == 403  # no CSRF token
    response = post(client, f"/admin/members/{sam.id}/slack/automatic")
    assert response.headers["location"] == "/admin/members?notice=slack_automatic"
    unlocked = member(migrated, "sam@example.com")
    assert (unlocked.slack_user_id, unlocked.slack_link_locked) == ("U02SAM", False)
    assert slack_audits(migrated)[-1] == ("member.slack_link_unlocked", {"slack_user_id": "U02SAM"})
    post(client, f"/admin/members/{sam.id}/slack/automatic")  # already automatic: nothing more
    assert len(slack_audits(migrated)) == 2

    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    response = client.post(f"/admin/members/{sam.id}/slack/automatic", data={"csrf_token": csrf_from(client)})
    assert response.status_code == 403
