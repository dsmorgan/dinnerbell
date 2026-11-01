# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contributions and badges (M4, FR-38, FR-39, FR-43).

``record_done`` counts a call's contributions when it reaches done, and ``record_feedback`` counts
feedback when a member gives it. Both record rows that ignore a repeat, so a call finished twice,
or by two requests at once, counts once, and then award every badge tier the new counts reach.
An award is one row per member, badge, and tier, so it happens once too. ``recount_role`` moves
a role's count when an admin corrects who did it after the meal is done (#162), and takes back the
badges the old assignee no longer reaches. A live award starts
``pending``: the channel is told later, by the Slack slice, which reads ``pending_announcements``
and calls ``mark_announced``. Awards made while catching up (``ensure_backfilled``, including the
one-time catch-up for the three ladders, and a lowered threshold) are ``skipped`` and never
announced.

Nothing here logs or audits more than IDs, display names, kinds, and counts (SEC-5).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
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
    VoteRepository,
)
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import MealLogRepository, household_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.badges import (
    BADGES,
    DEFAULT_LADDERS,
    ROLL_KINDS,
    SETTINGS_KEY,
    Announcement,
    Award,
    BadgeKey,
    Ladder,
    Ladders,
    Tier,
    check_ladders,
    ladders_from,
    ladders_setting,
    longest_streak,
    tiers_reached,
)
from dinnerbell.domain.catalog import CatalogItem
from dinnerbell.domain.contributions import ROLE_KIND, Credit, DoneFacts, Kind, credits_for_done, newness_by_meal
from dinnerbell.domain.events import AssignmentRole, Event
from dinnerbell.domain.meals import MealLog
from dinnerbell.domain.members import Member
from dinnerbell.services import activity
from dinnerbell.services.common import audit, require_admin

log = logging.getLogger(__name__)

BACKFILLED_KEY = "contributions_backfilled"
"""The household setting that says the meal log has been counted: an ISO timestamp."""
GAMBLER_KEY = "gambler_backfilled"
"""The household setting that says history has been counted for ``CATCH_UP_KINDS`` (Gambler, #108):
an ISO timestamp. Households that ran the first backfill before the kind existed get one run."""
CATCH_UP_KINDS = frozenset({Kind.GAMBLED})
LADDERS_KEY = "badge_ladders_awarded"
"""The household setting that says the tiers the three ladders and On a Roll's grace week (#130)
put in reach have been awarded, quietly: an ISO timestamp."""
ADMINS_ONLY = "Only an admin changes the badge counts."


# Counting --------------------------------------------------------------------------------------


async def record_done(db: AsyncSession, event: Event, *, now: datetime) -> list[Award]:
    """Count a done call's contributions and award the badges they reach. Safe to run again for
    the same call. Returns the awards this run made, which are pending announcement."""
    household_id = event.household_id
    members = {m.id: m for m in await MemberRepository(db).list(household_id)}
    items = {i.id: i for i in await CatalogRepository(db).items(household_id)}
    meals = await MealLogRepository(db).between(household_id, None, None)
    added = await _count_event(db, event, members, items, _History(meals), now)
    awards = await _award(db, household_id, {c.member_id for c in added}, now, Announcement.PENDING, event.id)
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
            extra={"member_id": member.id, "member": member.display_name, "credits": len(added)},
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


class _History:
    """The meal log, read once: each call's meal, and how new every meal was to the household."""

    def __init__(self, meals: list[MealLog]) -> None:
        self.by_event = {m.event_id: m for m in meals if m.event_id is not None}
        self.fresh = newness_by_meal(meals)


