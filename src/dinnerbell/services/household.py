# SPDX-License-Identifier: AGPL-3.0-or-later
"""The household row: the source of truth for its name and time zone.

DINNERBELL_HOUSEHOLD_NAME and DINNERBELL_TIMEZONE only seed it on first start.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.household import Household


class NoHousehold(LookupError):
    """The database has no household yet: bootstrap hasn't run."""


async def current_household(db: AsyncSession) -> Household:
    household = await HouseholdRepository(db).first()
    if household is None:
        raise NoHousehold("no household; bootstrap has not run")
    return household
