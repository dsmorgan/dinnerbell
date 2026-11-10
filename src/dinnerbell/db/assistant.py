# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's usage rows and conversations (ADR 0009)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import AssistantConversationRow, AssistantUsageRow
from dinnerbell.domain.assistant import Conversation, Pseudonyms, TokenUsage, turns_from_json

RESERVED = "reserved"
SETTLED = "settled"


def _conversation(row: AssistantConversationRow) -> Conversation:
    return Conversation(
        key=row.key,
        private=row.private,
        owner_member_id=row.owner_member_id,
        pseudonyms=Pseudonyms.from_json(row.pseudonyms),
        turns=turns_from_json(row.transcript),
        last_message_at=row.last_message_at,
    )


class AssistantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Usage -------------------------------------------------------------------------------------

    async def month_total(self, household_id: int, month: str) -> int:
        """Micro-dollars the month has spent or reserved."""
        total = await self._session.scalar(
            select(func.coalesce(func.sum(AssistantUsageRow.cost_micros), 0)).where(
                AssistantUsageRow.household_id == household_id, AssistantUsageRow.month == month
            )
        )
        return int(total or 0)

    async def by_member(self, household_id: int, month: str) -> list[tuple[int | None, int]]:
        """The month's micro-dollars by member ID (None for a member since deleted), most first."""
        total = func.sum(AssistantUsageRow.cost_micros)
        rows = await self._session.execute(
            select(AssistantUsageRow.member_id, total)
            .where(AssistantUsageRow.household_id == household_id, AssistantUsageRow.month == month)
            .group_by(AssistantUsageRow.member_id)
            .order_by(total.desc(), AssistantUsageRow.member_id)
        )
        return [(member_id, int(cost)) for member_id, cost in rows.all()]

    async def reserve(
        self, household_id: int, member_id: int, model: str, month: str, micros: int, now: datetime
    ) -> int:
        row = AssistantUsageRow(
            household_id=household_id,
            member_id=member_id,
            model=model,
            month=month,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_micros=micros,
            state=RESERVED,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def settle(self, household_id: int, usage_id: int, usage: TokenUsage, micros: int, now: datetime) -> None:
        await self._session.execute(
            update(AssistantUsageRow)
            .where(AssistantUsageRow.household_id == household_id, AssistantUsageRow.id == usage_id)
            .values(
                model=usage.model[:64],
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_micros=micros,
                state=SETTLED,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )

    async def release(self, household_id: int, usage_id: int) -> None:
        """Delete a reservation for a call the API refused before running it."""
        await self._session.execute(
            delete(AssistantUsageRow).where(
                AssistantUsageRow.household_id == household_id, AssistantUsageRow.id == usage_id
            )
        )

    # Conversations -----------------------------------------------------------------------------

    async def conversation(self, household_id: int, key: str) -> Conversation | None:
        row = await self._session.scalar(
            select(AssistantConversationRow).where(
                AssistantConversationRow.household_id == household_id, AssistantConversationRow.key == key
            )
        )
        return None if row is None else _conversation(row)

    async def save_conversation(self, household_id: int, conversation: Conversation, now: datetime) -> None:
        row = await self._session.scalar(
            select(AssistantConversationRow).where(
                AssistantConversationRow.household_id == household_id,
                AssistantConversationRow.key == conversation.key,
            )
        )
        if row is None:
            row = AssistantConversationRow(household_id=household_id, key=conversation.key, created_at=now)
            self._session.add(row)
        row.private = conversation.private
        row.owner_member_id = conversation.owner_member_id
        row.pseudonyms = conversation.pseudonyms.to_json()
        row.transcript = [turn.to_json() for turn in conversation.turns]
        row.last_message_at = conversation.last_message_at
        await self._session.flush()

    async def delete_conversation(self, household_id: int, key: str) -> None:
        await self._session.execute(
            delete(AssistantConversationRow).where(
                AssistantConversationRow.household_id == household_id, AssistantConversationRow.key == key
            )
        )

    async def any_idle(self, household_id: int, last_before: datetime | None) -> bool:
        """Whether ``delete_idle`` with the same arguments would delete anything."""
        statement = select(AssistantConversationRow.id).where(AssistantConversationRow.household_id == household_id)
        if last_before is not None:
            statement = statement.where(AssistantConversationRow.last_message_at <= last_before)
        return await self._session.scalar(statement.limit(1)) is not None

    async def delete_idle(self, household_id: int, last_before: datetime | None) -> int:
        """Delete the conversations whose last message is at or before ``last_before``, or every
        conversation when it's None. Returns how many."""
        statement = delete(AssistantConversationRow).where(AssistantConversationRow.household_id == household_id)
        if last_before is not None:
            statement = statement.where(AssistantConversationRow.last_message_at <= last_before)
        result = await self._session.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)
