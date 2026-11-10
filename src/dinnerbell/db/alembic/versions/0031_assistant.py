# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional assistant (ADR 0009, #17): ``assistant_usage`` and ``assistant_conversation``.

``assistant_usage`` has a row per model call: reserved at the call's worst case before it, then
settled at its cost, for the household's monthly cap. ``assistant_conversation`` keeps each
conversation, in pseudonym form, until Admin's Conversation hours pass. Both start empty, and
nothing reads them unless the operator sets ``DINNERBELL_ANTHROPIC_API_KEY`` and turns on the
``assistant`` flag. The downgrade drops both tables, losing the month's usage and every
conversation.

Revision ID: 0031_assistant
Revises: 0030_color_mode
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_assistant"
down_revision: str | None = "0030_color_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("owner_member_id", sa.Integer(), nullable=True),
        sa.Column("pseudonyms", sa.JSON(), nullable=False),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(private = 1) = (owner_member_id IS NOT NULL)", name=op.f("ck_assistant_conversation_owner")
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_assistant_conversation_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_member_id"],
            ["member.id"],
            name=op.f("fk_assistant_conversation_owner_member_id_member"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_conversation")),
        sa.UniqueConstraint("household_id", "key", name=op.f("uq_assistant_conversation_household_id_key")),
    )
    op.create_index(
        "ix_assistant_conversation_household_last", "assistant_conversation", ["household_id", "last_message_at"]
    )
    op.create_index(op.f("ix_assistant_conversation_owner_member_id"), "assistant_conversation", ["owner_member_id"])
    op.create_table(
        "assistant_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micros", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('reserved', 'settled')", name=op.f("ck_assistant_usage_state")),
        sa.CheckConstraint("cost_micros >= 0", name=op.f("ck_assistant_usage_cost")),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_assistant_usage_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_assistant_usage_member_id_member"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_usage")),
    )
    op.create_index("ix_assistant_usage_household_month", "assistant_usage", ["household_id", "month"])
    op.create_index(op.f("ix_assistant_usage_member_id"), "assistant_usage", ["member_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_assistant_usage_member_id"), table_name="assistant_usage")
    op.drop_index("ix_assistant_usage_household_month", table_name="assistant_usage")
    op.drop_table("assistant_usage")
    op.drop_index(op.f("ix_assistant_conversation_owner_member_id"), table_name="assistant_conversation")
    op.drop_index("ix_assistant_conversation_household_last", table_name="assistant_conversation")
    op.drop_table("assistant_conversation")
