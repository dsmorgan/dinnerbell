# SPDX-License-Identifier: AGPL-3.0-or-later
"""The notification outbox (spec 8, FR-34). See docs/channels.md for the delivery contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import OutboxRow
from dinnerbell.domain.notifications import NULL_CHANNEL_ID, UPDATE_KINDS, MessageRef, Notification, to_payload


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    id: int
    household_id: int
    event_id: int | None
    kind: str
    payload: dict[str, Any]
    idempotency_key: str
    message_key: str | None
    thread_key: str | None
    available_at: datetime
    claimed_until: datetime | None
    attempts: int


def _entry(row: OutboxRow) -> OutboxEntry:
    return OutboxEntry(
        id=row.id,
        household_id=row.household_id,
        event_id=row.event_id,
        kind=row.kind,
        payload=dict(row.payload),
        idempotency_key=row.idempotency_key,
        message_key=row.message_key,
        thread_key=row.thread_key,
        available_at=row.available_at,
        claimed_until=row.claimed_until,
        attempts=row.attempts,
    )


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _pending() -> Any:
    return OutboxRow.delivered_at.is_(None) & OutboxRow.failed_at.is_(None)


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Writing ----------------------------------------------------------------------------------

    async def enqueue(self, household_id: int, event_id: int | None, notification: Notification, now: datetime) -> None:
        """Add ``notification`` to the current transaction's outbox.

        A key already stored is ignored, which makes every write idempotent. An update to a
        message replaces any undelivered, unclaimed update to the same message instead of
        queueing another: the latest state wins.
        """
        payload = to_payload(notification)
        if notification.is_update and await self._coalesce(household_id, notification, payload, now):
            return
        key = notification.idempotency_key
        if notification.is_update:
            key = f"{key}:{await self._update_count(household_id, notification) + 1}"
        await self._session.execute(
            insert(OutboxRow)
            .values(
                household_id=household_id,
                event_id=event_id,
                kind=notification.kind,
                payload=payload,
                idempotency_key=key,
                message_key=notification.message_key,
                thread_key=notification.thread_key,
                created_at=now,
                updated_at=now,
                available_at=now,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["household_id", "idempotency_key"])
        )

    async def _coalesce(
        self, household_id: int, notification: Notification, payload: dict[str, Any], now: datetime
    ) -> bool:
        waiting = await self._session.scalar(
            select(OutboxRow.id)
            .where(
                OutboxRow.household_id == household_id,
                OutboxRow.message_key == notification.message_key,
                OutboxRow.kind == notification.kind,
                OutboxRow.claimed_until.is_(None),
                _pending(),
            )
            .order_by(OutboxRow.id)
            .limit(1)
        )
        if waiting is None:
            return False
        result = await self._session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == waiting, OutboxRow.claimed_until.is_(None), _pending())
            .values(payload=payload, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    async def _update_count(self, household_id: int, notification: Notification) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(OutboxRow)
            .where(
                OutboxRow.household_id == household_id,
                OutboxRow.message_key == notification.message_key,
                OutboxRow.kind == notification.kind,
            )
        )
        return int(count or 0)

    # Draining ---------------------------------------------------------------------------------

    async def due(self, limit: int, now: datetime) -> list[OutboxEntry]:
        """Rows ready to deliver now, oldest first, across households: pending, past any retry
        delay, and not claimed by a live drainer. Rows waiting to retry are left out, so any
        number of them can't crowd out newer events' rows."""
        rows = await self._session.scalars(
            select(OutboxRow)
            .where(
                _pending(),
                OutboxRow.available_at <= now,
                OutboxRow.claimed_until.is_(None) | (OutboxRow.claimed_until <= now),
            )
            .order_by(OutboxRow.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return [_entry(row) for row in rows]

    async def first_pending_post(self, household_id: int, event_id: int) -> int | None:
        """The oldest undelivered post (not an update) for an event. Rows after it wait for it."""
        first: int | None = await self._session.scalar(
            select(func.min(OutboxRow.id)).where(
                OutboxRow.household_id == household_id,
                OutboxRow.event_id == event_id,
                OutboxRow.kind.not_in(UPDATE_KINDS),
                _pending(),
            )
        )
        return first

    async def get(self, entry_id: int) -> OutboxEntry | None:
        row = await self._session.scalar(
            select(OutboxRow).where(OutboxRow.id == entry_id).execution_options(populate_existing=True)
        )
        return None if row is None else _entry(row)

    async def claim(self, entry_id: int, token: str, *, now: datetime, until: datetime) -> bool:
        """Claim a row for delivery. Only one caller can win: the UPDATE matches only a row
        that is pending, due, and not claimed by anyone whose claim is still live."""
        result = await self._session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.id == entry_id,
                _pending(),
                OutboxRow.available_at <= now,
                OutboxRow.claimed_until.is_(None) | (OutboxRow.claimed_until <= now),
            )
            .values(claim_token=token, claimed_until=until, attempts=OutboxRow.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    async def delivered(
        self, entry_id: int, token: str, *, now: datetime, ref: MessageRef | None, note: str | None = None
    ) -> bool:
        result = await self._session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == entry_id, OutboxRow.claim_token == token)
            .values(
                delivered_at=now,
                claim_token=None,
                claimed_until=None,
                last_error=note,
                ref_channel=ref.channel_id if ref else None,
                ref_message=ref.message_id if ref else None,
            )
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    async def failed(self, entry_id: int, token: str, *, error: str, retry_at: datetime | None, now: datetime) -> bool:
        """Record a failed attempt: retry at ``retry_at``, or give up when it's None."""
        values: dict[str, Any] = {"claim_token": None, "claimed_until": None, "last_error": error[:500]}
        if retry_at is None:
            values["failed_at"] = now
        else:
            values["available_at"] = retry_at
        result = await self._session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == entry_id, OutboxRow.claim_token == token)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    async def message_ref(self, household_id: int, message_key: str) -> MessageRef | None:
        """Where the post that created ``message_key`` landed, once it's delivered."""
        found = (
            await self._session.execute(
                select(OutboxRow.ref_channel, OutboxRow.ref_message)
                .where(
                    OutboxRow.household_id == household_id,
                    OutboxRow.message_key == message_key,
                    OutboxRow.kind.not_in(UPDATE_KINDS),
                    OutboxRow.delivered_at.is_not(None),
                    OutboxRow.ref_message.is_not(None),
                )
                .order_by(OutboxRow.id.desc())
                .limit(1)
            )
        ).first()
        if found is None or found[0] is None or found[1] is None or found[0] == NULL_CHANNEL_ID:
            return None  # no message, or one NullChannel only pretended to post
        return MessageRef(channel_id=found[0], message_id=found[1])

    async def backlog(self) -> tuple[int, int]:
        """(rows waiting for delivery, rows that failed for good)."""
        waiting = await self._session.scalar(select(func.count()).select_from(OutboxRow).where(_pending()))
        failed = await self._session.scalar(
            select(func.count()).select_from(OutboxRow).where(OutboxRow.failed_at.is_not(None))
        )
        return int(waiting or 0), int(failed or 0)

    async def for_household(self, household_id: int) -> list[OutboxEntry]:
        rows = await self._session.scalars(
            select(OutboxRow).where(OutboxRow.household_id == household_id).order_by(OutboxRow.id)
        )
        return [_entry(row) for row in rows]
