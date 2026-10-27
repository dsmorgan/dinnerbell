# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catalog data access: tags, items, and their tag and cook links."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import CatalogItemCookRow, CatalogItemRow, CatalogItemTagRow, TagRow
from dinnerbell.domain.catalog import MEAL_ORDER, CatalogItem, ItemKind, Meal, Mode, Tag, TagKind, name_key

MEAL_BITS = {meal: 1 << index for index, meal in enumerate(MEAL_ORDER)}
"""``catalog_item.meals`` stores the meals an item is for as bits: breakfast 1, lunch 2, dinner 4."""


def meals_to_bits(meals: frozenset[Meal]) -> int:
    return sum(MEAL_BITS[meal] for meal in meals)


def meals_from_bits(bits: int) -> frozenset[Meal]:
    return frozenset(meal for meal, bit in MEAL_BITS.items() if bits & bit)


def _tag(row: TagRow) -> Tag:
    return Tag(id=row.id, household_id=row.household_id, kind=TagKind(row.kind), name=row.name)


@dataclass(frozen=True, slots=True)
class ItemValues:
    """Every stored field of an item, as the services layer sets them."""

    kind: ItemKind
    name: str
    modes: frozenset[Mode]
    meals: frozenset[Meal]
    tag_ids: frozenset[int]
    cook_ids: frozenset[int]
    url: str | None
    order_url: str | None
    notes: str | None
    default_servings: int | None
    active: bool


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Tags ------------------------------------------------------------------------------------

    async def tags(self, household_id: int) -> list[Tag]:
        rows = await self._session.scalars(
            select(TagRow).where(TagRow.household_id == household_id).order_by(TagRow.name_key)
        )
        return [_tag(row) for row in rows]

    async def tag(self, household_id: int, tag_id: int) -> Tag | None:
        row = await self._tag_row(household_id, tag_id)
        return None if row is None else _tag(row)

    async def _tag_row(self, household_id: int, tag_id: int) -> TagRow | None:
        return await self._session.scalar(
            select(TagRow).where(TagRow.household_id == household_id, TagRow.id == tag_id)
        )

    async def add_tag(self, household_id: int, kind: TagKind, name: str, now: datetime) -> Tag:
        row = TagRow(household_id=household_id, kind=kind.value, name=name, name_key=name_key(name), created_at=now)
        self._session.add(row)
        await self._session.flush()
        return _tag(row)

    async def rename_tag(self, household_id: int, tag_id: int, name: str) -> Tag:
        row = await self._tag_row(household_id, tag_id)
        if row is None:
            raise LookupError(tag_id)
        row.name, row.name_key = name, name_key(name)
        await self._session.flush()
        return _tag(row)

    async def tag_usage(self, household_id: int) -> dict[int, int]:
        """How many items carry each tag."""
        rows = await self._session.execute(
            select(CatalogItemTagRow.tag_id, func.count())
            .where(CatalogItemTagRow.household_id == household_id)
            .group_by(CatalogItemTagRow.tag_id)
        )
        return {row[0]: row[1] for row in rows}

    async def move_tag_links(self, household_id: int, source_id: int, target_id: int) -> int:
        """Point every item tagged ``source_id`` at ``target_id`` instead, without duplicates.
        Returns how many items gained the target tag."""
        tagged_source = set(
            await self._session.scalars(
                select(CatalogItemTagRow.item_id).where(
                    CatalogItemTagRow.household_id == household_id, CatalogItemTagRow.tag_id == source_id
                )
            )
        )
        tagged_target = set(
            await self._session.scalars(
                select(CatalogItemTagRow.item_id).where(
                    CatalogItemTagRow.household_id == household_id, CatalogItemTagRow.tag_id == target_id
                )
            )
        )
        await self._session.execute(
            delete(CatalogItemTagRow).where(
                CatalogItemTagRow.household_id == household_id, CatalogItemTagRow.tag_id == source_id
            )
        )
        gained = tagged_source - tagged_target
        self._session.add_all(
            CatalogItemTagRow(household_id=household_id, item_id=item_id, tag_id=target_id) for item_id in gained
        )
        await self._session.flush()
        return len(gained)

    async def delete_tag(self, household_id: int, tag_id: int) -> None:
        await self._session.execute(delete(TagRow).where(TagRow.household_id == household_id, TagRow.id == tag_id))

    # Items -----------------------------------------------------------------------------------

    async def is_empty(self, household_id: int) -> bool:
        items = await self._session.scalar(
            select(func.count()).select_from(CatalogItemRow).where(CatalogItemRow.household_id == household_id)
        )
        tags = await self._session.scalar(
            select(func.count()).select_from(TagRow).where(TagRow.household_id == household_id)
        )
        return not items and not tags

    async def items(self, household_id: int) -> list[CatalogItem]:
        rows = list(
            await self._session.scalars(
                select(CatalogItemRow)
                .where(CatalogItemRow.household_id == household_id)
                .order_by(CatalogItemRow.name_key, CatalogItemRow.id)
            )
        )
        return await self._with_links(household_id, rows)

    async def item(self, household_id: int, item_id: int) -> CatalogItem | None:
        row = await self._item_row(household_id, item_id)
        if row is None:
            return None
        [item] = await self._with_links(household_id, [row])
        return item

    async def item_by_name(self, household_id: int, kind: ItemKind, name: str) -> CatalogItem | None:
        row = await self._session.scalar(
            select(CatalogItemRow).where(
                CatalogItemRow.household_id == household_id,
                CatalogItemRow.kind == kind.value,
                CatalogItemRow.name_key == name_key(name),
            )
        )
        if row is None:
            return None
        [item] = await self._with_links(household_id, [row])
        return item

    async def _item_row(self, household_id: int, item_id: int) -> CatalogItemRow | None:
        return await self._session.scalar(
            select(CatalogItemRow).where(CatalogItemRow.household_id == household_id, CatalogItemRow.id == item_id)
        )

    async def _with_links(self, household_id: int, rows: list[CatalogItemRow]) -> list[CatalogItem]:
        ids = [row.id for row in rows]
        tags: dict[int, set[int]] = defaultdict(set)
        cooks: dict[int, set[int]] = defaultdict(set)
        if ids:
            for item_id, tag_id in await self._session.execute(
                select(CatalogItemTagRow.item_id, CatalogItemTagRow.tag_id).where(
                    CatalogItemTagRow.household_id == household_id, CatalogItemTagRow.item_id.in_(ids)
                )
            ):
                tags[item_id].add(tag_id)
            for item_id, member_id in await self._session.execute(
                select(CatalogItemCookRow.item_id, CatalogItemCookRow.member_id).where(
                    CatalogItemCookRow.household_id == household_id, CatalogItemCookRow.item_id.in_(ids)
                )
            ):
                cooks[item_id].add(member_id)
        return [
            CatalogItem(
                id=row.id,
                household_id=row.household_id,
                kind=ItemKind(row.kind),
                name=row.name,
                modes=frozenset(Mode(m) for m in row.modes),
                tag_ids=frozenset(tags[row.id]),
                cook_ids=frozenset(cooks[row.id]),
                url=row.url,
                order_url=row.order_url,
                notes=row.notes,
                default_servings=row.default_servings,
                active=row.active,
                created_by=row.created_by,
                meals=meals_from_bits(row.meals),
            )
            for row in rows
        ]

    async def add_item(
        self, household_id: int, values: ItemValues, *, created_by: int | None, now: datetime
    ) -> CatalogItem:
        row = CatalogItemRow(household_id=household_id, created_by=created_by, created_at=now)
        self._apply(row, values, now)
        self._session.add(row)
        await self._session.flush()
        await self._set_links(household_id, row.id, values.tag_ids, values.cook_ids)
        [item] = await self._with_links(household_id, [row])
        return item

    async def update_item(self, household_id: int, item_id: int, values: ItemValues, now: datetime) -> CatalogItem:
        row = await self._item_row(household_id, item_id)
        if row is None:
            raise LookupError(item_id)
        self._apply(row, values, now)
        await self._set_links(household_id, item_id, values.tag_ids, values.cook_ids)
        [item] = await self._with_links(household_id, [row])
        return item

    async def set_active(self, household_id: int, item_id: int, active: bool, now: datetime) -> None:
        row = await self._item_row(household_id, item_id)
        if row is None:
            raise LookupError(item_id)
        row.active, row.updated_at = active, now
        await self._session.flush()

    @staticmethod
    def _apply(row: CatalogItemRow, values: ItemValues, now: datetime) -> None:
        row.kind = values.kind.value
        row.name, row.name_key = values.name, name_key(values.name)
        row.modes = sorted(m.value for m in values.modes)
        row.meals = meals_to_bits(values.meals)
        row.url, row.order_url, row.notes = values.url, values.order_url, values.notes
        row.default_servings = values.default_servings
        row.active = values.active
        row.updated_at = now

    async def _set_links(
        self, household_id: int, item_id: int, tag_ids: Iterable[int], cook_ids: Iterable[int]
    ) -> None:
        await self._session.execute(
            delete(CatalogItemTagRow).where(
                CatalogItemTagRow.household_id == household_id, CatalogItemTagRow.item_id == item_id
            )
        )
        await self._session.execute(
            delete(CatalogItemCookRow).where(
                CatalogItemCookRow.household_id == household_id, CatalogItemCookRow.item_id == item_id
            )
        )
        self._session.add_all(
            CatalogItemTagRow(household_id=household_id, item_id=item_id, tag_id=tag_id) for tag_id in set(tag_ids)
        )
        self._session.add_all(
            CatalogItemCookRow(household_id=household_id, item_id=item_id, member_id=member_id)
            for member_id in set(cook_ids)
        )
        await self._session.flush()
