# SPDX-License-Identifier: AGPL-3.0-or-later
"""Composite badge pictures (#114): the ``slack_badge_composite`` table, one row per member whose
badges are drawn as one PNG for App Home, with the Slack file's ID, a hash of the PNG so a change
in the member's badges (or the art) uploads a new one, and when it was uploaded.

The table is new, so nothing existing is rebuilt and no data moves. A downgrade drops it; the
pictures upload again the next time a badge is earned or Home is opened.

Revision ID: 0019_slack_badge_composite
Revises: 0018_gambler
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_slack_badge_composite"
down_revision: str | None = "0018_gambler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slack_badge_composite",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("file_id", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_slack_badge_composite_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_slack_badge_composite_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slack_badge_composite")),
        sa.UniqueConstraint("household_id", "member_id", name=op.f("uq_slack_badge_composite_household_id_member_id")),
    )


def downgrade() -> None:
    op.drop_table("slack_badge_composite")
