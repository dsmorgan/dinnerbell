# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback data access (FR-45 to FR-49). Repositories return whole ``Feedback`` entities;
services decide who may see what of them."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import FeedbackRow
from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.feedback import Feedback, FeedbackInput, MealFacts, PlaceIssue, Rating
from dinnerbell.domain.meals import MemberRef


def _feedback(row: FeedbackRow) -> Feedback:
    cook = MemberRef(row.cook_member_id, row.cook_name or "") if row.cook_member_id is not None else None
    return Feedback(
        id=row.id,
        household_id=row.household_id,
        meal_id=row.meal_id,
        member_id=row.member_id,
        answers=FeedbackInput(
            food=Rating(row.food) if row.food else None,
            place=Rating(row.place) if row.place else None,
            place_issues=frozenset(PlaceIssue(i) for i in row.place_issues),
            cook_thanks=row.cook_thanks,
            cook_note=row.cook_note,
        ),
        item_id=row.item_id,
        item_name=row.item_name,
        item_kind=ItemKind(row.item_kind),
        mode=Mode(row.mode) if row.mode else None,
        cook=cook,
        participants=tuple(MemberRef(int(p["id"]), str(p["name"])) for p in row.participants),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _fill(row: FeedbackRow, data: FeedbackInput, facts: MealFacts, now: datetime) -> None:
    row.food = data.food.value if data.food else None
    row.place = data.place.value if data.place else None
    row.place_issues = sorted(i.value for i in data.place_issues)
    row.cook_thanks = data.cook_thanks
    row.cook_note = data.cook_note
    row.item_id, row.item_name, row.item_kind = facts.item_id, facts.item_name, facts.item_kind.value
    row.mode = facts.mode.value if facts.mode else None
    row.cook_member_id = facts.cook.id if facts.cook else None
    row.cook_name = facts.cook.name if facts.cook else None
    row.participants = [{"id": p.id, "name": p.name} for p in facts.participants]
    row.updated_at = now


class FeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, household_id: int, meal_id: int, member_id: int) -> FeedbackRow | None:
        row: FeedbackRow | None = await self._session.scalar(
            select(FeedbackRow).where(
                FeedbackRow.household_id == household_id,
                FeedbackRow.meal_id == meal_id,
                FeedbackRow.member_id == member_id,
            )
        )
        return row

    async def get(self, household_id: int, meal_id: int, member_id: int) -> Feedback | None:
        row = await self._row(household_id, meal_id, member_id)
        return None if row is None else _feedback(row)

    async def save(
        self, household_id: int, member_id: int, data: FeedbackInput, facts: MealFacts, now: datetime
    ) -> Feedback:
        """Insert or replace ``member_id``'s feedback on ``facts.meal_id``, snapshotting ``facts``."""
        row = await self._row(household_id, facts.meal_id, member_id)
        if row is None:
            row = FeedbackRow(household_id=household_id, meal_id=facts.meal_id, member_id=member_id, created_at=now)
            self._session.add(row)
        _fill(row, data, facts, now)
        await self._session.flush()
        return _feedback(row)

    async def delete(self, household_id: int, meal_id: int, member_id: int) -> bool:
        row = await self._row(household_id, meal_id, member_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def clear(self, household_id: int) -> int:
        """Delete all of the household's feedback, for ``scrub-test``. Returns how many rows."""
        found = await self._session.scalar(
            select(func.count()).select_from(FeedbackRow).where(FeedbackRow.household_id == household_id)
        )
        await self._session.execute(delete(FeedbackRow).where(FeedbackRow.household_id == household_id))
        return int(found or 0)

    async def for_meals(self, household_id: int, meal_ids: Iterable[int]) -> list[Feedback]:
        """Everyone's feedback on these meals, oldest first. Services must filter it."""
        wanted = list(set(meal_ids))
        if not wanted:
            return []
        rows = await self._session.scalars(
            select(FeedbackRow)
            .where(FeedbackRow.household_id == household_id, FeedbackRow.meal_id.in_(wanted))
            .order_by(FeedbackRow.id)
        )
        return [_feedback(row) for row in rows]

    async def given_by(self, household_id: int, member_id: int, meal_ids: Iterable[int]) -> set[int]:
        """Which of these meals ``member_id`` has given feedback on."""
        wanted = list(set(meal_ids))
        if not wanted:
            return set()
        found = await self._session.scalars(
            select(FeedbackRow.meal_id).where(
                FeedbackRow.household_id == household_id,
                FeedbackRow.member_id == member_id,
                FeedbackRow.meal_id.in_(wanted),
            )
        )
        return set(found)

    async def for_items(self, household_id: int, item_ids: Iterable[int] | None = None) -> list[Feedback]:
        """Feedback on these catalog items (as each feedback snapshotted the item), or on every
        item, for counting per item. Services must reduce it to counts."""
        query: Any = select(FeedbackRow).where(FeedbackRow.household_id == household_id)
        if item_ids is not None:
            query = query.where(FeedbackRow.item_id.in_(list(set(item_ids))))
        return [_feedback(row) for row in await self._session.scalars(query.order_by(FeedbackRow.id))]
