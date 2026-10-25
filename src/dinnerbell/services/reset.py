# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell reset-history``: start production's record afresh before going live (#56).

Calls, votes, meals, and feedback from testing would skew History, "eaten lately", and stats
once the household relies on Dinnerbell. The reset deletes that history and keeps the setup.
Every table is either ``KEEP`` or ``CLEAR``, here and nowhere else, and a test fails on a table
that is neither, so a table added later has to be classified.

It works on production; unlike ``scrub-test`` and ``discard-call``, it doesn't need a label. In
one transaction that holds the write lock, it refuses while a call is open, deletes every row of
each ``CLEAR`` table, children first, and writes one ``history.reset`` audit row with the
counts. The CLI shows a read-only preview and takes a backup first.

Slack messages from before stay as they are, and nothing edits them afterwards: the outbox, which
records where each message landed, is cleared, and a tap on an old button finds no call. Event IDs
are never reused (``event`` is AUTOINCREMENT), so an old button or link can't reach a new call.
App Home is drawn from the database each time it opens, so it shows no stale calls. The scheduler
keeps no job per call (ADR 0004); its next tick opens calls as usual.

Nothing on a ``KEEP`` table counts history yet: no streaks or totals on members or in household
settings. A counter added to a ``KEEP`` table later must be reset here too.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import Table, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.models import Base
from dinnerbell.domain.events import OPEN_STATES, EventKind, EventState, should_open_today
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.schedule import slot_schedules
from dinnerbell.services.common import audit

KEEP: Final = frozenset(
    {
        "household",  # its name, time zone, and settings: the schedule, ranking weights, variety window
        "member",  # members, roles, what each can do, and Slack links
        "session",  # who's signed in
        "audit",  # the audit log, which records the reset itself
        "tag",  # the catalog ...
        "catalog_item",
        "catalog_item_tag",
        "catalog_item_cook",
        "preference",  # likes, dislikes, and vetoes
    }
)
"""Tables the reset leaves alone."""

CLEAR: Final = frozenset(
    {
        "event",  # calls ...
        "response",  # ... and everything in them (``db.events.EVENT_ROWS``)
        "suggestion",
        "proposal",
        "vote",
        "assignment",
        "meal_log",  # what the household ate
        "feedback",  # and how it was
        "outbox",  # notifications, sent and unsent, and where each Slack message landed
    }
)
"""Tables the reset empties."""

OPEN_CALLS = (
    "A call is still open, so nothing changed. Call off each one below (Call it off on its card, or in Slack), "
    "then run reset-history again."
)


class ResetRefused(Exception):
    """The reset didn't run. The message says why."""


@dataclass(frozen=True, slots=True)
class OpenCall:
    event_id: int
    slot: Slot
    day: date
    state: EventState

    def line(self) -> str:
        return f"{self.slot.value} on {self.day.isoformat()} (ID {self.event_id}): {self.state.label}"


def refusal(calls: Collection[OpenCall]) -> str:
    return "\n".join([OPEN_CALLS, *(f"  {call.line()}" for call in calls)])


