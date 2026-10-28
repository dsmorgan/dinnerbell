# SPDX-License-Identifier: AGPL-3.0-or-later
"""Joining a call late, during the vote (#76): ``response.late`` marks someone who joined after
missing the answers, and ``response.vote_counts`` whether the coordinator counts their votes.
Every existing answer was on time, so both start false.

SQLite adds a column in place, so nothing is rebuilt. A downgrade drops both columns in place
too (SQLite 3.35 or later), and with them who joined late: those answers become ordinary ones.

Revision ID: 0014_late_join
Revises: 0013_breakfast
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_late_join"
down_revision: str | None = "0013_breakfast"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("late", "vote_counts")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("response", sa.Column(name, sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.execute(f"ALTER TABLE response DROP COLUMN {name}")
