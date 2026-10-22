# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recent eating, for the History page now and for ranking in M2.

``variety_window`` is the one query both use, so the page and the ranking engine always agree
on what "eaten recently" means.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.domain.household import Household
from dinnerbell.domain.variety import VarietyWindow, summarize, variety_window_days, window_start


def household_today(household: Household, now: datetime) -> date:
    """Today's date in the household's time zone, from the household row."""
    return now.astimezone(ZoneInfo(household.timezone)).date()


async def window_days(db: AsyncSession, household_id: int) -> int:
    return variety_window_days(await household_settings(db, household_id))


async def variety_window(db: AsyncSession, household_id: int, today: date) -> VarietyWindow:
    """Items and cuisines eaten in the household's variety window ending ``today``."""
    days = await window_days(db, household_id)
    meals = await MealLogRepository(db).between(household_id, window_start(today, days), today)
    names = {tag.id: tag.name for tag in await CatalogRepository(db).tags(household_id)}
    return summarize(meals, today=today, days=days, tag_names=names)


async def last_eaten(db: AsyncSession, household_id: int) -> dict[int, date]:
    return await MealLogRepository(db).last_eaten(household_id)
