# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gambler badge (#108): ``contribution.kind`` accepts ``gambled``.

The check becomes the 0016 kinds plus ``gambled``. Nothing else changes: the badge key is a
plain string with no check, and the counts for calls already done are filled in by the app on
its next start (``services.contributions.ensure_backfilled``, marked by ``gambler_backfilled``
in the household settings).

SQLite changes a check constraint only by rebuilding the table. Nothing references
``contribution``, so the rebuild deletes nothing in other tables, and its rows are copied as
they are.

A downgrade refuses while any ``gambled`` row exists, rather than delete counts the old check
can't store. Production never downgrades; it rolls back by restoring the backup taken before
the upgrade (docs/setup.md, "Roll back").

Revision ID: 0018_gambler
Revises: 0017_slack_badge_file
Create Date: 2026-10-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_gambler"
down_revision: str | None = "0017_slack_badge_file"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GAMBLED_LEFT = (
    "This database has {rows} gambled contribution(s), which {revision} can't store. "
    "Restore the backup taken before the upgrade instead."
)
KINDS = (
    "'cooked', 'picked_up', 'ordered', 'coordinated', 'participated', 'responded_on_time', "
    "'suggested', 'added_item', 'explored', 'hosted', 'gave_feedback'"
)


def _replace_kind_check(kinds: str) -> None:
    with op.batch_alter_table("contribution", recreate="always") as batch_op:
        batch_op.drop_constraint("kind", type_="check")
        batch_op.create_check_constraint("kind", f"kind IN ({kinds})")


def upgrade() -> None:
    _replace_kind_check(f"{KINDS}, 'gambled'")


def downgrade() -> None:
    rows = op.get_bind().exec_driver_sql("SELECT count(*) FROM contribution WHERE kind = 'gambled'").scalar()
    if rows:
        raise RuntimeError(GAMBLED_LEFT.format(rows=rows, revision=down_revision))
    _replace_kind_check(KINDS)
