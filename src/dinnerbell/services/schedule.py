# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Schedule: slot schedules, ranking weights, and the variety window (FR-1, FR-43).

In M2 only the admin edits these; FR-44 (any member edits the standing schedule) is M3.
Changes apply from the next call: an open call keeps the windows it opened with.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.schedule import (
    PROPOSAL_COUNT_KEY,
    SLOTS_KEY,
    WEIGHTS_KEY,
    InvalidSchedule,
    SlotSchedule,
    Weights,
    check_schedule,
    check_weights,
    proposal_count,
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
from dinnerbell.services.common import audit, require_admin

ADMINS_ONLY = "Only the household admin can change the schedule."


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    slots: dict[Slot, SlotSchedule]
    weights: Weights
    proposal_count: int
    variety_window_days: int


async def schedule_settings(db: AsyncSession, actor: Member) -> ScheduleSettings:
    require_admin(actor, ADMINS_ONLY)
    stored = await household_settings(db, actor.household_id)
    return ScheduleSettings(
        slots=slot_schedules(stored),
        weights=weights(stored),
        proposal_count=proposal_count(stored),
        variety_window_days=variety_window_days(stored),
    )


def _to_json(values: ScheduleSettings) -> dict[str, Any]:
    return {
        SLOTS_KEY: {slot.value: slot_to_json(schedule) for slot, schedule in values.slots.items()},
        WEIGHTS_KEY: weights_to_json(values.weights),
        PROPOSAL_COUNT_KEY: values.proposal_count,
        SETTINGS_KEY: values.variety_window_days,
    }


async def update_schedule(db: AsyncSession, actor: Member, values: ScheduleSettings, *, now: datetime) -> None:
    """Validate and store every schedule setting, keeping the household's other settings."""
    require_admin(actor, ADMINS_ONLY)
    for schedule in values.slots.values():
        check_schedule(schedule)
    check_weights(values.weights, values.proposal_count)
    if not MIN_VARIETY_WINDOW_DAYS <= values.variety_window_days <= MAX_VARIETY_WINDOW_DAYS:
        raise InvalidSchedule(f"The variety window runs {MIN_VARIETY_WINDOW_DAYS} to {MAX_VARIETY_WINDOW_DAYS} days.")
    stored = await household_settings(db, actor.household_id)
    before = _to_json(await schedule_settings(db, actor))
    after = _to_json(values)
    changed = {key: {"from": before[key], "to": value} for key, value in after.items() if before[key] != value}
    if not changed:
        return
    await HouseholdRepository(db).update_settings(actor.household_id, stored | after)
    await audit(
        db, actor.household_id, actor, "household.schedule_updated", f"household:{actor.household_id}", changed, now
    )
    await db.commit()
