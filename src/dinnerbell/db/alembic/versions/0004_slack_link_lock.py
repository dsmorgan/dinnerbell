# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remember when the admin set or cleared a member's Slack link by hand (FR-42).

``member.slack_link_locked`` stops the hourly email match and sign-in from changing a link the
admin chose, including a link the admin removed.

Revision ID: 0004_slack_link_lock
Revises: 0003_dinner_calls
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_slack_link_lock"
down_revision: str | None = "0003_dinner_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.add_column(sa.Column("slack_link_locked", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.drop_column("slack_link_locked")
