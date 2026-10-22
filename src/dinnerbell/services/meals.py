# SPDX-License-Identifier: AGPL-3.0-or-later
"""Logging meals and reading history (FR-30, FR-32, spec 5.6).

Any member logs a meal. Members edit and delete the meals they logged; the admin can change
any. Each log snapshots the item's cuisine tags and the participants, so later catalog or
member edits don't rewrite history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import TagKind
from dinnerbell.domain.meals import MealInput, MealLog, MemberRef, TagRef, by_week, can_change, check_meal, week_start
from dinnerbell.domain.members import Member
from dinnerbell.services.common import audit
from dinnerbell.services.errors import Forbidden, NotFound

WEEKS_PER_PAGE = 8


@dataclass(frozen=True, slots=True)
class History:
    weeks: list[tuple[date, list[MealLog]]]
    page: int
    has_older: bool


async def get_meal(db: AsyncSession, actor: Member, meal_id: int) -> MealLog:
    meal = await MealLogRepository(db).get(actor.household_id, meal_id)
    if meal is None:
        raise NotFound("No such meal.")
    return meal


async def participant_choices(db: AsyncSession, actor: Member, meal: MealLog | None = None) -> list[MemberRef]:
    """Who can be ticked as having eaten: active members, plus, when editing, everyone already
    on the meal, under the name they had then. Without the second group, editing a meal would
    silently drop a member who has since been deactivated."""
    return sorted(
        (MemberRef(i, name) for i, name in (await _eligible(db, actor, meal)).items()), key=lambda m: m.name.casefold()
    )


async def _eligible(db: AsyncSession, actor: Member, meal: MealLog | None) -> dict[int, str]:
    kept = {ref.id: ref.name for ref in meal.participants} if meal is not None else {}
    active = {m.id: m.display_name for m in await MemberRepository(db).list(actor.household_id) if m.active}
    return kept | active  # active members appear under their current name


async def _snapshots(
    db: AsyncSession, actor: Member, data: MealInput, today: date, editing: MealLog | None = None
) -> tuple[tuple[TagRef, ...], tuple[MemberRef, ...]]:
    catalog = CatalogRepository(db)
    item = await catalog.item(actor.household_id, data.item_id)
    eligible = await _eligible(db, actor, editing)
    check_meal(
        data,
        today=today,
        active_member_ids=eligible,
        item_exists=item is not None,
        original_day=editing.day if editing is not None else None,
    )
    tag_ids = item.tag_ids if item is not None else frozenset()
    cuisines = tuple(
        TagRef(tag.id, tag.name)
        for tag in await catalog.tags(actor.household_id)
        if tag.id in tag_ids and tag.kind is TagKind.CUISINE
    )
    participants = tuple(
        MemberRef(i, name)
        for i, name in sorted(eligible.items(), key=lambda pair: pair[1].casefold())
        if i in data.participant_ids
    )
    return cuisines, participants


def _payload(data: MealInput) -> dict[str, object]:
    return {
        "date": data.day.isoformat(),
        "slot": data.slot.value,
        "item_id": data.item_id,
        "participants": sorted(data.participant_ids),
    }


async def log_meal(db: AsyncSession, actor: Member, data: MealInput, *, today: date, now: datetime) -> MealLog:
    cuisines, participants = await _snapshots(db, actor, data, today)
    meal_id = await MealLogRepository(db).add(
        household_id=actor.household_id,
        day=data.day,
        slot=data.slot,
        item_id=data.item_id,
        cuisines=cuisines,
        participants=participants,
        logged_by=actor.id,
        now=now,
    )
    await audit(db, actor.household_id, actor, "meal.logged", f"meal:{meal_id}", _payload(data), now)
    await db.commit()
    return await get_meal(db, actor, meal_id)


async def _changeable(db: AsyncSession, actor: Member, meal_id: int) -> MealLog:
    meal = await get_meal(db, actor, meal_id)
    if not can_change(actor, meal):
        raise Forbidden("You can change only meals you logged. Ask the admin to fix this one.")
    return meal


async def update_meal(
    db: AsyncSession, actor: Member, meal_id: int, data: MealInput, *, today: date, now: datetime
) -> MealLog:
    existing = await _changeable(db, actor, meal_id)
    cuisines, participants = await _snapshots(db, actor, data, today, editing=existing)
    await MealLogRepository(db).update(
        actor.household_id,
        meal_id,
        day=data.day,
        slot=data.slot,
        item_id=data.item_id,
        cuisines=cuisines,
        participants=participants,
        now=now,
    )
    await audit(db, actor.household_id, actor, "meal.updated", f"meal:{meal_id}", _payload(data), now)
    await db.commit()
    return await get_meal(db, actor, meal_id)


async def delete_meal(db: AsyncSession, actor: Member, meal_id: int, *, now: datetime) -> None:
    meal = await _changeable(db, actor, meal_id)
    await MealLogRepository(db).delete(actor.household_id, meal_id)
    payload: dict[str, object] = {
        "date": meal.day.isoformat(),
        "slot": meal.slot.value,
        "item_id": meal.item_id,
        "logged_by": meal.logged_by,
    }
    await audit(db, actor.household_id, actor, "meal.deleted", f"meal:{meal_id}", payload, now)
    await db.commit()


async def history(db: AsyncSession, actor: Member, *, today: date, page: int = 0) -> History:
    """Meals by week, ``WEEKS_PER_PAGE`` weeks per page, newest first."""
    page = max(page, 0)
    end = week_start(today) + timedelta(days=6) - timedelta(weeks=WEEKS_PER_PAGE * page)
    start = end - timedelta(weeks=WEEKS_PER_PAGE) + timedelta(days=1)
    repo = MealLogRepository(db)
    meals = await repo.between(actor.household_id, start, end)
    earliest = await repo.earliest(actor.household_id)
    return History(weeks=by_week(meals), page=page, has_older=earliest is not None and earliest < start)
