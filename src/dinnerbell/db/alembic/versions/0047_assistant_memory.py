# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's memory (#367, ADR 0012, spec 6.14 **Memory**): ``assistant_memory_fact``.

Each row is one fact about one member, written by the memory rollup from that member's own
messages: its kind, its text, the conversation it came from, when it was written or last
replaced, and, for an experience, when it expires. IDs are AUTOINCREMENT, so **Remove** on an old
page never removes a newer fact.

A conversation's rollup bookkeeping lives in its transcript JSON, with no schema change: each
member message kept from now on records when it was written and, while **Memory** is on, that
it waits for the rollup. Messages stored before this revision have neither, so nothing already
kept is ever rolled up.

A new table, so nothing is rebuilt. A downgrade drops it, and every fact with it.

Revision ID: 0047_assistant_memory
Revises: 0046_direct_actions
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from dinnerbell.db.types import UTCDateTime

revision: str = "0047_assistant_memory"
down_revision: str | None = "0046_direct_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assistant_memory_fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.String(length=160), nullable=False),
        sa.Column("source_key", sa.String(length=80), nullable=False),
        sa.Column("written_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('like', 'dislike', 'usual', 'experience', 'belief', 'routine')",
            name=op.f("ck_assistant_memory_fact_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_assistant_memory_fact_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_assistant_memory_fact_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_memory_fact")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_assistant_memory_fact_household_member", "assistant_memory_fact", ["household_id", "member_id"])
    op.create_index(op.f("ix_assistant_memory_fact_member_id"), "assistant_memory_fact", ["member_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_assistant_memory_fact_member_id"), table_name="assistant_memory_fact")
    op.drop_index("ix_assistant_memory_fact_household_member", table_name="assistant_memory_fact")
    op.drop_table("assistant_memory_fact")
