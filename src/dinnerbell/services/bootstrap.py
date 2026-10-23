# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-start setup: the household and its first admin."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import AuditRepository, HouseholdRepository, MemberRepository
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Capabilities, Role, default_display_name
from dinnerbell.domain.schedule import default_settings

log = logging.getLogger(__name__)


async def bootstrap(
    db: AsyncSession, *, household_name: str, timezone: str, admin_email: str, now: datetime
) -> Household:
    """Create the household and admin if missing. Safe to run on every start."""
    households = HouseholdRepository(db)
    members = MemberRepository(db)
    audit = AuditRepository(db)

    household = await households.first()
    if household is None:
        # The schedule and ranking defaults are stored, not implied, so Admin > Schedule and the
        # database agree from the first start (FR-1).
        household = await households.create(household_name, timezone, now, default_settings())
        await audit.record(
            household_id=household.id,
            actor_member_id=None,
            action="bootstrap.household_created",
            target=f"household:{household.id}",
            payload={"name": household.name},
            now=now,
        )
        log.info("created household", extra={"household_id": household.id})

    if await members.by_email(household.id, admin_email) is None:
        admin = await members.add(
            household_id=household.id,
            display_name=default_display_name(admin_email),
            email=admin_email,
            role=Role.ADMIN,
            capabilities=Capabilities(),
            now=now,
        )
        await audit.record(
            household_id=household.id,
            actor_member_id=None,
            action="bootstrap.admin_invited",
            target=f"member:{admin.id}",
            payload={},
            now=now,
        )
        log.info("invited bootstrap admin", extra={"member_id": admin.id})

    await db.commit()
    return household
