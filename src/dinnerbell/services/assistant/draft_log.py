# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a conversation records when its member confirms or cancels a draft (#363, spec 6.14,
**Drafts**), from the web or Slack: an entry such as "Confirmed on Tuesday, November 24: Start a
lunch call for today. The lunch call for today is started.", so the page shows it after a reload
and the next request tells the model.

The entry is in pseudonym form, as the rest of the conversation is: a person the draft or its
result names goes in as the conversation's label, and a held value as not shown. The page puts
names back for its member. It goes only into a conversation Dinnerbell still keeps: none at
Conversation hours 0, past them, or while the switch is off, as ``conversation._keep`` keeps
nothing then.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import (
    DRAFT,
    MAX_STORED_BYTES,
    Conversation,
    Turn,
    assistant_settings,
    recent_turns,
    used_draft_text,
)
from dinnerbell.domain.assistant_catalog import NOT_SHOWN
from dinnerbell.domain.assistant_coordinating import with_held, with_people
from dinnerbell.domain.assistant_drafts import Draft
from dinnerbell.domain.assistant_memory import cut_waiting
from dinnerbell.domain.members import Member
from dinnerbell.services.variety import household_today

log = logging.getLogger(__name__)


async def record_use(
    db: AsyncSession,
    member: Member,
    draft: Draft,
    *,
    confirmed: bool,
    outcome: str = "",
    now: datetime,
    verb: str | None = None,
) -> bool:
    """Add ``draft``'s entry to its conversation, once ``member`` has confirmed or cancelled it.
    ``outcome`` is Confirm's result, with each person as ``assistant_coordinating.person`` of
    their member ID, never a name. Returns whether the conversation kept it: nothing is added to
    a conversation that's gone, expired, or another member's, or while nothing is kept. The
    conversation's last message stays when it was, so the entry never makes it last longer.

    A direct action's record (#374) adds "Done" and "Undone" entries, by ``verb``; "Done" carries
    the record's ID, for the page to show its card there."""
    household_id = member.household_id
    repo = AssistantRepository(db)
    async with write_transaction(db):
        settings = assistant_settings(await household_settings(db, household_id))
        stored = await repo.conversation(household_id, draft.conversation_key)
        if not settings.enabled or stored is None or stored.expired(now, settings.conversation_hours):
            return False
        if stored.private and stored.owner_member_id != member.id:  # pragma: no cover - drafts are their member's
            return False
        pseudonyms = stored.pseudonyms.with_member(member.id)

        def label(member_id: int) -> str:
            nonlocal pseudonyms
            pseudonyms = pseudonyms.with_member(member_id)
            return pseudonyms.label(member_id) or "a member"

        def told(text: str) -> str:
            return with_held(with_people(text, label), lambda _: NOT_SHOWN)

        household = await HouseholdRepository(db).get(household_id)
        if household is None:  # pragma: no cover - members cascade with their household
            return False
        day = household_today(household, now)
        text = used_draft_text(told(draft.title), confirmed=confirmed, day=day, outcome=told(outcome), verb=verb)
        record = draft.id if draft.direct and verb == "Done" else None
        entry = Turn(DRAFT, text, pseudonyms.label(member.id), record=record)
        turns = recent_turns([*stored.turns, entry], MAX_STORED_BYTES)
        if lost := cut_waiting(stored.turns, turns):
            log.warning("assistant memory messages cut before the rollup", extra={"count": lost})
        kept = Conversation(
            key=stored.key,
            private=stored.private,
            owner_member_id=stored.owner_member_id,
            pseudonyms=pseudonyms,
            turns=turns,
            last_message_at=stored.last_message_at,
        )
        await repo.save_conversation(household_id, kept, now)
        await db.commit()
    return True
