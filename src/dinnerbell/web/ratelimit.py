# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-memory sliding-window rate limiter. One process, so memory is enough."""

from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict, deque
from collections.abc import Callable

MAX_TRACKED_KEYS = 10_000
IPV6_PREFIX = 64


class SlidingWindowLimiter:
    """Allow ``limit`` hits per key within any ``window_seconds``.

    Memory is bounded: past ``MAX_TRACKED_KEYS`` keys, the least recently used key is dropped.
    A key that keeps hitting stays recent, so flooding other keys can't reset its count.
    """

    def __init__(self, limit: int, window_seconds: float, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window_seconds
        self._monotonic = monotonic
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def hit(self, key: str) -> float | None:
        """Record a request. Return None if allowed, or the seconds to wait if not."""
        now = self._monotonic()
        hits = self._hits.get(key)
        if hits is None:
            while len(self._hits) >= MAX_TRACKED_KEYS:
                self._hits.popitem(last=False)
            hits = self._hits[key] = deque()
        else:
            self._hits.move_to_end(key)
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1.0, self._window - (now - hits[0]))
        hits.append(now)
        return None

    def __len__(self) -> int:
        return len(self._hits)


def client_key(client_ip: str) -> str:
    """Rate-limit key for a client address. IPv6 clients share their /64, which one host controls."""
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return f"ip:{client_ip}"
    if address.version == 6:
        network = ipaddress.ip_network(f"{address}/{IPV6_PREFIX}", strict=False)
        return f"ip:{network}"
    return f"ip:{address}"
