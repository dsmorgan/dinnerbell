# SPDX-License-Identifier: AGPL-3.0-or-later
"""A member's own preferences and settings (FR-27 to FR-29).

Every function here that returns stances acts on ``actor`` and takes no other member's ID.
That is the privacy rule: no one, admins included, can read or write another member's
preferences. Preferences are deliberately not audited, because the audit log is visible to the
admin.

The exceptions, ``group_preferences`` and ``with_ratings``, are for ranking. They return the
combined vetoes and summed scores of a group, never a breakdown by member, so their result
can't say who vetoed what. ``with_ratings`` adds what each member's ratings did to their own
item scores (FR-55, ADR 0008).

Setting a stance on an item records when, so ratings the member saved before it stop moving
their score: the stance they set wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.preferences import PreferenceRepository
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.catalog import CatalogItem, Tag
from dinnerbell.domain.members import Capabilities, Member
from dinnerbell.domain.preferences import NEUTRAL, Stance, TargetType
from dinnerbell.domain.ranking import GroupPreferences
from dinnerbell.domain.ratings import Opinion, adjustment, combined_scores, effective, has_ratings, ratings_note
from dinnerbell.domain.schedule import feedback_cap
from dinnerbell.services.common import is_unique_violation, require_member
from dinnerbell.services.errors import NotFound


@dataclass(frozen=True, slots=True)
class MyPreferences:
    tags: list[Tag]
    items: list[CatalogItem]
    stances: dict[tuple[TargetType, int], Stance]
    rating_notes: dict[int, str] = field(default_factory=dict)
    """Item ID to what the member's own ratings since their last edit make their score, such as
    "From your ratings: about +1" (FR-55). Only they see it."""

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


async def my_preferences(db: AsyncSession, actor: Member, *, today: date) -> MyPreferences:
    """The actor's stances, with every tag and active item they can rate, and what their own
    ratings make their score for each item, as of ``today`` in the household."""
    require_member(actor)
    repo = CatalogRepository(db)
    stances = {
        (p.target_type, p.target_id): p.stance
        for p in await PreferenceRepository(db).for_member(actor.household_id, actor.id)
    }
    items = [
        item for item in await repo.items(actor.household_id) if item.active or (TargetType.ITEM, item.id) in stances
    ]
    notes = await _rating_notes(db, actor, stances, today=today)
    return MyPreferences(tags=await repo.tags(actor.household_id), items=items, stances=stances, rating_notes=notes)


async def _rating_notes(
    db: AsyncSession, actor: Member, stances: dict[tuple[TargetType, int], Stance], *, today: date
) -> dict[int, str]:
    """``MyPreferences.rating_notes``: nothing while the Feedback setting is off, since ratings
    then move no score, and nothing for a veto."""
    cap = feedback_cap(await household_settings(db, actor.household_id))
    if cap == 0:
        return {}
    edits = await PreferenceRepository(db).item_edits(actor.household_id, [actor.id])
    by_item: dict[int, list[Opinion]] = {}
    for opinion in await FeedbackRepository(db).opinions(actor.household_id, member_ids=[actor.id]):
        by_item.setdefault(opinion.item_id, []).append(opinion)
    notes: dict[int, str] = {}
    for item_id, theirs in by_item.items():
        edited = edits.get((actor.id, item_id))
        stance = stances.get((TargetType.ITEM, item_id))
        if (stance is not None and stance.veto) or not has_ratings(theirs, today=today, edited_at=edited):
            continue
        stated = stance.score if stance is not None else None
        score = effective(stated or 0, adjustment(theirs, today=today, cap=cap, edited_at=edited))
        note = ratings_note(stated, score)
        if note is not None:
            notes[item_id] = note
    return notes


async def my_stance(
    db: AsyncSession, actor: Member, target_type: TargetType, target_id: int
) -> tuple[str, Stance] | None:
    """The actor's stored stance on one tag or item, with its name, or None if it's gone."""
    require_member(actor)
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


async def with_ratings(
    db: AsyncSession, household_id: int, member_ids: frozenset[int], group: GroupPreferences, *, day: date, cap: int
) -> GroupPreferences:
    """``group``, the ``group_preferences`` of ``member_ids``, with each member's item scores
    moved by their own ratings as of ``day`` (FR-55). ``cap`` is the household's Feedback
    setting (``domain.schedule.feedback_cap``). Vetoes and tag scores stay as they are. At
    Feedback 0 this is ``group`` unchanged."""
    if cap == 0 or not member_ids:
        return group
    opinions = await FeedbackRepository(db).opinions(household_id, member_ids=member_ids)
    if not any(o.rating is not None for o in opinions):
        return group
    preferences = PreferenceRepository(db)
    stated = await preferences.item_scores(household_id, member_ids)
    edits = await preferences.item_edits(household_id, member_ids)
    scores = combined_scores(stated, opinions, edits, today=day, cap=cap)
    return replace(group, item_scores=scores)


async def group_preferences(db: AsyncSession, household_id: int, member_ids: frozenset[int]) -> GroupPreferences:
    """Everyone in ``member_ids`` at once: the union of their vetoes and the sum of their scores."""
    combined = await PreferenceRepository(db).group_preferences(household_id, member_ids)
    return GroupPreferences(
        vetoed_tags=combined.vetoed_tags,
        vetoed_items=combined.vetoed_items,
        tag_scores=combined.tag_scores,
        item_scores=combined.item_scores,
    )
