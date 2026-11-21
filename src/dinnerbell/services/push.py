# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web push (FR-53, ADR 0010, spec 6.15): turning notifications on and off for a device, and
draining the push queue through a ``domain.push.PushSender``.

The push queue is apart from the Slack outbox, so an outage of either never delays the other.
``OutboxRepository.enqueue_with_push`` fills it; this drains it. Each row is claimed with a lease,
as the outbox's are, and checked just before it's sent: the switch is on, the member is active
and still on **Web app** or **Both**, and the call hasn't moved past it. A row that fails a check
closes unsent. Logs carry the subscription's ID and the member's, never the endpoint, which
identifies the device.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import EventRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.push import PushRepository, QueuedPush, rejected_burst, rejected_recently, without_push_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain import push
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import PayloadError, from_payload
from dinnerbell.domain.push import Outcome, PushSender
from dinnerbell.services import outbox
from dinnerbell.services.clock import Clock
from dinnerbell.services.common import require_member
from dinnerbell.services.web_app import require_web_app, web_app_enabled

log = logging.getLogger(__name__)

BATCH = 50
LEASE = timedelta(minutes=2)
"""How long a claim lasts. A send times out within 10 seconds, so a batch fits easily."""
MAX_ATTEMPTS = 8
MAX_AGE = timedelta(hours=24)
"""A push older than this closes unsent: a day-old "Are you in?" does more harm than good."""
PURGE_AFTER = timedelta(days=7)

SWITCHED_OFF = "skipped: the web app is off"
NOT_WANTED = "skipped: the member doesn't get pushes now"
EXPIRED = "expired: queued more than 24 hours ago, not sent late"
UNKNOWN_SERVICE = "refused: not a known push service"
NOT_THEIR_DEVICE = "skipped: the device belongs to someone else now"
GUESTS_DONT = "Guests can't turn on notifications."


# Devices ----------------------------------------------------------------------------------------


async def subscribe(
    db: AsyncSession, actor: Member, session_id: str, endpoint: str, p256dh: str, auth: str, *, now: datetime
) -> int:
    """Turn on notifications for the actor's device (spec 6.15), through their session
    ``session_id``: when the session ends, so do the device's notifications. NotFound while the
    switch is off, Forbidden for a guest, and ``push.InvalidSubscription`` for an endpoint that
    isn't a known push service's or keys that can't work. Returns the subscription's ID."""
    require_member(actor, GUESTS_DONT)
    await require_web_app(db, actor.household_id)
    keys = push.check_subscription(endpoint, p256dh, auth)
    async with write_transaction(db):
        subscription_id = await PushRepository(db).subscribe(actor.household_id, actor.id, session_id, keys, now)
        await db.commit()
    log.info("push subscription saved", extra={"subscription_id": subscription_id, "member_id": actor.id})
    return subscription_id


async def unsubscribe(db: AsyncSession, actor: Member, endpoint: str) -> bool:
    """Turn notifications off for the actor's device, with whatever is queued for it. Returns
    whether there was one. NotFound while the switch is off."""
    require_member(actor, GUESTS_DONT)
    await require_web_app(db, actor.household_id)
    removed = await PushRepository(db).unsubscribe(actor.household_id, actor.id, endpoint)
    await db.commit()
    if removed:
        log.info("push subscription removed", extra={"member_id": actor.id})
    return removed


async def switched_off(db: AsyncSession, household_id: int) -> None:
    """Turning the web app off (``services.web_app.switched_off``): delete every device and every
    queued push of the household, and the rejection warning, in the caller's transaction."""
    devices, pushes = await PushRepository(db).clear(household_id)
    settings = await household_settings(db, household_id)
    if without_push_settings(settings) != settings:
        await HouseholdRepository(db).update_settings(household_id, without_push_settings(settings))
    if devices or pushes:
        log.info("push subscriptions deleted", extra={"devices": devices, "pushes": pushes})


@dataclass(frozen=True, slots=True)
class PushStatus:
    devices: int
    waiting: int
    rejected_at: datetime | None
    """When the push services last refused a burst of pushes, within a week: Admin warns."""


async def status(db: AsyncSession, household_id: int, now: datetime) -> PushStatus:
    devices, waiting = await PushRepository(db).counts(household_id)
    rejected = rejected_recently(await household_settings(db, household_id), now)
    return PushStatus(devices, waiting, rejected)


# Draining ---------------------------------------------------------------------------------------


@dataclass
class PushDrainResult:
    sent: int = 0
    retrying: int = 0
    closed: int = 0
    """Closed unsent by a check: stale, expired, the switch off, or the member's choice."""
    failed: int = 0
    devices_deleted: int = 0


async def drain(
    sessionmaker: async_sessionmaker[AsyncSession],
    sender: PushSender,
    clock: Clock,
    base_url: str,
    *,
    limit: int = BATCH,
) -> PushDrainResult:
    """Send the pushes that are due, oldest first. Safe to run beside itself: each row is
    claimed with a conditional UPDATE before it's sent."""
    result = PushDrainResult()
    async with sessionmaker() as db:
        repo = PushRepository(db)
        due = await repo.due(limit, clock())
        await db.rollback()
        for push_id in due:
            token = secrets.token_hex(16)
            now = clock()
            queued = await repo.claim(push_id, token, now=now, until=now + LEASE)
            await db.commit()
            if queued is not None:
                await _send(db, sender, queued, token, clock, base_url, result)
        if due:
            await repo.purge(clock() - PURGE_AFTER)
            await db.commit()
    return result


