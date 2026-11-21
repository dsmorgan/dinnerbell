# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web push (#138, spec 6.15): ``push_subscription`` and ``push_queue``, both empty.

``push_subscription`` holds each device a member turned notifications on for: the browser's push
endpoint and keys. ``push_queue`` holds one row per push to one device, queued with its outbox
row and keyed by it and the device. Both start empty, and nothing else changes, so a household
that never turns the web app on has two empty tables. A downgrade drops both; members turn
notifications on again after the next upgrade.

Revision ID: 0043_web_push
Revises: 0042_sign_in_codes
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from dinnerbell.db.types import UTCDateTime

revision: str = "0043_web_push"
down_revision: str | None = "0042_sign_in_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscription",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=1024), nullable=False),
        sa.Column("p256dh", sa.String(length=100), nullable=False),
        sa.Column("auth", sa.String(length=32), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_success_at", UTCDateTime(), nullable=True),
        sa.Column("auth_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("auth_failed_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_push_subscription_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_push_subscription_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_subscription")),
        sa.UniqueConstraint("household_id", "endpoint", name=op.f("uq_push_subscription_household_id_endpoint")),
    )
    op.create_index(op.f("ix_push_subscription_member_id"), "push_subscription", ["member_id"], unique=False)
    op.create_table(
        "push_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("outbox_id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("available_at", UTCDateTime(), nullable=False),
        sa.Column("claim_token", sa.String(length=32), nullable=True),
        sa.Column("claimed_until", UTCDateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.Column("sent_at", UTCDateTime(), nullable=True),
        sa.Column("closed_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_push_queue_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_push_queue_member_id_member"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"], ["outbox.id"], name=op.f("fk_push_queue_outbox_id_outbox"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["push_subscription.id"],
            name=op.f("fk_push_queue_subscription_id_push_subscription"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_queue")),
        sa.UniqueConstraint("outbox_id", "subscription_id", name=op.f("uq_push_queue_outbox_id_subscription_id")),
    )
    op.create_index(op.f("ix_push_queue_household_id"), "push_queue", ["household_id"], unique=False)
    op.create_index(op.f("ix_push_queue_subscription_id"), "push_queue", ["subscription_id"], unique=False)
    op.create_index("ix_push_queue_pending", "push_queue", ["closed_at", "sent_at", "available_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_push_queue_pending", table_name="push_queue")
    op.drop_index(op.f("ix_push_queue_subscription_id"), table_name="push_queue")
    op.drop_index(op.f("ix_push_queue_household_id"), table_name="push_queue")
    op.drop_table("push_queue")
    op.drop_index(op.f("ix_push_subscription_member_id"), table_name="push_subscription")
    op.drop_table("push_subscription")
