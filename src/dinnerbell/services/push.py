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

import asyncio
import logging
import secrets
from collections.abc import Awaitable
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
PER_DEVICE = 3
"""Pushes one device may take in a pass. A device's pushes go out one batch at a time, each
within ``SEND_DEADLINE``, so without a cap one device with many due, behind a push service
that hangs, holds the pass for minutes while other devices' pushes wait outside it."""
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
GAVE_UP = "gave up: its claims kept running out"
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
    try:
        keys = push.check_subscription(endpoint, p256dh, auth)
    except push.InvalidSubscription as exc:
        # The host only, never the path, which identifies the device: so a push service
        # Dinnerbell doesn't know yet can be found and added to the allowlist.
        # An allowed host's endpoint refused for its keys logs no host at all.
        extra: dict[str, object] = {"member_id": actor.id, "reason": "keys"}
        if str(exc) == push.NOT_A_PUSH_SERVICE:
            extra |= {"reason": "host", "host": push.endpoint_host(endpoint)}
        log.info("push subscription refused", extra=extra)
        raise
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
    if len(endpoint) > push.MAX_ENDPOINT_LENGTH:
        return False  # never stored
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


HTTPS_SUBJECT = (
    "The web app is on, but DINNERBELL_BASE_URL isn't https://. Apple refuses pushes signed with a "
    "VAPID subject that isn't an https URL, so iPhones and iPads won't get notifications."
)


async def check_base_url(db: AsyncSession, household_id: int, base_url: str) -> bool:
    """At startup, and when an admin turns the switch on: warn when the web app is on and the
    base URL, the VAPID ``sub``, isn't https. Returns whether it warned."""
    if base_url.startswith("https://") or not await web_app_enabled(db, household_id):
        return False
    log.warning(HTTPS_SUBJECT)
    return True


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

PARALLEL = 4
"""Pushes in flight at once, so one slow push service doesn't hold up the others."""
SEND_DEADLINE = 15.0
"""Seconds one push may take in all, past httpx's own timeouts, before it counts as failed."""


@dataclass
class PushDrainResult:
    sent: int = 0
    retrying: int = 0
    closed: int = 0
    """Closed unsent by a check: stale, expired, the switch off, or the member's choice."""
    failed: int = 0
    devices_deleted: int = 0


@dataclass(frozen=True, slots=True)
class _Ready:
    """A claimed push, checked and built, waiting for its batch to go out."""

    queued: QueuedPush
    token: str
    body: bytes
    ttl: int
    urgency: str
    topic: str | None


async def drain(
    sessionmaker: async_sessionmaker[AsyncSession],
    sender: PushSender,
    clock: Clock,
    base_url: str,
    *,
    limit: int = BATCH,
    deadline: float = SEND_DEADLINE,
) -> PushDrainResult:
    """Send the pushes that are due, oldest first, ``PARALLEL`` at a time and at most
    ``PER_DEVICE`` to each device; the rest wait for the next pass. Safe to run beside
    itself: each row is claimed with a conditional UPDATE before it's sent. Any error with one
    row retries or closes that row and never ends the pass; a row whose claim is lost to a crash
    comes back after ``LEASE``, its attempt counted, and closes after ``MAX_ATTEMPTS``."""
    result = PushDrainResult()
    async with sessionmaker() as db:
        repo = PushRepository(db)
        waiting = await repo.due(limit, clock(), per_device=PER_DEVICE)
        await db.rollback()
        while waiting:
            batch, waiting = _one_per_device(waiting)
            ready: list[_Ready] = []
            for push_id, _ in batch:
                prepared = await _prepare(db, push_id, clock, base_url, result)
                if prepared is not None:
                    ready.append(prepared)
            answers = await asyncio.gather(*(_post(sender, item, deadline) for item in ready))
            for item, answer in zip(ready, answers, strict=True):
                await _guarded(db, item.queued, item.token, clock, result, _settle(db, item, answer, clock, result))
    return result


