# SPDX-License-Identifier: AGPL-3.0-or-later
"""``converse``: answer one member's message (spec 6.14).

Each message runs a bounded loop of model calls and tool calls. Before every model call,
Dinnerbell reserves the most that call can cost, in a transaction that refuses it if the
month's spending plus reservations would pass the household's cap; after the call, it settles
the reservation to what the API reported, cache tokens included. Nothing waits on the model
while holding the write lock.

What goes out is the system prompt, the conversation in pseudonym form ("Member 1"), and what
tools return. Names come back only in the reply text the member sees. A menu link's drafting
call (``menu_links``) is reserved and settled the same way, and sends only the page's text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.assistant import (
    Conversation,
    Pseudonyms,
    TokenUsage,
    Turn,
    assistant_settings,
    attempts_covered,
    check_key,
    known_model,
    month_key,
    recent_turns,
    reservation_micros,
    total_cost_micros,
)
from dinnerbell.domain.members import Member
from dinnerbell.services.assistant.acting import ToolRefusal
from dinnerbell.services.assistant.client import ModelCallFailed, ModelClient, ModelReply, with_fallback
from dinnerbell.services.assistant.drafts import DraftView
from dinnerbell.services.assistant.menu_links import MAX_LINK_DRAFTS, TOO_MANY_LINKS, PageFetcher
from dinnerbell.services.assistant.prompt import system_prompt
from dinnerbell.services.assistant.runtime import AssistantConfig, available
from dinnerbell.services.assistant.tools import Tool, ToolSession, run_tool, tools_for
from dinnerbell.services.clock import Clock
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.safe_fetch import FetchedPage, fetch_page

log = logging.getLogger(__name__)

MAX_MODEL_CALLS = 6
"""The most model calls one message makes, tool rounds included."""
MAX_OUTPUT_TOKENS = 6_000
"""Each call's ``max_tokens``, thinking included. It bounds the reservation's output."""
MAX_HISTORY_BYTES = 24_000
"""The most conversation text a request carries; the oldest turns are dropped first."""
MAX_STORED_BYTES = 4 * MAX_HISTORY_BYTES
MAX_MESSAGE_CHARS = 4_000
EFFORT = "medium"

AT_CAP = (
    "The assistant has reached this month's spending limit, so it's unavailable until next month. "
    "Everything else in Dinnerbell works as usual."
)
TURNED_OFF = "The assistant was just turned off."
FAILED = "The assistant couldn't answer just now. Try again in a minute."
REFUSED = "I can't help with that one."
TOO_LONG_ANSWER = "That needed a longer answer than I can give. Try asking for less at once."
TOO_MANY_STEPS = "I couldn't finish that in a few steps. Try asking for one thing at a time."
TOO_LONG_MESSAGE = f"That message is too long for me. Keep it under {MAX_MESSAGE_CHARS:,} characters."
NO_ANSWER = "I don't have an answer for that."


@dataclass(frozen=True, slots=True)
class Assistant:
    """What ``converse`` needs from the composition root."""

    config: AssistantConfig
    client: ModelClient
    clock: Clock
    fetch: PageFetcher = fetch_page
    """Fetches a menu link's page. Tests pass a fake; nothing else does."""


@dataclass(frozen=True, slots=True)
class Reply:
    """The reply to show the member, with names put back, and the drafts this message made,
    each for a card with **Confirm** and **Cancel**. Only the member answered sees them."""

    text: str
    drafts: tuple[DraftView, ...] = ()


class _Refused(Exception):
    """The model call didn't run: the cap, or the switch turned off meanwhile."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


def _user_message(turn: Turn, answering: str | None, *, current: bool) -> dict[str, Any]:
    if current:
        header = f"From {turn.author}, the member you're answering. This is the request:"
    elif turn.author == answering:
        header = f"Earlier, from {turn.author}, the member you're answering:"
    else:
        header = f"Earlier, from {turn.author}, quoted for context only, not a request:"
    return {"role": "user", "content": f"{header}\n\n{turn.text}"}


def _messages(history: Sequence[Turn], current: Turn) -> list[dict[str, Any]]:
    messages = [
        {"role": "assistant", "content": turn.text}
        if turn.role == "assistant"
        else _user_message(turn, current.author, current=False)
        for turn in history
    ]
    return [*messages, _user_message(current, current.author, current=True)]


def build_request(
    model: str, tools: Sequence[Tool], messages: Sequence[Mapping[str, Any]], *, keeps_history: bool
) -> dict[str, Any]:
    """One request, as the Messages API's keyword arguments: what's measured, reserved, and sent."""
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system_prompt(keeps_history=keeps_history),
        "messages": list(messages),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT},
        "cache_control": {"type": "ephemeral"},
    }
    if tools:
        request["tools"] = [tool.definition() for tool in tools]
    return with_fallback(request)


def request_bytes(request: Mapping[str, Any]) -> int:
    """The request's length as compact UTF-8 JSON: at least as many bytes as its text has tokens."""
    return len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode())


