# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deleting conversations once Admin's Conversation hours pass (spec 6.14, **History**), and
forgetting them all for ``scrub-test``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import ENABLED_KEY, assistant_settings
from dinnerbell.services.assistant.admin import delete_expired


async def purge_idle_conversations(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Delete every household's conversations whose last message is Conversation hours ago or
    more, whether or not the assistant is on. Idempotent. Returns how many it deleted."""
    async with sessionmaker() as db:
        households = await HouseholdRepository(db).all()
    removed = 0
    for household in households:
        async with sessionmaker() as db, write_transaction(db):
            hours = assistant_settings(await household_settings(db, household.id)).conversation_hours
            removed += await delete_expired(db, household.id, hours, now)
            await db.commit()
    return removed


async def forget_for_test(db: AsyncSession, household_id: int) -> tuple[bool, int]:
    """For ``scrub-test``, in its transaction: turn Admin's Assistant switch off and delete every
    conversation. Returns whether the switch was on and how many conversations went. Usage rows
    stay: they hold member IDs and token counts, no text."""
    stored = await household_settings(db, household_id)
    was_on = assistant_settings(stored).enabled
    if ENABLED_KEY in stored:
        await HouseholdRepository(db).update_settings(household_id, stored | {ENABLED_KEY: False})
    deleted = await AssistantRepository(db).delete_idle(household_id, None)
    return was_on, deleted
