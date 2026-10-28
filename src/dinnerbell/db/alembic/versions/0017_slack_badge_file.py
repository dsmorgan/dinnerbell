# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slack badge art (#105): the ``slack_badge_file`` table, one row per badge tier whose PNG is
uploaded to Slack, with the Slack file's ID and a hash of the PNG so changed art uploads again.

The table is new, so nothing existing is rebuilt and no data moves. A downgrade drops it; the
art uploads again the next time a badge is shown.

Revision ID: 0017_slack_badge_file
Revises: 0016_contributions
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_slack_badge_file"
down_revision: str | None = "0016_contributions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slack_badge_file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("badge_key", sa.String(length=24), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("file_id", sa.String(length=64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_slack_badge_file_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_slack_badge_file")),
        sa.UniqueConstraint(
            "household_id", "badge_key", "tier", name=op.f("uq_slack_badge_file_household_id_badge_key_tier")
        ),
    )


def downgrade() -> None:
    op.drop_table("slack_badge_file")
