# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for client IP resolution, rate limiting, signing, and the auth rate limit."""

from __future__ import annotations

import ipaddress
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from dinnerbell.web.clientip import resolve_client_ip
from dinnerbell.web.ratelimit import SlidingWindowLimiter
from dinnerbell.web.signing import SignedPayload, csrf_token, derive_key, tokens_match
from tests.support.clock import FakeClock

PROXIES = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8"))


class TestClientIp:
    def test_untrusted_peer_ignores_forwarded_for(self) -> None:
        assert resolve_client_ip("203.0.113.9", "1.2.3.4", PROXIES) == "203.0.113.9"

    def test_no_trusted_proxies_by_default(self) -> None:
        assert resolve_client_ip("10.0.0.2", "1.2.3.4", ()) == "10.0.0.2"

    def test_trusted_peer_uses_forwarded_for(self) -> None:
        assert resolve_client_ip("10.0.0.2", "198.51.100.7", PROXIES) == "198.51.100.7"

    def test_spoofed_left_entries_are_ignored(self) -> None:
        header = "6.6.6.6, 198.51.100.7, 10.0.0.3"
        assert resolve_client_ip("10.0.0.2", header, PROXIES) == "198.51.100.7"

    def test_all_trusted_returns_leftmost(self) -> None:
        assert resolve_client_ip("10.0.0.2", "10.0.0.4, 10.0.0.3", PROXIES) == "10.0.0.4"

    def test_garbage_hop_stops_the_walk(self) -> None:
        assert resolve_client_ip("10.0.0.2", "198.51.100.7, garbage", PROXIES) == "10.0.0.2"

    def test_ipv6(self) -> None:
        assert resolve_client_ip("fd00::1", "2001:db8::5", PROXIES) == "2001:db8::5"

    def test_missing_or_odd_peer(self) -> None:
        assert resolve_client_ip(None, "1.2.3.4", PROXIES) == "unknown"
        assert resolve_client_ip("testclient", None, PROXIES) == "testclient"


class TestRateLimiter:
    def test_sliding_window(self) -> None:
        now = [0.0]
        limiter = SlidingWindowLimiter(3, 60, monotonic=lambda: now[0])
        assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
        assert limiter.hit("a") == 60
        assert limiter.hit("b") is None
        now[0] = 30
        assert limiter.hit("a") == 30
        now[0] = 60
        assert limiter.hit("a") is None

    def test_eviction_bounds_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dinnerbell.web import ratelimit

        monkeypatch.setattr(ratelimit, "MAX_TRACKED_KEYS", 3)
        now = [0.0]
        limiter = SlidingWindowLimiter(1, 10, monotonic=lambda: now[0])
        for key in "abc":
            limiter.hit(key)
        now[0] = 20
        assert limiter.hit("d") is None
        assert len(limiter._hits) == 1  # stale keys evicted
        for key in "efg":
            limiter.hit(key)
        assert len(limiter._hits) <= 3


class TestSigning:
    def test_keys_differ_by_purpose(self) -> None:
        assert derive_key("secret", "csrf") != derive_key("secret", "login-state")

    def test_round_trip_and_expiry(self) -> None:
        clock = FakeClock()
        signer = SignedPayload(b"k" * 32, timedelta(minutes=10), clock)
        value = signer.dumps({"state": "abc", "verifier": None})
        assert signer.loads(value) == {"state": "abc", "verifier": None}
        clock.advance(timedelta(minutes=11))
        assert signer.loads(value) is None

    @pytest.mark.parametrize("value", [None, "", "no-dot", "a.b.c", "abc.def", "!!!.???"])
    def test_garbage_is_rejected(self, value: str | None) -> None:
        assert SignedPayload(b"k" * 32, timedelta(minutes=10), FakeClock()).loads(value) is None

    def test_other_key_is_rejected(self) -> None:
        clock = FakeClock()
        value = SignedPayload(b"a" * 32, timedelta(minutes=10), clock).dumps({"state": "x"})
        assert SignedPayload(b"b" * 32, timedelta(minutes=10), clock).loads(value) is None

    def test_future_timestamp_is_rejected(self) -> None:
        clock = FakeClock()
        value = SignedPayload(b"k" * 32, timedelta(minutes=10), clock).dumps({"state": "x"})
        clock.advance(timedelta(minutes=-5))
        assert SignedPayload(b"k" * 32, timedelta(minutes=10), clock).loads(value) is None

    def test_signed_non_object_is_rejected(self) -> None:
        import base64
        import hashlib
        import hmac

        body = base64.urlsafe_b64encode(b'{"iat": "x", "data": 1}').rstrip(b"=").decode()
        mac = base64.urlsafe_b64encode(hmac.new(b"k" * 32, body.encode(), hashlib.sha256).digest()).rstrip(b"=")
        assert SignedPayload(b"k" * 32, timedelta(minutes=10), FakeClock()).loads(f"{body}.{mac.decode()}") is None

    def test_csrf_tokens(self) -> None:
        token = csrf_token(b"k" * 32, "session-hash")
        assert tokens_match(token, token)
        assert not tokens_match(token, None)
        assert not tokens_match(token, csrf_token(b"k" * 32, "other-session"))


def test_auth_routes_are_rate_limited_per_ip(client: TestClient) -> None:
    statuses = [client.get("/auth/login").status_code for _ in range(21)]
    assert statuses[:20] == [302] * 20
    assert statuses[20] == 429
    limited = client.get("/auth/login")
    assert int(limited.headers["retry-after"]) >= 1
    assert client.get("/").status_code == 200  # other routes are not limited


def test_debug_logs_show_peer_and_client_ip(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.DEBUG, logger="dinnerbell.web.access"):
        client.get("/", headers={"X-Forwarded-For": "198.51.100.7"})
    record = next(r for r in caplog.records if r.getMessage() == "client address")
    assert record.peer_ip == "testclient"  # type: ignore[attr-defined]
    assert record.client_ip == "testclient"  # type: ignore[attr-defined]
