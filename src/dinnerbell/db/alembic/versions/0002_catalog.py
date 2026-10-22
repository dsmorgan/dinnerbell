# SPDX-License-Identifier: AGPL-3.0-or-later
"""M1 tables: tag, catalog_item, catalog_item_tag, catalog_item_cook, preference, meal_log.

Also gives every household an explicit ``variety_window_days`` setting (default 7).

``meal_log.event_id`` is a plain nullable integer for now. M2 creates the ``event`` table and
adds the foreign key in its own revision; until then every logged meal has ``event_id`` NULL.

Revision ID: 0002_catalog
Revises: 0001_initial
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_catalog"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_VARIETY_WINDOW_DAYS = 7


def upgrade() -> None:
    op.create_table(
        "tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("name_key", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('cuisine', 'protein', 'dietary', 'other')", name=op.f("ck_tag_kind")),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_tag_household_id_household"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tag")),
        sa.UniqueConstraint("household_id", "name_key", name=op.f("uq_tag_household_id_name_key")),
    )
    op.create_table(
        "catalog_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("name_key", sa.String(length=100), nullable=False),
        sa.Column("modes", sa.JSON(), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=True),
        sa.Column("order_url", sa.String(length=2000), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("default_servings", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('home', 'restaurant')", name=op.f("ck_catalog_item_kind")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["member.id"], name=op.f("fk_catalog_item_created_by_member"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_catalog_item_household_id_household"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_item")),
        sa.UniqueConstraint(
            "household_id", "kind", "name_key", name=op.f("uq_catalog_item_household_id_kind_name_key")
        ),
    )
    op.create_table(
        "catalog_item_cook",
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_catalog_item_cook_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_catalog_item_cook_item_id_catalog_item"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_catalog_item_cook_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("item_id", "member_id", name=op.f("pk_catalog_item_cook")),
    )
    op.create_index(op.f("ix_catalog_item_cook_member_id"), "catalog_item_cook", ["member_id"], unique=False)
    op.create_table(
        "catalog_item_tag",
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_catalog_item_tag_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_catalog_item_tag_item_id_catalog_item"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tag.id"], name=op.f("fk_catalog_item_tag_tag_id_tag"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("item_id", "tag_id", name=op.f("pk_catalog_item_tag")),
    )
    op.create_index(op.f("ix_catalog_item_tag_tag_id"), "catalog_item_tag", ["tag_id"], unique=False)
    op.create_table(
        "meal_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("slot", sa.String(length=16), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("cuisine_tags", sa.JSON(), nullable=False),
        sa.Column("participants", sa.JSON(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("logged_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("slot IN ('lunch', 'dinner')", name=op.f("ck_meal_log_slot")),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_meal_log_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_meal_log_item_id_catalog_item"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["logged_by"], ["member.id"], name=op.f("fk_meal_log_logged_by_member"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_meal_log")),
    )
    op.create_index("ix_meal_log_household_date", "meal_log", ["household_id", "date"], unique=False)
    op.create_index(op.f("ix_meal_log_item_id"), "meal_log", ["item_id"], unique=False)
    op.create_table(
        "preference",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("veto", sa.Boolean(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("(tag_id IS NULL) <> (item_id IS NULL)", name=op.f("ck_preference_one_target")),
        sa.CheckConstraint("NOT (veto AND score <> 0)", name=op.f("ck_preference_veto_has_no_score")),
        sa.CheckConstraint("score BETWEEN -2 AND 2", name=op.f("ck_preference_score_range")),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_preference_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["catalog_item.id"], name=op.f("fk_preference_item_id_catalog_item"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_preference_member_id_member"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], name=op.f("fk_preference_tag_id_tag"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preference")),
        sa.UniqueConstraint("member_id", "item_id", name=op.f("uq_preference_member_id_item_id")),
        sa.UniqueConstraint("member_id", "tag_id", name=op.f("uq_preference_member_id_tag_id")),
    )
    op.create_index(op.f("ix_preference_item_id"), "preference", ["item_id"], unique=False)
    op.create_index(op.f("ix_preference_member_id"), "preference", ["member_id"], unique=False)
    op.create_index(op.f("ix_preference_tag_id"), "preference", ["tag_id"], unique=False)
    op.execute(
        sa.text(
            "UPDATE household SET settings = json_set(settings, '$.variety_window_days', :days) "
            "WHERE json_extract(settings, '$.variety_window_days') IS NULL"
        ).bindparams(days=DEFAULT_VARIETY_WINDOW_DAYS)
    )


def downgrade() -> None:
    # Removing variety_window_days is safe: before 0002 nothing wrote it (M0 has no settings
    # editor, and bootstrap stores {}), so any value here came from 0002 or later M1 code that
    # revision 0001 doesn't read. Downgrading drops a custom window; upgrading again sets 7.
    op.execute(sa.text("UPDATE household SET settings = json_remove(settings, '$.variety_window_days')"))
    op.drop_index(op.f("ix_preference_tag_id"), table_name="preference")
    op.drop_index(op.f("ix_preference_member_id"), table_name="preference")
    op.drop_index(op.f("ix_preference_item_id"), table_name="preference")
    op.drop_table("preference")
    op.drop_index(op.f("ix_meal_log_item_id"), table_name="meal_log")
    op.drop_index("ix_meal_log_household_date", table_name="meal_log")
    op.drop_table("meal_log")
    op.drop_index(op.f("ix_catalog_item_tag_tag_id"), table_name="catalog_item_tag")
    op.drop_table("catalog_item_tag")
    op.drop_index(op.f("ix_catalog_item_cook_member_id"), table_name="catalog_item_cook")
    op.drop_table("catalog_item_cook")
    op.drop_table("catalog_item")
    op.drop_table("tag")
