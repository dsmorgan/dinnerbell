# SPDX-License-Identifier: AGPL-3.0-or-later
"""When an assistant draft was confirmed (#17): ``assistant_draft.confirmed_at``.

``used_at`` is set by a Confirm or a Cancel alike, so it can't tell them apart. The import link a
Slack Confirm gives for a catalog draft needs to know which, so that a cancelled draft can never
open the import preview. Every draft saved before the upgrade gets none, as if cancelled: a
catalog draft confirmed in the last 24 hours loses its import link, and its member asks again.
The downgrade drops the column.

Revision ID: 0033_assistant_draft_confirmed
Revises: 0032_assistant_drafts
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_assistant_draft_confirmed"
down_revision: str | None = "0032_assistant_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assistant_draft", sa.Column("confirmed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    # In place, as 0027 does, so the table comes back exactly as it was.
    op.execute("ALTER TABLE assistant_draft DROP COLUMN confirmed_at")