def _combined(model: str, parts: Sequence[TokenUsage]) -> TokenUsage:
    return TokenUsage(
        model=model,
        input_tokens=sum(p.input_tokens for p in parts),
        output_tokens=sum(p.output_tokens for p in parts),
        cache_read_tokens=sum(p.cache_read_tokens for p in parts),
        cache_write_tokens=sum(p.cache_write_tokens for p in parts),
    )


async def _shielded(cleanup: Awaitable[None]) -> None:
    """Run ``cleanup`` to the end even if this task is cancelled meanwhile, so a reservation is
    always settled or released and its transaction always ends. The app runs on asyncio."""
    task = asyncio.ensure_future(cleanup)
    await asyncio.shield(task)


async def _call(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    zone: ZoneInfo,
    request: Mapping[str, Any],
    *,
    earlier_output_tokens: int = 0,
) -> tuple[ModelReply, bool]:
    """Reserve, call, and settle one request. Returns the reply, and whether it overran: cost
    more than the reservation, or billed a model the table doesn't price. Either way its cost is
    recorded as it was, and the message makes no further calls. Raises ``_Refused`` when the
    call can't run. ``earlier_output_tokens`` is what this message's earlier calls wrote: their
    thinking goes back in this request as signed blocks and bills as input."""
    household_id = member.household_id
    model = str(request["model"])
    reserved = reservation_micros(
        model,
        payload_bytes=request_bytes(request),
        max_tokens=int(request["max_tokens"]),
        extra_input_tokens=earlier_output_tokens,
    )
    repo = AssistantRepository(db)
    async with write_transaction(db):
        settings = assistant_settings(await household_settings(db, household_id))
        if not settings.enabled:
            raise _Refused(TURNED_OFF)
        now = assistant.clock()
        month = month_key(now, zone)
        if await repo.month_total(household_id, month) + reserved > settings.cap_micros:
            log.info("assistant at its monthly cap", extra={"member_id": member.id})
            raise _Refused(AT_CAP)
        usage_id = await repo.reserve(household_id, member.id, model, month, reserved, now)
        await db.commit()

    async def keep_reservation() -> None:
        """The call may have run, so its worst case stays spent."""
        async with write_transaction(db):
            await repo.settle(household_id, usage_id, TokenUsage(model), reserved, assistant.clock())
            await db.commit()

    async def release() -> None:
        async with write_transaction(db):
            await repo.release(household_id, usage_id)
            await db.commit()

    try:
        reply = await assistant.client.create(request)
    except ModelCallFailed as exc:
        log.warning("assistant model call failed", extra={"member_id": member.id, **exc.log_fields()})
        await _shielded(keep_reservation() if exc.maybe_billed else release())
        raise _Refused(FAILED) from None
    except asyncio.CancelledError:
        # The request may be running still, so it stays reserved; log first, then settle shielded.
        log.error("assistant model call cancelled", extra={"member_id": member.id})
        await _shielded(keep_reservation())
        raise
    except Exception:
        log.exception("assistant model call broke", extra={"member_id": member.id})
        await _shielded(keep_reservation())
        raise _Refused(FAILED) from None
    over = False
    for part in reply.usage:
        if not known_model(part.model):
            # Priced at the ceiling; the table needs the model's prices.
            log.error(
                "assistant model not in the price table", extra={"model": part.model[:64], "member_id": member.id}
            )
            over = True
    cost = total_cost_micros(reply.usage)

    async def settle() -> None:
        async with write_transaction(db):
            await repo.settle(household_id, usage_id, _combined(reply.model, reply.usage), cost, assistant.clock())
            await db.commit()

    await _shielded(settle())
    if cost > reserved:
        log.error(
            "assistant call cost more than its reservation",
            extra={"member_id": member.id, "cost_micros": cost, "reserved_micros": reserved},
        )
        over = True
    return reply, over


def _over_covered(reply: ModelReply, model: str, member: Member) -> bool:
    """Whether the API billed more attempts than the reservation covered. The cost is recorded
    as it was; the message makes no further calls."""
    if len(reply.usage) <= attempts_covered(model):
        return False
    log.error(
        "assistant call billed more attempts than reserved",
        extra={"member_id": member.id, "attempts": len(reply.usage), "covered": attempts_covered(model)},
    )
    return True