async def _count_event(
    db: AsyncSession,
    event: Event,
    members: dict[int, Member],
    items: dict[int, CatalogItem],
    history: _History,
    now: datetime,
    kinds: frozenset[Kind] | None = None,
) -> list[Credit]:
    """Record what ``event`` earned: every kind, or only ``kinds`` when catching up one."""
    household_id = event.household_id
    meal = history.by_event.get(event.id)
    item = items.get(meal.item_id) if meal is not None else None
    proposals = await ProposalRepository(db).for_event(household_id, event.id)
    winner = next((p for p in proposals if p.item_id == event.decided_item_id and p.mode is event.decided_mode), None)
    voted: dict[int, set[int]] = {}
    for proposal_id, voters in (await VoteRepository(db).for_event(household_id, event.id)).items():
        for voter in voters:
            voted.setdefault(voter, set()).add(proposal_id)
    votes = {member_id: frozenset(ids) for member_id, ids in voted.items()}
    facts = DoneFacts(
        event=event,
        assignments=tuple(
            (a.member_id, a.role) for a in await AssignmentRepository(db).for_event(household_id, event.id)
        ),
        responses=tuple(await ResponseRepository(db).for_event(household_id, event.id)),
        guest_ids=frozenset(m.id for m in members.values() if m.is_guest),
        invitations=await InvitationRepository(db).for_event(household_id, event.id),
        winner=winner if meal is not None else None,
        newness=history.fresh[meal.id] if meal is not None else None,
        item_creator=item.created_by if item is not None and item.created_by in members else None,
        picks=frozenset(p.id for p in proposals),
        votes=votes,
    )
    credits = [c for c in credits_for_done(facts) if c.member_id in members and (kinds is None or c.kind in kinds)]
    return await ContributionRepository(db).add(household_id, credits, day=event.day, now=now, event_id=event.id)


# Correcting a done call (#162) -----------------------------------------------------------------


async def recount_role(db: AsyncSession, actor: Member, event: Event, role: AssignmentRole, *, now: datetime) -> None:
    """Make the done call's count for ``role`` match who has it now, after an admin's correction,
    in the caller's transaction. Whoever lost the role loses the call's count for it, and every
    badge tier their counts reached before and don't now, on the household's current ladders: a
    tier they kept through a raised threshold stays. Taking an award back deletes it, so a pending
    one is never announced, and the den never flips for it. Whoever has the role now gets the
    count and every tier it reaches, pending, like any badge earned when a call is done; one taken
    back and earned again is a new award. Running it again changes nothing, so correcting back
    restores the counts and the badges.

    A call that was never counted, which only a failure at Mark done leaves, is left to
    ``ensure_backfilled``, which counts it from the roles as corrected. Audited as
    ``event.contributions_recounted`` when anything changed."""
    household_id, kind = event.household_id, ROLE_KIND[role]
    counts = ContributionRepository(db)
    if not await counts.counted(household_id, event.id):
        return
    members = {m.id: m for m in await MemberRepository(db).list(household_id)}
    assigned = await AssignmentRepository(db).for_event(household_id, event.id)
    wanted = {a.member_id for a in assigned if a.role is role and a.member_id in members}
    had = await counts.credited(household_id, event.id, kind)
    losing, gaining = sorted(had - wanted), sorted(wanted - had)
    if not losing and not gaining:
        return
    ladders = ladders_from(await household_settings(db, household_id))
    before = {m: await _reached(counts, household_id, m, ladders) for m in losing}
    await counts.remove(household_id, event.id, kind, losing)
    await counts.add(household_id, [Credit(m, kind) for m in gaining], day=event.day, now=now, event_id=event.id)
    revoked: list[Award] = []
    for member_id in losing:
        after = set(await _reached(counts, household_id, member_id, ladders))
        for badge, tier in [t for t in before[member_id] if t not in after]:
            award = await BadgeRepository(db).revoke(household_id, member_id, badge, tier)
            if award is not None:
                revoked.append(award)
    awarded = await _award(db, household_id, set(gaining), now, Announcement.PENDING, event.id)
    payload = {
        "role": role.value,
        "kind": kind.value,
        "removed": losing,
        "added": gaining,
        "badges_revoked": [_award_payload(a) for a in revoked],
        "badges_awarded": [_award_payload(a) for a in awarded],
    }
    await audit(db, household_id, actor, "event.contributions_recounted", f"event:{event.id}", payload, now)
    activity.action(
        db, "contributions_recounted", event, actor, kind=kind.value, removed=len(losing), added=len(gaining)
    )
    for award in revoked:
        activity.action(
            db, "badge_revoked", event, members.get(award.member_id), badge=award.badge.value, tier=award.tier.value
        )
    for award in awarded:
        activity.action(
            db, "badge_earned", event, members.get(award.member_id), badge=award.badge.value, tier=award.tier.value
        )


