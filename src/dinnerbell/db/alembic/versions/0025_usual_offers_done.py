# SPDX-License-Identifier: AGPL-3.0-or-later
"""Usual offers start on (#180): ``response.usual_offers_done``.

Answering In on a call now turns on the offers a member's usual settings stand for, once per
call, and this column records that their turn has passed. Answers saved before this revision
were made without usual offers, so the upgrade marks every one done: an answer already saved
keeps the offers it has, and answering In on it again doesn't add any. Answers saved after it
start with the column off.

SQLite adds the column in place, so nothing is rebuilt. A downgrade drops it in place too
(SQLite 3.35 or later), as 0014 does.

Revision ID: 0025_usual_offers_done
Revises: 0024_badge_award_autoincrement
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_usual_offers_done"
down_revision: str | None = "0024_badge_award_autoincrement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("response", sa.Column("usual_offers_done", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute("UPDATE response SET usual_offers_done = 1")


def downgrade() -> None:
    op.execute("ALTER TABLE response DROP COLUMN usual_offers_done")
