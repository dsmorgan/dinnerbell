# SPDX-License-Identifier: AGPL-3.0-or-later
"""The feedback table: one member's feedback on one meal, with a snapshot of the meal (FR-45).

Feedback belongs to its meal: deleting a meal log deletes its feedback.

Revision ID: 0008_meal_feedback
Revises: 0007_event_autoincrement
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_meal_feedback"
down_revision: str | None = "0007_event_autoincrement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("meal_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("food", sa.String(length=16), nullable=True),
        sa.Column("place", sa.String(length=16), nullable=True),
        sa.Column("place_issues", sa.JSON(), nullable=False),
        sa.Column("cook_thanks", sa.Boolean(), nullable=False),
        sa.Column("cook_note", sa.String(length=200), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("item_name", sa.String(length=100), nullable=False),
        sa.Column("item_kind", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("cook_member_id", sa.Integer(), nullable=True),
        sa.Column("cook_name", sa.String(length=80), nullable=True),
        sa.Column("participants", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("food IS NULL OR food IN ('loved', 'fine', 'not_again')", name=op.f("ck_feedback_food")),
        sa.CheckConstraint("item_kind IN ('home', 'restaurant')", name=op.f("ck_feedback_item_kind")),
        sa.CheckConstraint("mode IS NULL OR mode IN ('cook', 'pickup', 'delivery')", name=op.f("ck_feedback_mode")),
        sa.CheckConstraint("place IS NULL OR place IN ('loved', 'fine', 'not_again')", name=op.f("ck_feedback_place")),
        sa.ForeignKeyConstraint(
            ["cook_member_id"], ["member.id"], name=op.f("fk_feedback_cook_member_id_member"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_feedback_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_feedback_item_id_catalog_item"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["meal_id"], ["meal_log.id"], name=op.f("fk_feedback_meal_id_meal_log"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_feedback_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("meal_id", "member_id", name=op.f("uq_feedback_meal_id_member_id")),
    )
    op.create_index(op.f("ix_feedback_cook_member_id"), "feedback", ["cook_member_id"])
    op.create_index(op.f("ix_feedback_item_id"), "feedback", ["item_id"])
    op.create_index(op.f("ix_feedback_member_id"), "feedback", ["member_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_member_id"), table_name="feedback")
    op.drop_index(op.f("ix_feedback_item_id"), table_name="feedback")
    op.drop_index(op.f("ix_feedback_cook_member_id"), table_name="feedback")
    op.drop_table("feedback")
