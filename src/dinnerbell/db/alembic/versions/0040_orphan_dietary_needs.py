# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drop dietary needs that no longer name a dietary tag (#298).

Before 0.18.0, merging two tags left members' and guests' own ``dietary_needs`` naming the
removed tag, so calls stopped honoring the need and Preferences didn't show it. This keeps only
the IDs of the household's dietary tags in each member's list. It can't tell which tag a lost
need was merged into, so the CHANGELOG asks members to check their needs.

The downgrade changes nothing: the dropped IDs named no tag.

Revision ID: 0040_orphan_dietary_needs
Revises: 0039_assistant_needs
Create Date: 2026-11-15
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op

revision: str = "0040_orphan_dietary_needs"
down_revision: str | None = "0039_assistant_needs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    dietary: dict[int, set[int]] = {}
    for household_id, tag_id in bind.exec_driver_sql("SELECT household_id, id FROM tag WHERE kind = 'dietary'"):
        dietary.setdefault(int(household_id), set()).add(int(tag_id))
    members = bind.exec_driver_sql("SELECT id, household_id, dietary_needs FROM member").fetchall()
    for member_id, household_id, stored in members:
        needs = [int(t) for t in json.loads(stored or "[]")]
        kept = sorted(set(needs) & dietary.get(int(household_id), set()))
        if kept != sorted(set(needs)):
            bind.exec_driver_sql("UPDATE member SET dietary_needs = ? WHERE id = ?", (json.dumps(kept), member_id))


def downgrade() -> None:
    pass
