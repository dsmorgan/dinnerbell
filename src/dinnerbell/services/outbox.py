# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draining the notification outbox through a channel (spec 8, FR-34). docs/channels.md has
the contract an adapter implements.

Delivery is in outbox order per event: while an event's earlier notification is undelivered
(waiting to retry, or claimed by another drainer), its later ones wait too, so an update never
reaches a channel before the post it edits. A row is claimed and committed before the channel
is called, and no transaction is held open during the call, so two drainers never deliver the
same row and a slow channel never blocks writers.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.outbox import OutboxEntry, OutboxRepository
from dinnerbell.domain.notifications import Channel, MessageRef, Notification, PayloadError, from_payload
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

BATCH = 100
LEASE = timedelta(minutes=5)
"""How long a claim lasts. A drainer that dies mid-delivery frees its row after this."""
MAX_ATTEMPTS = 8
FIRST_RETRY = timedelta(seconds=30)
LONGEST_RETRY = timedelta(hours=1)
NO_MESSAGE = "no message to update"


def retry_delay(attempts: int) -> timedelta:
    """30 s, 1 min, 2 min, ... doubling after each failed attempt, at most an hour."""
    delay: timedelta = FIRST_RETRY * 2 ** max(attempts - 1, 0)
    return min(delay, LONGEST_RETRY)


@dataclass
class DrainResult:
    delivered: int = 0
    retrying: int = 0
    failed: int = 0


async def drain(
    sessionmaker: async_sessionmaker[AsyncSession], channel: Channel, clock: Clock, *, limit: int = BATCH
) -> DrainResult:
    """Deliver what's due, oldest first. Safe to run concurrently with itself."""
    result = DrainResult()
    async with sessionmaker() as db:
        repo = OutboxRepository(db)
        blocked: set[int] = set()
        for entry in await repo.pending(limit):
            if entry.event_id is not None and entry.event_id in blocked:
                continue
            token = secrets.token_hex(16)
            now = clock()
            claimed = await repo.claim(entry.id, token, now=now, until=now + LEASE)
            # Deliver the row as claimed, not as listed: an update may have coalesced into it
            # since. Once claimed, nothing changes it, so this read is final.
            current = await repo.get(entry.id) if claimed else None
            await db.commit()
            delivered = current is not None and await _deliver(db, channel, current, token, clock, result)
            if not delivered and entry.event_id is not None:
                blocked.add(entry.event_id)
    return result


async def _deliver(
    db: AsyncSession, channel: Channel, entry: OutboxEntry, token: str, clock: Clock, result: DrainResult
) -> bool:
    """Deliver one claimed row. True when later rows for the same event may go next."""
    repo = OutboxRepository(db)
    try:
        notification = from_payload(entry.kind, entry.payload)
    except PayloadError as exc:
        log.error("undeliverable notification", extra={"outbox_id": entry.id, "kind": entry.kind})
        await _record_failure(db, entry, token, str(exc), clock, result, final=True)
        return True
    try:
        ref, skipped = await _send(repo, channel, entry, notification)
    except Exception as exc:
        final = entry.attempts >= MAX_ATTEMPTS  # the claim already counted this attempt
        await _record_failure(db, entry, token, f"{type(exc).__name__}: {exc}", clock, result, final=final)
        return final
    if not await repo.delivered(entry.id, token, now=clock(), ref=ref, note=NO_MESSAGE if skipped else None):
        log.warning("outbox claim expired during delivery", extra={"outbox_id": entry.id, "kind": entry.kind})
    await db.commit()
    result.delivered += 1
    return True


async def _send(
    repo: OutboxRepository, channel: Channel, entry: OutboxEntry, notification: Notification
) -> tuple[MessageRef | None, bool]:
    """Call the channel. Returns the new message's ref, and whether an update had no message."""
    if notification.is_update:
        target = await repo.message_ref(entry.household_id, entry.message_key or "")
        if target is None:
            return None, True  # the post failed for good, or the channel keeps no refs
        await channel.update(target, notification)
        return None, False
    thread = await repo.message_ref(entry.household_id, entry.thread_key) if entry.thread_key else None
    delivered = await channel.deliver(notification, thread=thread)
    return delivered.ref, False


async def _record_failure(
    db: AsyncSession, entry: OutboxEntry, token: str, error: str, clock: Clock, result: DrainResult, *, final: bool
) -> None:
    now = clock()
    attempts = entry.attempts
    retry_at: datetime | None = None if final else now + retry_delay(attempts)
    await OutboxRepository(db).failed(entry.id, token, error=error, retry_at=retry_at, now=now)
    await db.commit()
    if final:
        result.failed += 1
        log.error(
            "notification delivery gave up", extra={"outbox_id": entry.id, "kind": entry.kind, "attempts": attempts}
        )
    else:
        result.retrying += 1
        log.warning(
            "notification delivery failed", extra={"outbox_id": entry.id, "kind": entry.kind, "attempts": attempts}
        )
