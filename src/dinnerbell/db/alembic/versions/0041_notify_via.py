# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slack, the web app, or both (#138, spec 6.15): ``member.notify_via``, null for everyone.

Each member's choice of where Dinnerbell reaches them while the household's web app is on.
Null means they haven't chosen, and ``Member.delivery`` works out the default from the Slack
link: Slack for a member with a Slack user, the web app for one without. So nobody's default is
stored, and a member linked to Slack later, such as by signing in with Slack, moves to Slack on
their own. Every member and guest who exists already starts on null, as new ones do. A check
constraint keeps out any value but ``slack``, ``web``, and ``both``.

SQLite adds the column, with its check, in place, so nothing is rebuilt. A downgrade rebuilds
``member`` in batch mode, as 0030's does, dropping the check and then the column. Everyone's
choice is lost, which changes nothing while the web app is off.

Revision ID: 0041_notify_via
Revises: 0040_orphan_dietary_needs
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0041_notify_via"
down_revision: str | None = "0040_orphan_dietary_needs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Raw SQL, because SQLite only adds a column with its check in place this way; the check's
    # name is the one models.py's naming convention gives it.
    op.execute(
        "ALTER TABLE member ADD COLUMN notify_via VARCHAR(5) "
        "CONSTRAINT ck_member_notify_via CHECK (notify_via IN ('slack', 'web', 'both'))"
    )


def downgrade() -> None:
    with op.batch_alter_table("member") as batch_op:
        batch_op.drop_constraint(op.f("ck_member_notify_via"), type_="check")
        batch_op.drop_column("notify_via")
