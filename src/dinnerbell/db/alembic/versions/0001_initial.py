# SPDX-License-Identifier: AGPL-3.0-or-later
"""M0 tables: household, member, session, audit.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "household",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_household")),
    )
    op.create_table(
        "member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("oidc_subject", sa.String(length=255), nullable=True),
        sa.Column("slack_user_id", sa.String(length=32), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("can_cook", sa.Boolean(), nullable=False),
        sa.Column("can_pickup", sa.Boolean(), nullable=False),
        sa.Column("can_order", sa.Boolean(), nullable=False),
        sa.Column("mute_dm_reminders", sa.Boolean(), nullable=False),
        sa.Column("feedback_reminder", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'member')", name=op.f("ck_member_role")),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_member_household_id_household"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member")),
        sa.UniqueConstraint("household_id", "email", name=op.f("uq_member_household_id_email")),
        sa.UniqueConstraint("household_id", "oidc_subject", name=op.f("uq_member_household_id_oidc_subject")),
        sa.UniqueConstraint("household_id", "slack_user_id", name=op.f("uq_member_household_id_slack_user_id")),
    )
    op.create_index(op.f("ix_member_household_id"), "member", ["household_id"], unique=False)
    op.create_table(
        "session",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_session_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_session_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
    )
    op.create_index("ix_session_expires_at", "session", ["expires_at"], unique=False)
    op.create_index(op.f("ix_session_member_id"), "session", ["member_id"], unique=False)
    op.create_table(
        "audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("actor_member_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_member_id"], ["member.id"], name=op.f("fk_audit_actor_member_id_member"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_audit_household_id_household"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit")),
    )
    op.create_index("ix_audit_household_created", "audit", ["household_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_household_created", table_name="audit")
    op.drop_table("audit")
    op.drop_index(op.f("ix_session_member_id"), table_name="session")
    op.drop_index("ix_session_expires_at", table_name="session")
    op.drop_table("session")
    op.drop_index(op.f("ix_member_household_id"), table_name="member")
    op.drop_table("member")
    op.drop_table("household")
