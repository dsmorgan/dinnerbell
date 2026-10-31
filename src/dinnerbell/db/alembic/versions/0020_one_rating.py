# SPDX-License-Identifier: AGPL-3.0-or-later
"""One rating per meal (FR-46): ``feedback.rating`` replaces ``food`` and ``place``, and
``feedback.issue_note`` holds what went wrong for the "Other" issue.

``rating`` is one of ``hated``, ``disliked``, ``fine``, ``liked``, or ``loved`` (scores -2 to
+2), or NULL for no answer. The upgrade converts each row: ``loved`` scores +2, ``fine`` 0, and
``not_again`` -2; a row with both a food and a place rating gets their average (always a whole
number with these scores), a row with one gets that one, and a row with neither gets NULL. So
food loved and place fine becomes ``liked``, and food fine and place not again ``disliked``.

The downgrade turns ``rating`` back into ``food`` (``hated`` and ``disliked`` become
``not_again``, ``fine`` stays ``fine``, and ``liked`` and ``loved`` become ``loved``), leaves
``place`` NULL, and drops ``issue_note``. The "Other" chip stays. Production never downgrades;
it rolls back by restoring the backup taken before the upgrade (docs/setup.md, "Roll back").

SQLite drops a column or changes a check constraint only by rebuilding the table. Nothing
references ``feedback``, so the rebuild deletes nothing in other tables.

Revision ID: 0020_one_rating
Revises: 0019_slack_badge_composite
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_one_rating"
down_revision: str | None = "0019_slack_badge_composite"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VALUES = "'loved', 'fine', 'not_again'"
RATINGS = "'hated', 'disliked', 'fine', 'liked', 'loved'"


def _score(column: str) -> str:
    return f"(CASE {column} WHEN 'loved' THEN 2 WHEN 'fine' THEN 0 WHEN 'not_again' THEN -2 END)"


FOOD, PLACE = _score("food"), _score("place")
SCORE = f"(CASE WHEN food IS NULL THEN {PLACE} WHEN place IS NULL THEN {FOOD} ELSE ({FOOD} + {PLACE}) / 2 END)"
TO_RATING = (
    f"UPDATE feedback SET rating = CASE {SCORE} "  # noqa: S608 - fixed SQL
    "WHEN 2 THEN 'loved' WHEN 1 THEN 'liked' WHEN 0 THEN 'fine' WHEN -1 THEN 'disliked' WHEN -2 THEN 'hated' END"
)
TO_FOOD = (
    "UPDATE feedback SET place = NULL, food = CASE rating "
    "WHEN 'hated' THEN 'not_again' WHEN 'disliked' THEN 'not_again' WHEN 'fine' THEN 'fine' "
    "WHEN 'liked' THEN 'loved' WHEN 'loved' THEN 'loved' END"
)


def upgrade() -> None:
    op.add_column("feedback", sa.Column("rating", sa.String(length=16), nullable=True))
    op.add_column("feedback", sa.Column("issue_note", sa.String(length=200), nullable=True))
    op.execute(TO_RATING)
    with op.batch_alter_table("feedback", recreate="always") as batch_op:
        batch_op.drop_constraint("food", type_="check")
        batch_op.drop_constraint("place", type_="check")
        batch_op.drop_column("food")
        batch_op.drop_column("place")
        batch_op.create_check_constraint("rating", f"rating IS NULL OR rating IN ({RATINGS})")


def downgrade() -> None:
    op.add_column("feedback", sa.Column("food", sa.String(length=16), nullable=True))
    op.add_column("feedback", sa.Column("place", sa.String(length=16), nullable=True))
    op.execute(TO_FOOD)
    with op.batch_alter_table("feedback", recreate="always") as batch_op:
        batch_op.drop_constraint("rating", type_="check")
        batch_op.drop_column("rating")
        batch_op.drop_column("issue_note")
        batch_op.create_check_constraint("food", f"food IS NULL OR food IN ({OLD_VALUES})")
        batch_op.create_check_constraint("place", f"place IS NULL OR place IN ({OLD_VALUES})")
