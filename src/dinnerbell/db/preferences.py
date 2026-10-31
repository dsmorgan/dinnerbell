# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preference data access.

Every query that returns stances is scoped to one member, with two cross-member reads for
ranking. ``group_preferences`` returns only combined values (a union of vetoes and summed
scores), so no caller can learn one member's stances from it (FR-29). ``item_scores`` and
``item_edits`` return per-member values, for ``services.preferences`` to add each member's
ratings to their own score (FR-55) before it sums them; nothing else may call them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import PreferenceRow, StanceEditRow
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


@dataclass(frozen=True, slots=True)
class CombinedStances:
    vetoed_tags: frozenset[int] = frozenset()
    vetoed_items: frozenset[int] = frozenset()
    tag_scores: dict[int, int] = field(default_factory=dict)
    item_scores: dict[int, int] = field(default_factory=dict)


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
        """Store ``stance``, or delete the row when it's neutral. For an item, also record the
        time, neutral or not, so ratings saved before it stop moving the member's score (FR-55)."""
        if target_type is TargetType.ITEM:
            await self._edited(household_id, member_id, target_id, now)
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

    async def _edited(self, household_id: int, member_id: int, item_id: int, now: datetime) -> None:
        row = await self._session.scalar(
            select(StanceEditRow).where(
                StanceEditRow.household_id == household_id,
                StanceEditRow.member_id == member_id,
                StanceEditRow.item_id == item_id,
            )
        )
        if row is None:
            self._session.add(
                StanceEditRow(household_id=household_id, member_id=member_id, item_id=item_id, edited_at=now)
            )
        else:
            row.edited_at = now

    async def item_edits(self, household_id: int, member_ids: Iterable[int]) -> dict[tuple[int, int], datetime]:
        """(member, item) to when each member last set a stance on each item."""
        query = select(StanceEditRow.member_id, StanceEditRow.item_id, StanceEditRow.edited_at).where(
            StanceEditRow.household_id == household_id, StanceEditRow.member_id.in_(sorted(set(member_ids)))
        )
        return {(member, item): at for member, item, at in await self._session.execute(query)}

    async def item_scores(self, household_id: int, member_ids: Iterable[int]) -> dict[tuple[int, int], int]:
        """(member, item) to each member's stated score, for items they gave a nonzero score."""
        rows = await self._session.execute(
            select(PreferenceRow.member_id, PreferenceRow.item_id, PreferenceRow.score).where(
                PreferenceRow.household_id == household_id,
                PreferenceRow.member_id.in_(sorted(set(member_ids))),
                PreferenceRow.item_id.is_not(None),
                PreferenceRow.veto.is_(False),
                PreferenceRow.score != 0,
            )
        )
        return {(member, item): score for member, item, score in rows if item is not None}

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

    async def group_preferences(self, household_id: int, member_ids: Iterable[int]) -> CombinedStances:
        """The union of the members' vetoes and the sum of their scores, per tag and per item.
        The database does the combining; no per-member row leaves this method."""
        members = sorted(set(member_ids))
        if not members:
            return CombinedStances()
        scope = (PreferenceRow.household_id == household_id, PreferenceRow.member_id.in_(members))
        vetoes = await self._session.execute(
            select(PreferenceRow.tag_id, PreferenceRow.item_id).where(*scope, PreferenceRow.veto.is_(True)).distinct()
        )
        tags: set[int] = set()
        items: set[int] = set()
        for tag_id, item_id in vetoes:
            if tag_id is not None:
                tags.add(tag_id)
            elif item_id is not None:
                items.add(item_id)
        scores = await self._session.execute(
            select(PreferenceRow.tag_id, PreferenceRow.item_id, func.sum(PreferenceRow.score))
            .where(*scope, PreferenceRow.veto.is_(False))
            .group_by(PreferenceRow.tag_id, PreferenceRow.item_id)
        )
        tag_scores: dict[int, int] = {}
        item_scores: dict[int, int] = {}
        for tag_id, item_id, total in scores:
            if tag_id is not None:
                tag_scores[tag_id] = int(total)
            elif item_id is not None:
                item_scores[item_id] = int(total)
        return CombinedStances(frozenset(tags), frozenset(items), tag_scores, item_scores)