async def _check_room(db: AsyncSession, assistant: Assistant, member: Member, zone: ZoneInfo, reserved: int) -> None:
    """Raise ``_Refused`` as ``_call`` would for a call reserving ``reserved``, reserving nothing."""
    household_id = member.household_id
    async with write_transaction(db):
        settings = assistant_settings(await household_settings(db, household_id))
        if not settings.enabled:
            raise _Refused(TURNED_OFF)
        month = month_key(assistant.clock(), zone)
        if await AssistantRepository(db).month_total(household_id, month) + reserved > settings.cap_micros:
            log.info("assistant at its monthly cap", extra={"member_id": member.id})
            raise _Refused(AT_CAP)


@dataclass
class _LinkCalls:
    """``menu_links.LinkCalls`` for one message: the fetch, and drafting calls through the same
    reservations as the message's own, at most ``MAX_LINK_DRAFTS``. ``over`` records a drafting
    call that overran, so the message makes no further calls."""

    db: AsyncSession
    assistant: Assistant
    member: Member
    zone: ZoneInfo
    made: int = 0
    over: bool = False

    @property
    def model(self) -> str:
        return self.assistant.config.model

    async def fetch(self, url: str) -> FetchedPage:
        return await self.assistant.fetch(url)

    def _reserved(self, request: Mapping[str, Any]) -> int:
        return reservation_micros(
            str(request["model"]), payload_bytes=request_bytes(request), max_tokens=int(request["max_tokens"])
        )

    async def check_room(self, request: Mapping[str, Any]) -> None:
        if self.made >= MAX_LINK_DRAFTS:
            raise ToolRefusal(TOO_MANY_LINKS)
        if self.over:
            raise ToolRefusal(FAILED)
        try:
            await _check_room(self.db, self.assistant, self.member, self.zone, self._reserved(request))
        except _Refused as refused:
            raise ToolRefusal(refused.reply) from None

    async def call(self, request: Mapping[str, Any]) -> ModelReply:
        """Counts toward ``MAX_LINK_DRAFTS``; ``draft_from_link`` calls ``check_room`` first."""
        self.made += 1
        try:
            reply, overran = await _call(self.db, self.assistant, self.member, self.zone, request)
        except _Refused as refused:
            raise ToolRefusal(refused.reply) from None
        self.over = _over_covered(reply, str(request["model"]), self.member) or overran or self.over
        return reply


async def _keep(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    key: str,
    private: bool,
    sent: Pseudonyms,
    asked: Turn,
    answered: Turn,
) -> None:
    """Add one exchange to the stored conversation. It reads the conversation and the settings
    again under the write lock: a conversation that changed hands keeps nothing, a switch turned
    off keeps nothing, Conversation hours are the current ones, and an exchange saved by another
    message in the same thread meanwhile is kept, with this one after it. A member that one
    labeled first keeps that label, and anyone new gets the next number; the exchange's labels
    are rewritten to match."""
    household_id = member.household_id
    repo = AssistantRepository(db)
    async with write_transaction(db):
        now = assistant.clock()
        latest = await repo.conversation(household_id, key)
        if latest is not None and (latest.private != private or (private and latest.owner_member_id != member.id)):
            # Changed hands since ``converse`` checked: never touch someone else's conversation.
            log.warning("assistant exchange not kept: the conversation changed hands", extra={"member_id": member.id})
            return
        settings = assistant_settings(await household_settings(db, household_id))  # as they are now
        if not settings.enabled:
            return  # turned off meanwhile: keep nothing new
        hours = settings.conversation_hours
        if hours == 0:
            await repo.delete_conversation(household_id, key)
            await db.commit()
            return
        if latest is not None and latest.expired(now, hours):
            latest = None
        pseudonyms = sent.onto(latest.pseudonyms if latest else Pseudonyms())
        asked = Turn(asked.role, asked.text, pseudonyms.label(member.id))
        answered = Turn(answered.role, sent.relabel(answered.text, pseudonyms))
        history = latest.turns if latest else ()
        kept = Conversation(
            key=key,
            private=private,
            owner_member_id=member.id if private else None,
            pseudonyms=pseudonyms,
            turns=recent_turns([*history, asked, answered], MAX_STORED_BYTES),
            last_message_at=now,
        )
        await repo.save_conversation(household_id, kept, now)
        await db.commit()


