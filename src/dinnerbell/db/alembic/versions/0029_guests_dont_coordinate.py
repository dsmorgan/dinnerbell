# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guests no longer coordinate (#227): an open call a guest coordinates loses its coordinator.

Such a call then follows the path for nobody coordinating, as when a coordinator is deactivated:
a call still taking answers picks a member who's in when voting starts, anyone who's a member can
claim it until the decision, an admin can assign someone or choose the roles, and a pending
``roles_due_at`` still runs out, so the first volunteers do the roles. The guest's answer stays,
so a guest who was in because they coordinated is still in. An ``assignment`` row for coordinating
goes with it. Each call gets an ``event.coordinator_released`` audit row, with no actor, as the
scheduler's do, and ``why`` set to ``guest``.

Calls that are done, abandoned, or cancelled keep their history: their coordinator, their
assignment, and the ``coordinated`` contributions and badges they earned. A re-run of one doesn't
bring the guest back (``lifecycle.reopen``).

A direct message about coordinating that's queued for a guest, or for the member who invited one,
is closed unsent: the notification no longer knows how to reach that member, and it isn't true now.
The app isn't running while it migrates, so no drainer holds those rows. Messages already posted
still name the guest until the call's next change updates them.

The downgrade restores nothing: that guest could take coordinating again by claiming it.

Revision ID: 0029_guests_dont_coordinate
Revises: 0028_unregistered_guest_badges
Create Date: 2026-11-07
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0029_guests_dont_coordinate"
down_revision: str | None = "0028_unregistered_guest_badges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN_STATES = "('scheduled', 'collecting', 'voting', 'decided')"
"""``EventState.is_open``, as it is today."""
GUESTS = "SELECT id FROM member WHERE role = 'guest'"
CLOSED_NOTE = "Guests no longer coordinate (#227)."


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")  # as ``UTCDateTime`` stores it
    guests = set(bind.execute(sa.text(GUESTS)).scalars())
    if not guests:
        return
    calls = bind.execute(
        sa.text(
            "SELECT id, household_id, coordinator_id FROM event "  # noqa: S608
            f"WHERE state IN {OPEN_STATES} AND coordinator_id IN ({GUESTS})"
        )
    ).all()
    for event_id, household_id, guest_id in calls:
        bind.execute(
            sa.text("UPDATE event SET coordinator_id = NULL, updated_at = :now WHERE id = :id"),
            {"now": now, "id": event_id},
        )
        bind.execute(
            sa.text(
                "INSERT INTO audit (household_id, actor_member_id, action, target, payload, created_at) "
                "VALUES (:household, NULL, 'event.coordinator_released', :target, :payload, :now)"
            ),
            {
                "household": household_id,
                "target": f"event:{event_id}",
                "payload": json.dumps({"member_id": guest_id, "why": "guest"}),
                "now": now,
            },
        )
    bind.execute(
        sa.text(
            f"DELETE FROM assignment WHERE role = 'coordinator' AND member_id IN ({GUESTS}) "  # noqa: S608
            f"AND event_id IN (SELECT id FROM event WHERE state IN {OPEN_STATES})"
        )
    )
    queued = bind.execute(
        sa.text(
            "SELECT id, payload FROM outbox WHERE delivered_at IS NULL AND failed_at IS NULL "
            "AND kind IN ('roles_to_choose', 'coordinating_given')"
        )
    ).all()
    for row_id, raw in queued:
        payload: dict[str, Any] = json.loads(raw) if isinstance(raw, str) else raw
        if (payload.get("member") or {}).get("id") in guests:
            bind.execute(
                sa.text("UPDATE outbox SET delivered_at = :now, last_error = :note WHERE id = :id"),
                {"now": now, "note": CLOSED_NOTE, "id": row_id},
            )


def downgrade() -> None:
    pass
