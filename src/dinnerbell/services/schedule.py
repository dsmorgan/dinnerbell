# SPDX-License-Identifier: AGPL-3.0-or-later
"""The schedule: slot schedules any member edits (FR-1, FR-44), and the ranking settings only
the admin edits (FR-43).

A change applies from the next call. A call that's open, or already scheduled for a later day,
keeps the times it was created with: each call stores its own windows.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import EventRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.outbox import OutboxRepository
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.catalog import sorted_modes
from dinnerbell.domain.events import AD_HOC_DAYS_AHEAD
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import PersonInfo, ScheduleChanged, SlotScheduleInfo
from dinnerbell.domain.schedule import (
    PROPOSAL_COUNT_KEY,
    SLOTS_KEY,
    WEIGHTS_KEY,
    InvalidSchedule,
    SlotSchedule,
    Weights,
    can_open_today,
    check_schedule,
    check_weights,
    first_day_using,
    format_hhmm,
    proposal_count,
    slot_changes,
    slot_fingerprint,
    slot_schedules,
    slot_to_json,
    weights,
    weights_to_json,
)
from dinnerbell.domain.variety import (
    MAX_VARIETY_WINDOW_DAYS,
    MIN_VARIETY_WINDOW_DAYS,
    SETTINGS_KEY,
    variety_window_days,
)
from dinnerbell.services import lifecycle
from dinnerbell.services.common import audit, require_admin
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.variety import household_today

ADMINS_ONLY = "Only the household admin can change the ranking settings."
MEMBERS_ONLY = "Only active members can change the schedule."
SCHEDULE_PATH = "/schedule"


class ScheduleMoved(InvalidSchedule):
    """Someone else saved the slot's schedule after the form was drawn. The message is safe to show."""


# Slot schedules: any member (FR-44) ---------------------------------------------------------------


def _require_member(actor: Member) -> None:
    if not actor.active:
        raise Forbidden(MEMBERS_ONLY)


async def slot_schedules_for(db: AsyncSession, actor: Member) -> dict[Slot, SlotSchedule]:
    """Every slot's standing schedule, for any active member to see."""
    _require_member(actor)
    return slot_schedules(await household_settings(db, actor.household_id))


def _info(schedule: SlotSchedule) -> SlotScheduleInfo:
    return SlotScheduleInfo(
        enabled=schedule.enabled,
        call_time=schedule.call_time,
        response_minutes=schedule.response_minutes,
        vote_minutes=schedule.vote_minutes,
        reminder_minutes=schedule.reminder_minutes,
        meal_time=schedule.meal_time,
        modes=tuple(sorted_modes(schedule.modes)),
    )


