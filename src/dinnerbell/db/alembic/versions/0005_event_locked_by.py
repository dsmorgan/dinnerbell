# SPDX-License-Identifier: AGPL-3.0-or-later
"""Store who locked a call's decision (FR-15), so every edit of the decision message can say so.

Revision ID: 0005_event_locked_by
Revises: 0004_slack_link_lock
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_event_locked_by"
down_revision: str | None = "0004_slack_link_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("event") as batch_op:
        batch_op.add_column(sa.Column("locked_by", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_event_locked_by_member"), "member", ["locked_by"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("event") as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_event_locked_by_member"), type_="foreignkey")
        batch_op.drop_column("locked_by")
