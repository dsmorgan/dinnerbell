# SPDX-License-Identifier: AGPL-3.0-or-later
"""The full OpenID Connect round trip against the in-process fake provider."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MemberRow, SessionRow
from dinnerbell.services.auth import hash_token
from tests.conftest import csrf_from, invite, page, sign_in, start_login
from tests.support.fake_oidc import DROP, REDIRECT_URI, FakeProvider

SESSION_COOKIE = "__Host-dinnerbell_session"
LOGIN_COOKIE = "__Host-dinnerbell_login"


def rows(settings: Settings, model: Any) -> list[Any]:
    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as db:
            return list(db.scalars(select(model)))
    finally:
        engine.dispose()


def member(settings: Settings, email: str) -> MemberRow:
    return next(m for m in rows(settings, MemberRow) if m.email == email)


def refusals(settings: Settings) -> list[AuditRow]:
    return [a for a in rows(settings, AuditRow) if a.action == "auth.sign_in_refused"]


def test_login_redirects_with_state_nonce_and_pkce(client: TestClient) -> None:
    response = client.get("/auth/login")
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    params = dict(parse_qsl(location.query))
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://idp.test/authorize"
    assert params["redirect_uri"] == REDIRECT_URI
    assert params["scope"] == "openid email profile"
    assert len(params["state"]) >= 40 and len(params["nonce"]) >= 40
    assert params["code_challenge_method"] == "S256"
    assert "team" not in params
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{LOGIN_COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", "Max-Age=600"):
        assert attribute in cookie
    assert params["state"] not in cookie  # signed and encoded, never the raw value alone


def test_first_sign_in_links_invited_member(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    response = sign_in(client, provider, claims={"email": "Admin@Example.com"})

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(SESSION_COOKIE))
    for attribute in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/", f"Max-Age={90 * 24 * 3600}"):
        assert attribute in session_cookie
    assert any(c.startswith(f"{LOGIN_COOKIE}=;") and "Max-Age=0" in c for c in cookies)

    admin = member(migrated, "admin@example.com")
    assert admin.oidc_subject == "subject-admin"
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None
    stored = rows(migrated, SessionRow)
    assert [s.id for s in stored] == [hash_token(token)]
    assert token not in {s.id for s in stored}
    assert "Hi, admin" in page(client.get("/"))
    assert any(a.action == "member.linked" for a in rows(migrated, AuditRow))


def test_later_sign_ins_match_on_subject_not_email(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    response = sign_in(client, provider, claims={"email": "changed@elsewhere.example", "email_verified": False})
    assert response.status_code == 303
    assert member(migrated, "admin@example.com").oidc_subject == "subject-admin"


def test_pkce_verifier_is_sent_and_checked(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    form = provider.token_requests[-1]["form"]
    assert len(form["code_verifier"]) >= 43


def test_no_pkce_when_provider_does_not_offer_it(client: TestClient, provider: FakeProvider) -> None:
    provider.supports_pkce = False
    params = start_login(client)
    assert "code_challenge" not in params
    code = provider.authorize(params)
    response = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert response.status_code == 303
    assert "code_verifier" not in provider.token_requests[-1]["form"]


def test_client_secret_post_is_used_when_basic_is_not_offered(client: TestClient, provider: FakeProvider) -> None:
    provider.auth_methods = ["client_secret_post"]
    assert sign_in(client, provider).status_code == 303
    request = provider.token_requests[-1]
    assert request["authorization"] is None
    assert request["form"]["client_secret"] == "client-secret-value"


def test_unknown_email_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    response = sign_in(client, provider, claims={"sub": "stranger", "email": "stranger@example.com"})
    assert response.status_code == 403
    assert "hasn't invited you" in page(response)
    assert SESSION_COOKIE not in client.cookies
    assert rows(migrated, SessionRow) == []
    [audit] = refusals(migrated)
    assert audit.payload["reason"] == "unknown_email"


def test_unverified_email_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    response = sign_in(client, provider, claims={"email_verified": False})
    assert response.status_code == 403
    assert "isn't verified" in page(response)
    assert member(migrated, "admin@example.com").oidc_subject is None
    assert [a.payload["reason"] for a in refusals(migrated)] == ["email_not_verified"]


@pytest.mark.parametrize("verified", ["true", True])
def test_email_verified_accepts_string_true(client: TestClient, provider: FakeProvider, verified: object) -> None:
    assert sign_in(client, provider, claims={"email_verified": verified}).status_code == 303


def test_missing_email_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    response = sign_in(client, provider, claims={"email": DROP})
    assert response.status_code == 403
    assert [a.payload["reason"] for a in refusals(migrated)] == ["email_not_verified"]


def test_inactive_member_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    sam = member(migrated, "sam@example.com")
    client.post(f"/admin/members/{sam.id}/deactivate", data={"csrf_token": csrf_from(client)})
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})

    response = sign_in(client, provider, claims={"sub": "subject-sam", "email": "sam@example.com"})
    assert response.status_code == 403
    assert "deactivated" in page(response)
    assert [a.payload["reason"] for a in refusals(migrated)] == ["inactive"]


def test_inactive_linked_member_is_refused_by_subject(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    sign_in(client, provider, claims={"sub": "subject-sam", "email": "sam@example.com"})
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    sign_in(client, provider)
    sam = member(migrated, "sam@example.com")
    client.post(f"/admin/members/{sam.id}/deactivate", data={"csrf_token": csrf_from(client)})
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})

    response = sign_in(client, provider, claims={"sub": "subject-sam", "email": "sam@example.com"})
    assert response.status_code == 403
    assert refusals(migrated)[-1].payload["reason"] == "inactive"


def test_email_already_linked_to_another_subject_is_refused(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider)
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    response = sign_in(client, provider, claims={"sub": "impostor"})
    assert response.status_code == 403
    assert refusals(migrated)[-1].payload["reason"] == "email_linked_elsewhere"


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": "T-HOUSE"}])
def test_wrong_slack_team_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    response = sign_in(client, provider, claims={"https://slack.com/team_id": "T-OTHER"})
    assert response.status_code == 403
    assert "different Slack workspace" in page(response)
    assert member(migrated, "admin@example.com").oidc_subject is None
    assert [a.payload["reason"] for a in refusals(migrated)] == ["wrong_team"]


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": "T-HOUSE"}])
def test_missing_slack_team_is_refused_when_required(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    assert sign_in(client, provider).status_code == 403


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": "T-HOUSE"}])
def test_right_slack_team_signs_in_and_stores_slack_user(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    params = start_login(client)
    assert params["team"] == "T-HOUSE"
    code = provider.authorize(
        params, claims={"https://slack.com/team_id": "T-HOUSE", "https://slack.com/user_id": "U123"}
    )
    response = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert response.status_code == 303
    assert member(migrated, "admin@example.com").slack_user_id == "U123"


def test_slack_user_id_is_stored_without_team_requirement(
    client: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    sign_in(client, provider, claims={"https://slack.com/user_id": "U999"})
    assert member(migrated, "admin@example.com").slack_user_id == "U999"


def test_state_mismatch_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    params = start_login(client)
    code = provider.authorize(params)
    response = client.get("/auth/callback", params={"code": code, "state": "forged-state"})
    assert response.status_code == 400
    assert "Sign-in expired" in page(response)
    assert provider.token_requests == []  # the code was never redeemed
    assert rows(migrated, SessionRow) == []


def test_missing_login_cookie_is_refused(client: TestClient, provider: FakeProvider) -> None:
    params = start_login(client)
    code = provider.authorize(params)
    client.cookies.delete(LOGIN_COOKIE)
    response = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert response.status_code == 400


def test_tampered_login_cookie_is_refused(client: TestClient, provider: FakeProvider) -> None:
    params = start_login(client)
    code = provider.authorize(params)
    value = client.cookies[LOGIN_COOKIE]
    body, mac = value.split(".")
    client.cookies.set(LOGIN_COOKIE, f"{body}.{mac[:-2]}AA", domain="dinnerbell.test")
    response = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert response.status_code == 400


def test_expired_login_state_is_refused(client: TestClient, provider: FakeProvider, clock: Any) -> None:
    from datetime import timedelta

    params = start_login(client)
    code = provider.authorize(params)
    clock.advance(timedelta(minutes=11))
    response = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert response.status_code == 400


def test_nonce_mismatch_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    response = sign_in(client, provider, claims={"nonce": "some-other-nonce"})
    assert response.status_code == 400
    assert "couldn't be verified" in page(response)
    assert rows(migrated, SessionRow) == []
    assert member(migrated, "admin@example.com").oidc_subject is None


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://evil.test"},
        {"aud": "someone-else"},
        {"aud": ["dinnerbell-client", "someone-else"]},
        {"azp": "someone-else"},
        {"exp": 1_000_000_000},
        {"iat": DROP},
        {"exp": DROP},
        {"sub": DROP},
        {"sub": ""},
        {"nonce": DROP},
    ],
    ids=[
        "issuer",
        "audience",
        "multi-aud-no-azp",
        "azp",
        "expired",
        "no-iat",
        "no-exp",
        "no-sub",
        "empty-sub",
        "no-nonce",
    ],
)
def test_invalid_id_token_claims_are_refused(
    client: TestClient, provider: FakeProvider, migrated: Settings, claims: dict[str, Any]
) -> None:
    response = sign_in(client, provider, claims=claims)
    assert response.status_code == 400
    assert rows(migrated, SessionRow) == []


def test_multiple_audiences_with_matching_azp_are_accepted(client: TestClient, provider: FakeProvider) -> None:
    claims = {"aud": ["dinnerbell-client", "other"], "azp": "dinnerbell-client"}
    assert sign_in(client, provider, claims=claims).status_code == 303


def test_token_signed_by_unknown_key_is_refused(
    client: TestClient, provider: FakeProvider, other_key: RSAKey, migrated: Settings
) -> None:
    response = sign_in(client, provider, signing_key=other_key)
    assert response.status_code == 400
    assert rows(migrated, SessionRow) == []


def test_unsigned_token_is_refused(client: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    import base64
    import json

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    params = start_login(client)
    code = provider.authorize(params)
    pending = provider.codes[code]
    forged = f"{part({'alg': 'none'})}.{part(pending.claims)}."
    provider.sign = lambda *_a, **_k: forged  # type: ignore[method-assign]
    response = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert response.status_code == 400
    assert rows(migrated, SessionRow) == []


def test_rotated_key_is_fetched_once(client: TestClient, provider: FakeProvider, clock: Any) -> None:
    from datetime import timedelta

    from tests.support.fake_oidc import generate_key

    sign_in(client, provider)
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    fetches = provider.jwks_fetches
    rotated = generate_key("key-2")
    provider.extra_keys.append(rotated)

    # Within a minute of the last fetch an unknown kid does not trigger another fetch.
    assert sign_in(client, provider, signing_key=rotated).status_code == 400
    assert provider.jwks_fetches == fetches

    clock.advance(timedelta(minutes=2))
    assert sign_in(client, provider, signing_key=rotated).status_code == 303
    assert provider.jwks_fetches == fetches + 1


def test_provider_error_param_is_handled(client: TestClient) -> None:
    start_login(client)
    response = client.get("/auth/callback", params={"error": "access_denied", "state": "x"})
    assert response.status_code == 400
    assert "access_denied" not in page(response)


def test_missing_code_is_refused(client: TestClient) -> None:
    params = start_login(client)
    assert client.get("/auth/callback", params={"state": params["state"]}).status_code == 400


def test_code_cannot_be_replayed(client: TestClient, provider: FakeProvider) -> None:
    params = start_login(client)
    code = provider.authorize(params)
    assert client.get("/auth/callback", params={"code": code, "state": params["state"]}).status_code == 303
    client.post("/auth/logout", data={"csrf_token": csrf_from(client)})
    client.cookies.clear()
    replay = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert replay.status_code == 400


def test_provider_down_shows_friendly_page(client: TestClient, provider: FakeProvider) -> None:
    provider.available = False
    response = client.get("/auth/login")
    assert response.status_code == 502
    assert "sign-in provider" in page(response)


def test_discovery_issuer_mismatch_blocks_sign_in(client: TestClient, provider: FakeProvider) -> None:
    provider.discovery_issuer = "https://evil.test"
    assert client.get("/auth/login").status_code == 502


def test_signed_in_user_visiting_login_goes_home(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    response = client.get("/auth/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_token_endpoint_outage_shows_provider_page(client: TestClient, provider: FakeProvider) -> None:
    params = start_login(client)
    code = provider.authorize(params)
    provider.token_outage = 504
    response = client.get("/auth/callback", params={"code": code, "state": params["state"]})
    assert response.status_code == 502
    assert "sign-in provider" in page(response)


def test_wrong_client_secret_is_diagnosable(
    client: TestClient, provider: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A 4xx from the token endpoint is a refusal, not an outage, and the log names it."""
    import logging

    provider.client_secret = "rotated-at-the-provider"
    with caplog.at_level(logging.WARNING):
        response = sign_in(client, provider)
    assert response.status_code == 400
    assert "couldn't be verified" in page(response)
    reasons = [getattr(r, "reason", "") for r in caplog.records if r.getMessage() == "sign-in verification failed"]
    assert reasons == ["token endpoint refused the request: HTTP 401, error=invalid_client"]
