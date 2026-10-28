# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contribution and badge data access (FR-38, FR-39). Inserts ignore a duplicate instead of
failing, so recording the same call twice, or two at once, is safe and says how many rows were new."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import BadgeAwardRow, ContributionRow
from dinnerbell.domain.badges import Announcement, Award, BadgeKey, Tier
from dinnerbell.domain.contributions import Credit, Kind


class ContributionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        household_id: int,
        credits: Iterable[Credit],
        *,
        day: date,
        now: datetime,
        event_id: int | None = None,
        meal_id: int | None = None,
    ) -> list[Credit]:
        """Record ``credits`` for one call or one meal; return the ones that were new."""
        added: list[Credit] = []
        for credit in credits:
            statement = (
                insert(ContributionRow)
                .values(
                    household_id=household_id,
                    member_id=credit.member_id,
                    kind=credit.kind.value,
                    day=day,
                    event_id=event_id,
                    meal_id=meal_id,
                    item_id=credit.item_id,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
            if (await self._session.execute(statement)).rowcount:  # type: ignore[attr-defined]
                added.append(credit)
        return added

    async def counts(self, household_id: int, member_id: int) -> dict[Kind, int]:
        rows = await self._session.execute(
            select(ContributionRow.kind, func.count())
            .where(ContributionRow.household_id == household_id, ContributionRow.member_id == member_id)
            .group_by(ContributionRow.kind)
        )
        return {Kind(kind): n for kind, n in rows}

    async def days(self, household_id: int, member_id: int, kinds: Iterable[Kind]) -> set[date]:
        rows = await self._session.scalars(
            select(ContributionRow.day)
            .where(
                ContributionRow.household_id == household_id,
                ContributionRow.member_id == member_id,
                ContributionRow.kind.in_([k.value for k in kinds]),
            )
            .distinct()
        )
        return set(rows)

    async def totals(self, household_id: int) -> dict[Kind, int]:
        """Each kind counted across the household. Nobody's own count is in it."""
        rows = await self._session.execute(
            select(ContributionRow.kind, func.count())
            .where(ContributionRow.household_id == household_id)
            .group_by(ContributionRow.kind)
        )
        return {Kind(kind): n for kind, n in rows}

    async def clear_feedback(self, household_id: int) -> int:
        """Delete the household's ``gave_feedback`` counts, for ``scrub-test``: they say who gave
        feedback, as the feedback rows do. Returns how many rows."""
        result = await self._session.execute(
            delete(ContributionRow).where(
                ContributionRow.household_id == household_id, ContributionRow.kind == Kind.GAVE_FEEDBACK.value
            )
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def members(self, household_id: int) -> list[int]:
        """Everyone with at least one contribution."""
        rows = await self._session.scalars(
            select(ContributionRow.member_id).where(ContributionRow.household_id == household_id).distinct()
        )
        return sorted(rows)


def _award(row: BadgeAwardRow) -> Award:
    return Award(
        id=row.id,
        household_id=row.household_id,
        member_id=row.member_id,
        badge=BadgeKey(row.badge_key),
        tier=Tier(row.tier),
        awarded_at=row.awarded_at,
        announcement=Announcement(row.announcement),
        announced_at=row.announced_at,
        event_id=row.event_id,
    )


class BadgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        household_id: int,
        member_id: int,
        badge: BadgeKey,
        tier: Tier,
        *,
        now: datetime,
        announcement: Announcement,
        event_id: int | None = None,
    ) -> Award | None:
        """Record the award; None when the member already had it, which is how it stays once."""
        statement = (
            insert(BadgeAwardRow)
            .values(
                household_id=household_id,
                member_id=member_id,
                badge_key=badge.value,
                tier=tier.value,
                event_id=event_id,
                awarded_at=now,
                announcement=announcement.value,
            )
            .on_conflict_do_nothing()
            .returning(BadgeAwardRow.id)
        )
        award_id = (await self._session.execute(statement)).scalar_one_or_none()
        if award_id is None:
            return None
        return Award(award_id, household_id, member_id, badge, tier, now, announcement, None, event_id)

    async def for_member(self, household_id: int, member_id: int) -> list[Award]:
        rows = await self._session.scalars(
            select(BadgeAwardRow)
            .where(BadgeAwardRow.household_id == household_id, BadgeAwardRow.member_id == member_id)
            .order_by(BadgeAwardRow.awarded_at, BadgeAwardRow.id)
        )
        return [_award(r) for r in rows]

    async def recent(self, household_id: int, limit: int) -> list[Award]:
        """The newest awards in the household, newest first. Quiet awards (``skipped``: the
        backfill, a lowered threshold) are not news, so they are left out."""
        rows = await self._session.scalars(
            select(BadgeAwardRow)
            .where(
                BadgeAwardRow.household_id == household_id,
                BadgeAwardRow.announcement != Announcement.SKIPPED.value,
            )
            .order_by(BadgeAwardRow.awarded_at.desc(), BadgeAwardRow.id.desc())
            .limit(limit)
        )
        return [_award(r) for r in rows]

    async def count(self, household_id: int) -> int:
        n = await self._session.scalar(
            select(func.count()).select_from(BadgeAwardRow).where(BadgeAwardRow.household_id == household_id)
        )
        return int(n or 0)

    async def pending(self, household_id: int) -> list[Award]:
        """Awards the channel hasn't been told about, oldest first."""
        rows = await self._session.scalars(
            select(BadgeAwardRow)
            .where(
                BadgeAwardRow.household_id == household_id,
                BadgeAwardRow.announcement == Announcement.PENDING.value,
            )
            .order_by(BadgeAwardRow.awarded_at, BadgeAwardRow.id)
        )
        return [_award(r) for r in rows]

    async def mark_announced(self, household_id: int, award_ids: Iterable[int], now: datetime) -> int:
        """Mark pending awards announced; those already announced or skipped stay as they are,
        so a post that is retried or runs twice announces each badge once."""
        result = await self._session.execute(
            update(BadgeAwardRow)
            .where(
                BadgeAwardRow.household_id == household_id,
                BadgeAwardRow.id.in_(list(award_ids)),
                BadgeAwardRow.announcement == Announcement.PENDING.value,
            )
            .values(announcement=Announcement.ANNOUNCED.value, announced_at=now)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
