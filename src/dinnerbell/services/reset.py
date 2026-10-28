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
records where each message landed, is cleared, and a tap on an old button finds no call. Event and
meal IDs are never reused (``event`` and ``meal_log`` are AUTOINCREMENT), so an old button, link,
or open form can't reach a new call or meal.
App Home is drawn from the database each time it opens, so it shows no stale calls. The scheduler
keeps no job per call (ADR 0004); its next tick opens calls as usual.

A day an admin skipped (``calls.skip_day``), or a call called off for today or a later day, is a
cancelled call, and that row is what keeps the scheduler from opening the day's call. The reset
deletes it with the rest, then adds a fresh cancelled call for the same slot and day: a new ID,
no note, and no link to who called it off. Those days stay off, and the audit row lists them.

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
from dinnerbell.db.events import EventRepository
from dinnerbell.db.models import Base
from dinnerbell.domain.events import OPEN_STATES, EventKind, EventState, should_open_today
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.schedule import slot_schedules
from dinnerbell.services.common import audit

KEEP: Final = frozenset(
    {
        "household",  # its name, time zone, and settings: the schedule, ranking weights, variety window
        "member",  # members, roles, what each can do, and Slack links; guests and their dietary needs too
        "session",  # who's signed in
        "audit",  # the audit log, which records the reset itself
        "tag",  # the catalog ...
        "catalog_item",
        "catalog_item_tag",
        "catalog_item_cook",
        "preference",  # likes, dislikes, and vetoes
        "slack_badge_file",  # the badge art uploaded to Slack: not history, and reusable (#105)
        "slack_badge_composite",  # members' badge pictures: a cache Home checks by hash (#114)
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
        "invitation",  # which guests were invited to which call (#38)
        "meal_log",  # what the household ate
        "feedback",  # and how it was
        "outbox",  # notifications, sent and unsent, and where each Slack message landed
        "contribution",  # what each member did for those meals (M4) ...
        "badge_award",  # ... and the badges it earned
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
class DayOff:
    """A slot and day, today or later, whose call is cancelled: skipped, or called off."""

    slot: Slot
    day: date

    def label(self) -> str:
        return f"{self.slot.value} on {self.day.isoformat()}"


def _days_off_line(days: Collection[DayOff]) -> list[str]:
    return [f"Days off kept: {', '.join(day.label() for day in days)}"] if days else []


@dataclass(frozen=True, slots=True)
class ResetPreview:
    """What a reset would delete and keep, by table, for the operator to check first."""

    household_name: str
    clear: dict[str, int]
    keep: dict[str, int]
    open_calls: tuple[OpenCall, ...]
    pending_outbox: int
    posts_again: tuple[Slot, ...]
    """Today's finished calls whose response window is still open: once they're deleted, the
    scheduler's next tick posts them again, as if they had never been."""
    days_off: tuple[DayOff, ...] = ()
    """Skipped and called-off days, today or later, that stay off."""

    def lines(self) -> list[str]:
        lines = [
            f"Household: {self.household_name}",
            f"Deletes: {_counts_line(self.clear)}",
            f"Keeps: {_counts_line(self.keep)}",
            f"Notifications not yet sent: {self.pending_outbox}",
            *_days_off_line(self.days_off),
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
    days_off: tuple[DayOff, ...] = ()

    def lines(self) -> list[str]:
        return [
            f"Backup: {self.backup}",
            f"Deleted: {_counts_line(self.deleted)}",
            f"Kept: {_counts_line(self.kept)}",
            *_days_off_line(self.days_off),
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


async def _days_off(db: AsyncSession, household_id: int, today: date, present: set[str]) -> tuple[DayOff, ...]:
    if "event" not in present:
        return ()
    event = _table("event")
    rows = await db.execute(
        select(event.c.slot, event.c.date)
        .where(
            event.c.household_id == household_id,
            event.c.kind == EventKind.CALL.value,
            event.c.state == EventState.CANCELLED.value,
            event.c.date >= today,
        )
        .order_by(event.c.date, event.c.slot)
    )
    return tuple(DayOff(Slot(row.slot), row.date) for row in rows)


async def _posts_again(
    db: AsyncSession, household_id: int, zone: str, settings: Mapping[str, Any], now: datetime, present: set[str]
) -> tuple[Slot, ...]:
    """Today's calls the tick would open again once they're deleted. A cancelled one is kept
    off (``DayOff``), and an open one refuses the reset, so these are finished calls."""
    if "event" not in present:
        return ()
    tz = ZoneInfo(zone)
    today = now.astimezone(tz).date()
    event = _table("event")
    existing = set(
        await db.scalars(
            select(event.c.slot).where(
                event.c.household_id == household_id,
                event.c.kind == EventKind.CALL.value,
                event.c.date == today,
                event.c.state != EventState.CANCELLED.value,
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
        days_off=await _days_off(db, row.id, now.astimezone(ZoneInfo(row.timezone)).date(), present),
    )


async def reset_history(db: AsyncSession, *, backup: str, now: datetime) -> ResetSummary:
    """Delete the household's history: every row of every ``CLEAR`` table. ``backup`` names the
    backup the operator took first, for the audit row. Raises ResetRefused, changing nothing,
    when there's no household or a call is open. It holds the write lock throughout, so no call
    can open between the check and the delete, and no day off goes missing in between."""
    async with write_transaction(db):
        homes = _table("household")
        found = (
            await db.execute(select(homes.c.id, homes.c.timezone, homes.c.settings).order_by(homes.c.id).limit(1))
        ).first()
        if found is None:
            raise ResetRefused("The database has no household yet, so there's no history to reset.")
        household, zone = int(found.id), ZoneInfo(found.timezone)
        present = await _present(db)
        calls = await _open_calls(db, household, present)
        if calls:
            raise ResetRefused(refusal(calls))
        days_off = await _days_off(db, household, now.astimezone(zone).date(), present)
        deleted: dict[str, int] = {}
        for table in _clear_order():
            result = await db.execute(delete(table).where(_owned(table, household)))
            deleted[table.name] = int(getattr(result, "rowcount", 0) or 0)
        deleted = dict(sorted(deleted.items()))
        await _keep_off(db, household, zone, found.settings, days_off, now)
        kept = await _counts(db, household, KEEP, present)
        payload = {
            "rows": deleted,
            "days_off_kept": [{"slot": d.slot.value, "date": d.day.isoformat()} for d in days_off],
            "backup": backup,
        }
        await audit(db, household, None, "history.reset", f"household:{household}", payload, now)
        await db.commit()
    return ResetSummary(deleted=deleted, kept=kept, backup=backup, days_off=days_off)


async def _keep_off(
    db: AsyncSession, household_id: int, zone: ZoneInfo, settings: Any, days: Collection[DayOff], now: datetime
) -> None:
    """Add a fresh cancelled call for each day off, as ``calls.skip_day`` does, with the slot's
    times as the schedule has them now. Nobody is named as having called it off."""
    schedules = slot_schedules(settings if isinstance(settings, dict) else {})
    events = EventRepository(db)
    for day in days:
        schedule = schedules[day.slot]
        opens_at = schedule.opens_at(day.day, zone)
        await events.add(
            household_id=household_id,
            slot=day.slot,
            day=day.day,
            state=EventState.CANCELLED,
            opens_at=opens_at,
            respond_deadline=opens_at + schedule.response_window,
            reminder_minutes=schedule.reminder_minutes,
            vote_minutes=schedule.vote_minutes,
            created_by=None,
            now=now,
        )
