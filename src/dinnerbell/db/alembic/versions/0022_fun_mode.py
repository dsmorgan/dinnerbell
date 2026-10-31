# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fun mode (#153): ``member.fun_mode``, on for everyone.

Each member and guest can turn Dinnerbone's Den off for themselves. Everyone who exists already
starts with it on, as new members and guests do. The downgrade drops the column.

Revision ID: 0022_fun_mode
Revises: 0021_feedback_ranking
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_fun_mode"
down_revision: str | None = "0021_feedback_ranking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.add_column(sa.Column("fun_mode", sa.Boolean(), server_default=sa.true(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.drop_column("fun_mode")
