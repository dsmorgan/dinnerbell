# SPDX-License-Identifier: AGPL-3.0-or-later
"""Never reuse a proposal ID (#330): rebuild ``proposal`` with AUTOINCREMENT.

A shuffle deletes picks and adds new ones, and a re-run deletes a call's picks and draws them
again. Without AUTOINCREMENT, SQLite gives each new pick the largest ID plus one, so the new
picks took the deleted picks' IDs. A Vote, Lock this, Break the tie, or way names its pick by ID
alone, so a tap on a message or page that still showed the old picks acted on a different place.
With AUTOINCREMENT the old ID is gone for good, and the tap gets "No such proposal."

Rebuilding a parent table is safe only with foreign keys off: dropping the old ``proposal`` with
them on cascade-deletes every vote. ``run_migrations`` turns them off; this revision refuses to
run if they're on. Copying the rows sets ``sqlite_sequence`` to the largest ID, so no existing ID
is reused. An empty table has no ``sqlite_sequence`` row until its first insert, so the upgrade
adds one at 0. An ID deleted before the upgrade, above the largest one left, can't be known and
could come back once.

A downgrade drops that record, and upgrading again reseeds it from the largest ID left, so an
ID deleted in between could be given out again. Production never downgrades: it rolls back by
restoring a backup (docs/setup.md, "Roll back").

Revision ID: 0045_proposal_autoincrement
Revises: 0044_local_weather
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0045_proposal_autoincrement"
down_revision: str | None = "0044_local_weather"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_ON = (
    "Rebuilding the proposal table with foreign keys on would delete every vote. "
    "Run migrations with `dinnerbell migrate`, which turns them off."
)
SEED_SEQUENCE = (
    "INSERT INTO sqlite_sequence (name, seq) "
    "SELECT 'proposal', coalesce((SELECT max(id) FROM proposal), 0) "
    "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'proposal')"
)


def _require_foreign_keys_off() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(FOREIGN_KEYS_ON)


def upgrade() -> None:
    _require_foreign_keys_off()
    with op.batch_alter_table("proposal", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass
    op.execute(SEED_SEQUENCE)


def downgrade() -> None:
    _require_foreign_keys_off()
    with op.batch_alter_table("proposal", recreate="always"):
        pass
