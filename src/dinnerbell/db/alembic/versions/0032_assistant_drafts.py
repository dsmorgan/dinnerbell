# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's drafts (ADR 0009, #17): ``assistant_draft``.

A row per change the assistant drafted for a member: a proposal, an offer, or catalog items,
with what **Confirm** does. A draft lasts 30 minutes and is confirmed at most once; the
scheduler deletes it once it expires. The table starts empty, and nothing writes it unless the
assistant is on. The downgrade drops it, losing drafts still waiting for Confirm.

Revision ID: 0032_assistant_drafts
Revises: 0031_assistant
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_assistant_drafts"
down_revision: str | None = "0031_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_draft",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("conversation_key", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("run", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("lines", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind IN ('proposal', 'offer', 'catalog')", name=op.f("ck_assistant_draft_kind")),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_assistant_draft_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_assistant_draft_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_draft")),
    )
    op.create_index("ix_assistant_draft_household_expires", "assistant_draft", ["household_id", "expires_at"])
    op.create_index(op.f("ix_assistant_draft_member_id"), "assistant_draft", ["member_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_assistant_draft_member_id"), table_name="assistant_draft")
    op.drop_index("ix_assistant_draft_household_expires", table_name="assistant_draft")
    op.drop_table("assistant_draft")