async def update_slot(
    db: AsyncSession, actor: Member, slot: Slot, schedule: SlotSchedule, *, seen: str, now: datetime
) -> bool:
    """FR-44: any active member changes ``slot``'s standing schedule. The change is audited
    (``household.slot_schedule_updated``, old and new values) and announced in the channel with
    the member's name. Returns whether anything changed: a save that changes nothing writes and
    announces nothing, even from a form drawn before someone else's change.

    ``seen`` is ``slot_fingerprint`` of the schedule the member was shown. When someone else has
    saved since, and this save would change something, ScheduleMoved is raised and nothing
    changes. The whole save holds the write lock (``write_transaction``), so two saves can't
    both read the old settings and each write over the other.

    The change applies from the next call. When today's call doesn't exist yet, today uses the
    new schedule if its call can still open today. If it can't, but the old schedule would
    still have opened one, today's call is scheduled now on the old schedule, so moving the
    call earlier never silently drops today's call. Turning the slot off applies today."""
    _require_member(actor)
    check_schedule(schedule)
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    async with write_transaction(db):
        stored = await household_settings(db, household.id)
        before = slot_schedules(stored)[slot]
        changes = slot_changes(before, schedule)
        if not changes:
            return False
        if seen != slot_fingerprint(before):
            raise ScheduleMoved(
                f"Someone changed the {slot.value} schedule while you were editing it. Here it is now; "
                "make your change again if you still want it."
            )
        zone = ZoneInfo(household.timezone)
        today = household_today(household, now)
        events = EventRepository(db)
        kept_today_at = None
        if (
            await events.call_for(household.id, slot, today) is None
            and schedule.enabled
            and can_open_today(before, today=today, now=now, zone=zone)
            and not can_open_today(schedule, today=today, now=now, zone=zone)
        ):
            # Settings still hold the old schedule, so the call gets the old time and windows.
            opens_at = before.opens_at(today, zone)
            await lifecycle.create_call(db, household, slot, today, opens_at=opens_at, actor=None, now=now)
            kept_today_at = before.call_time
        raw = stored.get(SLOTS_KEY)
        slots = dict(raw) if isinstance(raw, dict) else {}
        slots[slot.value] = slot_to_json(schedule)
        await HouseholdRepository(db).update_settings(household.id, stored | {SLOTS_KEY: slots})
        taken: set[date] = set()
        for ahead in range(AD_HOC_DAYS_AHEAD + 1):  # a call exists at most a week ahead
            day = today + timedelta(days=ahead)
            if await events.call_for(household.id, slot, day) is not None:
                taken.add(day)
        starts_on = first_day_using(schedule, today=today, now=now, zone=zone, taken=taken)
        change_id = secrets.token_hex(8)
        payload: dict[str, Any] = {
            "slot": slot.value,
            "changes": changes,
            "starts_on": starts_on.isoformat(),
            "change_id": change_id,
        }
        if kept_today_at is not None:
            payload["kept_today_at"] = format_hhmm(kept_today_at)
        target = f"household:{household.id}"
        await audit(db, household.id, actor, "household.slot_schedule_updated", target, payload, now)
        announcement = ScheduleChanged(
            household_id=household.id,
            change_id=change_id,
            slot=slot,
            changed_by=PersonInfo(actor.id, actor.display_name),
            before=_info(before),
            after=_info(schedule),
            starts_on=starts_on,
            path=SCHEDULE_PATH,
            kept_today_at=kept_today_at,
        )
        await OutboxRepository(db).enqueue(household.id, None, announcement, now)
        await db.commit()
    return True


# Ranking settings: the admin only (FR-43) ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RankingSettings:
    weights: Weights
    proposal_count: int
    variety_window_days: int


async def ranking_settings(db: AsyncSession, actor: Member) -> RankingSettings:
    require_admin(actor, ADMINS_ONLY)
    stored = await household_settings(db, actor.household_id)
    return RankingSettings(
        weights=weights(stored),
        proposal_count=proposal_count(stored),
        variety_window_days=variety_window_days(stored),
    )


def _to_json(values: RankingSettings) -> dict[str, Any]:
    return {
        WEIGHTS_KEY: weights_to_json(values.weights),
        PROPOSAL_COUNT_KEY: values.proposal_count,
        SETTINGS_KEY: values.variety_window_days,
    }


async def update_ranking(db: AsyncSession, actor: Member, values: RankingSettings, *, now: datetime) -> None:
    """Validate and store the ranking settings, keeping the household's other settings."""
    require_admin(actor, ADMINS_ONLY)
    check_weights(values.weights, values.proposal_count)
    if not MIN_VARIETY_WINDOW_DAYS <= values.variety_window_days <= MAX_VARIETY_WINDOW_DAYS:
        raise InvalidSchedule(f"The variety window runs {MIN_VARIETY_WINDOW_DAYS} to {MAX_VARIETY_WINDOW_DAYS} days.")
    async with write_transaction(db):  # read and write under one lock, so no save is lost
        stored = await household_settings(db, actor.household_id)
        before = _to_json(await ranking_settings(db, actor))
        after = _to_json(values)
        changed = {key: {"from": before[key], "to": value} for key, value in after.items() if before[key] != value}
        if not changed:
            return
        await HouseholdRepository(db).update_settings(actor.household_id, stored | after)
        target = f"household:{actor.household_id}"
        await audit(db, actor.household_id, actor, "household.ranking_updated", target, changed, now)
        await db.commit()
