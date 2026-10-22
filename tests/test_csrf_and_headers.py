# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSRF tokens, Origin checks, and security headers."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import MemberRow
from dinnerbell.web.middleware import SECURITY_HEADERS
from tests.conftest import BASE_URL, csrf_from, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.test_auth_flow import rows

INVITE = {"display_name": "Sam", "email": "sam@example.com", "role": "member"}


def invited(settings: Settings) -> bool:
    return any(m.email == "sam@example.com" for m in rows(settings, MemberRow))


def test_form_without_csrf_token_is_rejected(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    response = client.post("/admin/members", data=INVITE)
    assert response.status_code == 403
    assert not invited(migrated)


def test_form_with_wrong_csrf_token_is_rejected(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    response = client.post("/admin/members", data=INVITE | {"csrf_token": "0" * 64})
    assert response.status_code == 403
    assert not invited(migrated)


def test_csrf_token_from_another_session_is_rejected(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    old_token = csrf_from(client)
    client.post("/auth/logout", data={"csrf_token": old_token})
    sign_in(client, provider)
    assert client.post("/admin/members", data=INVITE | {"csrf_token": old_token}).status_code == 403
    assert not invited(migrated)


def test_htmx_header_token_is_accepted(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    response = client.post("/admin/members", data=INVITE, headers={"X-CSRF-Token": csrf_from(client)})
    assert response.status_code == 303
    assert invited(migrated)


def test_wrong_htmx_header_token_is_rejected(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    response = client.post("/admin/members", data=INVITE, headers={"X-CSRF-Token": "nope"})
    assert response.status_code == 403
    assert not invited(migrated)


def test_cross_origin_post_is_rejected_even_with_valid_token(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    token = csrf_from(client)
    response = client.post(
        "/admin/members", data=INVITE | {"csrf_token": token}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert not invited(migrated)


def test_null_origin_is_rejected(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    token = csrf_from(client)
    response = client.post("/admin/members", data=INVITE | {"csrf_token": token}, headers={"Origin": "null"})
    assert response.status_code == 403


def test_referer_is_used_when_origin_is_absent(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    token = csrf_from(client)
    del client.headers["Origin"]
    bad = client.post(
        "/admin/members", data=INVITE | {"csrf_token": token}, headers={"Referer": "https://evil.example/x"}
    )
    assert bad.status_code == 403
    missing = client.post("/admin/members", data=INVITE | {"csrf_token": token})
    assert missing.status_code == 403
    good = client.post(
        "/admin/members", data=INVITE | {"csrf_token": token}, headers={"Referer": f"{BASE_URL}/admin/members"}
    )
    assert good.status_code == 303
    assert invited(migrated)


def test_origin_with_different_port_is_rejected(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    token = csrf_from(client)
    response = client.post(
        "/admin/members", data=INVITE | {"csrf_token": token}, headers={"Origin": f"{BASE_URL}:8443"}
    )
    assert response.status_code == 403


def test_security_headers_on_pages(client: TestClient) -> None:
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "camera=()" in response.headers["permissions-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    assert "server" not in response.headers


def test_security_headers_on_errors_redirects_and_static(client: TestClient) -> None:
    for response in (client.get("/nope"), client.get("/admin/members"), client.get("/static/app.css")):
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.css").headers.get("cache-control") != "no-store"


def test_pages_have_no_inline_script(client: TestClient, provider: FakeProvider) -> None:
    import re

    sign_in(client, provider)
    for path in ("/", "/admin/members"):
        html = client.get(path).text
        scripts = re.findall(r"<script([^>]*)>(.*?)</script>", html, flags=re.S)
        assert scripts, path
        for attributes, body in scripts:
            assert 'src="/static/' in attributes
            assert body.strip() == ""
        assert " style=" not in html
        assert not re.search(r"\son[a-z]+=", html), f"inline event handler on {path}"


def test_vendored_htmx_is_served(client: TestClient) -> None:
    response = client.get("/static/htmx-2.0.11.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text[:200]


def test_unknown_route_renders_404_page(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert "Not found" in response.text


def test_method_not_allowed(client: TestClient) -> None:
    assert client.get("/auth/logout").status_code == 405


def test_unhandled_error_page_keeps_security_headers(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Regression: the 500 page was rendered outside the middleware and lost every header."""

    async def boom() -> None:
        raise RuntimeError("kaboom with secret detail")

    client.app.add_api_route("/boom", boom)  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR):
        response = client.get("/boom")
    assert response.status_code == 500
    assert "Something went wrong" in response.text
    assert "kaboom" not in response.text
    for header in SECURITY_HEADERS:
        assert header.lower() in response.headers, header
    assert response.headers["cache-control"] == "no-store"
    request_id = response.headers["x-request-id"]
    [record] = [r for r in caplog.records if r.getMessage() == "unhandled error"]
    assert record.exc_info is not None
    from dinnerbell.logs import JsonFormatter

    assert json.loads(JsonFormatter().format(record))["request_id"] == request_id


def test_validation_errors_render_pages_without_echoing_input(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    response = client.get("/admin/members/not-a-number")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    marker = "ECHO-MARKER-" + "x" * 400  # over the form field limit
    response = client.post(
        "/admin/members",
        data={"display_name": marker, "email": "a@example.com", "csrf_token": csrf_from(client)},
    )
    assert response.status_code == 400
    assert "text/html" in response.headers["content-type"]
    assert "ECHO-MARKER" not in response.text
    assert "frame-ancestors" in response.headers["content-security-policy"]
