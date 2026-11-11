# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's catalog edits and meal log (#254): ``assistant_draft.kind`` accepts five more kinds.

The check becomes the 0036 kinds plus ``item_edit`` (a catalog item's fields), ``item_active``
(taking an item off the catalog or putting it back), and ``meal_log``, ``meal_edit``, and
``meal_delete`` (History's meal log). Nothing else changes: a draft's payload is JSON already.

SQLite changes a check constraint only by rebuilding the table. Nothing references
``assistant_draft``, so the rebuild deletes nothing in other tables, and its rows are copied as
they are. The rebuild keeps AUTOINCREMENT and its high-water mark, as 0034 to 0036 do, so an old
Confirm button can never reach a newer draft.

A downgrade deletes the drafts of the new kinds, which the old check can't store. Drafts last 30
minutes, so a member who loses one asks again.

Revision ID: 0037_assistant_catalog_edits
Revises: 0036_assistant_coordinating
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037_assistant_catalog_edits"
down_revision: str | None = "0036_assistant_coordinating"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "'proposal', 'offer', 'catalog', 'answer', 'vote', 'suggestion', 'role', 'offer_off', 'guest', "
    "'guest_answer', 'call', 'preference', 'feedback', 'away', 'decide', 'shuffle', 'plan', 'hand_off', "
    "'mark_done', 'call_off'"
)
CATALOG_AND_MEALS = "'item_edit', 'item_active', 'meal_log', 'meal_edit', 'meal_delete'"


def _sequence() -> int:
    """``assistant_draft``'s AUTOINCREMENT high-water mark: the largest ID it ever gave out."""
    found = op.get_bind().exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'assistant_draft'").scalar()
    return int(found or 0)


def _restore_sequence(before: int) -> None:
    bind = op.get_bind()
    if before <= _sequence():
        return
    if bind.exec_driver_sql("SELECT count(*) FROM sqlite_sequence WHERE name = 'assistant_draft'").scalar():
        bind.exec_driver_sql("UPDATE sqlite_sequence SET seq = ? WHERE name = 'assistant_draft'", (before,))
    else:
        bind.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES ('assistant_draft', ?)", (before,))


def _replace_kind_check(kinds: str) -> None:
    before = _sequence()
    with op.batch_alter_table(
        "assistant_draft", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("kind", type_="check")
        batch_op.create_check_constraint("kind", f"kind IN ({kinds})")
    _restore_sequence(before)


def upgrade() -> None:
    _replace_kind_check(f"{KINDS}, {CATALOG_AND_MEALS}")


def downgrade() -> None:
    op.execute(f"DELETE FROM assistant_draft WHERE kind IN ({CATALOG_AND_MEALS})")  # noqa: S608 - fixed names
    _replace_kind_check(KINDS)
