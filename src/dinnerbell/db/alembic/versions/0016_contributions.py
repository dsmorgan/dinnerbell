# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contributions and badges (M4, FR-38, FR-39): the ``contribution`` table, one row per thing a
member did for a done call or a meal, and the ``badge_award`` table, one row per badge tier a
member earned, with whether the channel has been told.

Both tables are new, so nothing existing is rebuilt and no data moves. The counts for meals
already in the log are filled in by the app on its first start after the upgrade
(``services.contributions.ensure_backfilled``, which marks its work in the household settings),
and the badges they reach are recorded as already announced-or-skipped, never pending.

A downgrade drops both tables, so every contribution count and badge is lost, and it clears the
household's backfill marker so that upgrading again counts the meal log afresh. Restore a backup
instead to keep them.

Revision ID: 0016_contributions
Revises: 0015_coordinator
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_contributions"
down_revision: str | None = "0015_coordinator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "'cooked', 'picked_up', 'ordered', 'coordinated', 'participated', 'responded_on_time', "
    "'suggested', 'added_item', 'explored', 'hosted', 'gave_feedback'"
)


def upgrade() -> None:
    op.create_table(
        "contribution",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("meal_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"kind IN ({KINDS})", name=op.f("ck_contribution_kind")),
        sa.CheckConstraint("(event_id IS NULL) <> (meal_id IS NULL)", name=op.f("ck_contribution_one_source")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["event.id"], name=op.f("fk_contribution_event_id_event"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_contribution_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_contribution_item_id_catalog_item"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["meal_id"], ["meal_log.id"], name=op.f("fk_contribution_meal_id_meal_log"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_contribution_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contribution")),
        sa.UniqueConstraint("kind", "item_id", name=op.f("uq_contribution_kind_item_id")),
        sa.UniqueConstraint("member_id", "kind", "event_id", name=op.f("uq_contribution_member_id_kind_event_id")),
        sa.UniqueConstraint("member_id", "kind", "meal_id", name=op.f("uq_contribution_member_id_kind_meal_id")),
    )
    op.create_index("ix_contribution_household_day", "contribution", ["household_id", "day"], unique=False)
    op.create_index(op.f("ix_contribution_member_id"), "contribution", ["member_id"], unique=False)

    op.create_table(
        "badge_award",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("badge_key", sa.String(length=24), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("awarded_at", sa.DateTime(), nullable=False),
        sa.Column("announcement", sa.String(length=16), nullable=False),
        sa.Column("announced_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "announcement IN ('pending', 'announced', 'skipped')", name=op.f("ck_badge_award_announcement")
        ),
        sa.CheckConstraint(
            "tier IN ('first', 'bronze', 'silver', 'gold', 'diamond')", name=op.f("ck_badge_award_tier")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["event.id"], name=op.f("fk_badge_award_event_id_event"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_badge_award_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_badge_award_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_badge_award")),
        sa.UniqueConstraint("member_id", "badge_key", "tier", name=op.f("uq_badge_award_member_id_badge_key_tier")),
    )
    op.create_index(
        "ix_badge_award_household_announcement", "badge_award", ["household_id", "announcement"], unique=False
    )
    op.create_index(op.f("ix_badge_award_member_id"), "badge_award", ["member_id"], unique=False)


def downgrade() -> None:
    op.drop_table("badge_award")
    op.drop_table("contribution")
    op.execute("UPDATE household SET settings = json_remove(settings, '$.contributions_backfilled')")
