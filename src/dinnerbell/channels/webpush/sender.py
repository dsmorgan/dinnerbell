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

from dinnerbell.channels.webpush.encryption import BadKeys, encrypt
from dinnerbell.channels.webpush.vapid import VapidKeys
from dinnerbell.domain.push import PushResponse, PushTarget, UndeliverableKeys, known_push_service
from dinnerbell.services.clock import Clock

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class UnknownPushService(Exception):
    """A stored endpoint that isn't a push service's. Never sent."""


LONGEST_WAIT = 86400.0
"""The longest Retry-After Dinnerbell reads, in seconds: a push expires within a day anyway."""


def retry_after(value: str | None, now: datetime) -> float | None:
    """Retry-After in seconds, at most ``LONGEST_WAIT``: ASCII digits, or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(float(value[:12]), LONGEST_WAIT)
    try:
        at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return min(max((at - now).total_seconds(), 0.0), LONGEST_WAIT)


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
        try:
            body = encrypt(payload, target.p256dh, target.auth)
        except BadKeys:  # keys stored before they were checked on the curve, say; never a payload error
            raise UndeliverableKeys(f"subscription {target.subscription_id} has keys that can't work") from None
        response = await self._http.post(target.endpoint, content=body, headers=headers, follow_redirects=False)
        return PushResponse(response.status_code, retry_after(response.headers.get("retry-after"), now))


def new_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The push client: no redirects, and a 10-second timeout."""
    return httpx.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=False)
