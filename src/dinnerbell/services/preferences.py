# SPDX-License-Identifier: AGPL-3.0-or-later
"""A member's preferences and settings (FR-27 to FR-29).

The ``my_`` functions act on ``actor`` and take no other member's ID: no other member can read
or write someone's preferences. A member's own changes aren't audited, because the audit log is
visible to the admin. Dietary needs are the exception: ``guests.set_my_needs`` audits them as
``guest.needs_updated``, for members and guests alike.

The ``member_`` functions are the one exception (#181): an admin reads and changes another
member's preferences from Admin > Members. They take the member's ID, look it up in the admin's
own household, and write an audit row naming the admin as the actor and the member as the target
for every change. A save of settings, dietary needs, or fun mode that changes nothing writes no
row; a stance is always audited, because saving one, even the same level, resets what the
member's earlier ratings say (FR-55). The member isn't messaged.

The other exceptions, ``group_preferences`` and ``with_ratings``, are for ranking. They return the
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
from dinnerbell.services.common import audit, is_unique_violation, require_admin, require_member
from dinnerbell.services.errors import NotFound

EDIT_OTHERS = "Only admins can change another member's preferences."


@dataclass(frozen=True, slots=True)
class MyPreferences:
    tags: list[Tag]
    items: list[CatalogItem]
    stances: dict[tuple[TargetType, int], Stance]
    rating_notes: dict[int, str] = field(default_factory=dict)
    """Item ID to what the member's own ratings since their last edit make their score, such as
    "From your ratings: about +1" (FR-55). Only they, and an admin editing their preferences
    (#181), see it."""

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


async def member_for_admin(db: AsyncSession, actor: Member, member_id: int) -> Member:
    """The member whose preferences an admin opens on Admin > Members (#181): any member or admin
    of the actor's household, active or not, but never a guest. A guest has no Preferences page;
    an admin changes a guest's dietary needs on their row in the Guests section. Never the admin
    themselves either: their own are on Preferences, unaudited like anyone's."""
    require_admin(actor, EDIT_OTHERS)
    if member_id == actor.id:
        raise NotFound("That's you. Change your own on Preferences.")
    member = await MemberRepository(db).get(actor.household_id, member_id)
    if member is None or member.is_guest:
        raise NotFound("No such member.")
    return member


async def my_preferences(db: AsyncSession, actor: Member, *, today: date) -> MyPreferences:
    """The actor's stances, with every tag and active item they can rate, and what their own
    ratings make their score for each item, as of ``today`` in the household."""
    require_member(actor)
    return await _preferences(db, actor, today=today)


async def member_preferences(db: AsyncSession, actor: Member, member_id: int, *, today: date) -> MyPreferences:
    """``my_preferences`` of another member, for an admin (#181)."""
    return await _preferences(db, await member_for_admin(db, actor, member_id), today=today)


async def _stances(db: AsyncSession, member: Member) -> dict[tuple[TargetType, int], Stance]:
    return {
        (p.target_type, p.target_id): p.stance
        for p in await PreferenceRepository(db).for_member(member.household_id, member.id)
    }


async def _preferences(db: AsyncSession, member: Member, *, today: date) -> MyPreferences:
    repo = CatalogRepository(db)
    stances = await _stances(db, member)
    items = [
        item for item in await repo.items(member.household_id) if item.active or (TargetType.ITEM, item.id) in stances
    ]
    notes = await _rating_notes(db, member, stances, today=today)
    return MyPreferences(tags=await repo.tags(member.household_id), items=items, stances=stances, rating_notes=notes)


async def _rating_notes(
    db: AsyncSession, member: Member, stances: dict[tuple[TargetType, int], Stance], *, today: date
) -> dict[int, str]:
    """``MyPreferences.rating_notes``: nothing while the Feedback setting is off, since ratings
    then move no score, and nothing for a veto."""
    cap = feedback_cap(await household_settings(db, member.household_id))
    if cap == 0:
        return {}
    edits = await PreferenceRepository(db).item_edits(member.household_id, [member.id])
    by_item: dict[int, list[Opinion]] = {}
    for opinion in await FeedbackRepository(db).opinions(member.household_id, member_ids=[member.id]):
        by_item.setdefault(opinion.item_id, []).append(opinion)
    notes: dict[int, str] = {}
    for item_id, theirs in by_item.items():
        edited = edits.get((member.id, item_id))
        stance = stances.get((TargetType.ITEM, item_id))
        if (stance is not None and stance.veto) or not has_ratings(theirs, today=today, edited_at=edited):
            continue
        stated = stance.score if stance is not None else None
        score = effective(stated or 0, adjustment(theirs, today=today, cap=cap, edited_at=edited))
        note = ratings_note(stated, score)
        if note is not None:
            notes[item_id] = note
    return notes


async def _target(
    db: AsyncSession, household_id: int, target_type: TargetType, target_id: int
) -> Tag | CatalogItem | None:
    repo = CatalogRepository(db)
    if target_type is TargetType.TAG:
        return await repo.tag(household_id, target_id)
    return await repo.item(household_id, target_id)


async def my_stance(
    db: AsyncSession, actor: Member, target_type: TargetType, target_id: int
) -> tuple[str, Stance] | None:
    """The actor's stored stance on one tag or item, with its name, or None if it's gone."""
    require_member(actor)
    return await _stance(db, actor, target_type, target_id)


async def member_stance(
    db: AsyncSession, actor: Member, member_id: int, target_type: TargetType, target_id: int
) -> tuple[str, Stance] | None:
    """``my_stance`` of another member, for an admin (#181)."""
    return await _stance(db, await member_for_admin(db, actor, member_id), target_type, target_id)


async def _stance(
    db: AsyncSession, member: Member, target_type: TargetType, target_id: int
) -> tuple[str, Stance] | None:
    target = await _target(db, member.household_id, target_type, target_id)
    if target is None:
        return None
    return target.name, (await _stances(db, member)).get((target_type, target_id), NEUTRAL)


async def set_my_preference(
    db: AsyncSession, actor: Member, target_type: TargetType, target_id: int, stance: Stance, *, now: datetime
) -> str:
    """Store the actor's stance on a tag or item and return the target's name."""
    require_member(actor)
    return await _set_preference(db, actor, target_type, target_id, stance, now=now)


async def set_member_preference(
    db: AsyncSession,
    actor: Member,
    member_id: int,
    target_type: TargetType,
    target_id: int,
    stance: Stance,
    *,
    now: datetime,
) -> str:
    """An admin stores another member's stance on a tag or item (#181) and gets the target's
    name back. Audited as ``member.preference_set``, with the stance before and after."""
    member = await member_for_admin(db, actor, member_id)
    return await _set_preference(db, member, target_type, target_id, stance, now=now, admin=actor)


async def _set_preference(
    db: AsyncSession,
    member: Member,
    target_type: TargetType,
    target_id: int,
    stance: Stance,
    *,
    now: datetime,
    admin: Member | None = None,
) -> str:
    """Store ``member``'s stance. With ``admin``, the change is the admin's, and audited."""
    target = await _target(db, member.household_id, target_type, target_id)
    if target is None:
        raise NotFound(f"No such {target_type.value}.")
    for attempt in range(2):
        try:
            before = (await _stances(db, member)).get((target_type, target_id), NEUTRAL)
            await PreferenceRepository(db).set(member.household_id, member.id, target_type, target_id, stance, now)
            if admin is not None:
                payload = {
                    "target": f"{target_type.value}:{target_id}",
                    "name": target.name,
                    "from": before.level,
                    "to": stance.level,
                }
                await audit(
                    db, member.household_id, admin, "member.preference_set", f"member:{member.id}", payload, now
                )
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
    updated = await _update_settings(db, actor, settings, now=now)
    await db.commit()
    return updated


async def update_member_settings(
    db: AsyncSession, actor: Member, member_id: int, settings: MySettings, *, now: datetime
) -> Member:
    """An admin changes another member's **I can usually** and reminders (#181). Audited as
    ``member.settings_updated``, with the settings that changed and their new values. A save
    that changes nothing writes no row."""
    member = await member_for_admin(db, actor, member_id)
    updated = await _update_settings(db, member, settings, now=now)
    before = _describe_settings(member)
    changes = {key: value for key, value in _describe_settings(updated).items() if before[key] != value}
    if changes:
        await audit(db, actor.household_id, actor, "member.settings_updated", f"member:{member.id}", changes, now)
    await db.commit()
    return updated


def _describe_settings(member: Member) -> dict[str, bool]:
    return {
        "can_cook": member.capabilities.can_cook,
        "can_pickup": member.capabilities.can_pickup,
        "can_order": member.capabilities.can_order,
        "mute_dm_reminders": member.mute_dm_reminders,
        "feedback_reminder": member.feedback_reminder,
    }


async def _update_settings(db: AsyncSession, member: Member, settings: MySettings, *, now: datetime) -> Member:
    return await MemberRepository(db).update(
        member.household_id,
        member.id,
        now,
        can_cook=settings.capabilities.can_cook,
        can_pickup=settings.capabilities.can_pickup,
        can_order=settings.capabilities.can_order,
        mute_dm_reminders=settings.mute_dm_reminders,
        feedback_reminder=settings.feedback_reminder,
    )


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
