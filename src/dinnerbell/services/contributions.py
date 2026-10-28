# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contributions and badges (M4, FR-38, FR-39, FR-43).

``record_done`` counts a call's contributions when it reaches done, and ``record_feedback`` counts
feedback when a member gives it. Both record rows that ignore a repeat, so a call finished twice,
or by two requests at once, counts once, and then award every badge tier the new counts reach.
An award is one row per member, badge, and tier, so it happens once too. A live award starts
``pending``: the channel is told later, by the Slack slice, which reads ``pending_announcements``
and calls ``mark_announced``. Awards made while catching up (``ensure_backfilled`` and a lowered
threshold) are ``skipped`` and never announced.

Nothing here logs or audits more than IDs, display names, kinds, and counts (SEC-5).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.contributions import BadgeRepository, ContributionRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import (
    AssignmentRepository,
    EventRepository,
    InvitationRepository,
    ProposalRepository,
    ResponseRepository,
)
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.badges import (
    BADGES,
    ROLL_KINDS,
    SETTINGS_KEY,
    Announcement,
    Award,
    check_thresholds,
    longest_streak,
    thresholds_from,
    tiers_reached,
)
from dinnerbell.domain.contributions import Credit, DoneFacts, Kind, credits_for_done, newness
from dinnerbell.domain.events import Event
from dinnerbell.domain.meals import MealLog
from dinnerbell.domain.members import Member
from dinnerbell.services import activity
from dinnerbell.services.common import audit, require_admin

log = logging.getLogger(__name__)

BACKFILLED_KEY = "contributions_backfilled"
"""The household setting that says the meal log has been counted: an ISO timestamp."""
ADMINS_ONLY = "Only an admin changes the badge counts."


# Counting --------------------------------------------------------------------------------------


async def record_done(db: AsyncSession, event: Event, *, now: datetime) -> list[Award]:
    """Count a done call's contributions and award the badges they reach. Safe to run again for
    the same call. Returns the awards this run made, which are pending announcement."""
    members = {m.id: m for m in await MemberRepository(db).list(event.household_id)}
    added = await _count_event(db, event, members, now)
    awards = await _award(db, event.household_id, {c.member_id for c in added}, now, Announcement.PENDING, event.id)
    if added or awards:
        activity.action(db, "contributions_recorded", event, None, credits=len(added), badges=len(awards))
    for award in awards:
        activity.action(
            db, "badge_earned", event, members.get(award.member_id), badge=award.badge.value, tier=award.tier.value
        )
    return awards


async def record_feedback(db: AsyncSession, member: Member, meal: MealLog, *, now: datetime) -> list[Award]:
    """Count a member's feedback on a meal, once however often they change it, and award what
    it reaches. Guests give none (#38)."""
    if not member.is_member:
        return []
    credit = Credit(member.id, Kind.GAVE_FEEDBACK)
    added = await ContributionRepository(db).add(member.household_id, [credit], day=meal.day, now=now, meal_id=meal.id)
    awards = await _award(db, member.household_id, {member.id}, now, Announcement.PENDING, None)
    if added or awards:
        log.info(
            "contributions_recorded",
            extra={"member_id": member.id, "member": member.display_name, "meal_id": meal.id, "credits": len(added)},
        )
    for award in awards:
        log.info(
            "badge_earned",
            extra={
                "member_id": member.id,
                "member": member.display_name,
                "badge": award.badge.value,
                "tier": award.tier.value,
            },
        )
    return awards


async def _count_event(db: AsyncSession, event: Event, members: dict[int, Member], now: datetime) -> list[Credit]:
    household_id = event.household_id
    meals = MealLogRepository(db)
    meal_id = await meals.for_event(household_id, event.id)
    meal = await meals.get(household_id, meal_id) if meal_id is not None else None
    item = await CatalogRepository(db).item(household_id, meal.item_id) if meal is not None else None
    proposals = await ProposalRepository(db).for_event(household_id, event.id)
    winner = next((p for p in proposals if p.item_id == event.decided_item_id and p.mode is event.decided_mode), None)
    facts = DoneFacts(
        event=event,
        assignments=tuple(
            (a.member_id, a.role) for a in await AssignmentRepository(db).for_event(household_id, event.id)
        ),
        responses=tuple(await ResponseRepository(db).for_event(household_id, event.id)),
        guest_ids=frozenset(m.id for m in members.values() if m.is_guest),
        invitations=await InvitationRepository(db).for_event(household_id, event.id),
        winner=winner if meal is not None else None,
        newness=newness(meal, await meals.between(household_id, None, None)) if meal is not None else None,
        item_creator=item.created_by if item is not None and item.created_by in members else None,
    )
    credits = [c for c in credits_for_done(facts) if c.member_id in members]
    return await ContributionRepository(db).add(household_id, credits, day=event.day, now=now, event_id=event.id)


