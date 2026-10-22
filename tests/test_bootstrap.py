# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-start bootstrap."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from dinnerbell.config import Settings
from dinnerbell.db.engine import create_app_engine, create_sessionmaker
from dinnerbell.db.models import AuditRow, HouseholdRow, MemberRow
from dinnerbell.services.bootstrap import bootstrap
from tests.support.clock import FakeClock

pytestmark = pytest.mark.anyio


async def run(settings: Settings, clock: FakeClock, **overrides: str) -> None:
    engine = create_app_engine(settings.async_database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            await bootstrap(
                db,
                household_name=overrides.get("household_name", settings.household_name),
                timezone=settings.timezone,
                admin_email=overrides.get("admin_email", settings.admin_email),
                now=clock(),
            )
    finally:
        await engine.dispose()


async def count(settings: Settings, model: type) -> int:
    engine = create_app_engine(settings.async_database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            return int(await db.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await engine.dispose()


async def test_creates_household_and_admin_once(migrated: Settings, clock: FakeClock) -> None:
    await run(migrated, clock)
    await run(migrated, clock)
    assert await count(migrated, HouseholdRow) == 1
    assert await count(migrated, MemberRow) == 1
    assert await count(migrated, AuditRow) == 2

    engine = create_app_engine(migrated.async_database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            household = await db.scalar(select(HouseholdRow))
            admin = await db.scalar(select(MemberRow))
    finally:
        await engine.dispose()
    assert household is not None and admin is not None
    assert (household.name, household.timezone) == ("Home", "America/Chicago")
    assert (admin.email, admin.role, admin.active, admin.oidc_subject) == ("admin@example.com", "admin", True, None)
    assert admin.display_name == "admin"
    assert admin.household_id == household.id


async def test_new_admin_email_adds_another_admin(migrated: Settings, clock: FakeClock) -> None:
    await run(migrated, clock)
    await run(migrated, clock, admin_email="second@example.com", household_name="Ignored")
    assert await count(migrated, HouseholdRow) == 1
    assert await count(migrated, MemberRow) == 2
