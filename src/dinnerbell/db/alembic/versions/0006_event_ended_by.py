# SPDX-License-Identifier: AGPL-3.0-or-later
"""Store who called a call off and why (FR-56), for the call message, the web, and App Home.

Revision ID: 0006_event_ended_by
Revises: 0005_event_locked_by
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_event_ended_by"
down_revision: str | None = "0005_event_locked_by"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("event") as batch_op:
        batch_op.add_column(sa.Column("ended_by", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("end_note", sa.String(length=140), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_event_ended_by_member"), "member", ["ended_by"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("event") as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_event_ended_by_member"), type_="foreignkey")
        batch_op.drop_column("end_note")
        batch_op.drop_column("ended_by")
