# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign in with a code, and Devices (#138, spec 6.15): ``sign_in_code``, and each session's
``public_id`` and ``device``.

``sign_in_code`` holds one row per member: the SHA-256 hash of their newest code, who made it,
when it expires, and when it was used. It starts empty.

Every session gets a ``public_id``, the random name a page uses for it, such as to sign it out,
which is never the token's hash; and a ``device`` label from the browser. A session that exists
already gets a new random ID and the label "Unknown device", since its browser was never
asked. Nobody is signed out. SQLite can't add a NOT NULL column without a default, so both
columns are added empty, filled, and then made NOT NULL and unique in one batch rebuild of
``session``.

The downgrade drops the table and both columns. Codes not yet used stop working; sessions stay.

Revision ID: 0042_sign_in_codes
Revises: 0041_notify_via
Create Date: 2026-11-21
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_sign_in_codes"
down_revision: str | None = "0041_notify_via"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUBLIC_ID_BYTES = 16
UNKNOWN_DEVICE = "Unknown device"
"""As ``db.models.PUBLIC_ID_BYTES`` and ``domain.sessions.UNKNOWN_DEVICE``, written out so
this revision never changes."""


def upgrade() -> None:
    op.create_table(
        "sign_in_code",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("household_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"], ["member.id"], name=op.f("fk_sign_in_code_created_by_member"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_sign_in_code_household_id_household"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["member.id"], name=op.f("fk_sign_in_code_member_id_member"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sign_in_code")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_sign_in_code_code_hash")),
        sa.UniqueConstraint("member_id", name=op.f("uq_sign_in_code_member_id")),
    )
    op.add_column("session", sa.Column("public_id", sa.String(length=32), nullable=True))
    op.add_column("session", sa.Column("device", sa.String(length=40), nullable=True))
    connection = op.get_bind()
    for (session_id,) in connection.execute(sa.text("SELECT id FROM session")).all():
        connection.execute(
            sa.text("UPDATE session SET public_id = :public_id, device = :device WHERE id = :id"),
            {"public_id": secrets.token_urlsafe(PUBLIC_ID_BYTES), "device": UNKNOWN_DEVICE, "id": session_id},
        )
    with op.batch_alter_table("session") as batch_op:
        batch_op.alter_column("public_id", existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column("device", existing_type=sa.String(length=40), nullable=False)
        batch_op.create_unique_constraint(op.f("uq_session_public_id"), ["public_id"])


def downgrade() -> None:
    with op.batch_alter_table("session") as batch_op:
        batch_op.drop_constraint(op.f("uq_session_public_id"), type_="unique")
        batch_op.drop_column("device")
        batch_op.drop_column("public_id")
    op.drop_table("sign_in_code")
