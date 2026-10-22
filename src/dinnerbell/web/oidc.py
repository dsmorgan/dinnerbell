# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small, strict OpenID Connect relying party: discovery, authorization code flow with
state, nonce, and PKCE (S256) when offered, and ID token validation.

Nothing here is specific to Slack except reading two optional Slack claims.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet

from dinnerbell.domain.identity import IdentityClaims
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

SAFE_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
DISCOVERY_TTL = timedelta(hours=1)
JWKS_TTL = timedelta(hours=1)
JWKS_MIN_REFRESH = timedelta(minutes=1)
CLOCK_LEEWAY_SECONDS = 60
MAX_SUBJECT_LENGTH = 255
SLACK_TEAM_CLAIM = "https://slack.com/team_id"
SLACK_USER_CLAIM = "https://slack.com/user_id"


class OIDCError(Exception):
    """Sign-in could not be verified. ``reason`` is for logs and never shown to users."""

    def __init__(self, reason: str, *, provider_unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.provider_unavailable = provider_unavailable


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    supports_pkce: bool
    algorithms: tuple[str, ...]
    client_auth: str  # "client_secret_post" or "client_secret_basic"


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    url: str
    code_verifier: str | None


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _same_issuer(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


class OIDCClient:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        scopes: str,
        redirect_uri: str,
        http: httpx.AsyncClient,
        clock: Clock,
        extra_auth_params: dict[str, str] | None = None,
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._redirect_uri = redirect_uri
        self._http = http
        self._clock = clock
        self._extra_auth_params = dict(extra_auth_params or {})
        self._allow_http = urlsplit(issuer).scheme == "http"
        self._metadata: tuple[ProviderMetadata, datetime] | None = None
        self._keys: tuple[KeySet, datetime] | None = None

    # Discovery -----------------------------------------------------------------------------

    async def metadata(self) -> ProviderMetadata:
        now = self._clock()
        if self._metadata and now - self._metadata[1] < DISCOVERY_TTL:
            return self._metadata[0]
        document = await self._get_json(self._issuer.rstrip("/") + "/.well-known/openid-configuration")
        metadata = self._parse_metadata(document)
        self._metadata = (metadata, now)
        return metadata

    def _parse_metadata(self, doc: dict[str, Any]) -> ProviderMetadata:
        issuer = self._string(doc, "issuer")
        if not _same_issuer(issuer, self._issuer):
            raise OIDCError("discovery issuer does not match the configured issuer")
        offered = doc.get("id_token_signing_alg_values_supported") or list(SAFE_ALGORITHMS)
        algorithms = tuple(alg for alg in SAFE_ALGORITHMS if alg in offered)
        if not algorithms:
            raise OIDCError("provider offers no acceptable ID token signing algorithm")
        return ProviderMetadata(
            issuer=issuer,
            authorization_endpoint=self._endpoint(doc, "authorization_endpoint"),
            token_endpoint=self._endpoint(doc, "token_endpoint"),
            jwks_uri=self._endpoint(doc, "jwks_uri"),
            supports_pkce="S256" in (doc.get("code_challenge_methods_supported") or []),
            algorithms=algorithms,
            client_auth=self._client_auth(doc.get("token_endpoint_auth_methods_supported")),
        )

    @staticmethod
    def _client_auth(methods: object) -> str:
        if methods is None:
            return "client_secret_basic"
        if not isinstance(methods, list):
            raise OIDCError("token_endpoint_auth_methods_supported is malformed")
        for method in ("client_secret_post", "client_secret_basic"):
            if method in methods:
                return method
        raise OIDCError("provider supports neither client_secret_post nor client_secret_basic")

    @staticmethod
    def _string(doc: dict[str, Any], key: str) -> str:
        value = doc.get(key)
        if not isinstance(value, str) or not value:
            raise OIDCError(f"discovery document lacks {key}")
        return value

    def _endpoint(self, doc: dict[str, Any], key: str) -> str:
        value = self._string(doc, key)
        scheme = urlsplit(value).scheme
        if scheme != "https" and not (self._allow_http and scheme == "http"):
            raise OIDCError(f"{key} must use https")
        return value

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OIDCError(f"could not reach provider: {type(exc).__name__}", provider_unavailable=True) from None
        if response.status_code != 200:
            raise OIDCError(f"provider returned HTTP {response.status_code}", provider_unavailable=True)
        return self._json_object(response)

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise OIDCError("provider returned invalid JSON") from None
        if not isinstance(body, dict):
            raise OIDCError("provider returned a non-object JSON body")
        return body

    # Authorization -------------------------------------------------------------------------

    async def authorization_request(self, *, state: str, nonce: str) -> AuthorizationRequest:
        metadata = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": self._scopes,
            "state": state,
            "nonce": nonce,
            **self._extra_auth_params,
        }
        verifier = None
        if metadata.supports_pkce:
            verifier = secrets.token_urlsafe(64)
            params |= {"code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256"}
        separator = "&" if urlsplit(metadata.authorization_endpoint).query else "?"
        return AuthorizationRequest(f"{metadata.authorization_endpoint}{separator}{urlencode(params)}", verifier)

    async def exchange_code(self, code: str, code_verifier: str | None) -> str:
        """Redeem an authorization code and return the raw ID token."""
        metadata = await self.metadata()
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri}
        headers = {"Accept": "application/json"}
        if code_verifier:
            form["code_verifier"] = code_verifier
        if metadata.client_auth == "client_secret_post":
            form |= {"client_id": self._client_id, "client_secret": self._client_secret}
        else:
            headers["Authorization"] = self._basic_auth()
        try:
            response = await self._http.post(metadata.token_endpoint, data=form, headers=headers)
        except httpx.HTTPError as exc:
            raise OIDCError(f"token request failed: {type(exc).__name__}", provider_unavailable=True) from None
        body = self._json_object(response) if response.content else {}
        if response.status_code != 200 or "error" in body:
            error = body.get("error")
            raise OIDCError(f"token endpoint refused the code: HTTP {response.status_code} {error!s:.64}")
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OIDCError("token response has no id_token")
        return id_token

    def _basic_auth(self) -> str:
        pair = f"{quote(self._client_id, safe='')}:{quote(self._client_secret, safe='')}"
        return "Basic " + base64.b64encode(pair.encode()).decode()

    # ID token validation -------------------------------------------------------------------

    async def verify_id_token(self, id_token: str, *, nonce: str) -> IdentityClaims:
        metadata = await self.metadata()
        claims = await self._decode(id_token, metadata)
        now = int(self._clock().timestamp())
        try:
            jwt.JWTClaimsRegistry(
                now=now,
                leeway=CLOCK_LEEWAY_SECONDS,
                exp={"essential": True},
                iat={"essential": True},
            ).validate(claims)
        except JoseError as exc:
            raise OIDCError(f"ID token time claims invalid: {exc.error}") from None
        self._check_issuer_and_audience(claims, metadata)
        self._check_nonce(claims, nonce)
        return self._identity(claims)

    async def _decode(self, id_token: str, metadata: ProviderMetadata) -> dict[str, Any]:
        keys = await self._key_set(metadata)
        try:
            return dict(jwt.decode(id_token, keys, algorithms=metadata.algorithms).claims)
        except InvalidKeyIdError:
            keys = await self._key_set(metadata, refresh=True)
        except (JoseError, ValueError) as exc:
            raise OIDCError(f"ID token signature invalid: {type(exc).__name__}") from None
        try:
            return dict(jwt.decode(id_token, keys, algorithms=metadata.algorithms).claims)
        except (JoseError, ValueError) as exc:
            raise OIDCError(f"ID token signature invalid: {type(exc).__name__}") from None

    async def _key_set(self, metadata: ProviderMetadata, *, refresh: bool = False) -> KeySet:
        now = self._clock()
        if self._keys:
            keys, fetched = self._keys
            age = now - fetched
            if (not refresh and age < JWKS_TTL) or (refresh and age < JWKS_MIN_REFRESH):
                return keys
        document = await self._get_json(metadata.jwks_uri)
        try:
            keys = KeySet.import_key_set(document)  # type: ignore[arg-type]
        except (JoseError, ValueError, TypeError, KeyError) as exc:
            raise OIDCError(f"provider JWKS is invalid: {type(exc).__name__}") from None
        self._keys = (keys, now)
        return keys

    def _check_issuer_and_audience(self, claims: dict[str, Any], metadata: ProviderMetadata) -> None:
        if claims.get("iss") != metadata.issuer:
            raise OIDCError("ID token issuer mismatch")
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if not isinstance(audiences, list) or self._client_id not in audiences:
            raise OIDCError("ID token audience mismatch")
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self._client_id:
            raise OIDCError("ID token authorized party mismatch")

    @staticmethod
    def _check_nonce(claims: dict[str, Any], expected: str) -> None:
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode(), expected.encode()):
            raise OIDCError("ID token nonce mismatch")

    @staticmethod
    def _identity(claims: dict[str, Any]) -> IdentityClaims:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
            raise OIDCError("ID token subject missing or invalid")
        email = claims.get("email")
        verified = claims.get("email_verified")
        return IdentityClaims(
            subject=subject,
            email=email if isinstance(email, str) else None,
            email_verified=verified is True or verified == "true",
            slack_team_id=_optional_string(claims.get(SLACK_TEAM_CLAIM)),
            slack_user_id=_optional_string(claims.get(SLACK_USER_CLAIM)),
        )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
