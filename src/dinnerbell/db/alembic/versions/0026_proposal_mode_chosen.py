# SPDX-License-Identifier: AGPL-3.0-or-later
"""Takeout's way is chosen (#178): ``proposal.mode_chosen``, off for every pick.

While voting, the coordinator or an admin can set a takeout pick whose place allows both to
pickup or delivery. A pick they set doesn't ask again at the decision. Every pick that exists
already starts unset, as a new drawn pick does. The downgrade drops the column.

Revision ID: 0026_proposal_mode_chosen
Revises: 0025_usual_offers_done
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_proposal_mode_chosen"
down_revision: str | None = "0025_usual_offers_done"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("proposal") as batch_op:
        batch_op.add_column(sa.Column("mode_chosen", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("proposal") as batch_op:
        batch_op.drop_column("mode_chosen")