def _award_payload(award: Award) -> dict[str, object]:
    return {"member_id": award.member_id, "badge": award.badge.value, "tier": award.tier.value}


# Awarding --------------------------------------------------------------------------------------


async def _reached(
    contributions: ContributionRepository, household_id: int, member_id: int, ladders: Ladders
) -> list[tuple[BadgeKey, Tier]]:
    """Every badge tier the member's counts reach on ``ladders``, in ``BADGES`` order, lowest tier first."""
    counts = await contributions.counts(household_id, member_id)
    weeks = longest_streak(await contributions.days(household_id, member_id, ROLL_KINDS))
    return [
        (badge.key, tier)
        for badge in BADGES
        for tier in tiers_reached(weeks if badge.kind is None else counts.get(badge.kind, 0), ladders[badge.ladder])
    ]


async def _award(
    db: AsyncSession,
    household_id: int,
    member_ids: set[int],
    now: datetime,
    announcement: Announcement,
    event_id: int | None,
) -> list[Award]:
    """Award every tier each member's counts reach that they don't hold yet."""
    ladders = ladders_from(await household_settings(db, household_id))
    contributions, badges = ContributionRepository(db), BadgeRepository(db)
    made: list[Award] = []
    for member_id in sorted(member_ids):
        for badge, tier in await _reached(contributions, household_id, member_id, ladders):
            award = await badges.add(
                household_id, member_id, badge, tier, now=now, announcement=announcement, event_id=event_id
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


async def ladders(db: AsyncSession, household_id: int) -> dict[Ladder, tuple[int, ...]]:
    return ladders_from(await household_settings(db, household_id))


async def set_thresholds(
    db: AsyncSession, actor: Member, values: Mapping[Ladder, Sequence[int]], *, now: datetime
) -> None:
    """The admin's badge counts (FR-43): five per ladder, one per tier. Raises
    ``domain.badges.InvalidThresholds``. Audited.

    Lowering a count awards the tiers members already have the counts for, quietly: no one is
    announced a badge for a change of rules. Raising one takes nothing away."""
    require_admin(actor, ADMINS_ONLY)
    await _save_ladders(db, actor, check_ladders(values), "household.badge_thresholds_updated", now)


async def reset_thresholds(db: AsyncSession, actor: Member, *, now: datetime) -> None:
    """Put every ladder back to its default counts, as ``set_thresholds`` would. Audited."""
    require_admin(actor, ADMINS_ONLY)
    await _save_ladders(db, actor, DEFAULT_LADDERS, "household.badge_thresholds_reset", now)


async def _save_ladders(
    db: AsyncSession, actor: Member, ladders: Mapping[Ladder, tuple[int, ...]], action: str, now: datetime
) -> None:
    async with write_transaction(db):
        stored = await household_settings(db, actor.household_id)
        before = ladders_from(stored)
        if before == ladders:
            return
        to = ladders_setting(ladders)
        await HouseholdRepository(db).update_settings(actor.household_id, stored | {SETTINGS_KEY: to})
        everyone = set(await ContributionRepository(db).members(actor.household_id))
        caught_up = await _award(db, actor.household_id, everyone, now, Announcement.SKIPPED, None)
        payload = {"from": ladders_setting(before), "to": to, "caught_up": len(caught_up)}
        await audit(db, actor.household_id, actor, action, _target(actor), payload, now)
        await db.commit()
    log.info("badge thresholds changed", extra={"member_id": actor.id, "caught_up": len(caught_up)})


def _target(actor: Member) -> str:
    return f"household:{actor.household_id}"


# History ---------------------------------------------------------------------------------------


BATCH = 200
"""Events counted per transaction while catching up, so startup holds the write lock for moments."""


async def ensure_backfilled(db: AsyncSession, household_id: int, *, now: datetime) -> bool:
    """Count the meals already in the log on the first start after the upgrade, and on every
    start after it count any done call that has no counts, which only a failure while it was
    finished can leave (``lifecycle.finish`` keeps counting from blocking Mark done). Returns
    whether it made the first run or counted anything. A done call with nothing to credit is
    looked at again on each start, which costs one indexed query, and counts for nothing.

    The badges these counts reach are ``skipped``, never pending, so an upgrade doesn't flood the
    channel and a missed one isn't announced late. The first run counts calls and feedback only: a
    meal someone logged by hand has no roles or answers to count. It uses members' current roles,
    so someone who was a guest then and is a member now counts as a member.

    A household that ran the first run before Gambler existed has no ``gambler_backfilled``, so its
    next start counts ``gambled`` for every done call, quietly, and sets it. Other kinds are left
    as they were.

    A household without ``badge_ladders_awarded`` started before the three ladders and On a Roll's
    grace week (#130), which can put tiers in reach that members don't hold. Its next start awards
    them, quietly, as a lowered threshold does, and sets it.

    It commits every ``BATCH`` calls and sets ``contributions_backfilled`` last, so a start that
    is killed halfway counts the rest next time: every insert ignores a repeat."""
    stored = await household_settings(db, household_id)
    first = BACKFILLED_KEY not in stored
    catching_up = not first and GAMBLER_KEY not in stored
    regrading = LADDERS_KEY not in stored
    events = await EventRepository(db).done(household_id, uncounted=not first and not catching_up)
    if not events and not first and not catching_up and not regrading:
        return False
    # A household that ran the first backfill before a kind existed counts every done call for
    # just that kind. A call with no counts at all still gets all of them.
    fully = {e.id for e in await EventRepository(db).done(household_id, uncounted=True)} if catching_up else set()
    members = {m.id: m for m in await MemberRepository(db).list(household_id)}
    items = {i.id: i for i in await CatalogRepository(db).items(household_id)}
    history = _History(await MealLogRepository(db).between(household_id, None, None))
    counted = 0
    for start in range(0, len(events), BATCH):
        async with write_transaction(db):
            for event in events[start : start + BATCH]:
                # Under the lock: a call discarded since the list was read is skipped, not counted.
                if await EventRepository(db).get(household_id, event.id) is not None:
                    only = CATCH_UP_KINDS if catching_up and event.id not in fully else None
                    counted += len(await _count_event(db, event, members, items, history, now, only))
            await db.commit()
    awards: list[Award] = []
    async with write_transaction(db):
        if first:
            for member_id, meal_id, day in await FeedbackRepository(db).given(household_id):
                if member_id in members and members[member_id].is_member:
                    credit = Credit(member_id, Kind.GAVE_FEEDBACK)
                    added = await ContributionRepository(db).add(
                        household_id, [credit], day=day, now=now, meal_id=meal_id
                    )
                    counted += len(added)
        everyone = set(await ContributionRepository(db).members(household_id))
        awards = await _award(db, household_id, everyone, now, Announcement.SKIPPED, None)
        if first or catching_up or regrading:
            # The first run counts every kind, Gambler included, and awards on the current ladders.
            due = {BACKFILLED_KEY: first, GAMBLER_KEY: first or catching_up, LADDERS_KEY: regrading}
            marks = {key: now.isoformat() for key, mark in due.items() if mark}
            stored = await household_settings(db, household_id)
            await HouseholdRepository(db).update_settings(household_id, stored | marks)
        await db.commit()
    log.info(
        "contributions backfilled",
        extra={"events": len(events), "credits": counted, "badges": len(awards), "first": first},
    )
    return first or catching_up or regrading or counted > 0
