# SPDX-License-Identifier: AGPL-3.0-or-later
"""Breakfast calls (#58): ``event.slot`` and ``meal_log.slot`` accept ``breakfast``.

Both checks become ``slot IN ('breakfast', 'lunch', 'dinner')``. Nothing else changes: the
breakfast schedule lives in the household settings JSON, and a household without one reads
the default, which has no daily call.

SQLite changes a check constraint only by rebuilding the table. ``event`` is the parent of
every call's rows, and ``meal_log`` of all feedback, so this refuses to run with foreign keys
on, which would cascade-delete them, as 0007, 0009, and 0010 do. ``run_migrations`` turns them
off. Both tables are AUTOINCREMENT, and each keeps its high-water mark across the rebuild, so
an ID deleted before the upgrade is never given out again.

A downgrade refuses while any breakfast call, offer, or meal exists, rather than delete them:
they're the household's history, and the old checks can't store them. Production never
downgrades; it rolls back by restoring the backup taken before the upgrade (docs/setup.md,
"Roll back").

Revision ID: 0013_breakfast
Revises: 0012_catalog_meals
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_breakfast"
down_revision: str | None = "0012_catalog_meals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_ON = (
    "Rebuilding the event and meal_log tables with foreign keys on would delete every call's responses, "
    "proposals, votes, assignments, outbox rows, and all feedback on meals. Run migrations with "
    "`dinnerbell migrate`, which turns them off."
)
BREAKFAST_LEFT = (
    "This database has {events} breakfast call(s) or offer(s) and {meals} breakfast meal(s), which "
    "{revision} can't store. Restore the backup taken before the upgrade instead."
)
TABLES = ("event", "meal_log")
WITH_BREAKFAST = "slot IN ('breakfast', 'lunch', 'dinner')"
WITHOUT_BREAKFAST = "slot IN ('lunch', 'dinner')"


def _require_foreign_keys_off() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(FOREIGN_KEYS_ON)


def _sequence(table: str) -> int | None:
    found = op.get_bind().exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).scalar()
    return None if found is None else int(found)


def _restore_sequence(table: str, before: int | None) -> None:
    """A rebuild copies the rows into a new table, which reseeds the sequence from the largest
    ID left. Keep the old mark when it's higher, so a discarded call's ID (#60) or a deleted
    meal's (#56) never comes back."""
    if before is None:
        return
    now = _sequence(table)
    if now is None:
        op.get_bind().exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, before))
    elif now < before:
        op.get_bind().exec_driver_sql("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (before, table))


def _replace_slot_check(check: str) -> None:
    for table in TABLES:
        before = _sequence(table)
        with op.batch_alter_table(table, recreate="always", table_kwargs={"sqlite_autoincrement": True}) as batch_op:
            batch_op.drop_constraint("slot", type_="check")
            batch_op.create_check_constraint("slot", check)
        _restore_sequence(table, before)


def upgrade() -> None:
    _require_foreign_keys_off()
    _replace_slot_check(WITH_BREAKFAST)


def downgrade() -> None:
    _require_foreign_keys_off()
    bind = op.get_bind()
    events = bind.exec_driver_sql("SELECT count(*) FROM event WHERE slot = 'breakfast'").scalar()
    meals = bind.exec_driver_sql("SELECT count(*) FROM meal_log WHERE slot = 'breakfast'").scalar()
    if events or meals:
        raise RuntimeError(BREAKFAST_LEFT.format(events=events, meals=meals, revision=down_revision))
    _replace_slot_check(WITHOUT_BREAKFAST)
