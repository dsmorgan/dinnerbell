# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light, dark, or auto (#231): ``member.color_mode``, auto for everyone.

Each member and guest chooses whether the web pages follow their device (``auto``) or stay
``light`` or ``dark``. Everyone who exists already starts on auto, which is how pages looked
before, as new members and guests do. A check constraint keeps out any other value.

SQLite adds the column, with its check, in place, so nothing is rebuilt. A downgrade drops it in
place too (SQLite 3.35 or later), as 0025 does, so everyone's choice is lost and pages follow
the device again.

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
    op.execute("ALTER TABLE member DROP COLUMN color_mode")