def _one_per_device(waiting: list[tuple[int, int]]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """The next batch: up to ``PARALLEL`` pushes, oldest first, at most one per device, so two
    pushes to one device never go out together or out of order. Returns (batch, the rest)."""
    batch: list[tuple[int, int]] = []
    rest: list[tuple[int, int]] = []
    devices: set[int] = set()
    for push_id, subscription_id in waiting:
        if len(batch) < PARALLEL and subscription_id not in devices:
            batch.append((push_id, subscription_id))
            devices.add(subscription_id)
        else:
            rest.append((push_id, subscription_id))
    return batch, rest


async def purge(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Delete pushes that ended more than ``PURGE_AFTER`` ago. The scheduler runs this hourly,
    whether or not anything is due; it reads first, so a household that never turned the web app
    on gets no write. The outbox rows keep the record."""
    async with sessionmaker() as db:
        repo = PushRepository(db)
        if not await repo.anything_to_purge(now - PURGE_AFTER):
            return 0
        removed = await repo.purge(now - PURGE_AFTER)
        await db.commit()
    return removed


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


async def _guarded(
    db: AsyncSession, queued: QueuedPush, token: str, clock: Clock, result: PushDrainResult, step: Awaitable[None]
) -> None:
    """Run one row's ``step``. On any error, retry the row, or close it once it has used up its
    attempts, logging the error's type and the IDs only: its text can carry the endpoint."""
    try:
        await step
    except Exception as exc:
        await db.rollback()
        try:
            if isinstance(exc, PayloadError | push.PushTooLarge):  # a retry would read the same row
                if await _close(db, queued, token, clock, f"undeliverable: {type(exc).__name__}"):
                    result.failed += 1
                    log.error("push undeliverable", extra=_about(queued))
                return
            await _retry(db, queued, token, clock, result, type(exc).__name__, timedelta(0))
        except Exception:
            await db.rollback()
            log.exception("push could not be settled; its claim runs out", extra=_about(queued))


async def _prepare(
    db: AsyncSession, push_id: int, clock: Clock, base_url: str, result: PushDrainResult
) -> _Ready | None:
    """Claim a push, check it, and build its request, or settle it here. None unless it's to go."""
    token = secrets.token_hex(16)
    now = clock()
    try:
        queued = await PushRepository(db).claim(push_id, token, now=now, until=now + LEASE)
        await db.commit()
    except Exception:
        await db.rollback()
        log.exception("push could not be claimed", extra={"push_id": push_id})
        return None
    if queued is None:
        return None
    built: list[_Ready] = []

    async def check_and_build() -> None:
        reason = await _stale(db, queued, clock())
        if reason is not None:
            if await _close(db, queued, token, clock, reason):
                result.closed += 1
                log.info("push closed", extra=_about(queued, note=reason))
            return
        notification = from_payload(queued.kind, queued.payload)
        at = clock()
        body = push.message(notification, at).payload(base_url)
        built.append(
            _Ready(
                queued, token, body, push.ttl(notification, at), push.urgency(queued.kind), push.topic_for(notification)
            )
        )

    await _guarded(db, queued, token, clock, result, check_and_build())
    await db.rollback()  # no transaction open while the batch is out
    return built[0] if built else None


async def _post(sender: PushSender, item: _Ready, deadline: float) -> push.PushResponse | Exception:
    """Send one push, with a deadline for the whole request. Returns the answer or the error."""
    try:
        async with asyncio.timeout(deadline):
            return await sender.send(
                item.queued.target, item.body, ttl=item.ttl, urgency=item.urgency, topic=item.topic
            )
    except Exception as exc:
        return exc


async def _stale(db: AsyncSession, queued: QueuedPush, now: datetime) -> str | None:
    """Why ``queued`` closes unsent, or None to send it. Reads the device's owner again, just
    before sending."""
    if queued.attempts > MAX_ATTEMPTS:  # claims a crash or a hang ran out on
        return GAVE_UP
    if not await web_app_enabled(db, queued.household_id):
        return SWITCHED_OFF
    if not push.known_push_service(queued.target.endpoint):  # stored before the allowlist changed
        return UNKNOWN_SERVICE
    owner = await PushRepository(db).owner(queued.target.subscription_id)
    if owner != queued.member_id:  # signed in as someone else since it was queued
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


async def _lost(db: AsyncSession, queued: QueuedPush) -> None:
    """The row didn't settle: this drainer changes nothing more, so a stale claim never counts a
    strike or deletes a device twice. Either the row is gone, deleted while the push was out with
    its device (notifications turned off, a sign-out, or the switch turned off) or its outbox row
    (a reset), which is routine, or its claim ran out and another drainer may hold it."""
    try:
        gone = not await PushRepository(db).exists(queued.id)
        await db.rollback()
    except Exception:
        await db.rollback()
        gone = False
    if gone:
        log.info("push deleted while it was out; nothing to settle", extra=_about(queued))
    else:
        log.warning("push claim ran out before it settled", extra=_about(queued))


async def _close(db: AsyncSession, queued: QueuedPush, token: str, clock: Clock, note: str) -> bool:
    """Close the row unsent, committed. False, and nothing else may follow, if the claim ran out."""
    closed = await PushRepository(db).close(queued.id, token, clock(), note)
    await db.commit()
    if not closed:
        await _lost(db, queued)
    return closed


async def _settle(
    db: AsyncSession, item: _Ready, answer: push.PushResponse | Exception, clock: Clock, result: PushDrainResult
) -> None:
    queued, token = item.queued, item.token
    if isinstance(answer, push.UndeliverableKeys):
        if await _close(db, queued, token, clock, "failed: keys that can't work"):
            await PushRepository(db).delete_subscription(queued.target.subscription_id)
            await db.commit()
            result.devices_deleted += 1
            log.warning("push subscription had keys that can't work; deleted", extra=_about(queued))
    elif isinstance(answer, push.PushTooLarge):
        if await _close(db, queued, token, clock, "failed: too large"):  # the device is fine
            result.failed += 1
            log.error("push too large to send", extra=_about(queued))
    elif isinstance(answer, Exception):
        await _retry(db, queued, token, clock, result, type(answer).__name__, timedelta(0))
    else:
        await _answer(db, queued, token, clock, result, answer)


async def _answer(
    db: AsyncSession, queued: QueuedPush, token: str, clock: Clock, result: PushDrainResult, response: push.PushResponse
) -> None:
    repo = PushRepository(db)
    now = clock()
    subscription_id = queued.target.subscription_id
    status = response.status
    found = push.outcome(status)
    if found is Outcome.SENT:
        if not await repo.sent(queued.id, token, now):
            await db.commit()
            await _lost(db, queued)
            return
        await repo.succeeded(subscription_id, now)
        await db.commit()
        result.sent += 1
        log.info("push sent", extra=_about(queued, status=status))
    elif found is Outcome.GONE:
        if await _close(db, queued, token, clock, f"gone: {status}"):
            await repo.delete_subscription(subscription_id)
            await db.commit()
            result.devices_deleted += 1
            log.info("push subscription ended", extra=_about(queued, status=status))
    elif found is Outcome.REJECTED:
        if not await _close(db, queued, token, clock, f"rejected: {status}"):
            return
        strikes = await repo.rejected(subscription_id, now)
        if strikes >= push.AUTH_STRIKES:
            await repo.delete_subscription(subscription_id)
            result.devices_deleted += 1
        await db.commit()
        result.failed += 1
        log.warning("push rejected", extra=_about(queued, status=status, strikes=strikes))
        await _count_rejection(db, queued, status, now)
    elif found is Outcome.FAILED:
        if await _close(db, queued, token, clock, f"failed: {status}"):
            result.failed += 1
            log.error("push failed", extra=_about(queued, status=status))
    else:
        asked = min(response.retry_after or 0.0, outbox.LONGEST_RETRY.total_seconds())
        await _retry(db, queued, token, clock, result, f"status {status}", timedelta(seconds=asked))


async def _count_rejection(db: AsyncSession, queued: QueuedPush, status: int, now: datetime) -> None:
    """Count a 401 or 403 toward the household's burst. The row is closed already, so a failure
    here is logged as what it is, never as a retry."""
    try:
        async with write_transaction(db):
            burst = await rejected_burst(db, queued.household_id, now)
            await db.commit()
    except Exception:
        await db.rollback()
        log.exception("push rejection not counted toward a burst", extra=_about(queued, status=status))
        return
    if burst:
        log.error(
            "push services are refusing Dinnerbell's signature; check the clock and the secret key",
            extra={"household_id": queued.household_id, "status": status},
        )


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
        if await _close(db, queued, token, clock, f"gave up: {error}"):
            result.failed += 1
            log.error("push gave up", extra=_about(queued, attempts=queued.attempts, error=error))
        return
    wait = min(max(outbox.retry_delay(queued.attempts), asked), outbox.LONGEST_RETRY)
    retried = await repo.retry(queued.id, token, at=now + wait, note=error)
    await db.commit()
    if not retried:
        await _lost(db, queued)
        return
    result.retrying += 1
    log.warning("push failed, will retry", extra=_about(queued, attempts=queued.attempts, error=error))
