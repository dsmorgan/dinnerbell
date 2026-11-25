# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant acts directly, with **Undo** (#374, spec 6.14): ``assistant_draft.direct`` and
``assistant_draft.undone_at``.

In a private conversation, an answer, a vote, a role, a suggestion, an answer for a guest, and
some changes to the member's own data happen at once. Each is stored as a draft row its action
used at once, marked ``direct``, with what **Undo** puts back in its payload. ``undone_at`` is when
**Undo** used it; null while it hasn't. Every row that exists already is a draft, so ``direct``
starts false and ``undone_at`` null.

SQLite adds both columns in place, so nothing is rebuilt. A downgrade drops them in batch mode;
the direct actions' rows then read as confirmed drafts until the purge deletes them.

Revision ID: 0046_direct_actions
Revises: 0045_proposal_autoincrement
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from dinnerbell.db.types import UTCDateTime

revision: str = "0046_direct_actions"
down_revision: str | None = "0045_proposal_autoincrement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assistant_draft", sa.Column("direct", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("assistant_draft", sa.Column("undone_at", UTCDateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("assistant_draft") as batch_op:
        batch_op.drop_column("undone_at")
        batch_op.drop_column("direct")
