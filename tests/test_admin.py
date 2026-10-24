# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Members."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow, SessionRow
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.test_auth_flow import member, rows


def sign_in_as(client: TestClient, provider: FakeProvider, sub: str, email: str) -> None:
    """Sign out whoever is signed in, then sign in as someone else."""
    if "hx-headers" in client.get("/").text:
        client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    response = sign_in(client, provider, claims={"sub": sub, "email": email})
    assert response.status_code == 303, page(response)


def audit_actions(settings: Settings) -> list[str]:
    return [a.action for a in rows(settings, AuditRow)]


def post(client: TestClient, path: str, data: dict[str, str] | None = None, **headers: str) -> httpx.Response:
    return client.post(path, data=(data or {}) | {"csrf_token": csrf_from(client)}, headers=headers)


def test_admin_sees_members_page(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    response = client.get("/admin/members")
    assert response.status_code == 200
    assert "admin@example.com" in response.text
    assert "Invite someone" in response.text


def test_new_member_form_defaults_to_member(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    html = client.get("/admin/members").text
    select = html[html.index('<select name="role">') : html.index("</select>", html.index('<select name="role">'))]
    assert '<option value="member" selected>' in select
    assert '<option value="admin">' in select
    assert select.index('value="member"') < select.index('value="admin"')


def test_invite_creates_member_and_audit_row(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    response = invite(client, "  Sam@Example.com ", " Sam  Smith ", can_cook=True, can_order=True)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/members?notice=invited"
    sam = member(migrated, "sam@example.com")
    assert (sam.display_name, sam.role, sam.active, sam.oidc_subject) == ("Sam Smith", "member", True, None)
    assert (sam.can_cook, sam.can_pickup, sam.can_order) == (True, False, True)
    assert not sam.mute_dm_reminders and not sam.feedback_reminder
    listing = page(client.get("/admin/members?notice=invited"))
    assert "Invitation saved" in listing and "Invited" in listing
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "member.invited"]
    assert audit.actor_member_id == member(migrated, "admin@example.com").id
    assert audit.target == f"member:{sam.id}"


def test_invited_member_can_sign_in(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sign_in_as(client, provider, "subject-sam", "SAM@example.com")
    assert "Hi, Sam" in page(client.get("/"))
    assert member(migrated, "sam@example.com").oidc_subject == "subject-sam"


def test_duplicate_email_is_rejected(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    response = invite(client, "SAM@example.com", "Sam again")
    assert response.status_code == 400
    assert "already exists" in page(response)
    assert 'value="Sam again"' in response.text  # the form keeps what was typed


def test_invalid_invite_is_rejected(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    assert invite(client, "not-an-email", "Sam").status_code == 400
    assert invite(client, "sam@example.com", "   ").status_code == 400
    assert invite(client, "sam@example.com", "Sam", role="owner").status_code == 400
    assert invite(client, "sam@example.com", "x" * 81).status_code == 400
    assert len(rows(migrated, MemberRow)) == 1


def test_member_gets_403_on_every_admin_route(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    admin_id = member(migrated, "admin@example.com").id
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    token = csrf_from(client)
    assert client.get("/admin/members").status_code == 403
    assert client.get(f"/admin/members/{admin_id}").status_code == 403
    for path in ("/admin/members", f"/admin/members/{admin_id}", f"/admin/members/{admin_id}/deactivate"):
        response = client.post(path, data={"csrf_token": token, "display_name": "x", "email": "x@example.com"})
        assert response.status_code == 403, path
    assert member(migrated, "admin@example.com").active
    assert "Members" not in client.get("/").text.split("<main")[0]


def test_edit_member(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    assert "Sam" in client.get(f"/admin/members/{sam.id}").text
    data = {"display_name": "Samantha", "email": "samantha@example.com", "role": "admin", "can_pickup": "on"}
    response = post(client, f"/admin/members/{sam.id}", data)
    assert response.status_code == 303
    updated = member(migrated, "samantha@example.com")
    assert (updated.display_name, updated.role, updated.can_pickup, updated.can_cook) == (
        "Samantha",
        "admin",
        True,
        False,
    )
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "member.updated"]
    assert set(audit.payload) == {"display_name", "email", "role", "can_pickup"}


def test_linked_member_email_is_locked(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    admin_id = member(migrated, "admin@example.com").id
    sign_in(client, provider)
    assert "disabled" in client.get(f"/admin/members/{admin_id}").text
    response = post(
        client, f"/admin/members/{admin_id}", {"display_name": "Boss", "email": "new@example.com"} | {"role": "admin"}
    )
    assert response.status_code == 400
    # A disabled field is not posted at all; the email stays as it was.
    response = post(client, f"/admin/members/{admin_id}", {"display_name": "Boss", "role": "admin"})
    assert response.status_code == 303
    assert member(migrated, "admin@example.com").display_name == "Boss"


def test_edit_unknown_member_is_404(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    assert client.get("/admin/members/9999").status_code == 404


def test_deactivate_and_reactivate(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    sign_in_as(client, provider, "subject-admin", "admin@example.com")
    sam = member(migrated, "sam@example.com")
    sam_sessions = [s for s in rows(migrated, SessionRow) if s.member_id == sam.id]
    assert sam_sessions == []  # signed out above

    response = post(client, f"/admin/members/{sam.id}/deactivate")
    assert response.status_code == 303
    assert not member(migrated, "sam@example.com").active
    assert "Deactivated" in client.get("/admin/members").text

    response = post(client, f"/admin/members/{sam.id}/reactivate")
    assert response.status_code == 303
    assert member(migrated, "sam@example.com").active
    assert audit_actions(migrated).count("member.deactivated") == 1
    assert audit_actions(migrated).count("member.reactivated") == 1


def test_deactivation_ends_existing_sessions(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    from fastapi.testclient import TestClient as Client

    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    with Client(
        client.app, base_url=str(client.base_url), headers={"Origin": "https://dinnerbell.test"}, follow_redirects=False
    ) as sam_browser:
        sign_in(sam_browser, provider, claims={"sub": "subject-sam", "email": "sam@example.com"})
        assert "Hi, Sam" in page(sam_browser.get("/"))
        sam = member(migrated, "sam@example.com")
        assert any(s.member_id == sam.id for s in rows(migrated, SessionRow))
        post(client, f"/admin/members/{sam.id}/deactivate")
        # The service deletes the rows itself; this is checked before Sam's next request, which
        # would also drop the session through the inactive-member check.
        assert all(s.member_id != sam.id for s in rows(migrated, SessionRow))
        assert 'href="/auth/login"' in sam_browser.get("/").text


def test_session_of_member_deactivated_outside_the_app_is_refused(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    """The per-request guard: a session whose member is inactive is refused even if its row
    survived (for example, a member deactivated directly in the database)."""
    from sqlalchemy import create_engine, update
    from sqlalchemy.orm import Session

    sign_in(client, provider)
    engine = create_engine(migrated.sync_database_url)
    try:
        with Session(engine) as db:
            db.execute(update(MemberRow).where(MemberRow.email == "admin@example.com").values(active=False))
            db.commit()
    finally:
        engine.dispose()
    assert len(rows(migrated, SessionRow)) == 1
    assert client.get("/admin/members").status_code == 303
    assert 'href="/auth/login"' in client.get("/").text
    assert rows(migrated, SessionRow) == []


def test_web_admin_guard_holds_without_the_service_guard(
    client: TestClient, provider: FakeProvider, migrated: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The web layer's require_admin must refuse members on its own, not lean on the service."""
    from dinnerbell.services import members as member_service

    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    monkeypatch.setattr(member_service, "require_admin", lambda _actor, _message: None)
    assert client.get("/admin/members").status_code == 403
    response = post(client, "/admin/members", {"display_name": "Eve", "email": "eve@example.com"})
    assert response.status_code == 403
    assert all(m.email != "eve@example.com" for m in rows(migrated, MemberRow))


def test_htmx_deactivate_returns_row_fragment(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    response = client.post(
        f"/admin/members/{sam.id}/deactivate",
        headers={"HX-Request": "true", "X-CSRF-Token": csrf_from(client)},
    )
    assert response.status_code == 200
    assert response.text.lstrip().startswith(f'<li class="member inactive" id="member-{sam.id}"')
    assert "Reactivate" in response.text


def test_cannot_deactivate_yourself(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "other@example.com", "Other", role="admin")
    admin_id = member(migrated, "admin@example.com").id
    response = post(client, f"/admin/members/{admin_id}/deactivate")
    assert response.status_code == 409
    assert "deactivate yourself" in page(response)
    assert member(migrated, "admin@example.com").active


def test_cannot_change_own_role(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "other@example.com", "Other", role="admin")
    admin_id = member(migrated, "admin@example.com").id
    response = post(client, f"/admin/members/{admin_id}", {"display_name": "Admin", "role": "member"})
    assert response.status_code == 400
    assert member(migrated, "admin@example.com").role == "admin"


def test_last_admin_rules_in_service(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "co@example.com", "Co", role="admin")
    sign_in_as(client, provider, "subject-co", "co@example.com")
    admin = member(migrated, "admin@example.com")
    # Demote the original admin: fine, co remains.
    response = post(client, f"/admin/members/{admin.id}", {"display_name": "Admin", "role": "member"})
    assert response.status_code == 303
    # Co is the last admin and cannot deactivate or demote themself.
    co = member(migrated, "co@example.com")
    assert post(client, f"/admin/members/{co.id}/deactivate").status_code == 409
    assert (
        post(
            client, f"/admin/members/{co.id}", {"display_name": "Co", "email": "co@example.com", "role": "member"}
        ).status_code
        == 400
    )
    assert member(migrated, "co@example.com").role == "admin"


def test_htmx_rule_violation_returns_row_with_error(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    admin_id = member(migrated, "admin@example.com").id
    response = client.post(
        f"/admin/members/{admin_id}/deactivate",
        headers={"HX-Request": "true", "X-CSRF-Token": csrf_from(client)},
    )
    assert response.status_code == 409
    assert 'role="alert"' in response.text


def test_edit_error_keeps_what_the_admin_typed(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    invite(client, "taken@example.com", "Taken")
    sam = member(migrated, "sam@example.com")
    data = {"display_name": "Samantha Q", "email": "taken@example.com", "role": "member", "can_order": "on"}
    response = post(client, f"/admin/members/{sam.id}", data)
    assert response.status_code == 400
    assert "already exists" in page(response)
    assert 'value="Samantha Q"' in response.text
    assert 'value="taken@example.com"' in response.text
    assert 'name="can_order" checked' in response.text
    assert member(migrated, "sam@example.com").display_name == "Sam"


def test_invalid_role_on_edit_is_a_friendly_error(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    response = post(
        client, f"/admin/members/{sam.id}", {"display_name": "Sam", "email": "sam@example.com", "role": "owner"}
    )
    assert response.status_code == 400
    assert "Choose a role" in page(response)
