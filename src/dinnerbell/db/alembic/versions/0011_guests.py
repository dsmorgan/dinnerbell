# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests (#38): a member's ``guest`` role, guests' dietary needs, whether an admin removed them,
and the ``invitation`` table, one row per guest invited to a call.

A guest never has an email: ``member.email`` is how members sign in (FR-41), so it becomes
nullable, with a check that a guest's is empty and everyone else's isn't. The email Slack shares
for a guest who joined from the channel goes in ``slack_email`` instead, which sign-in never
reads; it's only for spotting a member the admin is about to add twice. SQLite changes a column's nullability and adds a
check constraint only by rebuilding the table. ``member`` is the parent of nearly every table,
so this refuses to run with foreign keys on, which would cascade-delete sessions, preferences,
responses, votes, assignments, and feedback, as 0007 and 0010 do. ``run_migrations`` turns them
off. ``member`` isn't AUTOINCREMENT, but if it ever has a sequence row, its high-water mark is
kept across the rebuild.

A downgrade removes every guest with their invitations, responses, and assignments; restore a
backup instead.

Revision ID: 0011_guests
Revises: 0010_offers
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import dinnerbell.db.types

revision: str = "0011_guests"
down_revision: str | None = "0010_offers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_ON = (
    "Rebuilding the member table with foreign keys on would delete every member's sessions, preferences, "
    "answers, and feedback. Run migrations with `dinnerbell migrate`, which turns them off."
)


def _require_foreign_keys_off() -> None:
    if op.get_bind().exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(FOREIGN_KEYS_ON)


def _sequence(table: str) -> int | None:
    found = op.get_bind().exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).scalar()
    return None if found is None else int(found)


def _restore_sequence(table: str, before: int | None) -> None:
    """Keep ``table``'s AUTOINCREMENT high-water mark, if it has one, across a rebuild."""
    if before is None:
        return
    now = _sequence(table)
    if now is None:
        op.get_bind().exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, before))
    elif now < before:
        op.get_bind().exec_driver_sql("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (before, table))


def upgrade() -> None:
    _require_foreign_keys_off()
    before = _sequence("member")
    with op.batch_alter_table("member", recreate="always") as batch_op:
        batch_op.alter_column("email", existing_type=sa.String(length=254), nullable=True)
        batch_op.add_column(sa.Column("dietary_needs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch_op.add_column(sa.Column("join_refused", sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.drop_constraint("role", type_="check")
        batch_op.create_check_constraint("role", "role IN ('admin', 'member', 'guest')")
        batch_op.add_column(sa.Column("slack_email", sa.String(length=254), nullable=True))
        batch_op.create_check_constraint(
            "email", "(role = 'guest' AND email IS NULL) OR (role <> 'guest' AND email IS NOT NULL)"
        )
    _restore_sequence("member", before)
    op.create_table(
        "invitation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("guest_id", sa.Integer(), nullable=False),
        sa.Column("invited_by", sa.Integer(), nullable=True),
        sa.Column("created_at", dinnerbell.db.types.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["event.id"], name=op.f("fk_invitation_event_id_event"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["guest_id"], ["member.id"], name=op.f("fk_invitation_guest_id_member"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_invitation_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"], ["member.id"], name=op.f("fk_invitation_invited_by_member"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitation")),
        sa.UniqueConstraint("event_id", "guest_id", name=op.f("uq_invitation_event_id_guest_id")),
    )
    with op.batch_alter_table("invitation") as batch_op:
        batch_op.create_index(batch_op.f("ix_invitation_guest_id"), ["guest_id"], unique=False)


def downgrade() -> None:
    _require_foreign_keys_off()
    with op.batch_alter_table("invitation") as batch_op:
        batch_op.drop_index(batch_op.f("ix_invitation_guest_id"))
    op.drop_table("invitation")
    # Guests can't be stored without the role. With foreign keys off nothing cascades, so their
    # rows go explicitly.
    guests = "SELECT id FROM member WHERE role = 'guest'"
    for table, column in (
        ("vote", "member_id"),
        ("suggestion", "member_id"),
        ("response", "member_id"),
        ("assignment", "member_id"),
        ("preference", "member_id"),
        ("feedback", "member_id"),
        ("session", "member_id"),
        ("catalog_item_cook", "member_id"),
    ):
        op.execute(f"DELETE FROM {table} WHERE {column} IN ({guests})")  # noqa: S608 - fixed names
    for table, column in (
        ("event", "coordinator_id"),
        ("event", "created_by"),
        ("event", "locked_by"),
        ("event", "ended_by"),
        ("proposal", "proposed_by"),
        ("audit", "actor_member_id"),
        ("meal_log", "logged_by"),
        ("catalog_item", "created_by"),
        ("feedback", "cook_member_id"),
    ):
        op.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} IN ({guests})")  # noqa: S608
    op.execute("DELETE FROM member WHERE role = 'guest'")
    before = _sequence("member")
    with op.batch_alter_table("member", recreate="always") as batch_op:
        batch_op.drop_constraint("email", type_="check")
        batch_op.drop_column("slack_email")
        batch_op.drop_constraint("role", type_="check")
        batch_op.create_check_constraint("role", "role IN ('admin', 'member')")
        batch_op.drop_column("join_refused")
        batch_op.drop_column("dietary_needs")
        batch_op.alter_column("email", existing_type=sa.String(length=254), nullable=False)
    _restore_sequence("member", before)
