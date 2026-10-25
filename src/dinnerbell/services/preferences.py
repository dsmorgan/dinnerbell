# SPDX-License-Identifier: AGPL-3.0-or-later
"""A member's own preferences and settings (FR-27 to FR-29).

Every function here that returns stances acts on ``actor`` and takes no other member's ID.
That is the privacy rule: no one, admins included, can read or write another member's
preferences. Preferences are deliberately not audited, because the audit log is visible to the
admin.

The one exception, ``group_preferences``, is for ranking. It returns the combined vetoes and
summed scores of a group, never a breakdown by member, so its result can't say who vetoed what.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.preferences import PreferenceRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import CatalogItem, Tag
from dinnerbell.domain.members import Capabilities, Member
from dinnerbell.domain.preferences import NEUTRAL, Stance, TargetType
from dinnerbell.domain.ranking import GroupPreferences
from dinnerbell.services.common import is_unique_violation, require_member
from dinnerbell.services.errors import NotFound


@dataclass(frozen=True, slots=True)
class MyPreferences:
    tags: list[Tag]
    items: list[CatalogItem]
    stances: dict[tuple[TargetType, int], Stance]

    def stance(self, target_type: TargetType, target_id: int) -> Stance:
        return self.stances.get((target_type, target_id), NEUTRAL)

    def vetoes(self) -> list[tuple[TargetType, int, str]]:
        """Vetoed tags, then vetoed items, as (type, ID, name), each sorted by name."""
        tags = [(TargetType.TAG, t.id, t.name) for t in self.tags if self.stance(TargetType.TAG, t.id).veto]
        items = [(TargetType.ITEM, i.id, i.name) for i in self.items if self.stance(TargetType.ITEM, i.id).veto]
        return sorted(tags, key=lambda v: v[2].casefold()) + sorted(items, key=lambda v: v[2].casefold())


@dataclass(frozen=True, slots=True)
class MySettings:
    capabilities: Capabilities
    mute_dm_reminders: bool
    feedback_reminder: bool


async def my_preferences(db: AsyncSession, actor: Member) -> MyPreferences:
    """The actor's stances, with every tag and active item they can rate."""
    require_member(actor)
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
    require_member(actor)
    repo = CatalogRepository(db)
    target = (
        await repo.tag(actor.household_id, target_id)
        if target_type is TargetType.TAG
        else await repo.item(actor.household_id, target_id)
    )
    if target is None:
        raise NotFound(f"No such {target_type.value}.")
    for attempt in range(2):
        try:
            await PreferenceRepository(db).set(actor.household_id, actor.id, target_type, target_id, stance, now)
            await db.commit()
            break
        except IntegrityError as exc:
            # A double tap can race to insert the same row; the retry finds it and updates it.
            await db.rollback()
            if attempt or not is_unique_violation(exc):
                raise
    return target.name


async def update_my_settings(db: AsyncSession, actor: Member, settings: MySettings, *, now: datetime) -> Member:
    require_member(actor)
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


async def group_preferences(db: AsyncSession, household_id: int, member_ids: frozenset[int]) -> GroupPreferences:
    """Everyone in ``member_ids`` at once: the union of their vetoes and the sum of their scores."""
    combined = await PreferenceRepository(db).group_preferences(household_id, member_ids)
    return GroupPreferences(
        vetoed_tags=combined.vetoed_tags,
        vetoed_items=combined.vetoed_items,
        tag_scores=combined.tag_scores,
        item_scores=combined.item_scores,
    )
