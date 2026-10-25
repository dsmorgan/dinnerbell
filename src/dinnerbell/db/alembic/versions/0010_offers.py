# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offers (FR-4): an event's spare servings, ``capacity``, set for offers and only for offers.

An offer is an ``event`` of kind ``offer``. Its item, mode, and time use the columns a decided
call has; only the spare servings are new. The check constraint keeps calls without a capacity
and every offer between 1 and 20 seats.

SQLite adds a check constraint only by rebuilding the table, so this rebuilds ``event`` as 0007
does, keeping AUTOINCREMENT and its high-water mark, so an ID deleted before the upgrade is never
given out again. It refuses to run with foreign keys on, which would cascade-delete every call's
rows. ``run_migrations`` turns them off.

Revision ID: 0010_offers
Revises: 0009_meal_log_autoincrement
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_offers"
down_revision: str | None = "0009_meal_log_autoincrement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_ON = (
    "Rebuilding the event table with foreign keys on would delete every call's responses, proposals, "
    "votes, assignments, and outbox rows. Run migrations with `dinnerbell migrate`, which turns them off."
)
CAPACITY = (
    "(kind = 'call' AND capacity IS NULL) OR (kind = 'offer' AND capacity IS NOT NULL AND capacity BETWEEN 1 AND 20)"
)


def _require_foreign_keys_off() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(FOREIGN_KEYS_ON)


def _sequence() -> int:
    """``event``'s AUTOINCREMENT high-water mark: the largest ID it ever gave out."""
    found = op.get_bind().exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'event'").scalar()
    return int(found or 0)


def _restore_sequence(before: int) -> None:
    """A rebuild copies the rows into a new table, which reseeds the sequence from the largest
    ID left. An ID deleted before it, such as a call discarded on a test instance (#60), would
    come back, so keep the old mark when it's higher."""
    bind = op.get_bind()
    now = int(bind.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'event'").scalar() or 0)
    if before <= now:
        return
    if bind.exec_driver_sql("SELECT count(*) FROM sqlite_sequence WHERE name = 'event'").scalar():
        bind.exec_driver_sql("UPDATE sqlite_sequence SET seq = ? WHERE name = 'event'", (before,))
    else:
        bind.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES ('event', ?)", (before,))


def upgrade() -> None:
    _require_foreign_keys_off()
    before = _sequence()
    with op.batch_alter_table("event", recreate="always", table_kwargs={"sqlite_autoincrement": True}) as batch_op:
        batch_op.add_column(sa.Column("capacity", sa.Integer(), nullable=True))
        batch_op.create_check_constraint("capacity", CAPACITY)
    _restore_sequence(before)


def downgrade() -> None:
    _require_foreign_keys_off()
    # Offers can't be stored without the column. With foreign keys off nothing cascades, so
    # their rows go explicitly, and a meal an offer logged stays in the history unlinked.
    offers = "SELECT id FROM event WHERE kind = 'offer'"
    for table in ("vote", "proposal", "suggestion", "response", "assignment", "outbox"):
        op.execute(f"DELETE FROM {table} WHERE event_id IN ({offers})")  # noqa: S608 - fixed names
    op.execute(f"UPDATE meal_log SET event_id = NULL WHERE event_id IN ({offers})")  # noqa: S608
    op.execute("DELETE FROM event WHERE kind = 'offer'")
    before = _sequence()
    with op.batch_alter_table("event", recreate="always", table_kwargs={"sqlite_autoincrement": True}) as batch_op:
        batch_op.drop_constraint("capacity", type_="check")
        batch_op.drop_column("capacity")
    _restore_sequence(before)
