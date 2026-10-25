# SPDX-License-Identifier: AGPL-3.0-or-later
"""Never reuse a meal ID (#56): rebuild ``meal_log`` with AUTOINCREMENT.

Without it SQLite gives a new row the largest ID plus one, so once ``dinnerbell reset-history``
or a deleted meal frees the newest IDs, the next meal takes one. A History or feedback page
left open across the change would then post to a different meal. ``event`` (0007) and
``assignment`` are AUTOINCREMENT for the same reason.

Rebuilding a parent table is safe only with foreign keys off: dropping the old ``meal_log``
with them on cascade-deletes every row of ``feedback``. ``run_migrations`` turns them off; this
revision refuses to run if they're on. Copying the rows sets ``sqlite_sequence`` to the largest
ID, so no existing ID is reused.

A downgrade drops that record, and upgrading again reseeds it from the largest ID left, so an
ID deleted in between could be given out again. Production never downgrades: it rolls back by
restoring a backup (docs/setup.md, "Roll back").

Revision ID: 0009_meal_log_autoincrement
Revises: 0008_meal_feedback
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_meal_log_autoincrement"
down_revision: str | None = "0008_meal_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_ON = (
    "Rebuilding the meal_log table with foreign keys on would delete all feedback on meals. "
    "Run migrations with `dinnerbell migrate`, which turns them off."
)


def _require_foreign_keys_off() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(FOREIGN_KEYS_ON)


def upgrade() -> None:
    _require_foreign_keys_off()
    with op.batch_alter_table("meal_log", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass


def downgrade() -> None:
    _require_foreign_keys_off()
    with op.batch_alter_table("meal_log", recreate="always"):
        pass
