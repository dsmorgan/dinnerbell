# SPDX-License-Identifier: AGPL-3.0-or-later
"""How Slack words a badge for now: its name with a tier mark, as plain text. The badge art
(the Stats page and, later, PNGs in Slack) replaces these marks; nothing else depends on them."""

from __future__ import annotations

from dinnerbell.domain.badges import Tier

TIER_MARKS = {
    Tier.FIRST.label: "🎗️",
    Tier.BRONZE.label: "🥉",
    Tier.SILVER.label: "🥈",
    Tier.GOLD.label: "🥇",
    Tier.DIAMOND.label: "💎",
}
"""A mark for each tier by its label. Each differs by shape as well as color, so none relies on color."""


def tag(name: str, tier: str) -> str:
    """``🥇 Chef · Gold``, with the name and tier as given (already safe to show once escaped)."""
    return f"{TIER_MARKS.get(tier, '')} {name} · {tier}".strip()
