# SPDX-License-Identifier: AGPL-3.0-or-later
"""The clock seam. Production uses ``utc_now``; tests pass a fake."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
