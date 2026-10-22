# SPDX-License-Identifier: AGPL-3.0-or-later
"""HMAC helpers: purpose-specific keys, short-lived signed payloads, CSRF tokens."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import timedelta

from dinnerbell.services.clock import Clock


def derive_key(secret: str, purpose: str) -> bytes:
    """Derive an independent key per purpose so one use can never forge another."""
    return hmac.new(secret.encode(), f"dinnerbell/{purpose}".encode(), hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SignedPayload:
    """A small JSON object signed with HMAC-SHA256 and valid for ``max_age``.

    Signed, not encrypted: the browser can read it but cannot alter or extend it.
    """

    def __init__(self, key: bytes, max_age: timedelta, clock: Clock) -> None:
        self._key = key
        self._max_age = max_age
        self._clock = clock

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def dumps(self, data: dict[str, str | None]) -> str:
        issued = int(self._clock().timestamp())
        body = _b64(json.dumps({"iat": issued, "data": data}, separators=(",", ":")).encode())
        return f"{body}.{self._mac(body)}"

    def loads(self, value: str | None) -> dict[str, str | None] | None:
        """Return the payload, or None if the value is missing, forged, or expired."""
        if not value or value.count(".") != 1:
            return None
        body, mac = value.split(".")
        if not hmac.compare_digest(mac, self._mac(body)):
            return None
        try:
            decoded = json.loads(_unb64(body))
        except (ValueError, binascii.Error):
            return None
        issued, data = decoded.get("iat"), decoded.get("data")
        if not isinstance(issued, int) or not isinstance(data, dict):
            return None
        age = self._clock().timestamp() - issued
        if age < 0 or age > self._max_age.total_seconds():
            return None
        return data


def csrf_token(key: bytes, session_hash: str) -> str:
    """The CSRF token for a session: bound to it, needs no storage, and changes when it does."""
    return hmac.new(key, session_hash.encode(), hashlib.sha256).hexdigest()


def tokens_match(expected: str, supplied: str | None) -> bool:
    return supplied is not None and hmac.compare_digest(expected.encode(), supplied.encode())
