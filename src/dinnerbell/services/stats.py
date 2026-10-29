# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Stats page and App Home show (spec 11, 12): a member's own counts and badges, and
the household's totals.

Nothing here ranks members. A member sees their own counts and progress, and only theirs. The
household view has totals for the whole household and the badges earned lately, with names, since
those were announced in the channel; it never lists one member's counts beside another's, never
orders people by a count, and holds nothing from anyone's preferences or feedback.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.contributions import BadgeRepository, ContributionRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.badges import (
    BADGES,
    ROLL_KINDS,
    Announcement,
    Award,
    Progress,
    Tier,
    highest_tiers,
    ladders_from,
    longest_streak,
    progress,
)
from dinnerbell.domain.contributions import PITCHING_IN, Kind
from dinnerbell.domain.members import Member
from dinnerbell.services.badge_art import Held, held_list
from dinnerbell.services.common import require_member
from dinnerbell.services.errors import Forbidden

RECENT_BADGES = 10
GUESTS_ONLY = "That's for guests."
NEW_FOR = timedelta(days=14)
"""How long a badge counts as newly earned on the Stats page."""


@dataclass(frozen=True, slots=True)
class MemberStats:
    """``Member``'s own standing: how many of each kind they have, where each badge stands, and
    the badges they hold."""

    member_id: int
    counts: dict[Kind, int]
    progress: tuple[Progress, ...]
    """Every badge, in the order of ``domain.badges.BADGES``."""
    earned: tuple[Award, ...]
    """Oldest first."""


@dataclass(frozen=True, slots=True)
class RecentBadge:
    member_name: str
    award: Award


@dataclass(frozen=True, slots=True)
class HouseholdStats:
    meals: int
    totals: dict[Kind, int]
    """Each kind counted across everyone: no member's count is in it."""
    badges_earned: int
    recent: tuple[RecentBadge, ...]
    """The newest badges, newest first."""


def newly_earned(stats: MemberStats, now: datetime) -> tuple[Award, ...]:
    """The badges earned in the last two weeks, newest first. A badge earned quietly (the
    backfill, a lowered count) is not news."""
    return tuple(
        a
        for a in reversed(stats.earned)
        if a.announcement is not Announcement.SKIPPED and now - a.awarded_at <= NEW_FOR
    )


async def my_stats(db: AsyncSession, actor: Member) -> MemberStats:
    """``actor``'s own counts, badge progress, and badges."""
    require_member(actor)
    return await _standing(db, actor)


async def guest_stats(db: AsyncSession, actor: Member) -> MemberStats:
    """A guest's own standing. A guest earns only the pitching-in badges (Chef, Road Runner,
    Order Up, Quarterback) and On a Roll, so those are the badges and counts they see."""
    if not (actor.active and actor.is_guest):
        raise Forbidden(GUESTS_ONLY)
    found = await _standing(db, actor)
    shown = tuple(p for p in found.progress if p.badge.kind is None or p.badge.kind in PITCHING_IN)
    counts = {kind: n for kind, n in found.counts.items() if kind in PITCHING_IN}
    return replace(found, counts=counts, progress=shown)


async def _standing(db: AsyncSession, actor: Member) -> MemberStats:
    household_id = actor.household_id
    ladders = ladders_from(await household_settings(db, household_id))
    contributions = ContributionRepository(db)
    counts = await contributions.counts(household_id, actor.id)
    weeks = longest_streak(await contributions.days(household_id, actor.id, ROLL_KINDS))
    earned = await BadgeRepository(db).for_member(household_id, actor.id)
    steps: list[Progress] = []
    for badge in BADGES:
        count = weeks if badge.kind is None else counts.get(badge.kind, 0)
        held: list[Tier] = [a.tier for a in earned if a.badge is badge.key]
        steps.append(progress(badge, count, ladders, earned=held))
    return MemberStats(actor.id, counts, tuple(steps), tuple(earned))


async def household_stats(db: AsyncSession, actor: Member) -> HouseholdStats:
    """The household's totals and its newest badges."""
    require_member(actor)
    household_id = actor.household_id
    badges = BadgeRepository(db)
    names = {m.id: m.display_name for m in await MemberRepository(db).list(household_id)}
    recent = tuple(
        RecentBadge(names.get(a.member_id, "A former member"), a)
        for a in await badges.recent(household_id, RECENT_BADGES)
    )
    return HouseholdStats(
        meals=await MealLogRepository(db).count(household_id),
        totals=await ContributionRepository(db).totals(household_id),
        badges_earned=await badges.count(household_id),
        recent=recent,
    )


async def picture_badges(db: AsyncSession, household_id: int, member_id: int) -> tuple[Held, ...] | None:
    """The badges to draw on a member's Home picture (#114): each they hold at its highest tier, in
    ``BADGES`` order. None for someone who gets no picture: a guest, a member who left or was
    deactivated, or one who isn't there."""
    member = await MemberRepository(db).get(household_id, member_id)
    if member is None or not member.is_member:
        return None
    return held_list(highest_tiers(await BadgeRepository(db).for_member(household_id, member_id)))
