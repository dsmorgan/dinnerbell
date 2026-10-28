# SPDX-License-Identifier: AGPL-3.0-or-later
"""How Slack shows a badge: its art when the PNG is uploaded (``art_section``), and otherwise its
name with a tier mark, as plain text (``tag``). The text is always the fallback: art is never needed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dinnerbell.domain.badges import BADGES, Tier

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


ArtIds = Mapping[tuple[str, str], str]
"""Slack file IDs of badge art, by badge key and tier value. Empty, every badge shows as text."""
BY_NAME = {b.name: b.key.value for b in BADGES}
BY_LABEL = {t.label: t.value for t in Tier}


def wanted(name: str, tier: str) -> tuple[str, str] | None:
    """The ``ArtIds`` key for a badge named ``name`` at the tier labeled ``tier``, or None."""
    key, value = BY_NAME.get(name), BY_LABEL.get(tier)
    return (key, value) if key and value else None


def art_element(file_id: str, alt: str) -> dict[str, Any]:
    """The image element that shows an uploaded badge. The only place that decides how art is
    referenced: a Slack file by ID. If Slack won't show a channel-less upload to every member,
    this is what changes, to an image URL (#105)."""
    return {"type": "image", "slack_file": {"id": file_id}, "alt_text": alt}
