# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side sessions: sliding expiry, expiry, sign-out."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import SessionRow
from tests.conftest import csrf_from, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.test_auth_flow import SESSION_COOKIE, rows


def session_row(settings: Settings) -> SessionRow:
    [row] = rows(settings, SessionRow)
    return row


def test_signed_out_visitor_sees_sign_in_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/auth/login"' in response.text
    assert "hx-headers" not in response.text


def test_session_slides_forward_when_used(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    sign_in(client, provider)
    created = session_row(migrated)
    assert created.expires_at == clock.now + timedelta(days=90)

    clock.advance(timedelta(minutes=30))
    response = client.get("/")
    assert "set-cookie" not in response.headers  # too soon to renew
    assert session_row(migrated).expires_at == created.expires_at

    clock.advance(timedelta(days=60))
    response = client.get("/")
    assert "Hi, admin" in page(response)
    renewed = session_row(migrated)
    assert renewed.expires_at == clock.now + timedelta(days=90)
    assert renewed.last_seen == clock.now
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(SESSION_COOKIE) and f"Max-Age={90 * 24 * 3600}" in cookie


def test_session_expires_after_90_idle_days(
    client: TestClient, provider: FakeProvider, migrated: Settings, clock: FakeClock
) -> None:
    sign_in(client, provider)
    clock.advance(timedelta(days=90))
    response = client.get("/")
    assert 'href="/auth/login"' in response.text
    assert rows(migrated, SessionRow) == []
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_unknown_session_token_is_cleared(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "not-a-real-token", domain="dinnerbell.test")
    response = client.get("/")
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert client.get("/admin/members").status_code == 303


def test_sign_out_deletes_the_session(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    token = client.cookies[SESSION_COOKIE]
    response = client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    assert response.status_code == 303
    assert rows(migrated, SessionRow) == []
    assert "Max-Age=0" in response.headers["set-cookie"]

    client.cookies.set(SESSION_COOKIE, token, domain="dinnerbell.test")  # a stolen copy is now useless
    assert 'href="/auth/login"' in client.get("/").text


def test_sign_out_without_session_just_redirects(client: TestClient) -> None:
    response = client.post("/auth/logout")
    assert response.status_code == 303


def test_sign_out_requires_csrf_token(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    assert client.post("/auth/logout").status_code == 403
    assert len(rows(migrated, SessionRow)) == 1


def test_protected_page_redirects_when_signed_out(client: TestClient) -> None:
    response = client.get("/admin/members")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_htmx_request_without_session_gets_hx_redirect(client: TestClient) -> None:
    response = client.get("/admin/members", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/"
