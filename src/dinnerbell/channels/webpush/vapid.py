# SPDX-License-Identifier: AGPL-3.0-or-later
"""The VAPID key pair and its JWT (RFC 8292, spec 6.15 "Keys").

The key pair comes from ``DINNERBELL_SECRET_KEY``: 48 bytes of HKDF-SHA256 with the info
``dinnerbell/vapid``, reduced mod (n - 1) plus 1, which is a P-256 private key in [1, n - 1]
(the same reduction as FIPS 186-5's key generation by extra random bits). It's apart from every
other key derived from the secret, and nothing stores it. Changing the secret ends every
subscription: the push service refuses the new key, and the sender deletes each device it does.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from joserfc import jwt
from joserfc.jwk import ECKey

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
INFO = b"dinnerbell/vapid"
TOKEN_LIFETIME = timedelta(hours=12)
"""RFC 8292 allows at most 24 hours."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def private_value(secret: str) -> int:
    """The P-256 private key's scalar, from the secret key."""
    okm = HKDF(algorithm=hashes.SHA256(), length=48, salt=None, info=INFO).derive(secret.encode())
    return int.from_bytes(okm, "big") % (P256_ORDER - 1) + 1


@dataclass(frozen=True)
class VapidKeys:
    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def from_secret(cls, secret: str) -> VapidKeys:
        return cls(ec.derive_private_key(private_value(secret), ec.SECP256R1()))

    @property
    def public_key(self) -> str:
        """The public key as browsers take it (``applicationServerKey``): the uncompressed
        point, base64url."""
        point = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return b64url(point)

    def token(self, endpoint: str, subject: str, now: datetime) -> str:
        """The JWT for a push to ``endpoint``: ``aud`` is its origin, ``exp`` 12 hours on, and
        ``sub`` the instance's base URL, never an email."""
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int((now + TOKEN_LIFETIME).timestamp()),
            "sub": subject,
        }
        return jwt.encode({"alg": "ES256", "typ": "JWT"}, claims, ECKey.import_key(self.private_key))

    def authorization(self, endpoint: str, subject: str, now: datetime) -> str:
        """The ``Authorization`` header (RFC 8292 section 3)."""
        return f"vapid t={self.token(endpoint, subject, now)}, k={self.public_key}"
