# SPDX-License-Identifier: AGPL-3.0-or-later
"""Away (#196): ``member.away_first`` and ``member.away_last``, and ``response.away_until``.

A member's away window is a first and a last day away, both empty until they set one. An out
that the window wrote for them carries the day they're back in ``response.away_until``; every
answer saved before the upgrade has none. The downgrade drops the columns, so away windows and
the "away until" marks are lost, and those outs read as ordinary outs.

Revision ID: 0027_away
Revises: 0026_proposal_mode_chosen
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_away"
down_revision: str | None = "0026_proposal_mode_chosen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("member", sa.Column("away_first", sa.Date(), nullable=True))
    op.add_column("member", sa.Column("away_last", sa.Date(), nullable=True))
    op.add_column("response", sa.Column("away_until", sa.Date(), nullable=True))


def downgrade() -> None:
    # In place, as 0025 does, so the tables come back exactly as they were.
    op.execute("ALTER TABLE response DROP COLUMN away_until")
    op.execute("ALTER TABLE member DROP COLUMN away_last")
    op.execute("ALTER TABLE member DROP COLUMN away_first")
