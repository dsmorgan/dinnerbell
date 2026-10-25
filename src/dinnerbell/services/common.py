# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers every use case shares: the admin check and the audit row (SEC-9)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import AuditRepository
from dinnerbell.domain.members import Member
from dinnerbell.services.errors import Forbidden


def is_unique_violation(exc: IntegrityError) -> bool:
    """True when ``exc`` is a lost race on a unique constraint, which callers may treat as an
    expected conflict. Any other integrity error, such as a broken foreign key, is a bug."""
    return "UNIQUE constraint failed" in str(exc.orig)


MEMBERS_ONLY = "That's for household members."


def require_member(actor: Member, message: str = MEMBERS_ONLY) -> None:
    """Raise Forbidden with ``message`` unless ``actor`` is an active member or admin: not a
    guest (#38), who reaches only the calls they're invited to."""
    if not actor.is_member:
        raise Forbidden(message)


def require_admin(actor: Member, message: str) -> None:
    """Raise Forbidden with ``message`` unless ``actor`` is an active admin."""
    if not (actor.active and actor.is_admin):
        raise Forbidden(message)


async def audit(
    db: AsyncSession,
    household_id: int,
    actor: Member | None,
    action: str,
    target: str,
    payload: Mapping[str, object],
    now: datetime,
) -> None:
    """Add an audit row to the current transaction. ``actor`` is None for system actions."""
    await AuditRepository(db).record(
        household_id=household_id,
        actor_member_id=actor.id if actor else None,
        action=action,
        target=target,
        payload=dict(payload),
        now=now,
    )
