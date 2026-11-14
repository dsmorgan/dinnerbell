# SPDX-License-Identifier: AGPL-3.0-or-later
"""The places a call has shown (#290): ``shown_place``.

A row per catalog place a call's run has shown as a ranked pick, with the shuffle count when it
was last shown (0 for the first draw), so a shuffle draws places not shown yet (FR-61). A
shuffle writes the rows of the picks it's about to redraw. The table starts empty: a call voting
during the upgrade records its picks at its next shuffle, as a new call does. The downgrade
drops it, and a later shuffle then leaves out only the places it draws away.

Revision ID: 0038_shown_places
Revises: 0037_assistant_catalog_edits
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_shown_places"
down_revision: str | None = "0037_assistant_catalog_edits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shown_place",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("run", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("shuffle", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["event.id"], name=op.f("fk_shown_place_event_id_event"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_shown_place_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_shown_place_item_id_catalog_item"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shown_place")),
        sa.UniqueConstraint("event_id", "run", "item_id", name=op.f("uq_shown_place_event_id_run_item_id")),
    )
    op.create_index(op.f("ix_shown_place_item_id"), "shown_place", ["item_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_shown_place_item_id"), table_name="shown_place")
    op.drop_table("shown_place")
