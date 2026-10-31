# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback-informed ranking (FR-55, ADR 0008): the ``stance_edit`` table,
``feedback.rating_saved_at``, and the **Feedback** setting off for every household that exists
already.

``stance_edit`` records when a member last set their stance on an item, so ratings saved before
it stop moving their score. A neutral stance deletes its ``preference`` row, so the time needs a
table of its own. The upgrade fills it from every item stance stored now, with the time it was
last saved; a stance set back to neutral before this revision left no row, so its earlier
ratings count until the member sets it again.

``feedback.rating_saved_at`` is when each rating last changed, which a stance edit after it
overrides; re-saving feedback without changing the rating leaves it. The upgrade fills it from
``updated_at``. The downgrade drops it, rebuilding ``feedback``, which nothing references.

The upgrade also sets ``feedback_cap`` to 0 in each household's settings, so ratings don't move
scores until the admin turns Feedback on. Households created later start at the default, 2. The
downgrade drops the table, the column, and the setting. Nothing references ``stance_edit``.

Revision ID: 0021_feedback_ranking
Revises: 0020_one_rating
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_feedback_ranking"
down_revision: str | None = "0020_one_rating"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = (
    "INSERT INTO stance_edit (household_id, member_id, item_id, edited_at) "
    "SELECT household_id, member_id, item_id, updated_at FROM preference WHERE item_id IS NOT NULL"
)


def upgrade() -> None:
    op.create_table(
        "stance_edit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("edited_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_stance_edit_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_stance_edit_item_id_catalog_item"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_stance_edit_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stance_edit")),
        sa.UniqueConstraint("member_id", "item_id", name=op.f("uq_stance_edit_member_id_item_id")),
    )
    with op.batch_alter_table("stance_edit", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_stance_edit_item_id"), ["item_id"], unique=False)
    op.execute(BACKFILL)
    op.add_column("feedback", sa.Column("rating_saved_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE feedback SET rating_saved_at = updated_at")
    op.execute("UPDATE household SET settings = json_set(settings, '$.feedback_cap', 0)")


def downgrade() -> None:
    op.execute("UPDATE household SET settings = json_remove(settings, '$.feedback_cap')")
    with op.batch_alter_table("feedback", recreate="always") as batch_op:
        batch_op.drop_column("rating_saved_at")
    with op.batch_alter_table("stance_edit", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_stance_edit_item_id"))
    op.drop_table("stance_edit")
