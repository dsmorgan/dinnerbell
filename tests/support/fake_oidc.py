# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-process OpenID Connect provider for tests.

It serves discovery, JWKS, and a token endpoint, and issues ID tokens signed with a generated
key. Tests play the user's part at the authorization endpoint by calling ``authorize``.
This module lives under tests/ and is never packaged or shipped in the image.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from dinnerbell.services.clock import Clock

ISSUER = "https://idp.test"
CLIENT_ID = "dinnerbell-client"
CLIENT_SECRET = "client-secret-value"
REDIRECT_URI = "https://dinnerbell.test/auth/callback"


@dataclass
class PendingCode:
    nonce: str
    code_challenge: str | None
    redirect_uri: str
    claims: dict[str, Any]
    header: dict[str, Any] | None
    signing_key: RSAKey | None


@dataclass
class FakeProvider:
    key: RSAKey
    clock: Clock
    issuer: str = ISSUER
    discovery_issuer: str | None = None
    supports_pkce: bool = True
    auth_methods: list[str] | None = field(default_factory=lambda: ["client_secret_basic", "client_secret_post"])
    available: bool = True
    codes: dict[str, PendingCode] = field(default_factory=dict)
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    jwks_fetches: int = 0
    extra_keys: list[RSAKey] = field(default_factory=list)
    token_outage: int | None = None  # set to an HTTP status to make the token endpoint fail
    client_secret: str = CLIENT_SECRET  # the secret the provider expects

    # Endpoints ---------------------------------------------------------------------------

    @property
    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/.well-known/openid-configuration", self._discovery),
                Route("/jwks", self._jwks),
                Route("/token", self._token, methods=["POST"]),
            ]
        )

    def discovery_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "issuer": self.discovery_issuer or self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        if self.supports_pkce:
            doc["code_challenge_methods_supported"] = ["S256"]
        if self.auth_methods is not None:
            doc["token_endpoint_auth_methods_supported"] = self.auth_methods
        return doc

    async def _discovery(self, _request: Request) -> JSONResponse:
        if not self.available:
            return JSONResponse({"error": "down"}, status_code=503)
        return JSONResponse(self.discovery_document())

    async def _jwks(self, _request: Request) -> JSONResponse:
        self.jwks_fetches += 1
        keys = KeySet([self.key, *self.extra_keys])
        return JSONResponse(keys.as_dict(private=False))

    async def _token(self, request: Request) -> JSONResponse:
        form = dict(await request.form())
        self.token_requests.append({"form": form, "authorization": request.headers.get("authorization")})
        if self.token_outage is not None:
            return PlainTextResponse("upstream trouble", status_code=self.token_outage)  # type: ignore[return-value]
        if not self._client_authenticated(form, request.headers.get("authorization")):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        pending = self.codes.pop(str(form.get("code")), None)
        if pending is None or form.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if form.get("redirect_uri") != pending.redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if pending.code_challenge is not None:
            verifier = str(form.get("code_verifier", ""))
            if _challenge(verifier) != pending.code_challenge:
                return JSONResponse({"error": "invalid_grant", "error_description": "pkce"}, status_code=400)
        id_token = self.sign(pending.claims, header=pending.header, key=pending.signing_key)
        return JSONResponse({"access_token": secrets.token_hex(8), "token_type": "Bearer", "id_token": id_token})

    def _client_authenticated(self, form: dict[str, Any], authorization: str | None) -> bool:
        if authorization and authorization.startswith("Basic "):
            client_id, _, secret = base64.b64decode(authorization[6:]).decode().partition(":")
            return unquote(client_id) == CLIENT_ID and unquote(secret) == self.client_secret
        return form.get("client_id") == CLIENT_ID and form.get("client_secret") == self.client_secret

    # The user's side ---------------------------------------------------------------------

    def authorize(
        self,
        params: dict[str, str],
        *,
        claims: dict[str, Any] | None = None,
        header: dict[str, Any] | None = None,
        signing_key: RSAKey | None = None,
    ) -> str:
        """Approve an authorization request and return the code the provider would redirect with."""
        assert params["client_id"] == CLIENT_ID
        assert params["response_type"] == "code"
        assert "openid" in params["scope"].split()
        now = int(self.clock().timestamp())
        token_claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "subject-admin",
            "email": "admin@example.com",
            "email_verified": True,
            "iat": now,
            "exp": now + 300,
            "nonce": params["nonce"],
        }
        token_claims.update(claims or {})
        token_claims = {key: value for key, value in token_claims.items() if value is not _DROP}
        code = secrets.token_urlsafe(16)
        self.codes[code] = PendingCode(
            nonce=params["nonce"],
            code_challenge=params.get("code_challenge"),
            redirect_uri=params["redirect_uri"],
            claims=token_claims,
            header=header,
            signing_key=signing_key,
        )
        return code

    def sign(self, claims: dict[str, Any], *, header: dict[str, Any] | None = None, key: RSAKey | None = None) -> str:
        signing_key = key or self.key
        return jwt.encode(header or {"alg": "RS256", "kid": signing_key.kid}, claims, signing_key)


DROP = object()
_DROP = DROP


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def generate_key(kid: str) -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid, "alg": "RS256", "use": "sig"})
