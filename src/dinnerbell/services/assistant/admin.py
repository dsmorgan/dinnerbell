# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Assistant (ADR 0009): the switch, the monthly cap, Conversation hours, and the
month's usage. Admins only, and only while the operator has configured the assistant. Every
change is audited (SEC-9). The assistant's tools never call these."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.assistant import (
    AssistantSettings,
    assistant_settings,
    check_assistant_settings,
    month_key,
    settings_to_json,
)
from dinnerbell.domain.members import Member
from dinnerbell.services.assistant.runtime import AssistantConfig
from dinnerbell.services.common import audit, require_admin
from dinnerbell.services.errors import NotFound

ADMINS_ONLY = "Only the household admin can change the assistant's settings."
NOT_CONFIGURED = "The assistant isn't set up on this server."
FORMER_MEMBER = "A former member"


@dataclass(frozen=True, slots=True)
class MemberUsage:
    name: str
    micros: int


@dataclass(frozen=True, slots=True)
class AssistantAdmin:
    """What Admin > Assistant shows."""

    settings: AssistantSettings
    month: str
    """The calendar month in the household's time zone, such as ``2026-11``."""
    spent_micros: int
    """Spent and reserved this month: what counts toward the cap."""
    by_member: tuple[MemberUsage, ...]


def _require(config: AssistantConfig, actor: Member) -> None:
    require_admin(actor, ADMINS_ONLY)
    if not config.configured:
        raise NotFound(NOT_CONFIGURED)


async def assistant_admin(db: AsyncSession, config: AssistantConfig, actor: Member, *, now: datetime) -> AssistantAdmin:
    _require(config, actor)
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    month = month_key(now, ZoneInfo(household.timezone))
    repo = AssistantRepository(db)
    names = {m.id: m.display_name for m in await MemberRepository(db).list(household.id)}
    return AssistantAdmin(
        settings=assistant_settings(await household_settings(db, household.id)),
        month=month,
        spent_micros=await repo.month_total(household.id, month),
        by_member=tuple(
            MemberUsage(names.get(member_id, FORMER_MEMBER) if member_id is not None else FORMER_MEMBER, micros)
            for member_id, micros in await repo.by_member(household.id, month)
        ),
    )


async def update_assistant(
    db: AsyncSession, config: AssistantConfig, actor: Member, values: AssistantSettings, *, now: datetime
) -> bool:
    """Save Admin > Assistant, keeping the household's other settings. Audited as
    ``household.assistant_updated`` with each change's old and new value. Lowering Conversation
    hours deletes the conversations already past the new limit at once (spec 6.14, **History**).
    Returns whether anything changed."""
    _require(config, actor)
    check_assistant_settings(values)
    async with write_transaction(db):
        stored = await household_settings(db, actor.household_id)
        before = settings_to_json(assistant_settings(stored))
        after = settings_to_json(values)
        changed = {key: {"from": before[key], "to": value} for key, value in after.items() if before[key] != value}
        if not changed:
            return False
        await HouseholdRepository(db).update_settings(actor.household_id, stored | after)
        await delete_expired(db, actor.household_id, values.conversation_hours, now)
        await audit(
            db,
            actor.household_id,
            actor,
            "household.assistant_updated",
            f"household:{actor.household_id}",
            changed,
            now,
        )
        await db.commit()
    return True


def expiry_cutoff(hours: int, now: datetime) -> datetime | None:
    """The last-message time at or before which a conversation is past ``hours``; None at 0,
    when every conversation is."""
    return None if hours == 0 else now - timedelta(hours=hours)


async def delete_expired(db: AsyncSession, household_id: int, hours: int, now: datetime) -> int:
    """Delete the conversations whose last message is ``hours`` or more ago (all of them at 0),
    in the caller's transaction."""
    return await AssistantRepository(db).delete_idle(household_id, expiry_cutoff(hours, now))
