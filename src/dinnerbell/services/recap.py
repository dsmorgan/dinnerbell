# SPDX-License-Identifier: AGPL-3.0-or-later
"""The monthly recap (FR-40).

``post_recaps`` runs on every scheduler tick and does nothing until a recap is due
(``domain.recap.due_month``). Then, in one transaction, it queues the post and stores the month
in the household setting ``recap_posted``. A restart, a second tick, or two ticks at once find
the marker and post nothing, and a crash before the commit posts nothing and tries again.

What it shows is what the household already sees: totals from the meal log, the cuisines and
catalog items of that month, a thank-you to the members who cooked, picked up, ordered, or
coordinated (grouped by how, in name order, with no one's count, so never a ranking), and the
badges announced that month. It never shows feedback, preferences, or anything about who did
less. A month with no meals posts nothing (the marker still moves on), because "0 meals" is not news.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.contributions import BadgeRepository, ContributionRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.household import Household
from dinnerbell.domain.notifications import MonthlyRecap, PersonInfo, ThanksGroup
from dinnerbell.domain.recap import (
    MAX_CUISINES,
    MAX_ITEM_NAMES,
    MAX_THANKS,
    SETTINGS_KEY,
    bounds_utc,
    due_month,
    month_key,
    next_month,
)
from dinnerbell.services.announcements import news_for

log = logging.getLogger(__name__)

_BAD_ZONES: set[int] = set()
"""Households whose time zone is invalid and have been warned about, so a tick doesn't log it every 30 seconds."""

THANKED = (Kind.COOKED, Kind.PICKED_UP, Kind.ORDERED, Kind.COORDINATED)


async def post_recaps(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Queue every household's recap that is due. Returns how many it queued."""
    async with sessionmaker() as db:
        households = await HouseholdRepository(db).all()
    queued = 0
    for household in households:
        async with sessionmaker() as db:
            try:
                queued += int(await recap_household(db, household, now=now))
            except Exception:
                await db.rollback()
                log.exception("could not queue the monthly recap", extra={"household_id": household.id})
    return queued


async def recap_household(db: AsyncSession, household: Household, *, now: datetime) -> bool:
    """Queue ``household``'s recap if one is due. True when it queued a post."""
    try:
        zone = ZoneInfo(household.timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if household.id not in _BAD_ZONES:
            _BAD_ZONES.add(household.id)
            log.warning("monthly recap off: the household time zone is not valid", extra={"household_id": household.id})
        return False
    local = now.astimezone(zone)
    if due_month(local, (await household_settings(db, household.id)).get(SETTINGS_KEY)) is None:
        return False  # the usual tick: no write lock for nothing
    async with write_transaction(db):
        stored = await household_settings(db, household.id)
        month = due_month(local, stored.get(SETTINGS_KEY))  # again, under the lock
        if month is None:
            return False
        recap = await build(db, household.id, month, zone)
        if recap is not None:
            await OutboxRepository(db).enqueue(household.id, None, recap, now)
        await HouseholdRepository(db).update_settings(household.id, stored | {SETTINGS_KEY: month_key(month)})
        await db.commit()
    log.info("monthly recap queued" if recap else "monthly recap skipped: no meals", extra={"month": month_key(month)})
    return recap is not None


async def build(db: AsyncSession, household_id: int, month: date, zone: ZoneInfo) -> MonthlyRecap | None:
    """``month``'s recap, or None when the household ate no meals that month."""
    first, after = month, next_month(month)
    meals = await MealLogRepository(db).between(household_id, None, after)  # newest first
    this_month = [m for m in meals if first <= m.day < after]
    if not this_month:
        return None
    earlier = {c.id for m in meals if m.day < first for c in m.cuisines}
    cuisines = {c.id: c.name for m in this_month for c in m.cuisines}
    ordered = sorted(cuisines.items(), key=lambda kv: (kv[1].casefold(), kv[0]))
    start, end = bounds_utc(month, zone)
    items = await CatalogRepository(db).names_created_between(household_id, start, end)
    members = {m.id: m for m in await MemberRepository(db).list(household_id) if m.active}
    counts = await ContributionRepository(db).between(household_id, first, after, THANKED)
    by_name = sorted(members.items(), key=lambda kv: (kv[1].display_name.casefold(), kv[0]))
    thanks: list[ThanksGroup] = []
    for kind in THANKED:
        who = [PersonInfo(m, member.display_name) for m, member in by_name if counts.get((m, kind))]
        if who:
            thanks.append(ThanksGroup(kind.value, tuple(who[:MAX_THANKS]), max(len(who) - MAX_THANKS, 0)))
    awards = await BadgeRepository(db).earned_between(household_id, start, end)
    news, more_badges = news_for(awards, members)
    return MonthlyRecap(
        household_id=household_id,
        month=month,
        meals=len(this_month),
        cuisines=tuple(name for _, name in ordered[:MAX_CUISINES]),
        first_cuisines=tuple(name for cid, name in ordered[:MAX_CUISINES] if cid not in earlier),
        new_items=len(items),
        new_item_names=tuple(items[:MAX_ITEM_NAMES]),
        thanks=tuple(thanks),
        badges=tuple(news),
        more_badges=more_badges,
    )