def _about(queued: QueuedPush, **more: object) -> dict[str, object]:
    fields = {
        "push_id": queued.id,
        "subscription_id": queued.target.subscription_id,
        "member_id": queued.member_id,
        "kind": queued.kind,
        "event_id": queued.event_id,
        **more,
    }
    return {key: value for key, value in fields.items() if value is not None}


async def _stale(db: AsyncSession, queued: QueuedPush, now: datetime) -> str | None:
    """Why ``queued`` closes unsent, or None to send it."""
    if not await web_app_enabled(db, queued.household_id):
        return SWITCHED_OFF
    if not push.known_push_service(queued.target.endpoint):  # stored before the allowlist changed
        return UNKNOWN_SERVICE
    if queued.owner_id != queued.member_id:  # signed in as someone else since it was queued
        return NOT_THEIR_DEVICE
    member = await MemberRepository(db).get(queued.household_id, queued.member_id)
    if member is None or not push.wants_push(member, queued.kind):
        return NOT_WANTED
    notification = from_payload(queued.kind, queued.payload)
    if queued.kind in push.DIRECT_PUSHES:
        reason = await outbox.overtaken_by(db, notification)
    else:
        event_info = getattr(notification, "event", None)
        if event_info is None:  # pragma: no cover - every channel post that pushes has an event
            return None
        event = await EventRepository(db).get(event_info.household_id, event_info.id)
        reason = push.channel_post_stale(
            queued.kind, event_info, event.state if event else None, event.run if event else None
        )
    if reason is not None:
        return reason
    return EXPIRED if now - queued.created_at > MAX_AGE else None


async def _send(
    db: AsyncSession,
    sender: PushSender,
    queued: QueuedPush,
    token: str,
    clock: Clock,
    base_url: str,
    result: PushDrainResult,
) -> None:
    repo = PushRepository(db)
    try:
        reason = await _stale(db, queued, clock())
    except PayloadError as exc:
        await repo.close(queued.id, token, clock(), str(exc))
        await db.commit()
        result.failed += 1
        log.error("push undeliverable", extra=_about(queued))
        return
    if reason is not None:
        await repo.close(queued.id, token, clock(), reason)
        await db.commit()
        result.closed += 1
        log.info("push closed", extra=_about(queued, note=reason))
        return
    notification = from_payload(queued.kind, queued.payload)
    now = clock()
    body = push.message(notification, now).payload(base_url)
    try:
        response = await sender.send(
            queued.target,
            body,
            ttl=push.ttl(notification, now),
            urgency=push.urgency(queued.kind),
            topic=push.topic_for(notification),
        )
    except Exception as exc:
        # Never the message: an HTTP error's text can carry the endpoint.
        await _retry(db, queued, token, clock, result, type(exc).__name__, timedelta(0))
        return
    await _answer(db, queued, token, clock, result, response)


async def _answer(
    db: AsyncSession, queued: QueuedPush, token: str, clock: Clock, result: PushDrainResult, response: push.PushResponse
) -> None:
    repo = PushRepository(db)
    now = clock()
    subscription_id = queued.target.subscription_id
    status = response.status
    found = push.outcome(status)
    if found is Outcome.SENT:
        await repo.sent(queued.id, token, now)
        await repo.succeeded(subscription_id, now)
        await db.commit()
        result.sent += 1
        log.info("push sent", extra=_about(queued, status=status))
    elif found is Outcome.GONE:
        await repo.delete_subscription(subscription_id)
        await db.commit()
        result.devices_deleted += 1
        log.info("push subscription ended", extra=_about(queued, status=status))
    elif found is Outcome.REJECTED:
        strikes = await repo.rejected(subscription_id, now)
        await repo.close(queued.id, token, now, f"rejected: {status}")
        if strikes >= push.AUTH_STRIKES:
            await repo.delete_subscription(subscription_id)
            result.devices_deleted += 1
        await db.commit()
        result.failed += 1
        log.warning("push rejected", extra=_about(queued, status=status, strikes=strikes))
        async with write_transaction(db):
            burst = await rejected_burst(db, queued.household_id, now)
            await db.commit()
        if burst:
            log.error(
                "push services are refusing Dinnerbell's signature; check the clock and the secret key",
                extra={"household_id": queued.household_id, "status": status},
            )
    elif found is Outcome.FAILED:
        await repo.close(queued.id, token, now, f"failed: {status}")
        await db.commit()
        result.failed += 1
        log.error("push failed", extra=_about(queued, status=status))
    else:
        wait = timedelta(seconds=response.retry_after or 0)
        await _retry(db, queued, token, clock, result, f"status {status}", wait)


async def _retry(
    db: AsyncSession,
    queued: QueuedPush,
    token: str,
    clock: Clock,
    result: PushDrainResult,
    error: str,
    asked: timedelta,
) -> None:
    repo = PushRepository(db)
    now = clock()
    if queued.attempts >= MAX_ATTEMPTS:
        await repo.close(queued.id, token, now, f"gave up: {error}")
        await db.commit()
        result.failed += 1
        log.error("push gave up", extra=_about(queued, attempts=queued.attempts, error=error))
        return
    wait = min(max(outbox.retry_delay(queued.attempts), asked), outbox.LONGEST_RETRY)
    await repo.retry(queued.id, token, at=now + wait, note=error)
    await db.commit()
    result.retrying += 1
    log.warning("push failed, will retry", extra=_about(queued, attempts=queued.attempts, error=error))
