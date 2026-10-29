# SPDX-License-Identifier: AGPL-3.0-or-later
"""The notification outbox (spec 8, FR-34). See docs/channels.md for the delivery contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
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
    created_at: datetime
    available_at: datetime
    claimed_until: datetime | None
    attempts: int

    @property
    def is_update(self) -> bool:
        return self.kind in UPDATE_KINDS

    @property
    def creates_message(self) -> bool:
        """A post others edit or thread under. Only these hold back their event's later rows."""
        return self.message_key is not None and not self.is_update


@dataclass(frozen=True, slots=True)
class DiscardedRows:
    """What ``OutboxRepository.discard_event`` did with a discarded call's rows."""

    pending: int
    """Rows deleted that hadn't been delivered or given up on."""
    deleted: int
    message_keys: list[str]
    """The kept posts' message keys, oldest first."""
    in_flight: bool
    """A deleted row was in a live drainer's hands, so its delivery may still land."""


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
        created_at=row.created_at,
        available_at=row.available_at,
        claimed_until=row.claimed_until,
        attempts=row.attempts,
    )


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _pending() -> Any:
    return OutboxRow.delivered_at.is_(None) & OutboxRow.failed_at.is_(None)


def _untried() -> tuple[Any, ...]:
    """Rows nobody has claimed yet. Only these absorb a newer update: a row that was tried may
    be in a channel's hands, and its retry must not carry state newer than what follows it."""
    return (OutboxRow.claimed_until.is_(None), OutboxRow.attempts == 0, _pending())


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Writing ----------------------------------------------------------------------------------

    async def enqueue(
        self,
        household_id: int,
        event_id: int | None,
        notification: Notification,
        now: datetime,
        *,
        available_at: datetime | None = None,
    ) -> None:
        """Add ``notification`` to the current transaction's outbox, due now or at
        ``available_at``.

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
                available_at=available_at or now,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["household_id", "idempotency_key"])
        )

    async def _coalesce(
        self, household_id: int, notification: Notification, payload: dict[str, Any], now: datetime
    ) -> bool:
        # Only the newest row for this message may take on the new state. An older untried row
        # can exist (a held row is given back untried), and merging into it would let a newer
        # row, with older state, supersede it. If the newest has been tried, queue a new row.
        newest = await self._session.scalar(
            select(func.max(OutboxRow.id)).where(
                OutboxRow.household_id == household_id,
                OutboxRow.message_key == notification.message_key,
                OutboxRow.kind == notification.kind,
            )
        )
        if newest is None:
            return False
        result = await self._session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == newest, *_untried())
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
        """The oldest undelivered post that creates a message (it has a ``message_key``) for an
        event. Rows after it wait for it, because they may edit or thread under that message.
        Direct messages and updates hold nothing back."""
        first: int | None = await self._session.scalar(
            select(func.min(OutboxRow.id)).where(
                OutboxRow.household_id == household_id,
                OutboxRow.event_id == event_id,
                OutboxRow.message_key.is_not(None),
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

    async def release(self, entry_id: int, token: str, *, until: datetime, note: str) -> bool:
        """Give back a claim without counting the attempt: the channel held the row, or was
        unavailable."""
        result = await self._session.execute(
            update(OutboxRow)
            .where(OutboxRow.id == entry_id, OutboxRow.claim_token == token)
            .values(
                claim_token=None,
                claimed_until=None,
                available_at=until,
                attempts=OutboxRow.attempts - 1,
                last_error=note[:500],
            )
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    async def close_unsent(
        self, household_id: int, event_id: int, kinds: frozenset[str], *, note: str, now: datetime
    ) -> int:
        """Close the event's undelivered rows of ``kinds`` unsent, with ``note`` as the reason.
        A row a live drainer holds is already in a channel's hands and is left alone. Returns
        how many rows it closed."""
        result = await self._session.execute(
            update(OutboxRow)
            .where(
                OutboxRow.household_id == household_id,
                OutboxRow.event_id == event_id,
                OutboxRow.kind.in_(kinds),
                _pending(),
                OutboxRow.claimed_until.is_(None) | (OutboxRow.claimed_until <= now),
            )
            .values(delivered_at=now, claim_token=None, claimed_until=None, last_error=note)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    async def newer_update_exists(self, entry: OutboxEntry) -> bool:
        """Whether a later update to the same message is queued or sent, making ``entry`` stale."""
        newer = await self._session.scalar(
            select(OutboxRow.id)
            .where(
                OutboxRow.household_id == entry.household_id,
                OutboxRow.message_key == entry.message_key,
                OutboxRow.kind == entry.kind,
                OutboxRow.id > entry.id,
                OutboxRow.failed_at.is_(None),
            )
            .limit(1)
        )
        return newer is not None

    async def older_update_in_flight(self, entry: OutboxEntry, now: datetime) -> bool:
        """Whether an earlier update to the same message is claimed by a live drainer. Sending
        ``entry`` now could land before that one does and then be overwritten by it."""
        older = await self._session.scalar(
            select(OutboxRow.id)
            .where(
                OutboxRow.household_id == entry.household_id,
                OutboxRow.message_key == entry.message_key,
                OutboxRow.kind == entry.kind,
                OutboxRow.id < entry.id,
                OutboxRow.claimed_until > now,
                _pending(),
            )
            .limit(1)
        )
        return older is not None

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

    async def clear(self, household_id: int) -> tuple[int, int]:
        """Delete every row of the household's outbox, delivered or not, and with them the
        message refs they hold. Returns (pending rows deleted, all rows deleted)."""
        pending = await self._session.scalar(
            select(func.count()).select_from(OutboxRow).where(OutboxRow.household_id == household_id, _pending())
        )
        result = await self._session.execute(
            delete(OutboxRow).where(OutboxRow.household_id == household_id).execution_options(synchronize_session=False)
        )
        return int(pending or 0), _rowcount(result)

    async def pending_for_event(self, household_id: int, event_id: int) -> int:
        """How many of the event's rows are waiting for delivery, held ones included."""
        found = await self._session.scalar(
            select(func.count())
            .select_from(OutboxRow)
            .where(OutboxRow.household_id == household_id, OutboxRow.event_id == event_id, _pending())
        )
        return int(found or 0)

    async def discard_event(self, household_id: int, event_id: int, now: datetime) -> DiscardedRows:
        """For discarding a call on a test instance (#60): delete the event's rows, sent or not,
        except the delivered posts that created a channel's message. Those are kept, detached
        from the event (``event_id`` NULL), as the record of where each message landed, so an
        update can still edit it. A row a drainer holds right now is deleted too; its delivery
        then finds no row to record, and ``DiscardedRows.in_flight`` says it may still land.
        """
        mine = (OutboxRow.household_id == household_id, OutboxRow.event_id == event_id)
        landed = (
            OutboxRow.message_key.is_not(None),
            OutboxRow.kind.not_in(UPDATE_KINDS),
            OutboxRow.delivered_at.is_not(None),
            OutboxRow.ref_message.is_not(None),
            OutboxRow.ref_channel.is_not(None),
            OutboxRow.ref_channel != NULL_CHANNEL_ID,
        )
        kept = list(
            await self._session.scalars(select(OutboxRow.message_key).where(*mine, *landed).order_by(OutboxRow.id))
        )
        await self._session.execute(
            update(OutboxRow).where(*mine, *landed).values(event_id=None).execution_options(synchronize_session=False)
        )
        pending = await self._session.scalar(select(func.count()).select_from(OutboxRow).where(*mine, _pending()))
        held = await self._session.scalar(
            select(func.count()).select_from(OutboxRow).where(*mine, _pending(), OutboxRow.claimed_until > now)
        )
        result = await self._session.execute(
            delete(OutboxRow).where(*mine).execution_options(synchronize_session=False)
        )
        return DiscardedRows(
            pending=int(pending or 0),
            deleted=_rowcount(result),
            message_keys=[key for key in dict.fromkeys(kept) if key is not None],
            in_flight=bool(held),
        )

    async def for_household(self, household_id: int) -> list[OutboxEntry]:
        rows = await self._session.scalars(
            select(OutboxRow).where(OutboxRow.household_id == household_id).order_by(OutboxRow.id)
        )
        return [_entry(row) for row in rows]
