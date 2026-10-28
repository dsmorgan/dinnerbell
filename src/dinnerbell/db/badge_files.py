# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data access for the Slack files that hold badge art (#105) and members' composite pictures (#114)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.models import SlackBadgeCompositeRow, SlackBadgeFileRow


@dataclass(frozen=True, slots=True)
class BadgeFile:
    badge_key: str
    tier: str
    content_hash: str
    file_id: str


@dataclass(frozen=True, slots=True)
class BadgeComposite:
    content_hash: str
    file_id: str
    uploaded_at: datetime


class BadgeFileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def all(self, household_id: int) -> dict[tuple[str, str], BadgeFile]:
        """The household's uploaded files, by badge key and tier value."""
        rows = await self._session.scalars(
            select(SlackBadgeFileRow).where(SlackBadgeFileRow.household_id == household_id)
        )
        return {(r.badge_key, r.tier): BadgeFile(r.badge_key, r.tier, r.content_hash, r.file_id) for r in rows}

    async def save(self, household_id: int, file: BadgeFile, now: datetime) -> None:
        """Record ``file`` as the badge tier's current upload, replacing an older one."""
        values = {
            "household_id": household_id,
            "badge_key": file.badge_key,
            "tier": file.tier,
            "content_hash": file.content_hash,
            "file_id": file.file_id,
            "uploaded_at": now,
        }
        await self._session.execute(
            insert(SlackBadgeFileRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["household_id", "badge_key", "tier"],
                set_={k: values[k] for k in ("content_hash", "file_id", "uploaded_at")},
            )
        )

    async def forget(self, household_id: int, file_ids: set[str]) -> None:
        """Drop the rows for ``file_ids``, such as files Slack no longer has: badge art and composites."""
        await self._session.execute(
            delete(SlackBadgeFileRow).where(
                SlackBadgeFileRow.household_id == household_id, SlackBadgeFileRow.file_id.in_(file_ids)
            )
        )
        await self._session.execute(
            delete(SlackBadgeCompositeRow).where(
                SlackBadgeCompositeRow.household_id == household_id, SlackBadgeCompositeRow.file_id.in_(file_ids)
            )
        )

    async def composite(self, household_id: int, member_id: int) -> BadgeComposite | None:
        """The member's current composite picture, or None."""
        row = await self._session.scalar(
            select(SlackBadgeCompositeRow).where(
                SlackBadgeCompositeRow.household_id == household_id, SlackBadgeCompositeRow.member_id == member_id
            )
        )
        return None if row is None else BadgeComposite(row.content_hash, row.file_id, row.uploaded_at)

    async def save_composite(self, household_id: int, member_id: int, file: BadgeComposite) -> None:
        """Record ``file`` as the member's composite, replacing an older one: one row per member."""
        values = {
            "household_id": household_id,
            "member_id": member_id,
            "content_hash": file.content_hash,
            "file_id": file.file_id,
            "uploaded_at": file.uploaded_at,
        }
        await self._session.execute(
            insert(SlackBadgeCompositeRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["household_id", "member_id"],
                set_={k: values[k] for k in ("content_hash", "file_id", "uploaded_at")},
            )
        )
