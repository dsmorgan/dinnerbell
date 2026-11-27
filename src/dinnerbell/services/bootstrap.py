# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-start setup: the household and its first admin."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import AuditRepository, HouseholdRepository, MemberRepository
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Capabilities, MemberStatus, Role, default_display_name
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
        # The schedule and ranking defaults are stored, not implied, so the Schedule pages and the
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
    else:
        _warn_on_ignored_settings(household, household_name=household_name, timezone=timezone)

    if await members.by_email(household.id, admin_email) is None:
        unclaimed = [m.id for m in await members.list(household.id) if m.is_admin and m.status is MemberStatus.INVITED]
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
        for earlier in unclaimed:
            # The setting changed before the admin it invited first signed in: most likely a typo
            # (#360), which anyone with that email could still claim. Never its email (SEC-5).
            log.warning(
                "DINNERBELL_ADMIN_EMAIL changed while an earlier admin invitation, member %s, is unclaimed. "
                "If that email was mistyped, deactivate member %s on Admin > Members once you're in.",
                earlier,
                earlier,
                extra={"member_id": earlier},
            )

    await db.commit()
    return household


def _warn_on_ignored_settings(household: Household, *, household_name: str, timezone: str) -> None:
    """The settings only seed the household row, so say when they no longer match it, and which
    command changes the row (issue 334).
    The time zone is named; the household name isn't, because it identifies the household (SEC-5)."""
    extra = {"household_id": household.id}
    if timezone != household.timezone:
        log.warning(
            "DINNERBELL_TIMEZONE is %s, but the household's time zone is %s, which Dinnerbell keeps using: it "
            "reads the setting on first start only. To change the household's zone, run dinnerbell set-timezone "
            "ZONE. To stop this warning, set DINNERBELL_TIMEZONE to the household's zone; see docs/configuration.md.",
            timezone,
            household.timezone,
            extra=extra,
        )
    if household_name != household.name:
        log.warning(
            "DINNERBELL_HOUSEHOLD_NAME differs from the household's name, which Dinnerbell keeps using: it reads "
            "the setting on first start only. To rename the household, run dinnerbell set-household-name NAME. To "
            "stop this warning, set DINNERBELL_HOUSEHOLD_NAME to the name the app shows; see docs/configuration.md.",
            extra=extra,
        )
