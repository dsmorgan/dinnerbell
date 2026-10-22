# SPDX-License-Identifier: AGPL-3.0-or-later
"""An in-memory sliding-window rate limiter. One process, so memory is enough."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

MAX_TRACKED_KEYS = 10_000


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window_seconds
        self._monotonic = monotonic
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Record a request. Return None if allowed, or the seconds to wait if not."""
        now = self._monotonic()
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= MAX_TRACKED_KEYS:
                self._evict(now)
            hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1.0, self._window - (now - hits[0]))
        hits.append(now)
        return None

    def _evict(self, now: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or now - hits[-1] >= self._window]
        for key in stale:
            del self._hits[key]
        if len(self._hits) >= MAX_TRACKED_KEYS:
            self._hits.clear()
