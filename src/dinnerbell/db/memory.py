# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's memory: each member's facts (#367, ADR 0012)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import AssistantMemoryFactRow
from dinnerbell.domain.assistant_memory import Fact


def _fact(row: AssistantMemoryFactRow) -> Fact:
    return Fact(
        id=row.id,
        member_id=row.member_id,
        kind=row.kind,
        text=row.text,
        written_at=row.written_at,
        expires_at=row.expires_at,
        source_key=row.source_key,
    )


class MemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def facts(self, household_id: int, now: datetime, member_id: int | None = None) -> list[Fact]:
        """The facts that haven't expired by ``now``, ``member_id``'s only when given, oldest first."""
        statement = select(AssistantMemoryFactRow).where(
            AssistantMemoryFactRow.household_id == household_id,
            or_(AssistantMemoryFactRow.expires_at.is_(None), AssistantMemoryFactRow.expires_at > now),
        )
        if member_id is not None:
            statement = statement.where(AssistantMemoryFactRow.member_id == member_id)
        rows = await self._session.scalars(statement.order_by(AssistantMemoryFactRow.id))
        return [_fact(row) for row in rows]

    async def counts(self, household_id: int) -> dict[int, int]:
        """How many facts each member has, expired or not, by member ID."""
        rows = await self._session.execute(
            select(AssistantMemoryFactRow.member_id, func.count())
            .where(AssistantMemoryFactRow.household_id == household_id)
            .group_by(AssistantMemoryFactRow.member_id)
        )
        return {member_id: int(count) for member_id, count in rows.all()}

    async def add(
        self,
        household_id: int,
        member_id: int,
        *,
        kind: str,
        text: str,
        source_key: str,
        now: datetime,
        expires_at: datetime | None,
    ) -> None:
        self._session.add(
            AssistantMemoryFactRow(
                household_id=household_id,
                member_id=member_id,
                kind=kind,
                text=text[:160],
                source_key=source_key[:80],
                written_at=now,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def replace(
        self,
        household_id: int,
        fact_id: int,
        *,
        kind: str,
        text: str,
        source_key: str,
        now: datetime,
        expires_at: datetime | None,
    ) -> None:
        await self._session.execute(
            update(AssistantMemoryFactRow)
            .where(AssistantMemoryFactRow.household_id == household_id, AssistantMemoryFactRow.id == fact_id)
            .values(kind=kind, text=text[:160], source_key=source_key[:80], written_at=now, expires_at=expires_at)
            .execution_options(synchronize_session=False)
        )

    async def delete(self, household_id: int, fact_ids: Collection[int], *, member_id: int | None = None) -> int:
        """Delete the facts ``fact_ids``, only ``member_id``'s when given. Returns how many."""
        if not fact_ids:
            return 0
        statement = delete(AssistantMemoryFactRow).where(
            AssistantMemoryFactRow.household_id == household_id, AssistantMemoryFactRow.id.in_(list(fact_ids))
        )
        if member_id is not None:
            statement = statement.where(AssistantMemoryFactRow.member_id == member_id)
        result = await self._session.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_all(self, household_id: int, member_id: int | None = None) -> int:
        """Delete ``member_id``'s facts, or every member's when it's None. Returns how many."""
        statement = delete(AssistantMemoryFactRow).where(AssistantMemoryFactRow.household_id == household_id)
        if member_id is not None:
            statement = statement.where(AssistantMemoryFactRow.member_id == member_id)
        result = await self._session.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)

    async def any_expired(self, household_id: int, now: datetime) -> bool:
        found = await self._session.scalar(
            select(AssistantMemoryFactRow.id)
            .where(AssistantMemoryFactRow.household_id == household_id, AssistantMemoryFactRow.expires_at <= now)
            .limit(1)
        )
        return found is not None

    async def delete_expired(self, household_id: int, now: datetime) -> int:
        result = await self._session.execute(
            delete(AssistantMemoryFactRow).where(
                AssistantMemoryFactRow.household_id == household_id, AssistantMemoryFactRow.expires_at <= now
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)
