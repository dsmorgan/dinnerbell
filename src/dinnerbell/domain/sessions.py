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


# Devices (spec 6.15) -----------------------------------------------------------------------------

UNKNOWN_DEVICE = "Unknown device"
"""The label of a session whose browser said nothing we recognize, and of every session that
existed before sessions had labels."""
MAX_DEVICE_LENGTH = 40
_USER_AGENT_PREFIX = 512
"""Only this much of a User-Agent is read: every marker below sits well inside it."""

# Order matters: an iPad's or iPhone's browser also names "Mac OS X", and Android also names Linux.
_PLATFORMS = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("CrOS", "Chromebook"),
    ("Macintosh", "Mac"),
    ("Windows", "Windows"),
    ("Linux", "Linux"),
)
# Order matters too: Edge, Opera, and Samsung Internet name Chrome and Safari, and Chrome names Safari.
_BROWSERS = (
    (("Edg/", "EdgA/", "EdgiOS/"), "Edge"),
    (("OPR/", "OPT/"), "Opera"),
    (("SamsungBrowser/",), "Samsung Internet"),
    (("Firefox/", "FxiOS/"), "Firefox"),
    (("Chrome/", "CriOS/"), "Chrome"),
    (("Safari/",), "Safari"),
)


def device_label(user_agent: str | None) -> str:
    """A short, coarse label for the browser that sent ``user_agent``, such as "iPhone · Safari"
    or "Mac · Chrome". Only the platform and the browser's name, never a version, so the label
    says which of a member's devices this is without being a fingerprint, and the User-Agent
    itself is never stored."""
    text = (user_agent or "")[:_USER_AGENT_PREFIX]
    platform = next((name for marker, name in _PLATFORMS if marker in text), None)
    browser = next((name for markers, name in _BROWSERS if any(m in text for m in markers)), None)
    parts = [part for part in (platform, browser) if part]
    return " · ".join(parts)[:MAX_DEVICE_LENGTH] if parts else UNKNOWN_DEVICE
