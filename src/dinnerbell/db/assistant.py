# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's usage rows, conversations, and drafts (ADR 0009)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import AssistantConversationRow, AssistantDraftRow, AssistantUsageRow
from dinnerbell.domain.assistant import Conversation, Pseudonyms, TokenUsage, turns_from_json
from dinnerbell.domain.assistant_drafts import Draft, DraftKind

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


def _draft(row: AssistantDraftRow) -> Draft:
    return Draft(
        id=row.id,
        household_id=row.household_id,
        member_id=row.member_id,
        conversation_key=row.conversation_key,
        kind=DraftKind(row.kind),
        payload=dict(row.payload),
        title=row.title,
        lines=tuple(str(line) for line in row.lines),
        created_at=row.created_at,
        expires_at=row.expires_at,
        event_id=row.event_id,
        run=row.run,
        used_at=row.used_at,
        confirmed_at=row.confirmed_at,
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

    # Drafts ------------------------------------------------------------------------------------

    async def add_draft(
        self,
        household_id: int,
        *,
        member_id: int,
        conversation_key: str,
        kind: DraftKind,
        payload: Mapping[str, Any],
        title: str,
        lines: Sequence[str],
        event_id: int | None,
        run: int | None,
        now: datetime,
        expires_at: datetime,
    ) -> Draft:
        row = AssistantDraftRow(
            household_id=household_id,
            member_id=member_id,
            conversation_key=conversation_key,
            kind=kind.value,
            payload=dict(payload),
            event_id=event_id,
            run=run,
            title=title[:200],
            lines=list(lines),
            created_at=now,
            expires_at=expires_at,
            used_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return _draft(row)

    async def draft(self, household_id: int, draft_id: int) -> Draft | None:
        row = await self._session.scalar(
            select(AssistantDraftRow).where(
                AssistantDraftRow.household_id == household_id, AssistantDraftRow.id == draft_id
            )
        )
        return None if row is None else _draft(row)

    async def drafts_waiting(self, household_id: int, member_id: int, key: str, now: datetime) -> list[Draft]:
        """``member_id``'s drafts in the conversation ``key`` that still wait for Confirm, oldest first."""
        rows = await self._session.scalars(
            select(AssistantDraftRow)
            .where(
                AssistantDraftRow.household_id == household_id,
                AssistantDraftRow.member_id == member_id,
                AssistantDraftRow.conversation_key == key,
                AssistantDraftRow.used_at.is_(None),
                AssistantDraftRow.expires_at > now,
            )
            .order_by(AssistantDraftRow.id)
        )
        return [_draft(row) for row in rows]

    async def waiting_drafts(self, household_id: int, member_id: int, now: datetime) -> int:
        """How many of ``member_id``'s drafts still wait for Confirm."""
        found = await self._session.scalar(
            select(func.count())
            .select_from(AssistantDraftRow)
            .where(
                AssistantDraftRow.household_id == household_id,
                AssistantDraftRow.member_id == member_id,
                AssistantDraftRow.used_at.is_(None),
                AssistantDraftRow.expires_at > now,
            )
        )
        return int(found or 0)

    async def use_draft(
        self,
        household_id: int,
        draft_id: int,
        member_id: int,
        now: datetime,
        *,
        confirmed: bool = False,
        keep_until: datetime | None = None,
    ) -> bool:
        """Mark the draft used, if it's ``member_id``'s, unused, and unexpired, and confirmed too
        when ``confirmed`` (a Cancel isn't). Returns whether it was: of two at once, only the first
        finds it unused, since SQLite has one writer. ``keep_until`` moves its expiry, so the
        purge keeps a used draft that long."""
        values: dict[str, datetime] = {"used_at": now}
        if confirmed:
            values["confirmed_at"] = now
        if keep_until is not None:
            values["expires_at"] = keep_until
        result = await self._session.execute(
            update(AssistantDraftRow)
            .where(
                AssistantDraftRow.household_id == household_id,
                AssistantDraftRow.id == draft_id,
                AssistantDraftRow.member_id == member_id,
                AssistantDraftRow.used_at.is_(None),
                AssistantDraftRow.expires_at > now,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def any_expired_drafts(self, household_id: int, now: datetime | None) -> bool:
        """Whether ``delete_drafts`` with the same arguments would delete anything."""
        statement = select(AssistantDraftRow.id).where(AssistantDraftRow.household_id == household_id)
        if now is not None:
            statement = statement.where(AssistantDraftRow.expires_at <= now)
        return await self._session.scalar(statement.limit(1)) is not None

    async def delete_drafts(self, household_id: int, now: datetime | None) -> int:
        """Delete the drafts that expired by ``now``, used or not, or every draft when it's
        None. Returns how many."""
        statement = delete(AssistantDraftRow).where(AssistantDraftRow.household_id == household_id)
        if now is not None:
            statement = statement.where(AssistantDraftRow.expires_at <= now)
        result = await self._session.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)
