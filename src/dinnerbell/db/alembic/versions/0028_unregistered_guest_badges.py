# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unregistered guests earn no badges (#201): take back the ones they have.

An unregistered guest is a ``member`` row with the role ``guest`` and no ``slack_user_id``. Since
#102 they earned badges like anyone; now they earn none. The upgrade deletes their awards,
quietly: a pending one is never announced. Their contributions stay, so household totals and the
monthly recap don't change, and the badges they reach are awarded, quietly, once an admin links
them to Slack or promotes them.

A badge post or a recap already queued and not yet delivered drops them too, and a badge post
that names no one else is deleted. The app isn't running while it migrates, so no drainer holds
those rows.

The downgrade restores nothing. Under an earlier version, the guest's next done call awards the
tiers their counts reach again.

Revision ID: 0028_unregistered_guest_badges
Revises: 0027_away
Create Date: 2026-11-04
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0028_unregistered_guest_badges"
down_revision: str | None = "0027_away"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNREGISTERED = "SELECT id FROM member WHERE role = 'guest' AND slack_user_id IS NULL"
POSTS = {"badges_earned": "news", "monthly_recap": "badges"}
"""Each queued post that names badge earners, and the payload list that names them."""


def upgrade() -> None:
    bind = op.get_bind()
    guests = set(bind.execute(sa.text(UNREGISTERED)).scalars())
    if not guests:
        return
    queued = bind.execute(
        sa.text(
            "SELECT id, kind, payload FROM outbox WHERE delivered_at IS NULL AND failed_at IS NULL "
            "AND kind IN ('badges_earned', 'monthly_recap')"
        )
    ).all()
    for row_id, kind, raw in queued:
        payload: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
        field = POSTS[kind]
        kept = [news for news in payload.get(field, []) if news["person"]["id"] not in guests]
        if len(kept) == len(payload.get(field, [])):
            continue
        if kind == "badges_earned" and not kept:
            bind.execute(sa.text("DELETE FROM outbox WHERE id = :id"), {"id": row_id})
            continue
        payload[field] = kept
        bind.execute(sa.text("UPDATE outbox SET payload = :p WHERE id = :id"), {"p": json.dumps(payload), "id": row_id})
    op.execute(
        "DELETE FROM badge_award WHERE member_id IN "
        "(SELECT id FROM member WHERE role = 'guest' AND slack_user_id IS NULL)"
    )


def downgrade() -> None:
    pass
