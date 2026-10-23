# SPDX-License-Identifier: AGPL-3.0-or-later
"""``dinnerbell scrub-test``: make a restored production backup safe to run as test (ADR 0006).

Run it with test stopped, after restoring the backup. In one transaction it:

- unlinks the Slack user of, deactivates, and signs out every member who isn't kept, and locks
  the empty link so the hourly email match doesn't link them again;
- turns off every slot's standing schedule, so test opens no calls by itself;
- ends every open event without a word: calls that haven't been decided are abandoned as
  ``missed``, the quiet ending, and decided ones are cancelled, since a decided call can't be
  abandoned and finishing it would write to the meal log;
- deletes the outbox, pending rows and the message refs of delivered posts alike, so test never
  sends production's queue or edits production's messages.

It never touches the meal log, the catalog, preferences, or the history of past events. Running
it again changes nothing but writes another audit row.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import EventRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository, SessionRepository
from dinnerbell.db.types import UTCDateTime
from dinnerbell.domain.events import OPEN_STATES, EndReason, EventState, transition
from dinnerbell.domain.members import InvalidMemberInput, normalize_email
from dinnerbell.domain.schedule import SLOTS_KEY, slot_schedules, slot_to_json
from dinnerbell.services.common import audit
from dinnerbell.services.household import current_household

NOT_LABELED = (
    "scrub-test runs only on a labeled instance. Set DINNERBELL_INSTANCE_LABEL (such as TEST) in the test "
    "instance's env file; never run it against production. Nothing changed."
)


@dataclass(frozen=True, slots=True)
class ScrubPreview:
    """What a scrub would act on, for the operator to check before confirming. ``last_activity``
    is the newest audit row: minutes ago suggests a live database, not a restored backup."""

    household_name: str
    active_members: int
    enabled_slots: int
    open_events: int
    pending_outbox: int
    last_activity: datetime | None

    def lines(self, now: datetime) -> list[str]:
        return [
            f"Household: {self.household_name}",
            f"Last activity: {_when(self.last_activity, now)}",
            f"Active members: {self.active_members}",
            f"Enabled slots: {self.enabled_slots}",
            f"Open events: {self.open_events}",
            f"Notifications not yet sent: {self.pending_outbox}",
        ]


def _when(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "none recorded"
    minutes = max(0, int((now - at).total_seconds() // 60))
    if minutes < 120:
        ago = f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    elif minutes < 48 * 60:
        ago = f"{minutes // 60} hours ago"
    else:
        ago = f"{minutes // (24 * 60)} days ago"
    return f"{at:%Y-%m-%d %H:%M} UTC ({ago})"


async def preview(db: AsyncSession) -> ScrubPreview | None:
    """What the scrub would act on, or None when there's no household. It reads with plain SQL,
    and counts 0 for tables a schema doesn't have yet (``event`` and ``outbox`` arrive in M2), so
    it works on a restored backup of any version before migrating."""
    tables = set(await db.scalars(text("SELECT name FROM sqlite_master WHERE type = 'table'")))
    if "household" not in tables:
        return None
    household = (await db.execute(text("SELECT id, name, settings FROM household ORDER BY id LIMIT 1"))).first()
    if household is None:
        return None
    household_id, name, raw = household
    stored = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    slots = slot_schedules(stored if isinstance(stored, dict) else {})

    async def count(table: str, sql: TextClause, **params: object) -> int:
        if table not in tables:
            return 0
        return int(await db.scalar(sql, {"household": household_id, **params}) or 0)

    open_events = text("SELECT count(*) FROM event WHERE household_id = :household AND state IN :states")
    newest = text("SELECT max(created_at) AS at FROM audit WHERE household_id = :household").columns(at=UTCDateTime())
    return ScrubPreview(
        household_name=str(name),
        active_members=await count(
            "member", text("SELECT count(*) FROM member WHERE household_id = :household AND active")
        ),
        enabled_slots=sum(1 for schedule in slots.values() if schedule.enabled),
        open_events=await count(
            "event",
            open_events.bindparams(bindparam("states", expanding=True)),
            states=[s.value for s in OPEN_STATES],
        ),
        pending_outbox=await count(
            "outbox",
            text(
                "SELECT count(*) FROM outbox WHERE household_id = :household "
                "AND delivered_at IS NULL AND failed_at IS NULL"
            ),
        ),
        last_activity=await db.scalar(newest, {"household": household_id}) if "audit" in tables else None,
    )


class ScrubRefused(Exception):
    """The scrub didn't run. The message says why and never includes an email."""