def _counts_line(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{table} {count}" for table, count in sorted(counts.items()))


@dataclass(frozen=True, slots=True)
class ResetPreview:
    """What a reset would delete and keep, by table, for the operator to check first."""

    household_name: str
    clear: dict[str, int]
    keep: dict[str, int]
    open_calls: tuple[OpenCall, ...]
    pending_outbox: int
    posts_again: tuple[Slot, ...]
    """Today's calls whose response window is still open: once they're deleted, the scheduler's
    next tick posts them again, as if they had never been."""

    def lines(self) -> list[str]:
        lines = [
            f"Household: {self.household_name}",
            f"Deletes: {_counts_line(self.clear)}",
            f"Keeps: {_counts_line(self.keep)}",
            f"Notifications not yet sent: {self.pending_outbox}",
        ]
        lines += [
            f"Today's {slot.value} call is still taking answers, so the scheduler posts it again after the reset."
            for slot in self.posts_again
        ]
        return lines


@dataclass(frozen=True, slots=True)
class ResetSummary:
    deleted: dict[str, int]
    kept: dict[str, int]
    backup: str

    def lines(self) -> list[str]:
        return [
            f"Backup: {self.backup}",
            f"Deleted: {_counts_line(self.deleted)}",
            f"Kept: {_counts_line(self.kept)}",
        ]


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _owned(table: Table, household_id: int) -> Any:
    """The household's rows in ``table``. Every table has ``household_id`` but ``household``."""
    column = table.c.id if table.name == "household" else table.c.household_id
    return column == household_id


def _clear_order() -> list[Table]:
    """``CLEAR`` tables, each before the tables it refers to (a vote before its proposal)."""
    return [table for table in reversed(Base.metadata.sorted_tables) if table.name in CLEAR]


async def _present(db: AsyncSession) -> set[str]:
    return set(await db.scalars(text("SELECT name FROM sqlite_master WHERE type = 'table'")))


async def _counts(db: AsyncSession, household_id: int, names: Collection[str], present: set[str]) -> dict[str, int]:
    """Rows per table, counting 0 for a table an older schema doesn't have yet."""
    counts: dict[str, int] = {}
    for name in sorted(names):
        found: int | None = 0
        if name in present:
            table = _table(name)
            found = await db.scalar(select(func.count()).select_from(table).where(_owned(table, household_id)))
        counts[name] = int(found or 0)
    return counts


async def _open_calls(db: AsyncSession, household_id: int, present: set[str]) -> tuple[OpenCall, ...]:
    if "event" not in present:
        return ()
    event = _table("event")
    rows = await db.execute(
        select(event.c.id, event.c.slot, event.c.date, event.c.state)
        .where(event.c.household_id == household_id, event.c.state.in_([s.value for s in OPEN_STATES]))
        .order_by(event.c.date, event.c.id)
    )
    return tuple(OpenCall(row.id, Slot(row.slot), row.date, EventState(row.state)) for row in rows)


async def _posts_again(
    db: AsyncSession, household_id: int, zone: str, settings: Mapping[str, Any], now: datetime, present: set[str]
) -> tuple[Slot, ...]:
    if "event" not in present:
        return ()
    tz = ZoneInfo(zone)
    today = now.astimezone(tz).date()
    event = _table("event")
    existing = set(
        await db.scalars(
            select(event.c.slot).where(
                event.c.household_id == household_id, event.c.kind == EventKind.CALL.value, event.c.date == today
            )
        )
    )
    again: list[Slot] = []
    for slot, schedule in slot_schedules(settings).items():
        opens_at = schedule.opens_at(today, tz)
        deadline = opens_at + schedule.response_window
        # The tick's own rule, as it will see the day once this call is gone.
        if slot.value in existing and should_open_today(
            enabled=schedule.enabled, opens_at=opens_at, respond_deadline=deadline, now=now, exists=False
        ):
            again.append(slot)
    return tuple(again)


async def preview(db: AsyncSession, *, now: datetime) -> ResetPreview | None:
    """What the reset would delete and keep, or None when there's no household. It only reads,
    and works on a read-only connection to a database that isn't migrated yet."""
    present = await _present(db)
    if "household" not in present:
        return None
    household = _table("household")
    row = (
        await db.execute(
            select(household.c.id, household.c.name, household.c.timezone, household.c.settings)
            .order_by(household.c.id)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    settings = row.settings if isinstance(row.settings, dict) else {}
    pending = 0
    if "outbox" in present:
        outbox = _table("outbox")
        waiting = outbox.c.delivered_at.is_(None) & outbox.c.failed_at.is_(None)
        pending = int(
            await db.scalar(select(func.count()).select_from(outbox).where(_owned(outbox, row.id), waiting)) or 0
        )
    return ResetPreview(
        household_name=str(row.name),
        clear=await _counts(db, row.id, CLEAR, present),
        keep=await _counts(db, row.id, KEEP, present),
        open_calls=await _open_calls(db, row.id, present),
        pending_outbox=pending,
        posts_again=await _posts_again(db, row.id, row.timezone, settings, now, present),
    )


async def reset_history(db: AsyncSession, *, backup: str, now: datetime) -> ResetSummary:
    """Delete the household's history: every row of every ``CLEAR`` table. ``backup`` names the
    backup the operator took first, for the audit row. Raises ResetRefused, changing nothing,
    when there's no household or a call is open. It holds the write lock throughout, so no call
    can open between the check and the delete."""
    async with write_transaction(db):
        first = _table("household").c.id
        household = await db.scalar(select(first).order_by(first).limit(1))
        if household is None:
            raise ResetRefused("The database has no household yet, so there's no history to reset.")
        present = await _present(db)
        calls = await _open_calls(db, household, present)
        if calls:
            raise ResetRefused(refusal(calls))
        deleted: dict[str, int] = {}
        for table in _clear_order():
            result = await db.execute(delete(table).where(_owned(table, household)))
            deleted[table.name] = int(getattr(result, "rowcount", 0) or 0)
        deleted = dict(sorted(deleted.items()))
        kept = await _counts(db, household, KEEP, present)
        payload = {"rows": deleted, "backup": backup}
        await audit(db, household, None, "history.reset", f"household:{household}", payload, now)
        await db.commit()
    return ResetSummary(deleted=deleted, kept=kept, backup=backup)
