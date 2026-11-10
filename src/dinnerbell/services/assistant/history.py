# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web **Assistant** page's one conversation per member (spec 6.14, **Conversations**):
what the page shows, with names put back, and **Start over**.

The conversation is keyed ``web_key(member.id)`` and is always private, so only its member
reads it or starts it over. Both raise NotFound while the assistant doesn't answer the member
(``runtime.available``), as every entry point does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import MemberRepository
from dinnerbell.domain.assistant import Conversation, assistant_settings, web_key
from dinnerbell.domain.members import Member
from dinnerbell.services.assistant.runtime import AssistantConfig, available
from dinnerbell.services.errors import NotFound

log = logging.getLogger(__name__)

NOT_HERE = "That page doesn't exist."


@dataclass(frozen=True, slots=True)
class Shown:
    """One message as the page shows it: the member's own (``mine``), or the assistant's, with
    members' names put back."""

    mine: bool
    text: str


@dataclass(frozen=True, slots=True)
class WebConversation:
    turns: tuple[Shown, ...]
    hours: int
    """Admin's Conversation hours. At 0, nothing is kept, so ``turns`` is always empty."""

    @property
    def keeps_history(self) -> bool:
        return self.hours > 0


async def _require(db: AsyncSession, config: AssistantConfig, member: Member) -> None:
    if not await available(db, config, member):
        raise NotFound(NOT_HERE)


def _owned(stored: Conversation | None, member: Member) -> Conversation | None:
    """The stored conversation, only if it's ``member``'s private one: never anyone else's."""
    if stored is None or not stored.private or stored.owner_member_id != member.id:
        return None
    return stored


async def web_conversation(db: AsyncSession, config: AssistantConfig, member: Member, now: datetime) -> WebConversation:
    """``member``'s web conversation as the page shows it: their messages and the assistant's
    answers, oldest first, until Admin's Conversation hours pass since the last message."""
    await _require(db, config, member)
    hours = assistant_settings(await household_settings(db, member.household_id)).conversation_hours
    stored = _owned(await AssistantRepository(db).conversation(member.household_id, web_key(member.id)), member)
    if stored is None or stored.expired(now, hours):
        return WebConversation((), hours)
    names = {m.id: m.display_name for m in await MemberRepository(db).list(member.household_id)}
    restore = stored.pseudonyms.restore_names
    turns = tuple(
        Shown(True, turn.text) if turn.role == "user" else Shown(False, restore(turn.text, names))
        for turn in stored.turns
    )
    return WebConversation(turns, hours)


async def start_over(db: AsyncSession, config: AssistantConfig, member: Member, now: datetime) -> int:
    """**Start over**: delete ``member``'s web conversation, and cancel the drafts still waiting
    in it, so the page starts empty. Returns how many drafts it cancelled."""
    await _require(db, config, member)
    key = web_key(member.id)
    repo = AssistantRepository(db)
    async with write_transaction(db):
        cancelled = 0
        for draft in await repo.drafts_waiting(member.household_id, member.id, key, now):
            cancelled += await repo.use_draft(member.household_id, draft.id, member.id, now)
        await repo.delete_conversation(member.household_id, key)
        await db.commit()
    log.info("assistant conversation started over", extra={"member_id": member.id, "drafts_cancelled": cancelled})
    return cancelled