@dataclass(frozen=True, slots=True)
class ScrubSummary:
    members_kept: int
    members_deactivated: int
    slack_links_removed: int
    slots_turned_off: int
    events_abandoned: int
    events_cancelled: int
    outbox_pending_deleted: int
    outbox_rows_deleted: int

    def lines(self) -> list[str]:
        return [
            f"Members kept: {self.members_kept}",
            f"Members deactivated: {self.members_deactivated}",
            f"Slack links removed: {self.slack_links_removed}",
            f"Slots turned off: {self.slots_turned_off}",
            f"Open calls abandoned: {self.events_abandoned}",
            f"Decided calls cancelled: {self.events_cancelled}",
            f"Outbox rows deleted: {self.outbox_rows_deleted} ({self.outbox_pending_deleted} not yet sent)",
        ]


async def scrub_test(db: AsyncSession, *, instance_label: str, keep: Iterable[str], now: datetime) -> ScrubSummary:
    """Scrub the household for test. Raises ScrubRefused, changing nothing, when the instance has
    no label, a kept email isn't a valid address or matches no member, or no kept member is an
    active admin (nobody could run test's admin pages)."""
    if not instance_label:
        raise ScrubRefused(NOT_LABELED)
    wanted: set[str] = set()
    for number, raw in enumerate(keep, start=1):
        try:
            wanted.add(normalize_email(raw))
        except InvalidMemberInput:
            raise ScrubRefused(f"--keep number {number} isn't a valid email address. Nothing changed.") from None
    if not wanted:
        raise ScrubRefused("Give at least one --keep email. Nothing changed.")

    try:
        household = await current_household(db)
    except LookupError:
        raise ScrubRefused("The database has no household yet, so there's nothing to scrub.") from None
    members_repo = MemberRepository(db)
    members = await members_repo.list(household.id)
    by_email = {m.email: m for m in members}
    for number, raw in enumerate(keep, start=1):
        if normalize_email(raw) not in by_email:
            raise ScrubRefused(f"No member has the email given as --keep number {number}. Nothing changed.")
    kept = [by_email[email] for email in sorted(wanted)]
    if not any(m.active and m.is_admin for m in kept):
        raise ScrubRefused("Keep at least one active admin, or nobody can manage test. Nothing changed.")

    kept_ids = {m.id for m in kept}
    deactivated = unlinked = 0
    sessions = SessionRepository(db)
    for member in members:
        if member.id in kept_ids:
            continue
        changes: dict[str, object] = {}
        if member.active:
            changes["active"] = False
            deactivated += 1
        if member.slack_user_id is not None:
            changes["slack_user_id"] = None
            unlinked += 1
        if not member.slack_link_locked:
            changes["slack_link_locked"] = True
        if changes:
            await members_repo.update(household.id, member.id, now, **changes)
        await sessions.delete_for_member(member.id)

    stored = await household_settings(db, household.id)
    slots = slot_schedules(stored)
    turned_off = sum(1 for schedule in slots.values() if schedule.enabled)
    if turned_off:
        off = {slot.value: slot_to_json(replace(schedule, enabled=False)) for slot, schedule in slots.items()}
        await HouseholdRepository(db).update_settings(household.id, stored | {SLOTS_KEY: off})

    events = EventRepository(db)
    abandoned = cancelled = 0
    for event_id in await events.open_ids(household.id):
        event = await events.get(household.id, event_id)
        if event is None:  # pragma: no cover - listed in this transaction
            continue
        if event.state is EventState.DECIDED:
            ended = replace(event, state=transition(event.state, EventState.CANCELLED))
            action = "event.cancelled"
            cancelled += 1
        else:
            ended = replace(event, state=transition(event.state, EventState.ABANDONED), end_reason=EndReason.MISSED)
            action = "event.abandoned"
            abandoned += 1
        await events.save(ended, now)
        payload = {"reason": "scrub", "from": event.state.value}
        await audit(db, household.id, None, action, f"event:{event.id}", payload, now)

    pending, rows = await OutboxRepository(db).clear(household.id)

    summary = ScrubSummary(
        members_kept=len(kept),
        members_deactivated=deactivated,
        slack_links_removed=unlinked,
        slots_turned_off=turned_off,
        events_abandoned=abandoned,
        events_cancelled=cancelled,
        outbox_pending_deleted=pending,
        outbox_rows_deleted=rows,
    )
    summary_payload = {"kept_member_ids": sorted(kept_ids), "instance_label": instance_label} | asdict(summary)
    target = f"household:{household.id}"
    await audit(db, household.id, None, "instance.scrubbed", target, summary_payload, now)
    await db.commit()
    return summary
