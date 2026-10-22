# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session lifetime rules: 90 days, sliding."""

from __future__ import annotations

from datetime import datetime, timedelta

SESSION_LIFETIME = timedelta(days=90)
RENEWAL_INTERVAL = timedelta(hours=1)
"""Renew at most this often so every request does not write to the database."""


def is_expired(expires_at: datetime, now: datetime) -> bool:
    return now >= expires_at


def needs_renewal(last_seen: datetime, now: datetime) -> bool:
    return now - last_seen >= RENEWAL_INTERVAL


def new_expiry(now: datetime) -> datetime:
    return now + SESSION_LIFETIME
