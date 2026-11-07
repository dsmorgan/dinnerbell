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

What's queued and not yet delivered changes too, so that it doesn't say a guest coordinates now. The
app isn't running while it migrates, so no drainer holds those rows:

- ``roles_to_choose``, ``coordinating_given``, and ``role_vacated`` are closed unsent when addressed
  to a guest: each tells a coordinator what to do. ``roles_to_choose`` and ``role_vacated`` also
  carry a button that would refuse them.
- ``assignment_made`` to a guest loses ``coordinator`` from its roles, and is closed unsent if that
  was its only role. A guest's other roles are still news.
- Every row about a call that lost its coordinator loses its coordinator line and the coordinator's
  assignment entry, so an update to a posted message says nobody coordinates. Two past-tense facts
  stay, because they're true: ``assigned_by`` can still name the guest as who assigned someone, and
  ``decision.tie_note`` can still say the guest broke a tie as coordinator.

No other direct message needs it: ``coordinator_picked`` never goes to a guest, and
``assignment_ended``, ``assignment_cancelled``, and ``place_changed`` only name the roles that do
the work. A row that doesn't read as an object is left alone, so it can't stop the upgrade.
Messages already posted still name the guest until the call's next change updates them.

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
COORDINATOR = "coordinator"
COORDINATORS_ONLY = frozenset({"roles_to_choose", "coordinating_given", "role_vacated"})
"""Direct messages that tell whoever coordinates what to do."""


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
    cleared = {event_id for event_id, _, _ in calls}
    queued = bind.execute(
        sa.text("SELECT id, event_id, kind, payload FROM outbox WHERE delivered_at IS NULL AND failed_at IS NULL")
    ).all()
    for row_id, event_id, kind, raw in queued:
        payload = _object(raw)
        if payload is None:
            continue
        changed = event_id in cleared and _forget_the_guest(payload, guests)
        to_guest = _names_a_guest(payload.get("member"), guests)
        if to_guest and kind in COORDINATORS_ONLY:
            close = True
        elif to_guest and kind == "assignment_made":
            roles = payload.get("roles")
            close = roles == [COORDINATOR]
            if isinstance(roles, list) and COORDINATOR in roles and not close:
                payload["roles"] = [role for role in roles if role != COORDINATOR]
                changed = True
        else:
            close = False
        if close:
            bind.execute(
                sa.text("UPDATE outbox SET delivered_at = :now, last_error = :note WHERE id = :id"),
                {"now": now, "note": CLOSED_NOTE, "id": row_id},
            )
        elif changed:
            bind.execute(
                sa.text("UPDATE outbox SET payload = :payload WHERE id = :id"),
                {"payload": json.dumps(payload), "id": row_id},
            )


def _object(raw: Any) -> dict[str, Any] | None:
    """The row's payload, or None for one that isn't a JSON object."""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _names_a_guest(person: object, guests: set[int]) -> bool:
    if not isinstance(person, dict):
        return False
    person_id = person.get("id")
    return isinstance(person_id, int) and person_id in guests


def _forget_the_guest(payload: dict[str, Any], guests: set[int]) -> bool:
    """Take the guest out of what ``payload`` says about who coordinates. Returns whether it did."""
    changed = False
    event = payload.get("event")
    if isinstance(event, dict) and _names_a_guest(event.get("coordinator"), guests):
        event["coordinator"] = None
        changed = True
    decision = payload.get("decision")
    held = decision.get("assignments") if isinstance(decision, dict) else None
    if isinstance(decision, dict) and isinstance(held, list):
        kept = [a for a in held if not _coordinating_guest(a, guests)]
        if len(kept) != len(held):
            decision["assignments"] = kept
            changed = True
    return changed


def _coordinating_guest(assignment: object, guests: set[int]) -> bool:
    return (
        isinstance(assignment, dict)
        and assignment.get("role") == COORDINATOR
        and _names_a_guest(assignment.get("member"), guests)
    )


def downgrade() -> None:
    pass
