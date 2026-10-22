# SPDX-License-Identifier: AGPL-3.0-or-later
"""A controllable clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 22, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta
