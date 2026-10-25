# SPDX-License-Identifier: AGPL-3.0-or-later
"""Never reuse an event ID (#60): rebuild ``event`` with AUTOINCREMENT.

Without it SQLite gives a new row the largest ID plus one, so discarding the newest call on a
test instance would hand its ID to the next call. Outbox keys, Slack buttons, page links, and
audit targets all name events by ID, and would then point at the wrong call. ``assignment`` is
AUTOINCREMENT for the same reason.

Rebuilding a parent table is safe only with foreign keys off: dropping the old ``event`` with
them on cascade-deletes every response, proposal, vote, assignment, and outbox row, and nulls
``meal_log.event_id``. ``run_migrations`` turns them off; this revision refuses to run if they're
on. Copying the rows sets ``sqlite_sequence`` to the largest ID, so no existing ID is reused.

Revision ID: 0007_event_autoincrement
Revises: 0006_event_ended_by
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_event_autoincrement"
down_revision: str | None = "0006_event_ended_by"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_ON = (
    "Rebuilding the event table with foreign keys on would delete every call's responses, proposals, "
    "votes, assignments, and outbox rows. Run migrations with `dinnerbell migrate`, which turns them off."
)


def _require_foreign_keys_off() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(FOREIGN_KEYS_ON)


def upgrade() -> None:
    _require_foreign_keys_off()
    with op.batch_alter_table("event", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass


def downgrade() -> None:
    _require_foreign_keys_off()
    with op.batch_alter_table("event", recreate="always"):
        pass
