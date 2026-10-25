# SPDX-License-Identifier: AGPL-3.0-or-later
"""Members as a chat channel sees them: who a Slack user is, and each member's Slack user ID
(FR-42). The Slack adapter uses these because ``channels`` may not import ``db``.

Dinnerbell serves one household per deployment, and the Slack workspace belongs to it, so a
Slack user is looked up in that household (the first, as sign-in does).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.household import Household
from dinnerbell.domain.members import Member
from dinnerbell.services.common import audit
from dinnerbell.services.errors import NotFound
from dinnerbell.services.variety import household_today

log = logging.getLogger(__name__)


async def _household(db: AsyncSession) -> Household:
    household = await HouseholdRepository(db).first()
    if household is None:  # pragma: no cover - bootstrap runs before anything else
        raise NotFound("No household.")
    return household


async def member_for_slack_user(db: AsyncSession, slack_user_id: str) -> Member | None:
    """The member linked to ``slack_user_id``, active or not, or None. Callers refuse inactive
    members themselves, so they can say why."""
    household = await _household(db)
    return await MemberRepository(db).by_slack_user_id(household.id, slack_user_id)


async def slack_directory(db: AsyncSession, household_id: int) -> dict[int, str]:
    """Member ID to Slack user ID, for every member who has one, active or not."""
    members = await MemberRepository(db).list(household_id)
    return {m.id: m.slack_user_id for m in members if m.slack_user_id}


async def today(db: AsyncSession, actor: Member, *, now: datetime) -> date:
    """Today's date in the actor's household time zone."""
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    return household_today(household, now)


@dataclass(frozen=True, slots=True)
class Unlinked:
    """An active member with no Slack user yet. ``email`` is for a lookup only; never log it."""

    member_id: int
    email: str


async def unlinked_members(db: AsyncSession) -> tuple[int, list[Unlinked]]:
    """The household's ID and its active members without a Slack user ID, except members whose
    link the admin set or cleared by hand."""
    household = await _household(db)
    members = await MemberRepository(db).list(household.id)
    unlinked = [m for m in members if m.is_member and not m.slack_user_id and not m.slack_link_locked and m.email]
    return household.id, [Unlinked(m.id, m.email) for m in unlinked if m.email]


async def link_slack_users(
    db: AsyncSession, household_id: int, found: Mapping[int, str], *, now: datetime
) -> list[int]:
    """Store Slack user IDs found by email. Skips members who got one meanwhile, members whose
    link the admin locked, and IDs another member already has. Each link is audited as a system
    action. Returns the linked member IDs."""
    repo = MemberRepository(db)
    members = {m.id: m for m in await repo.list(household_id)}
    taken = {m.slack_user_id for m in members.values() if m.slack_user_id}
    linked: list[int] = []
    for member_id, slack_user_id in found.items():
        member = members.get(member_id)
        if member is None or member.slack_user_id or member.slack_link_locked or not member.active:
            continue
        if slack_user_id in taken:
            log.warning("slack user already linked to another member", extra={"member_id": member_id})
            continue
        await repo.update(household_id, member_id, now, slack_user_id=slack_user_id)
        payload = {"slack_user_id": slack_user_id, "matched_by": "email"}
        await audit(db, household_id, None, "member.slack_linked", f"member:{member_id}", payload, now)
        taken.add(slack_user_id)
        linked.append(member_id)
    await db.commit()
    return linked