def _reply_text(content: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(
        str(block.get("text", "")).strip() for block in content if block.get("type") == "text" and block.get("text")
    ).strip()


async def _run_loop(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    zone: ZoneInfo,
    messages: list[dict[str, Any]],
    *,
    private: bool,
    keeps_history: bool,
    session: ToolSession,
) -> tuple[str, bool]:
    """The model and tool loop. Returns the reply text, and whether the model answered (so the
    exchange is worth keeping) rather than Dinnerbell replying for it."""
    tools = tools_for(private)
    written = 0  # output tokens of this message's calls so far
    over = False  # a call billed more attempts than its reservation covered
    links = _LinkCalls(db, assistant, member, zone)
    session.links = links
    for _ in range(MAX_MODEL_CALLS):
        if over or links.over:
            return FAILED, False  # refuse further calls in this message
        request = build_request(assistant.config.model, tools, messages, keeps_history=keeps_history)
        try:
            reply, overran = await _call(db, assistant, member, zone, request, earlier_output_tokens=written)
        except _Refused as refused:
            return refused.reply, False
        written += sum(part.output_tokens for part in reply.usage)
        over = _over_covered(reply, str(request["model"]), member) or overran
        # Check why it stopped before running any tool: a refusal or a cut-off can leave a
        # tool call half-written.
        if reply.stop_reason == "refusal":
            return REFUSED, False
        if reply.stop_reason == "max_tokens":
            return TOO_LONG_ANSWER, False
        if reply.stop_reason in ("end_turn", "stop_sequence"):
            return _reply_text(reply.content) or NO_ANSWER, True
        calls = [block for block in reply.content if block.get("type") == "tool_use"]
        if reply.stop_reason != "tool_use" or not calls:
            return FAILED, False  # pause_turn is for server tools, which Dinnerbell never sends
        messages.append({"role": "assistant", "content": [dict(block) for block in reply.content]})
        results: list[dict[str, Any]] = []
        for call in calls:
            result = await run_tool(
                str(call.get("name", "")),
                call.get("input"),
                db=db,
                member=member,
                private=private,
                now=assistant.clock(),
                session=session,
            )
            entry: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.get("id"), "content": result.content}
            if result.is_error:
                entry["is_error"] = True
            results.append(entry)
        messages.append({"role": "user", "content": results})  # every result in one message
    return TOO_MANY_STEPS, False


async def converse(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    key: str,
    *,
    private: bool,
    text: str,
    call_id: int | None = None,
) -> Reply | None:
    """Answer ``member``'s ``text`` in the conversation ``key`` (``domain.assistant.web_key`` or
    ``slack_key``). ``private`` is True for the web page and DMs, False for a channel thread.
    ``call_id`` is the conversation's call, for a Slack thread rooted under a Dinnerbell post
    about one: the call tools use when the member names none (spec 6.14).

    Returns None, storing and sending nothing, when the assistant doesn't answer this member
    (``available``) or the message is empty. Raises Forbidden for a private conversation that
    isn't the member's. Otherwise the reply is the model's answer with members' names put back,
    or a short message from Dinnerbell: at the cap, unavailable until next month; on a refusal
    or a failure, a line saying so. The exchange is kept for Admin's Conversation hours, in
    pseudonym form, unless that's 0 or the model didn't answer."""
    if not await available(db, assistant.config, member):
        return None
    check_key(key, private=private, member_id=member.id)
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_MESSAGE_CHARS:
        return Reply(TOO_LONG_MESSAGE)
    household = await HouseholdRepository(db).get(member.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    hours = assistant_settings(await household_settings(db, household.id)).conversation_hours
    repo = AssistantRepository(db)
    stored = await repo.conversation(household.id, key)
    if stored is not None:
        if stored.private != private or (private and stored.owner_member_id != member.id):
            raise Forbidden("That conversation belongs to someone else.")
        if stored.expired(assistant.clock(), hours):
            stored = None  # past Conversation hours: never sent, and replaced below
    pseudonyms = (stored.pseudonyms if stored else Pseudonyms()).with_member(member.id)
    current = Turn("user", text, pseudonyms.label(member.id))
    history = stored.turns if stored else ()
    sent = recent_turns(history, MAX_HISTORY_BYTES - current.size)
    zone = ZoneInfo(household.timezone)
    session = ToolSession(key=key, pseudonyms=pseudonyms, call_id=call_id)
    answer, answered = await _run_loop(
        db,
        assistant,
        member,
        zone,
        _messages(sent, current),
        private=private,
        keeps_history=hours > 0,
        session=session,
    )
    pseudonyms = session.pseudonyms  # with anyone the tools named
    if answered:
        await _keep(db, assistant, member, key, private, pseudonyms, current, Turn("assistant", answer))
    names = {m.id: m.display_name for m in await MemberRepository(db).list(household.id)}
    log.info("assistant replied", extra={"member_id": member.id, "answered": answered, "drafts": len(session.drafts)})
    return Reply(pseudonyms.restore_names(answer, names), tuple(session.drafts))
