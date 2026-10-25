# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discard a call on a test instance (#60), so a fresh one can start for that slot and day.

Only on a labeled instance (``config.is_test_instance``, the gate ``scrub-test`` uses), and only
an admin, or the operator with ``dinnerbell discard-call``. Production never discards: history
matters there, and a call that's over stays over.

In one transaction, under the event's lock, it deletes the call and everything that belongs to
it: responses (with their guests and role offers), suggestions, proposals, votes, the decision
(on the event row), assignments, the meal it logged, and its outbox rows, pending and held ones
too. The delivered posts that created its Slack messages stay, detached from the call, as the
record of where those messages are; the same transaction queues a ``CallDiscarded`` edit for
each, which the drainer can send only once the delete has committed. The audit row names the
call by ID and refers to no event, so it outlives it.

There's nothing to unschedule: the scheduler keeps no job per call (ADR 0004). A tick or a
Slack tap that reaches the call after it's gone finds no call (``NotFound``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.config import is_test_instance
from dinnerbell.db.events import EventRepository
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.events import EventState
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import load, locked_event
from dinnerbell.services.common import audit, require_admin
from dinnerbell.services.errors import NotFound

NOT_LABELED = (
    "Discarding a call works only on a labeled instance. Set DINNERBELL_INSTANCE_LABEL (such as TEST) in the "
    "test instance's env file; production never discards a call. Nothing changed."
)
ADMINS_ONLY = "Only an admin can discard a call."


class NotTestInstance(Exception):
    """The instance has no label, so nothing was discarded."""


def require_test_instance(instance_label: str) -> None:
    if not is_test_instance(instance_label):
        raise NotTestInstance(NOT_LABELED)


@dataclass(frozen=True, slots=True)
class DiscardPreview:
    """What discarding the call would delete, for the confirmation."""

    event_id: int
    slot: Slot
    day: date
    state: EventState
    run: int
    rows: dict[str, int]
    """Rows that belong to the call, by table."""
    pending_outbox: int

    def lines(self) -> list[str]:
        counts = ", ".join(f"{table} {count}" for table, count in self.rows.items())
        return [
            f"Call: {self.slot.value} on {self.day.isoformat()} (ID {self.event_id}, run {self.run})",
            f"State: {self.state.value}",
            f"Rows: {counts}",
            f"Notifications not yet sent: {self.pending_outbox}",
        ]


@dataclass(frozen=True, slots=True)
class DiscardSummary:
    event_id: int
    slot: Slot
    day: date
    rows: dict[str, int]
    outbox_pending_deleted: int
    outbox_rows_deleted: int
    messages_marked: int
    """Slack messages queued to say "Discarded (test)"."""

    def lines(self) -> list[str]:
        counts = ", ".join(f"{table} {count}" for table, count in self.rows.items())
        return [
            f"Discarded the {self.slot.value} call for {self.day.isoformat()} (ID {self.event_id}).",
            f"Rows deleted: {counts}",
            f"Outbox rows deleted: {self.outbox_rows_deleted} ({self.outbox_pending_deleted} not yet sent)",
            f"Messages to mark discarded: {self.messages_marked}",
        ]


async def preview(db: AsyncSession, household_id: int, event_id: int) -> DiscardPreview:
    event = await EventRepository(db).get(household_id, event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    return DiscardPreview(
        event_id=event.id,
        slot=event.slot,
        day=event.day,
        state=event.state,
        run=event.run,
        rows=await EventRepository(db).linked_counts(household_id, event_id),
        pending_outbox=await OutboxRepository(db).pending_for_event(household_id, event_id),
    )


async def discard_call(
    db: AsyncSession,
    household_id: int,
    event_id: int,
    *,
    actor: Member | None,
    instance_label: str,
    now: datetime,
) -> DiscardSummary:
    """Discard ``event_id`` and everything under it, and audit it. ``actor`` is the admin who
    asked, or None for the operator's ``dinnerbell discard-call``. Raises NotTestInstance on an
    unlabeled instance and Forbidden for anyone but an active admin, changing nothing, and
    NotFound when the household has no such call (another discard may have won)."""
    require_test_instance(instance_label)
    if actor is not None:
        require_admin(actor, ADMINS_ONLY)
        household_id = actor.household_id
    async with locked_event(db, household_id, event_id, now) as event:
        info = cc.event_info(await load(db, event))
        pending, deleted, message_keys = await OutboxRepository(db).discard_event(household_id, event.id)
        rows = await EventRepository(db).discard(household_id, event.id)
        outbox = OutboxRepository(db)
        for key in message_keys:
            await outbox.enqueue(household_id, None, note.CallDiscarded(info, key), now)
        summary = DiscardSummary(
            event_id=event.id,
            slot=event.slot,
            day=event.day,
            rows=rows,
            outbox_pending_deleted=pending,
            outbox_rows_deleted=deleted,
            messages_marked=len(message_keys),
        )
        payload = {
            "slot": event.slot.value,
            "date": event.day.isoformat(),
            "state": event.state.value,
            "run": event.run,
            "rows": rows,
            "outbox_rows_deleted": deleted,
            "outbox_pending_deleted": pending,
            "instance_label": instance_label,
        }
        await audit(db, household_id, actor, "event.discarded", f"event:{event.id}", payload, now)
    return summary
