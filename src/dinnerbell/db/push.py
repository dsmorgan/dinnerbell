# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web push subscriptions and the push queue (spec 6.15). ``queue_pushes`` runs beside
``OutboxRepository.enqueue`` in the same transaction; ``services.push`` drains the queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.meals import household_settings
from dinnerbell.db.models import OutboxRow, PushRow, PushSubscriptionRow
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain import push
from dinnerbell.domain.notifications import Notification
from dinnerbell.domain.push import PushTarget, SubscriptionKeys

WEB_APP_SETTING = "web_app"
"""The household setting Admin's switch writes (``services.web_app``). Only ``true`` is on."""


@dataclass(frozen=True, slots=True)
class Subscription:
    id: int
    household_id: int
    member_id: int
    target: PushTarget
    created_at: datetime
    last_success_at: datetime | None


@dataclass(frozen=True, slots=True)
class QueuedPush:
    """A push row as the drainer reads it, with its outbox row's notification and its device."""

    id: int
    household_id: int
    member_id: int
    kind: str
    payload: dict[str, Any]
    event_id: int | None
    created_at: datetime
    attempts: int
    target: PushTarget


@dataclass(frozen=True, slots=True)
class ClosedPush:
    """A push that closed unsent, as Admin > Health lists it (#339)."""

    id: int
    member_id: int
    kind: str
    event_id: int | None
    closed_at: datetime
    reason: str | None


def _subscription(row: PushSubscriptionRow) -> Subscription:
    target = PushTarget(row.id, row.endpoint, row.p256dh, row.auth)
    return Subscription(row.id, row.household_id, row.member_id, target, row.created_at, row.last_success_at)


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _pending() -> Any:
    return PushRow.sent_at.is_(None) & PushRow.closed_at.is_(None)


async def queue_pushes(
    session: AsyncSession,
    outbox_id: int,
    household_id: int,
    notification: Notification,
    now: datetime,
    available_at: datetime | None = None,
) -> int:
    """Queue one push per device of each member ``notification`` concerns who wants it, keyed by
    the outbox row and the device, in the caller's transaction. Nothing for a kind that doesn't
    push, or while the household's web app is off. Returns how many rows it added."""
    if not push.pushes(notification.kind):
        return 0
    if (await household_settings(session, household_id)).get(WEB_APP_SETTING) is not True:
        return 0
    wanted: list[int] = []
    members = MemberRepository(session)
    for member_id in push.audience(notification):
        member = await members.get(household_id, member_id)
        if member is not None and push.wants_push(member, notification.kind):
            wanted.append(member_id)
    if not wanted:
        return 0
    devices = (
        await session.execute(
            select(PushSubscriptionRow.id, PushSubscriptionRow.member_id).where(
                PushSubscriptionRow.household_id == household_id, PushSubscriptionRow.member_id.in_(wanted)
            )
        )
    ).all()
    added = 0
    for subscription_id, member_id in devices:
        result = await session.execute(
            insert(PushRow)
            .values(
                household_id=household_id,
                outbox_id=outbox_id,
                subscription_id=subscription_id,
                member_id=member_id,
                created_at=now,
                available_at=available_at or now,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["outbox_id", "subscription_id"])
        )
        added += _rowcount(result)
    return added


class PushRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # Subscriptions ----------------------------------------------------------------------------

    async def subscribe(
        self, household_id: int, member_id: int, session_id: str, keys: SubscriptionKeys, now: datetime
    ) -> int:
        """Store a device for ``member_id``, through the session ``session_id``, or update its
        keys, session, and owner if the endpoint is known. A device that changes owner, such as
        a shared phone signed in as someone else, loses every push still queued for the old
        owner, so the new one never sees them. Keeps the member to ``push.MAX_SUBSCRIPTIONS``
        by deleting their oldest. Returns the subscription's ID. Call in a write transaction."""
        known = (
            await self._session.execute(
                select(PushSubscriptionRow.id, PushSubscriptionRow.member_id).where(
                    PushSubscriptionRow.household_id == household_id, PushSubscriptionRow.endpoint == keys.endpoint
                )
            )
        ).first()
        if known is not None and known.member_id != member_id:
            await self._session.execute(delete(PushRow).where(PushRow.subscription_id == known.id, _pending()))
        found: int = (
            await self._session.execute(
                insert(PushSubscriptionRow)
                .values(
                    household_id=household_id,
                    member_id=member_id,
                    session_id=session_id,
                    endpoint=keys.endpoint,
                    p256dh=keys.p256dh,
                    auth=keys.auth,
                    created_at=now,
                    auth_failures=0,
                )
                .on_conflict_do_update(
                    index_elements=["household_id", "endpoint"],
                    set_={
                        "member_id": member_id,
                        "session_id": session_id,
                        "p256dh": keys.p256dh,
                        "auth": keys.auth,
                    },  # not auth_failures: push.js sends the same subscription again often
                )
                .returning(PushSubscriptionRow.id)
            )
        ).scalar_one()
        mine = list(
            await self._session.scalars(
                select(PushSubscriptionRow.id)
                .where(PushSubscriptionRow.household_id == household_id, PushSubscriptionRow.member_id == member_id)
                .order_by(PushSubscriptionRow.created_at.desc(), PushSubscriptionRow.id.desc())
            )
        )
        extra = [sid for sid in mine if sid != found][push.MAX_SUBSCRIPTIONS - 1 :]
        if extra:
            await self._session.execute(delete(PushSubscriptionRow).where(PushSubscriptionRow.id.in_(extra)))
        return found

    async def unsubscribe(self, household_id: int, member_id: int, endpoint: str) -> bool:
        result = await self._session.execute(
            delete(PushSubscriptionRow).where(
                PushSubscriptionRow.household_id == household_id,
                PushSubscriptionRow.member_id == member_id,
                PushSubscriptionRow.endpoint == endpoint,
            )
        )
        return _rowcount(result) == 1

    async def for_member(self, household_id: int, member_id: int) -> list[Subscription]:
        rows = await self._session.scalars(
            select(PushSubscriptionRow)
            .where(PushSubscriptionRow.household_id == household_id, PushSubscriptionRow.member_id == member_id)
            .order_by(PushSubscriptionRow.id)
        )
        return [_subscription(row) for row in rows]

    async def owner(self, subscription_id: int) -> int | None:
        """The member a device belongs to now, or None if it's gone."""
        found: int | None = await self._session.scalar(
            select(PushSubscriptionRow.member_id).where(PushSubscriptionRow.id == subscription_id)
        )
        return found

    async def delete_subscription(self, subscription_id: int) -> bool:
        """Delete a device and, with it, every push queued for it."""
        result = await self._session.execute(
            delete(PushSubscriptionRow).where(PushSubscriptionRow.id == subscription_id)
        )
        return _rowcount(result) == 1

    async def succeeded(self, subscription_id: int, now: datetime) -> None:
        await self._session.execute(
            update(PushSubscriptionRow)
            .where(PushSubscriptionRow.id == subscription_id)
            .values(last_success_at=now, auth_failures=0, auth_failed_at=None)
        )

    async def rejected(self, subscription_id: int, now: datetime) -> int:
        """Count a 401 or 403 against the device: the first in ``push.AUTH_WINDOW`` starts a new
        count. Returns the count, or 0 if the device is gone."""
        row = await self._session.get(PushSubscriptionRow, subscription_id, populate_existing=True)
        if row is None:
            return 0
        fresh = row.auth_failed_at is not None and now - row.auth_failed_at < push.AUTH_WINDOW
        count = row.auth_failures + 1 if fresh else 1
        row.auth_failures = count
        row.auth_failed_at = row.auth_failed_at if fresh else now
        await self._session.flush()
        return count

    async def clear(self, household_id: int) -> tuple[int, int]:
        """Delete every device and every queued push of the household. Returns (devices, pushes)."""
        pushes = await self._session.execute(delete(PushRow).where(PushRow.household_id == household_id))
        devices = await self._session.execute(
            delete(PushSubscriptionRow).where(PushSubscriptionRow.household_id == household_id)
        )
        return _rowcount(devices), _rowcount(pushes)

    async def oldest_pending(self, household_id: int) -> datetime | None:
        """When the household's oldest push still waiting was queued, or None if none waits."""
        found: datetime | None = await self._session.scalar(
            select(PushRow.created_at)
            .where(PushRow.household_id == household_id, _pending())
            .order_by(PushRow.created_at)
            .limit(1)
        )
        return found

    async def counts(self, household_id: int) -> tuple[int, int]:
        """(devices, pushes waiting) for the household."""
        devices = await self._session.scalar(
            select(func.count())
            .select_from(PushSubscriptionRow)
            .where(PushSubscriptionRow.household_id == household_id)
        )
        waiting = await self._session.scalar(
            select(func.count()).select_from(PushRow).where(PushRow.household_id == household_id, _pending())
        )
        return int(devices or 0), int(waiting or 0)

    async def recent_closed(
        self, household_id: int, limit: int, *, except_reasons: tuple[str, ...]
    ) -> list[ClosedPush]:
        """The household's newest ``limit`` pushes that closed unsent, newest first, leaving out
        those whose reason starts with one of ``except_reasons``. The queue keeps only a week
        (``services.push.PURGE_AFTER``), so sorting what it holds for the household stays cheap."""
        routine = [PushRow.last_error.startswith(prefix, autoescape=True) for prefix in except_reasons]
        found = await self._session.execute(
            select(
                PushRow.id, PushRow.member_id, OutboxRow.kind, OutboxRow.event_id, PushRow.closed_at, PushRow.last_error
            )
            .join(OutboxRow, OutboxRow.id == PushRow.outbox_id)
            .where(
                PushRow.closed_at.is_not(None),
                PushRow.household_id == household_id,
                *(~condition | PushRow.last_error.is_(None) for condition in routine),
            )
            .order_by(PushRow.closed_at.desc())
            .limit(limit)
        )
        return [
            ClosedPush(row.id, row.member_id, row.kind, row.event_id, row.closed_at, row.last_error)
            for row in found
            if row.closed_at is not None  # always, by the filter: this narrows the type
        ]

    # The queue --------------------------------------------------------------------------------

    async def due(self, limit: int, now: datetime, *, per_device: int) -> list[tuple[int, int]]:
        """(push ID, subscription ID) of pushes ready to send, oldest first: pending, past any
        retry delay, and unclaimed. At most ``per_device`` of each device's oldest, so one
        device with many waiting can't fill the pass and keep the others' out of it."""
        ready = (
            select(
                PushRow.id,
                PushRow.subscription_id,
                func.row_number().over(partition_by=PushRow.subscription_id, order_by=PushRow.id).label("nth"),
            )
            .where(
                _pending(),
                PushRow.available_at <= now,
                PushRow.claimed_until.is_(None) | (PushRow.claimed_until <= now),
            )
            .subquery()
        )
        found = await self._session.execute(
            select(ready.c.id, ready.c.subscription_id)
            .where(ready.c.nth <= per_device)
            .order_by(ready.c.id)
            .limit(limit)
        )
        return [(int(push_id), int(subscription_id)) for push_id, subscription_id in found]

    async def exists(self, push_id: int) -> bool:
        """Whether the push row is still there: deleting its device or the queue deletes it."""
        return await self._session.scalar(select(PushRow.id).where(PushRow.id == push_id)) is not None

    async def claim(self, push_id: int, token: str, *, now: datetime, until: datetime) -> QueuedPush | None:
        """Claim a push for sending, as the outbox does: one conditional UPDATE, so only one
        drainer wins. Returns it with its notification and device, or None."""
        result = await self._session.execute(
            update(PushRow)
            .where(
                PushRow.id == push_id,
                _pending(),
                PushRow.available_at <= now,
                PushRow.claimed_until.is_(None) | (PushRow.claimed_until <= now),
            )
            .values(claim_token=token, claimed_until=until, attempts=PushRow.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            return None
        found = (
            await self._session.execute(
                select(PushRow, OutboxRow.kind, OutboxRow.payload, OutboxRow.event_id, PushSubscriptionRow)
                .join(OutboxRow, OutboxRow.id == PushRow.outbox_id)
                .join(PushSubscriptionRow, PushSubscriptionRow.id == PushRow.subscription_id)
                .where(PushRow.id == push_id)
                .execution_options(populate_existing=True)
            )
        ).one()
        row, kind, payload, event_id, device = found
        target = PushTarget(device.id, device.endpoint, device.p256dh, device.auth)
        return QueuedPush(
            row.id,
            row.household_id,
            row.member_id,
            kind,
            dict(payload),
            event_id,
            row.created_at,
            row.attempts,
            target,
        )

    async def sent(self, push_id: int, token: str, now: datetime) -> bool:
        return await self._finish(push_id, token, sent_at=now, last_error=None)

    async def close(self, push_id: int, token: str, now: datetime, note: str) -> bool:
        """Close a push unsent: stale, expired, or refused for good."""
        return await self._finish(push_id, token, closed_at=now, last_error=note[:200])

    async def retry(self, push_id: int, token: str, *, at: datetime, note: str) -> bool:
        return await self._finish(push_id, token, available_at=at, last_error=note[:200])

    async def _finish(self, push_id: int, token: str, **values: Any) -> bool:
        result = await self._session.execute(
            update(PushRow)
            .where(PushRow.id == push_id, PushRow.claim_token == token)
            .values(claim_token=None, claimed_until=None, **values)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    async def anything_to_purge(self, before: datetime) -> bool:
        """Whether a push ended before ``before``: a read, so an idle install never writes."""
        found = await self._session.scalar(
            select(PushRow.id).where((PushRow.sent_at < before) | (PushRow.closed_at < before)).limit(1)
        )
        return found is not None

    async def purge(self, before: datetime) -> int:
        """Delete pushes that ended before ``before``: their outbox rows keep the record."""
        result = await self._session.execute(
            delete(PushRow).where(
                (PushRow.sent_at < before) | (PushRow.closed_at < before),
            )
        )
        return _rowcount(result)


async def rejected_burst(session: AsyncSession, household_id: int, now: datetime) -> bool:
    """Record one 401 or 403 in the household's settings. True when it makes
    ``push.AUTH_BURST`` within ``push.AUTH_BURST_WINDOW``, which also marks the warning Admin
    shows (``push_rejected_at``). Call inside a write transaction."""
    settings = await household_settings(session, household_id)
    window = settings.get(REJECTIONS_SETTING)
    since = _parse(window.get("since")) if isinstance(window, dict) else None
    count = window.get("count", 0) if isinstance(window, dict) else 0
    if since is None or not isinstance(count, int) or now - since >= push.AUTH_BURST_WINDOW:
        since, count = now, 0
    count += 1
    updated = settings | {REJECTIONS_SETTING: {"since": since.isoformat(), "count": count}}
    burst = count == push.AUTH_BURST
    if burst:
        updated[REJECTED_SETTING] = now.isoformat()
    await HouseholdRepository(session).update_settings(household_id, updated)
    return burst


REJECTIONS_SETTING = "push_rejections"
REJECTED_SETTING = "push_rejected_at"
WARNING_LASTS = timedelta(days=7)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def rejected_recently(settings: dict[str, Any], now: datetime) -> datetime | None:
    """When a burst of 401 and 403 answers last happened, within ``WARNING_LASTS``, or None."""
    at = _parse(settings.get(REJECTED_SETTING))
    return at if at is not None and now - at < WARNING_LASTS else None


def without_push_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in settings.items() if k not in (REJECTIONS_SETTING, REJECTED_SETTING)}
