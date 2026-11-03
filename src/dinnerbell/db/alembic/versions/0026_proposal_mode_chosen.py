# SPDX-License-Identifier: AGPL-3.0-or-later
"""Takeout's way is chosen (#178): ``proposal.mode_chosen``, off for every pick.

While voting, the coordinator or an admin can set a takeout pick whose place allows both to
pickup or delivery, and a pick a member added, or an offer, keeps the way they chose. A pick
whose way was chosen doesn't ask again at the decision. The upgrade marks every pick someone
added (``proposed_by`` set, which an offer's pick has too) as chosen, so a call voting during the
upgrade doesn't ask about one; a drawn pick starts unset. The downgrade drops the column.

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
    op.execute("UPDATE proposal SET mode_chosen = 1 WHERE proposed_by IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table("proposal") as batch_op:
        batch_op.drop_column("mode_chosen")
