# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draining the notification outbox through a channel (spec 8, FR-34). docs/channels.md has
the contract an adapter implements.

Delivery is in outbox order per event, with two kinds of holding back:

- A post that creates a message (it has a message_key) holds back its event's later rows until
  it lands, so an update or a threaded reply never reaches a channel before its message.
  Direct messages and updates hold nothing back.
- Updates to one message never overtake each other: a newer update waits while an older one is
  in a channel's hands, and an older one is closed as superseded, unsent, once a newer exists.

A row is claimed and committed before the channel is called, and no transaction is held open
during the call, so two drainers never deliver the same row and a slow channel never blocks
writers.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.outbox import OutboxEntry, OutboxRepository
from dinnerbell.domain.notifications import (
    Channel,
    ChannelUnavailable,
    HeldDelivery,
    MessageRef,
    Notification,
    PayloadError,
    PermanentDeliveryError,
    from_payload,
)
from dinnerbell.services.clock import Clock

log = logging.getLogger(__name__)

BATCH = 100
LEASE = timedelta(minutes=5)
"""How long a claim lasts. A drainer that dies mid-delivery frees its row after this."""
MAX_ATTEMPTS = 8
FIRST_RETRY = timedelta(seconds=30)
LONGEST_RETRY = timedelta(hours=1)
NO_MESSAGE = "no message to update"
MIN_HOLD = timedelta(seconds=5)
"""The shortest a held row waits, even if the channel asks for less."""
SUPERSEDED = "superseded by a newer update"


def retry_delay(attempts: int) -> timedelta:
    """30 s, 1 min, 2 min, ... doubling after each failed attempt, at most an hour."""
    delay: timedelta = FIRST_RETRY * 2 ** max(attempts - 1, 0)
    return min(delay, LONGEST_RETRY)


def asked_wait(exc: BaseException) -> timedelta:
    """How long the channel asked to wait, from a numeric ``retry_after`` (seconds) on the
    exception, such as Slack's Retry-After. Zero when it didn't ask; at most an hour."""
    seconds = getattr(exc, "retry_after", None)
    if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not seconds > 0:
        return timedelta(0)
    return timedelta(seconds=min(float(seconds), LONGEST_RETRY.total_seconds()))


@dataclass
class DrainResult:
    delivered: int = 0
    retrying: int = 0
    failed: int = 0
    superseded: int = 0
    held: int = 0
    stopped: bool = False
    """The channel was unavailable, so the pass ended early."""


async def drain(
    sessionmaker: async_sessionmaker[AsyncSession], channel: Channel, clock: Clock, *, limit: int = BATCH
) -> DrainResult:
    """Deliver what's due, oldest first. Safe to run concurrently with itself, in this process
    or another, though the scheduler also serializes its own drains (``scheduler.runner``)."""
    result = DrainResult()
    async with sessionmaker() as db:
        repo = OutboxRepository(db)
        blocked: set[int] = set()
        for entry in await repo.due(limit, clock()):
            if not await _may_send(repo, entry, blocked, clock()):
                continue
            token = secrets.token_hex(16)
            now = clock()
            claimed = await repo.claim(entry.id, token, now=now, until=now + LEASE)
            # Deliver the row as claimed, not as listed: an update may have coalesced into it
            # since. Once claimed, nothing changes it, so this read is final.
            current = await repo.get(entry.id) if claimed else None
            await db.commit()
            settled = current is not None and await _deliver(db, channel, current, token, clock, result)
            if result.stopped:
                break
            if not settled and entry.creates_message and entry.event_id is not None:
                blocked.add(entry.event_id)
    return result


async def _may_send(repo: OutboxRepository, entry: OutboxEntry, blocked: set[int], now: datetime) -> bool:
    """Whether ``entry`` may go now. A post that creates a message holds back its event's later
    rows until it lands; direct messages and other posts hold nothing back. An update also waits
    while an earlier update to the same message is in a channel's hands."""
    if entry.event_id is not None:
        if entry.event_id in blocked:
            return False
        first = await repo.first_pending_post(entry.household_id, entry.event_id)
        if first is not None and first < entry.id:  # an earlier message post is waiting or out
            blocked.add(entry.event_id)
            return False
    return not (entry.is_update and await repo.older_update_in_flight(entry, now))


async def _deliver(
    db: AsyncSession, channel: Channel, entry: OutboxEntry, token: str, clock: Clock, result: DrainResult
) -> bool:
    """Deliver one claimed row. True once it's settled: delivered, superseded, or given up."""
    repo = OutboxRepository(db)
    try:
        notification = from_payload(entry.kind, entry.payload)
    except PayloadError as exc:
        log.error("undeliverable notification", extra={"outbox_id": entry.id, "kind": entry.kind})
        await _record_failure(db, entry, token, str(exc), clock, result, final=True)
        return True
    if entry.is_update and await repo.newer_update_exists(entry):
        # A later update carries newer state; sending this one could only overwrite it.
        await repo.delivered(entry.id, token, now=clock(), ref=None, note=SUPERSEDED)
        await db.commit()
        result.superseded += 1
        return True
    try:
        ref, skipped = await _send(repo, channel, entry, notification)
    except HeldDelivery as exc:
        until = clock() + max(asked_wait(exc), MIN_HOLD)
        await repo.release(entry.id, token, until=until, note=f"{type(exc).__name__}: {exc}")
        await db.commit()
        result.held += 1
        return False
    except Exception as exc:
        result.stopped = isinstance(exc, ChannelUnavailable)
        # A permanent error won't get better with retries. Otherwise retry until the last
        # attempt; the claim already counted this one.
        final = isinstance(exc, PermanentDeliveryError) or entry.attempts >= MAX_ATTEMPTS
        error = f"{type(exc).__name__}: {exc}"
        await _record_failure(db, entry, token, error, clock, result, final=final, at_least=asked_wait(exc))
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
    db: AsyncSession,
    entry: OutboxEntry,
    token: str,
    error: str,
    clock: Clock,
    result: DrainResult,
    *,
    final: bool,
    at_least: timedelta = timedelta(0),
) -> None:
    now = clock()
    attempts = entry.attempts
    wait = min(max(retry_delay(attempts), at_least), LONGEST_RETRY)
    retry_at: datetime | None = None if final else now + wait
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