# Awarding --------------------------------------------------------------------------------------


async def _award(
    db: AsyncSession,
    household_id: int,
    member_ids: set[int],
    now: datetime,
    announcement: Announcement,
    event_id: int | None,
) -> list[Award]:
    """Award every tier each member's counts reach that they don't hold yet."""
    thresholds = thresholds_from(await household_settings(db, household_id))
    contributions, badges = ContributionRepository(db), BadgeRepository(db)
    made: list[Award] = []
    for member_id in sorted(member_ids):
        counts = await contributions.counts(household_id, member_id)
        weeks = longest_streak(await contributions.days(household_id, member_id, ROLL_KINDS))
        for badge in BADGES:
            count = weeks if badge.kind is None else counts.get(badge.kind, 0)
            for tier in tiers_reached(count, thresholds):
                award = await badges.add(
                    household_id, member_id, badge.key, tier, now=now, announcement=announcement, event_id=event_id
                )
                if award is not None:
                    made.append(award)
    return made


async def pending_announcements(db: AsyncSession, household_id: int) -> list[Award]:
    """Awards the channel hasn't been told about, oldest first."""
    return await BadgeRepository(db).pending(household_id)


async def mark_announced(db: AsyncSession, household_id: int, award_ids: list[int], *, now: datetime) -> int:
    """Record that ``award_ids`` were announced. Awards already announced stay as they were, so
    a post that runs twice announces each badge once. The caller commits."""
    return await BadgeRepository(db).mark_announced(household_id, award_ids, now)


# Thresholds ------------------------------------------------------------------------------------


async def thresholds(db: AsyncSession, household_id: int) -> tuple[int, ...]:
    return thresholds_from(await household_settings(db, household_id))


async def set_thresholds(db: AsyncSession, actor: Member, values: list[int], *, now: datetime) -> None:
    """The admin's badge counts (FR-43), one per tier. Raises ``domain.badges.InvalidThresholds``. Audited.

    Lowering a count awards the tiers members already have the counts for, quietly: no one is
    announced a badge for a change of rules. Raising one takes nothing away."""
    require_admin(actor, ADMINS_ONLY)
    ladder = check_thresholds(values)
    async with write_transaction(db):
        stored = await household_settings(db, actor.household_id)
        before = thresholds_from(stored)
        if before == ladder:
            return
        await HouseholdRepository(db).update_settings(actor.household_id, stored | {SETTINGS_KEY: list(ladder)})
        everyone = set(await ContributionRepository(db).members(actor.household_id))
        caught_up = await _award(db, actor.household_id, everyone, now, Announcement.SKIPPED, None)
        payload = {"from": list(before), "to": list(ladder), "caught_up": len(caught_up)}
        await audit(db, actor.household_id, actor, "household.badge_thresholds_updated", _target(actor), payload, now)
        await db.commit()
    log.info("badge thresholds changed", extra={"member_id": actor.id, "caught_up": len(caught_up)})


def _target(actor: Member) -> str:
    return f"household:{actor.household_id}"


# History ---------------------------------------------------------------------------------------


async def ensure_backfilled(db: AsyncSession, household_id: int, *, now: datetime) -> bool:
    """Count the meals already in the log, once, on the first start after the upgrade. Returns
    whether it ran. The badges those counts reach are ``skipped``, never pending, so an upgrade
    doesn't flood the channel. Calls and feedback only: a meal someone logged by hand has no
    roles or answers to count."""
    async with write_transaction(db):
        stored = await household_settings(db, household_id)
        if BACKFILLED_KEY in stored:
            return False
        members = {m.id: m for m in await MemberRepository(db).list(household_id)}
        events = await EventRepository(db).done(household_id)
        counted = 0
        for event in events:
            counted += len(await _count_event(db, event, members, now))
        for member_id, meal_id, day in await FeedbackRepository(db).given(household_id):
            if member_id in members and members[member_id].is_member:
                credit = Credit(member_id, Kind.GAVE_FEEDBACK)
                counted += len(
                    await ContributionRepository(db).add(household_id, [credit], day=day, now=now, meal_id=meal_id)
                )
        everyone = set(await ContributionRepository(db).members(household_id))
        awards = await _award(db, household_id, everyone, now, Announcement.SKIPPED, None)
        await HouseholdRepository(db).update_settings(household_id, stored | {BACKFILLED_KEY: now.isoformat()})
        await db.commit()
    log.info("contributions backfilled", extra={"events": len(events), "credits": counted, "badges": len(awards)})
    return True
