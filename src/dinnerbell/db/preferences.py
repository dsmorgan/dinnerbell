# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preference data access. Every query is scoped to one member; there is no "all members" read."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import PreferenceRow
from dinnerbell.domain.preferences import Preference, Stance, TargetType


def _preference(row: PreferenceRow) -> Preference:
    if row.tag_id is not None:
        target_type, target_id = TargetType.TAG, row.tag_id
    else:
        target_type, target_id = TargetType.ITEM, row.item_id or 0
    return Preference(
        member_id=row.member_id,
        target_type=target_type,
        target_id=target_id,
        stance=Stance(veto=row.veto, score=row.score),
    )


def _target_column(target_type: TargetType) -> str:
    return "tag_id" if target_type is TargetType.TAG else "item_id"


class PreferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_member(self, household_id: int, member_id: int) -> list[Preference]:
        rows = await self._session.scalars(
            select(PreferenceRow).where(
                PreferenceRow.household_id == household_id, PreferenceRow.member_id == member_id
            )
        )
        return [_preference(row) for row in rows]

    async def set(
        self,
        household_id: int,
        member_id: int,
        target_type: TargetType,
        target_id: int,
        stance: Stance,
        now: datetime,
    ) -> None:
        """Store ``stance``, or delete the row when it's neutral."""
        column = getattr(PreferenceRow, _target_column(target_type))
        row = await self._session.scalar(
            select(PreferenceRow).where(
                PreferenceRow.household_id == household_id, PreferenceRow.member_id == member_id, column == target_id
            )
        )
        if stance.is_neutral:
            if row is not None:
                await self._session.delete(row)
                await self._session.flush()
            return
        if row is None:
            row = PreferenceRow(household_id=household_id, member_id=member_id)
            setattr(row, _target_column(target_type), target_id)
            self._session.add(row)
        row.veto, row.score, row.updated_at = stance.veto, stance.score, now
        await self._session.flush()

    async def move_tag(
        self,
        household_id: int,
        source_id: int,
        target_id: int,
        resolve: Callable[[Stance, Stance], Stance],
        now: datetime,
    ) -> int:
        """Re-point every member's stance on ``source_id`` to ``target_id``. A member with a
        stance on both keeps ``resolve(target, source)``. Returns how many rows moved or merged."""
        rows = list(
            await self._session.scalars(
                select(PreferenceRow).where(
                    PreferenceRow.household_id == household_id, PreferenceRow.tag_id.in_([source_id, target_id])
                )
            )
        )
        on_target = {row.member_id: row for row in rows if row.tag_id == target_id}
        moved = 0
        for row in (r for r in rows if r.tag_id == source_id):
            kept = on_target.get(row.member_id)
            if kept is None:
                row.tag_id = target_id
                row.updated_at = now
            else:
                stance = resolve(Stance(veto=kept.veto, score=kept.score), Stance(veto=row.veto, score=row.score))
                kept.veto, kept.score, kept.updated_at = stance.veto, stance.score, now
                await self._session.delete(row)
            moved += 1
        await self._session.flush()
        return moved
