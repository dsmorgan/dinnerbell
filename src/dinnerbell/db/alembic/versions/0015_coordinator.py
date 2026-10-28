# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coordinator powers (#87): a shuffle counter and the picks message's note on it, the deadline
for the coordinator's choice of roles, and how a tie was broken.

``event.shuffles`` starts at 0 (every existing call has been drawn once, with its original
seed). The other four columns are NULL until they are used. SQLite adds each column in place, so
nothing is rebuilt. A downgrade drops them in place too (SQLite 3.35 or later): a call in flight
loses its shuffle count and its note on how a tie was broken. It refuses while any call is still
waiting on the coordinator's choice of roles (``roles_due_at`` set): that call has no one
assigned to its role yet, and an older release would never assign one. Let the choice finish, or
restore your backup.

Revision ID: 0015_coordinator
Revises: 0014_late_join
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_coordinator"
down_revision: str | None = "0014_late_join"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("shuffles", "shuffle_note", "roles_due_at", "tie_how", "tie_by")


def upgrade() -> None:
    op.add_column("event", sa.Column("shuffles", sa.Integer(), server_default="0", nullable=False))
    op.add_column("event", sa.Column("shuffle_note", sa.String(length=200), nullable=True))
    op.add_column("event", sa.Column("roles_due_at", sa.DateTime(), nullable=True))
    op.add_column("event", sa.Column("tie_how", sa.String(length=24), nullable=True))
    op.add_column("event", sa.Column("tie_by", sa.Integer(), nullable=True))


def downgrade() -> None:
    pending = op.get_bind().execute(sa.text("SELECT count(*) FROM event WHERE roles_due_at IS NOT NULL")).scalar_one()
    if pending:
        raise RuntimeError(
            f"{pending} call(s) are waiting for the coordinator to choose roles, and an older release would never "
            "assign them. Wait for the choice (or its time limit), or restore your backup, then downgrade."
        )
    for name in reversed(COLUMNS):
        op.execute(f"ALTER TABLE event DROP COLUMN {name}")
