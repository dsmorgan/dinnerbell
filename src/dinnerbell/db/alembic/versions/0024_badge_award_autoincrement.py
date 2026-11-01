# SPDX-License-Identifier: AGPL-3.0-or-later
"""Never reuse a badge award ID (#162): rebuild ``badge_award`` with AUTOINCREMENT.

An admin's correction after a meal is done can now take a badge back, which deletes its award.
Without AUTOINCREMENT, SQLite gives the next award the largest ID plus one, so deleting the newest
award would hand its ID to the next one. Two things name awards by ID and would then go wrong: a
badge post's outbox key is its lowest award ID, so the new award's post would be taken for one
already queued and never sent, and ``member.fun_seen_award`` is the newest award the den has
flipped for, so a member's next badge could go uncelebrated.

Nothing refers to ``badge_award`` by foreign key, so rebuilding it is safe with foreign keys on
or off. Copying the rows sets ``sqlite_sequence`` to the largest ID, so no existing ID is reused.
The upgrade then raises it to the largest ``member.fun_seen_award``, if that is higher: before
this revision, a deleted award's ID could already be in a den marker, such as one ``scrub-test``
cleared, and the next award must start above it to flip.

A downgrade drops that record, and upgrading again reseeds it from the largest ID left, so an
ID deleted in between could be given out again. Production never downgrades: it rolls back by
restoring a backup (docs/setup.md, "Roll back").

Revision ID: 0024_badge_award_autoincrement
Revises: 0023_fun_seen_award
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_badge_award_autoincrement"
down_revision: str | None = "0023_fun_seen_award"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ABOVE_DEN_MARKERS = (
    "UPDATE sqlite_sequence SET seq = max(seq, (SELECT coalesce(max(fun_seen_award), 0) FROM member)) "
    "WHERE name = 'badge_award'"
)
SEED_SEQUENCE = (
    "INSERT INTO sqlite_sequence (name, seq) "
    "SELECT 'badge_award', 0 WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'badge_award')"
)


def upgrade() -> None:
    with op.batch_alter_table("badge_award", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass
    # An empty table has no sqlite_sequence row until its first insert; add one so the marker counts.
    op.execute(SEED_SEQUENCE)
    op.execute(ABOVE_DEN_MARKERS)


def downgrade() -> None:
    with op.batch_alter_table("badge_award", recreate="always"):
        pass
