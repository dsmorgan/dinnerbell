# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode badges (#153): ``member.fun_seen_award``.

The den backflips once for each badge a member earns, the next time they open Today, and
``fun_seen_award`` is the newest award it has shown them. The upgrade sets it to each member's
newest award, so nobody gets a flip for a badge they earned before it. The downgrade drops the
column.

Revision ID: 0023_fun_seen_award
Revises: 0022_fun_mode
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_fun_seen_award"
down_revision: str | None = "0022_fun_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = (
    "UPDATE member SET fun_seen_award = "
    "(SELECT coalesce(max(id), 0) FROM badge_award WHERE badge_award.member_id = member.id)"
)


def upgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.add_column(sa.Column("fun_seen_award", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.execute(BACKFILL)


def downgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.drop_column("fun_seen_award")
