# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's memory (#367, ADR 0012, spec 6.14 **Memory**): the rollup that keeps short
facts from each member's own messages, what each member sees of their own, and the purge.

**The rollup.** While **Memory** is on, each member message a conversation keeps waits for the
rollup (``Turn.pending``). The purge job rolls a conversation up 15 minutes before its oldest
waiting message reaches Conversation hours, once 24,000 bytes wait, or as a last try once it's
past them (``domain.assistant_memory.due``); **Start over** rolls up first, too. A pass makes
one model call for each member whose messages it takes, with **Admin > Assistant**'s rollup
model. That call sends the member's facts, what their Preferences hold, and their own messages,
in a private conversation with the replies around them as context only: never anyone else's
messages, and never a channel thread's replies, which can echo housemates' words. It's reserved and
settled under the monthly cap like any call (``conversation.call_outside_message``), under that
member, and it waits while what's left wouldn't also cover the smallest private message.

The call runs outside any transaction. Then, under the write lock, its changes are kept only if
the switches are still on, the member is still active, and their facts are as the call read
them; otherwise their messages wait for the next run. ``domain.assistant_memory.check_changes``
drops any change that doesn't quote at least three words of the member's own message, says
what that message doesn't, reads as an instruction, names a fact that isn't theirs, or holds a
member label or a link, so a fact lands only in its author's record and says only what its
author said.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.memory import MemoryRepository
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.assistant import Conversation, Turn, assistant_settings, month_key, reservation_micros
from dinnerbell.domain.assistant_memory import (
    ROLLUP_PROMPT,
    ROLLUP_SCHEMA,
    Change,
    Fact,
    check_changes,
    due,
    expiry,
    failed,
    pass_input,
    plan,
    rolled,
    taken,
    waiting,
)
from dinnerbell.domain.members import Member
from dinnerbell.services.assistant import housemates
from dinnerbell.services.assistant.conversation import (
    Assistant,
    call_outside_message,
    least_private_call_micros,
    request_bytes,
)
from dinnerbell.services.assistant.runtime import AssistantConfig, available
from dinnerbell.services.errors import NotFound

log = logging.getLogger(__name__)

ROLLUP_SECONDS = 60.0
"""The most one member's rollup call runs, from the purge job."""
START_OVER_SECONDS = 10.0
"""The most **Start over**'s rollup call runs, since the member's page waits for it."""
ROLLUP_MAX_TOKENS = 4_000
"""Each rollup call's ``max_tokens``: room for 20 facts' changes, and any thinking."""
NOT_HERE = "That page doesn't exist."


