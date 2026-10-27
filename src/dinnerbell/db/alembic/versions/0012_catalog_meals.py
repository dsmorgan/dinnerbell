# SPDX-License-Identifier: AGPL-3.0-or-later
"""The meals each catalog item is for (#72): ``catalog_item.meals``, as bits (breakfast 1, lunch
2, dinner 4), with a check that at least one is set. Every existing item gets lunch and dinner
(6), the default, so ranking proposes the same items as before until someone changes one.
Breakfast is stored now, before it's a slot (#58), so breakfast calls need no migration.

SQLite adds a check constraint only by rebuilding the table. ``catalog_item`` is the parent of
its tags, cooks, preferences, suggestions, proposals, meals, and feedback, so this refuses to
run with foreign keys on, which would cascade-delete or refuse to drop them, as 0011 does.
``run_migrations`` turns them off. ``catalog_item`` isn't AUTOINCREMENT, but if it ever has a
sequence row, its high-water mark is kept across the rebuild.

A downgrade drops the column, and with it which meals each item is for.

Revision ID: 0012_catalog_meals
Revises: 0011_guests
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_catalog_meals"
down_revision: str | None = "0011_guests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_ON = (
    "Rebuilding the catalog_item table with foreign keys on would delete every item's tags, cooks, "
    "preferences, and suggestions. Run migrations with `dinnerbell migrate`, which turns them off."
)


def _require_foreign_keys_off() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(FOREIGN_KEYS_ON)


def _sequence(table: str) -> int | None:
    found = op.get_bind().exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).scalar()
    return None if found is None else int(found)


def _restore_sequence(table: str, before: int | None) -> None:
    """Keep ``table``'s AUTOINCREMENT high-water mark, if it has one, across a rebuild."""
    if before is None:
        return
    now = _sequence(table)
    if now is None:
        op.get_bind().exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, before))
    elif now < before:
        op.get_bind().exec_driver_sql("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (before, table))


def upgrade() -> None:
    _require_foreign_keys_off()
    before = _sequence("catalog_item")
    with op.batch_alter_table("catalog_item", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("meals", sa.Integer(), server_default=sa.text("6"), nullable=False))
        batch_op.create_check_constraint("meals", "meals BETWEEN 1 AND 7")
    _restore_sequence("catalog_item", before)


def downgrade() -> None:
    _require_foreign_keys_off()
    before = _sequence("catalog_item")
    with op.batch_alter_table("catalog_item", recreate="always") as batch_op:
        batch_op.drop_constraint("meals", type_="check")
        batch_op.drop_column("meals")
    _restore_sequence("catalog_item", before)
