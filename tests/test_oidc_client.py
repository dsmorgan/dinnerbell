# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edge cases in the OIDC client that the browser round trip doesn't reach."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from dinnerbell.web.oidc import OIDCClient, OIDCError, pkce_challenge
from tests.support.clock import FakeClock
from tests.support.fake_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, REDIRECT_URI, FakeProvider

pytestmark = pytest.mark.anyio


def client_for(handler: Any, clock: FakeClock, issuer: str = ISSUER) -> OIDCClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OIDCClient(
        issuer=issuer,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes="openid email",
        redirect_uri=REDIRECT_URI,
        http=http,
        clock=clock,
    )


def discovery(**overrides: Any) -> dict[str, Any]:
    doc = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize?prompt=login",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }
    doc.update(overrides)
    return {k: v for k, v in doc.items() if v is not None}


def serving(doc: dict[str, Any], token: httpx.Response | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=doc)
        if request.url.path == "/token" and token is not None:
            return token
        return httpx.Response(404)

    return handler


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        (discovery(issuer="https://other.test"), "issuer does not match"),
        (discovery(token_endpoint="http://idp.test/token"), "token_endpoint must use https"),
        (discovery(jwks_uri=None), "lacks jwks_uri"),
        (discovery(id_token_signing_alg_values_supported=["HS256", "none"]), "no acceptable"),
        (discovery(token_endpoint_auth_methods_supported=["private_key_jwt"]), "neither client_secret_post"),
        (discovery(token_endpoint_auth_methods_supported="client_secret_post"), "malformed"),
        (discovery(id_token_signing_alg_values_supported="RS256 HS256"), "malformed"),
        (discovery(code_challenge_methods_supported="S256"), "malformed"),
    ],
)
async def test_bad_discovery_documents(clock: FakeClock, doc: dict[str, Any], reason: str) -> None:
    with pytest.raises(OIDCError, match=reason):
        await client_for(serving(doc), clock).metadata()


async def test_defaults_when_optional_fields_absent(clock: FakeClock) -> None:
    metadata = await client_for(serving(discovery()), clock).metadata()
    assert metadata.client_auth == "client_secret_basic"
    assert not metadata.supports_pkce
    assert "RS256" in metadata.algorithms and "HS256" not in metadata.algorithms


async def test_metadata_is_cached(clock: FakeClock) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=discovery())

    oidc = client_for(handler, clock)
    await oidc.metadata()
    await oidc.metadata()
    assert len(calls) == 1


async def test_authorization_url_keeps_existing_query(clock: FakeClock) -> None:
    request = await client_for(serving(discovery()), clock).authorization_request(state="s", nonce="n")
    assert request.url.startswith(f"{ISSUER}/authorize?prompt=login&response_type=code")
    assert request.code_verifier is None


async def test_local_http_issuer_allows_http_endpoints(clock: FakeClock) -> None:
    local = "http://localhost:9000"
    doc = discovery(
        issuer=local, authorization_endpoint=f"{local}/a", token_endpoint=f"{local}/t", jwks_uri=f"{local}/k"
    )
    assert (await client_for(serving(doc), clock, issuer=local).metadata()).token_endpoint == f"{local}/t"


@pytest.mark.parametrize(
    ("response", "reason", "unavailable"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), "invalid_grant", False),
        (httpx.Response(200, json={"ok": False, "error": "invalid_code"}), "invalid_code", False),
        (httpx.Response(200, json={"access_token": "x"}), "no id_token", False),
        (httpx.Response(200, text="not json"), "invalid JSON", True),
        (httpx.Response(200, json=["list"]), "non-object", True),
        (httpx.Response(502, text="<html>Bad gateway</html>"), "HTTP 502", True),
        (httpx.Response(503, json={"error": "temporarily_unavailable"}), "HTTP 503", True),
        (httpx.Response(400, text="Bad Request"), "invalid JSON", True),
    ],
)
async def test_token_endpoint_failures(
    clock: FakeClock, response: httpx.Response, reason: str, unavailable: bool
) -> None:
    """A provider outage (5xx, non-JSON) shows the 502 page; a refused code shows the 400 page."""
    oidc = client_for(serving(discovery(), token=response), clock)
    with pytest.raises(OIDCError, match=reason) as caught:
        await oidc.exchange_code("code", None)
    assert caught.value.provider_unavailable is unavailable


async def test_network_failures_are_provider_unavailable(clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(OIDCError) as caught:
        await client_for(handler, clock).metadata()
    assert caught.value.provider_unavailable


async def test_token_network_failure(clock: FakeClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=discovery())

    with pytest.raises(OIDCError) as caught:
        await client_for(handler, clock).exchange_code("code", "verifier")
    assert caught.value.provider_unavailable


async def test_basic_auth_encodes_credentials(clock: FakeClock) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            seen["auth"] = request.headers["authorization"]
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"id_token": "x.y.z"})
        return httpx.Response(200, json=discovery())

    assert await client_for(handler, clock).exchange_code("code", "verifier") == "x.y.z"
    assert seen["auth"].startswith("Basic ")
    assert "client_secret" not in seen["body"]
    assert "code_verifier=verifier" in seen["body"]


async def test_invalid_jwks(clock: FakeClock, provider: FakeProvider) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [{"kty": "nope"}]})
        return httpx.Response(200, json=discovery())

    token = provider.sign({"iss": ISSUER})
    with pytest.raises(OIDCError, match="JWKS is invalid"):
        await client_for(handler, clock).verify_id_token(token, nonce="n")


async def test_garbage_id_token(clock: FakeClock, provider: FakeProvider) -> None:
    jwks = provider.app  # reuse the provider's JWKS through ASGI
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=jwks))
    oidc = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes="openid email",
        redirect_uri=REDIRECT_URI,
        http=http,
        clock=clock,
    )
    with pytest.raises(OIDCError, match="signature invalid"):
        await oidc.verify_id_token("not-a-jwt", nonce="n")


def test_pkce_challenge_matches_rfc7636_example() -> None:
    assert (
        pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
