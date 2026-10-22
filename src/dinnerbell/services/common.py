# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers every use case shares: the admin check and the audit row (SEC-9)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import AuditRepository
from dinnerbell.domain.members import Member
from dinnerbell.services.errors import Forbidden


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
