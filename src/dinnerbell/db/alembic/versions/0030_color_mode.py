# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light, dark, or auto (#231): ``member.color_mode``, auto for everyone.

Each member and guest chooses whether the web pages follow their device (``auto``) or stay
``light`` or ``dark``. Everyone who exists already starts on auto, which is how pages looked
before, as new members and guests do. A check constraint keeps out any other value.

SQLite adds the column, with its check, in place, so nothing is rebuilt. A downgrade rebuilds
``member`` in batch mode, as 0022's does, dropping the check and then the column: once a later
revision has rebuilt the table, the check is a table constraint, and SQLite refuses to drop a
column in place while one names it. Everyone's choice is lost and pages follow the device again.

Revision ID: 0030_color_mode
Revises: 0029_guests_dont_coordinate
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030_color_mode"
down_revision: str | None = "0029_guests_dont_coordinate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Raw SQL, because SQLite only adds a column with its check in place this way; the check's
    # name is the one models.py's naming convention gives it.
    op.execute(
        "ALTER TABLE member ADD COLUMN color_mode VARCHAR(5) DEFAULT 'auto' NOT NULL "
        "CONSTRAINT ck_member_color_mode CHECK (color_mode IN ('auto', 'light', 'dark'))"
    )


def downgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.drop_constraint(op.f("ck_member_color_mode"), type_="check")
        batch_op.drop_column("color_mode")
