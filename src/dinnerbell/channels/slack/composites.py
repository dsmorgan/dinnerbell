# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a member's composite badge picture draws (#114): the badges they hold now, for
``BadgeFiles.composites_soon`` to read in the background."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.services.badge_art import Held
from dinnerbell.services.stats import picture_badges


async def held_of_member(
    sessionmaker: async_sessionmaker[AsyncSession], household_id: int, member_id: int
) -> Sequence[Held] | None:
    """``stats.picture_badges`` in its own session: the member's badges, or None for no picture."""
    async with sessionmaker() as db:
        return await picture_badges(db, household_id, member_id)
