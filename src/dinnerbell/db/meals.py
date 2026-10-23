# SPDX-License-Identifier: AGPL-3.0-or-later
"""Meal log data access, and the household settings the variety window reads."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import CatalogItemRow, HouseholdRow, MealLogRow
from dinnerbell.domain.meals import MealLog, MemberRef, Slot, TagRef


def _refs(values: list[dict[str, Any]]) -> list[tuple[int, str]]:
    return [(int(v["id"]), str(v["name"])) for v in values]


def _meal(row: MealLogRow, item_name: str) -> MealLog:
    return MealLog(
        id=row.id,
        household_id=row.household_id,
        day=row.date,
        slot=Slot(row.slot),
        item_id=row.item_id,
        item_name=item_name,
        cuisines=tuple(TagRef(i, n) for i, n in _refs(row.cuisine_tags)),
        participants=tuple(MemberRef(i, n) for i, n in _refs(row.participants)),
        logged_by=row.logged_by,
        event_id=row.event_id,
    )


def _snapshot(refs: tuple[TagRef, ...] | tuple[MemberRef, ...]) -> list[dict[str, Any]]:
    return [{"id": ref.id, "name": ref.name} for ref in refs]


async def household_settings(session: AsyncSession, household_id: int) -> dict[str, Any]:
    """The household row's settings JSON, which is authoritative over environment defaults."""
    settings = await session.scalar(select(HouseholdRow.settings).where(HouseholdRow.id == household_id))
    return dict(settings or {})


class MealLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _select(self, household_id: int) -> Any:
        return (
            select(MealLogRow, CatalogItemRow.name)
            .join(CatalogItemRow, CatalogItemRow.id == MealLogRow.item_id)
            .where(MealLogRow.household_id == household_id)
        )

    async def get(self, household_id: int, meal_id: int) -> MealLog | None:
        found = (await self._session.execute(self._select(household_id).where(MealLogRow.id == meal_id))).first()
        return None if found is None else _meal(found[0], found[1])

    async def between(self, household_id: int, start: date | None, end: date | None) -> list[MealLog]:
        """Meals from ``start`` to ``end`` inclusive; None leaves that side open."""
        query = self._select(household_id)
        if start is not None:
            query = query.where(MealLogRow.date >= start)
        if end is not None:
            query = query.where(MealLogRow.date <= end)
        rows = await self._session.execute(query.order_by(MealLogRow.date.desc(), MealLogRow.id.desc()))
        return [_meal(row, name) for row, name in rows]

    async def for_event(self, household_id: int, event_id: int) -> int | None:
        """The ID of the meal a call logged, if it has logged one."""
        meal_id: int | None = await self._session.scalar(
            select(MealLogRow.id).where(MealLogRow.household_id == household_id, MealLogRow.event_id == event_id)
        )
        return meal_id

    async def earliest(self, household_id: int) -> date | None:
        value: date | None = await self._session.scalar(
            select(func.min(MealLogRow.date)).where(MealLogRow.household_id == household_id)
        )
        return value

    async def last_eaten(self, household_id: int) -> dict[int, date]:
        rows = await self._session.execute(
            select(MealLogRow.item_id, func.max(MealLogRow.date))
            .where(MealLogRow.household_id == household_id)
            .group_by(MealLogRow.item_id)
        )
        return {row[0]: row[1] for row in rows}

    async def add(
        self,
        *,
        household_id: int,
        day: date,
        slot: Slot,
        item_id: int,
        cuisines: tuple[TagRef, ...],
        participants: tuple[MemberRef, ...],
        logged_by: int | None,
        now: datetime,
        event_id: int | None = None,
    ) -> int:
        row = MealLogRow(
            household_id=household_id,
            date=day,
            slot=slot.value,
            item_id=item_id,
            cuisine_tags=_snapshot(cuisines),
            participants=_snapshot(participants),
            event_id=event_id,
            logged_by=logged_by,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def update(
        self,
        household_id: int,
        meal_id: int,
        *,
        day: date,
        slot: Slot,
        item_id: int,
        cuisines: tuple[TagRef, ...],
        participants: tuple[MemberRef, ...],
        now: datetime,
    ) -> None:
        row = await self._row(household_id, meal_id)
        row.date, row.slot, row.item_id = day, slot.value, item_id
        row.cuisine_tags, row.participants = _snapshot(cuisines), _snapshot(participants)
        row.updated_at = now
        await self._session.flush()

    async def delete(self, household_id: int, meal_id: int) -> None:
        await self._session.delete(await self._row(household_id, meal_id))
        await self._session.flush()

    async def _row(self, household_id: int, meal_id: int) -> MealLogRow:
        row = await self._session.scalar(
            select(MealLogRow).where(MealLogRow.household_id == household_id, MealLogRow.id == meal_id)
        )
        if row is None:
            raise LookupError(meal_id)
        return row

    async def repoint_cuisine(self, household_id: int, source_id: int, target_id: int) -> int:
        """After a tag merge, make snapshots that name ``source_id`` refer to ``target_id``.
        Names stay as they were logged; only the identity used for variety moves."""
        changed = 0
        for row in await self._session.scalars(select(MealLogRow).where(MealLogRow.household_id == household_id)):
            if not any(int(tag["id"]) == source_id for tag in row.cuisine_tags):
                continue
            has_target = any(int(tag["id"]) == target_id for tag in row.cuisine_tags)
            kept: list[dict[str, Any]] = []
            for tag in row.cuisine_tags:
                if int(tag["id"]) == source_id:
                    if has_target:
                        continue  # the target's own entry, with its name, stays
                    tag = {"id": target_id, "name": tag["name"]}
                kept.append({"id": int(tag["id"]), "name": tag["name"]})
            row.cuisine_tags = kept
            changed += 1
        await self._session.flush()
        return changed
