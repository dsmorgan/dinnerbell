# SPDX-License-Identifier: AGPL-3.0-or-later
"""``WebPushSender``: one encrypted push to one device, over HTTPS (RFC 8030).

It posts only to a known push service (``domain.push.known_push_service``, checked again here),
never follows a redirect, and times out within 10 seconds. It never logs: the endpoint
identifies the device, and the drainer logs the subscription's ID.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from dinnerbell.channels.webpush.encryption import encrypt
from dinnerbell.channels.webpush.vapid import VapidKeys
from dinnerbell.domain.push import PushResponse, PushTarget, known_push_service
from dinnerbell.services.clock import Clock

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class UnknownPushService(Exception):
    """A stored endpoint that isn't a push service's. Never sent."""


def retry_after(value: str | None, now: datetime) -> float | None:
    """Retry-After in seconds: a number of seconds, or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max((at - now).total_seconds(), 0.0)


class WebPushSender:
    def __init__(self, keys: VapidKeys, subject: str, http: httpx.AsyncClient, clock: Clock) -> None:
        self._keys = keys
        self._subject = subject
        self._http = http
        self._clock = clock

    async def send(
        self, target: PushTarget, payload: bytes, *, ttl: int, urgency: str, topic: str | None
    ) -> PushResponse:
        if not known_push_service(target.endpoint):
            raise UnknownPushService(f"subscription {target.subscription_id} isn't on a known push service")
        now = self._clock()
        headers = {
            "Authorization": self._keys.authorization(target.endpoint, self._subject, now),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),
            "Urgency": urgency,
        }
        if topic:
            headers["Topic"] = topic
        body = encrypt(payload, target.p256dh, target.auth)
        response = await self._http.post(target.endpoint, content=body, headers=headers, follow_redirects=False)
        return PushResponse(response.status_code, retry_after(response.headers.get("retry-after"), now))


def new_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The push client: no redirects, and a 10-second timeout."""
    return httpx.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=False)
