# SPDX-License-Identifier: AGPL-3.0-or-later
"""A member's own preferences and settings (FR-27 to FR-29).

Every function here acts on ``actor`` and takes no other member's ID. That is the privacy
rule: no one, admins included, can read or write another member's preferences. Preferences
are deliberately not audited, because the audit log is visible to the admin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.preferences import PreferenceRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import CatalogItem, Tag
from dinnerbell.domain.members import Capabilities, Member
from dinnerbell.domain.preferences import NEUTRAL, Stance, TargetType
from dinnerbell.services.errors import NotFound


@dataclass(frozen=True, slots=True)
class MyPreferences:
    tags: list[Tag]
    items: list[CatalogItem]
    stances: dict[tuple[TargetType, int], Stance]

    def stance(self, target_type: TargetType, target_id: int) -> Stance:
        return self.stances.get((target_type, target_id), NEUTRAL)


@dataclass(frozen=True, slots=True)
class MySettings:
    capabilities: Capabilities
    mute_dm_reminders: bool
    feedback_reminder: bool


async def my_preferences(db: AsyncSession, actor: Member) -> MyPreferences:
    """The actor's stances, with every tag and active item they can rate."""
    repo = CatalogRepository(db)
    stances = {
        (p.target_type, p.target_id): p.stance
        for p in await PreferenceRepository(db).for_member(actor.household_id, actor.id)
    }
    items = [
        item for item in await repo.items(actor.household_id) if item.active or (TargetType.ITEM, item.id) in stances
    ]
    return MyPreferences(tags=await repo.tags(actor.household_id), items=items, stances=stances)


async def my_stance(
    db: AsyncSession, actor: Member, target_type: TargetType, target_id: int
) -> tuple[str, Stance] | None:
    """The actor's stored stance on one tag or item, with its name, or None if it's gone."""
    repo = CatalogRepository(db)
    target = (
        await repo.tag(actor.household_id, target_id)
        if target_type is TargetType.TAG
        else await repo.item(actor.household_id, target_id)
    )
    if target is None:
        return None
    stances = {
        (p.target_type, p.target_id): p.stance
        for p in await PreferenceRepository(db).for_member(actor.household_id, actor.id)
    }
    return target.name, stances.get((target_type, target_id), NEUTRAL)


async def set_my_preference(
    db: AsyncSession, actor: Member, target_type: TargetType, target_id: int, stance: Stance, *, now: datetime
) -> str:
    """Store the actor's stance on a tag or item and return the target's name."""
    repo = CatalogRepository(db)
    target = (
        await repo.tag(actor.household_id, target_id)
        if target_type is TargetType.TAG
        else await repo.item(actor.household_id, target_id)
    )
    if target is None:
        raise NotFound(f"No such {target_type.value}.")
    await PreferenceRepository(db).set(actor.household_id, actor.id, target_type, target_id, stance, now)
    await db.commit()
    return target.name


async def update_my_settings(db: AsyncSession, actor: Member, settings: MySettings, *, now: datetime) -> Member:
    updated = await MemberRepository(db).update(
        actor.household_id,
        actor.id,
        now,
        can_cook=settings.capabilities.can_cook,
        can_pickup=settings.capabilities.can_pickup,
        can_order=settings.capabilities.can_order,
        mute_dm_reminders=settings.mute_dm_reminders,
        feedback_reminder=settings.feedback_reminder,
    )
    await db.commit()
    return updated