def rollup_request(model: str, text: str) -> dict[str, Any]:
    """One rollup call, as the Messages API's keyword arguments: the fixed prompt, one member's
    part of the pass, and the answer's JSON schema. No tools, no roster, no thinking asked for."""
    return {
        "model": model,
        "max_tokens": ROLLUP_MAX_TOKENS,
        "system": ROLLUP_PROMPT,
        "messages": [{"role": "user", "content": text}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": dict(ROLLUP_SCHEMA)}},
    }


def _answer(content: Sequence[Any]) -> object:
    """The rollup's JSON answer, from its text blocks joined, or None when it isn't JSON."""
    text = "".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    try:
        return json.loads(text)
    except ValueError:
        log.warning("assistant memory rollup answered without JSON")
        return None


@dataclass
class Pass:
    """What one run rolled up: members whose facts it changed, and messages that wait still."""

    changed: int = 0
    waiting: int = 0


async def roll_up(
    db: AsyncSession,
    assistant: Assistant,
    household_id: int,
    key: str,
    *,
    last: bool = False,
    seconds: float = ROLLUP_SECONDS,
) -> Pass:
    """One pass over the conversation ``key``: each member's waiting messages it takes, one call
    for each member, each within ``seconds``. With ``last``, its last try before it's deleted, it
    takes them whatever a failure put off. Idempotent: a message rolled up stops waiting, so a
    second run finds nothing. A failed call puts its messages off, and after ``MAX_TRIES``
    failures they stop waiting (``domain.assistant_memory.failed``), so a call that keeps failing
    on the same messages, as a cut-off or refused answer does, never retries without end."""
    result = Pass()
    settings = assistant_settings(await household_settings(db, household_id))
    stored = await AssistantRepository(db).conversation(household_id, key)
    if not assistant.config.configured or not settings.remembers or stored is None:
        return result
    household = await HouseholdRepository(db).get(household_id)
    if household is None:  # pragma: no cover - conversations cascade with their household
        return result
    by_label = {f"Member {n}": member_id for member_id, n in stored.pseudonyms.numbers.items()}
    now = assistant.clock()
    batch = taken(stored) if last else taken(stored, now)
    for author in dict.fromkeys(t.author for t in batch if t.author is not None):
        mine = [t for t in batch if t.author == author]
        member = await MemberRepository(db).get(household_id, by_label.get(author, 0))
        if member is None or not member.is_member:
            await _done(db, household_id, key, mine, None, (), (), assistant.clock())  # nobody to remember them for
            continue
        zone = ZoneInfo(household.timezone)
        if await _one(db, assistant, member, zone, stored, author, mine, settings.rollup_model, seconds):
            result.changed += 1
        else:
            result.waiting += len(mine)
    return result


async def _one(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    zone: ZoneInfo,
    stored: Conversation,
    author: str,
    mine: Sequence[Turn],
    model: str,
    seconds: float,
) -> bool:
    """Roll up ``member``'s messages ``mine``. Returns whether they're done."""
    household_id = member.household_id
    now = assistant.clock()
    facts = await MemoryRepository(db).facts(household_id, now, member.id)
    held = json.dumps(await housemates.summary(db, member), ensure_ascii=False)
    part = pass_input(stored.turns, author, mine, facts, held, replies=stored.private)
    request = rollup_request(model, part.text)
    if not await _room(db, assistant, member, zone, request):
        log.info("assistant memory waits for room under the cap", extra={"member_id": member.id})
        return False
    reply = await call_outside_message(db, assistant, member, zone, request, seconds=seconds)
    if reply is None or reply.stop_reason not in ("end_turn", "stop_sequence"):
        log.warning("assistant memory rollup failed", extra={"member_id": member.id})
        await _failed(db, household_id, stored.key, mine, member, assistant.clock())
        return False
    checked = check_changes(_answer(reply.content), part.messages, facts)
    if checked.dropped:
        log.info("assistant memory changes dropped", extra={"member_id": member.id, **checked.dropped})
    done = await _done(db, household_id, stored.key, mine, member, facts, checked.changes, assistant.clock())
    if done:
        log.info("assistant memory rolled up", extra={"member_id": member.id, "changes": len(checked.changes)})
    return done


async def _room(db: AsyncSession, assistant: Assistant, member: Member, zone: ZoneInfo, request: Any) -> bool:
    """Whether what's left this month covers this call and the smallest private message too, so
    a pass never takes the last of the cap from a member who's asking."""
    reserved = reservation_micros(
        str(request["model"]), payload_bytes=request_bytes(request), max_tokens=int(request["max_tokens"])
    )
    settings = assistant_settings(await household_settings(db, member.household_id))
    members = await MemberRepository(db).list(member.household_id)
    least = least_private_call_micros(assistant.config.model, members, memory=True)
    spent = await AssistantRepository(db).month_total(member.household_id, month_key(assistant.clock(), zone))
    return spent + reserved + least <= settings.cap_micros


async def _done(
    db: AsyncSession,
    household_id: int,
    key: str,
    mine: Sequence[Turn],
    member: Member | None,
    facts: Sequence[Fact],
    changes: Sequence[Change],
    now: datetime,
) -> bool:
    """Under the write lock: apply ``changes`` to ``member``'s facts, if the switches are on,
    they're active, and their facts are still ``facts``, and mark ``mine`` rolled up. With no
    ``member``, only mark them. Returns whether it did."""
    repo, memory = AssistantRepository(db), MemoryRepository(db)
    async with write_transaction(db):
        settings = assistant_settings(await household_settings(db, household_id))
        stored = await repo.conversation(household_id, key)
        if not settings.remembers or stored is None:
            return False
        if member is not None:
            fresh = await MemberRepository(db).get(household_id, member.id)
            if fresh is None or not fresh.is_member:
                return False
            current = await memory.facts(household_id, now, member.id)
            if [(f.id, f.kind, f.text) for f in current] != [(f.id, f.kind, f.text) for f in facts]:
                log.info("assistant memory changed during a rollup; it waits", extra={"member_id": member.id})
                return False
            applied = plan(current, changes, now)
            await memory.delete(household_id, applied.removed, member_id=member.id)
            for fact_id, (kind, text) in applied.replaced.items():
                await memory.replace(
                    household_id, fact_id, kind=kind, text=text, source_key=key, now=now, expires_at=expiry(kind, now)
                )
            for kind, text in applied.added:
                await memory.add(
                    household_id, member.id, kind=kind, text=text, source_key=key, now=now, expires_at=expiry(kind, now)
                )
        kept = Conversation(
            key=stored.key,
            private=stored.private,
            owner_member_id=stored.owner_member_id,
            pseudonyms=stored.pseudonyms,
            turns=rolled(stored.turns, mine),
            last_message_at=stored.last_message_at,
        )
        await repo.save_conversation(household_id, kept, now)
        await db.commit()
    return True


async def _failed(
    db: AsyncSession, household_id: int, key: str, mine: Sequence[Turn], member: Member, now: datetime
) -> None:
    """Under the write lock: count a failed try on ``mine`` and put them off, or stop them waiting
    after ``MAX_TRIES``, logging how many."""
    repo = AssistantRepository(db)
    async with write_transaction(db):
        stored = await repo.conversation(household_id, key)
        if stored is None:
            return
        turns, given_up = failed(stored.turns, mine, now)
        kept = Conversation(
            key=stored.key,
            private=stored.private,
            owner_member_id=stored.owner_member_id,
            pseudonyms=stored.pseudonyms,
            turns=turns,
            last_message_at=stored.last_message_at,
        )
        await repo.save_conversation(household_id, kept, now)
        await db.commit()
    if given_up:
        log.warning(
            "assistant memory messages given up after failed rollups", extra={"member_id": member.id, "count": given_up}
        )


async def roll_up_due(sessionmaker: async_sessionmaker[AsyncSession], assistant: Assistant | None) -> int:
    """For the purge job, before it deletes anything: roll up every conversation that's due
    (``domain.assistant_memory.due``), in every household with **Memory** on. A conversation past
    Conversation hours gets this one last try and is deleted after it either way. Idempotent.
    Returns how many members' facts it changed."""
    if assistant is None or not assistant.config.configured:
        return 0
    async with sessionmaker() as db:
        households = await HouseholdRepository(db).all()
    changed = 0
    for household in households:
        async with sessionmaker() as db:
            settings = assistant_settings(await household_settings(db, household.id))
            if not settings.remembers:
                continue
            now = assistant.clock()
            for conversation in await AssistantRepository(db).conversations(household.id):
                if not due(conversation, now, settings.conversation_hours):
                    continue
                last = conversation.expired(now, settings.conversation_hours)
                result = await roll_up(db, assistant, household.id, conversation.key, last=last)
                changed += result.changed
                if result.waiting and conversation.expired(now, settings.conversation_hours):
                    log.warning("assistant memory messages deleted before the rollup", extra={"count": result.waiting})
    return changed


async def roll_up_before_start_over(db: AsyncSession, assistant: Assistant, member: Member, key: str) -> None:
    """**Start over** saves to memory first (#367): roll up the member's waiting messages in
    ``key``, when Memory is on and any wait. A failure is logged, and the conversation is
    deleted anyway."""
    stored = await AssistantRepository(db).conversation(member.household_id, key)
    settings = assistant_settings(await household_settings(db, member.household_id))
    if stored is None or not settings.remembers or not waiting(stored):
        return
    result = await roll_up(db, assistant, member.household_id, key, last=True, seconds=START_OVER_SECONDS)
    if result.waiting:
        log.warning("assistant memory messages deleted before the rollup", extra={"count": result.waiting})


async def purge_expired_facts(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Delete every household's facts that expired by ``now``, whether Memory is on or not.
    Idempotent. Returns how many."""
    async with sessionmaker() as db:
        households = await HouseholdRepository(db).all()
    removed = 0
    for household in households:
        async with sessionmaker() as db:
            memory = MemoryRepository(db)
            if not await memory.any_expired(household.id, now):
                continue
            async with write_transaction(db):
                removed += await memory.delete_expired(household.id, now)
                await db.commit()
    return removed


# What a member sees ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MyMemory:
    """The Assistant page's **What the assistant remembers**: the member's own facts, newest
    first, and whether Memory is on."""

    facts: tuple[Fact, ...]
    on: bool


async def my_memory(db: AsyncSession, config: AssistantConfig, member: Member, now: datetime) -> MyMemory:
    """``member``'s own facts, never anyone else's. NotFound while the assistant doesn't answer
    them, as on every Assistant route."""
    if not await available(db, config, member):
        raise NotFound(NOT_HERE)
    settings = assistant_settings(await household_settings(db, member.household_id))
    facts = await MemoryRepository(db).facts(member.household_id, now, member.id)
    return MyMemory(tuple(sorted(facts, key=lambda f: (f.written_at, f.id), reverse=True)), settings.memory)


async def remove_my_fact(db: AsyncSession, config: AssistantConfig, member: Member, fact_id: int) -> bool:
    """**Remove** one of ``member``'s own facts. Returns whether it removed one: a fact that's
    gone, or someone else's, is left alone. Not audited, as a member's own Preferences aren't."""
    if not await available(db, config, member):
        raise NotFound(NOT_HERE)
    async with write_transaction(db):
        removed = await MemoryRepository(db).delete(member.household_id, [fact_id], member_id=member.id)
        await db.commit()
    if removed:
        log.info("assistant memory fact removed by its member", extra={"member_id": member.id})
    return removed > 0
