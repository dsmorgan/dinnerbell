# SPDX-License-Identifier: AGPL-3.0-or-later
"""``converse``: answer one member's message (spec 6.14).

Each message runs a bounded loop of model calls and tool calls. Before every model call,
Dinnerbell reserves the most that call can cost, in a transaction that refuses it if the
month's spending plus reservations would pass the household's cap; after the call, it settles
the reservation to what the API reported, cache tokens included. Nothing waits on the model
while holding the write lock.

What goes out is the system prompt, the conversation in pseudonym form ("Member 1"), and what
tools return. Names come back only in the reply text the member sees.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.assistant import (
    PRICE_TABLE,
    Conversation,
    Pseudonyms,
    TokenUsage,
    Turn,
    assistant_settings,
    check_key,
    known_model,
    month_key,
    recent_turns,
    reservation_micros,
    total_cost_micros,
)
from dinnerbell.domain.members import Member
from dinnerbell.services.assistant.client import ModelCallFailed, ModelClient, ModelReply
from dinnerbell.services.assistant.prompt import system_prompt
from dinnerbell.services.assistant.runtime import AssistantConfig, available
from dinnerbell.services.assistant.tools import Tool, run_tool, tools_for
from dinnerbell.services.clock import Clock
from dinnerbell.services.errors import Forbidden, NotFound

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
FALLBACK_BETA = "server-side-fallback-2026-07-01"
"""Anthropic's server-side refusal fallback, in its ``"default"`` form (Claude API only)."""

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


@dataclass(frozen=True, slots=True)
class Reply:
    """The reply to show the member, with names put back. ``drafts`` come in a later issue."""

    text: str
    drafts: tuple[object, ...] = ()


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
    card = PRICE_TABLE.get(model)
    if card is not None and card.falls_back_to:
        request["fallbacks"] = "default"
        request["betas"] = [FALLBACK_BETA]
    return request


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


async def _call(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    zone: ZoneInfo,
    request: Mapping[str, Any],
    *,
    earlier_output_tokens: int = 0,
) -> ModelReply:
    """Reserve, call, and settle one request. Raises ``_Refused`` when the call can't run.
    ``earlier_output_tokens`` is what this message's earlier calls wrote: their thinking goes
    back in this request as signed blocks and bills as input, though the bytes don't show it."""
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

    try:
        reply = await assistant.client.create(request)
    except ModelCallFailed as exc:
        if exc.maybe_billed:
            await keep_reservation()
        else:
            async with write_transaction(db):
                await repo.release(household_id, usage_id)
                await db.commit()
        log.warning("assistant model call failed", extra={"member_id": member.id, **exc.log_fields()})
        raise _Refused(FAILED) from None
    except asyncio.CancelledError:
        await keep_reservation()  # the request may be running still; never leave it reserved
        log.error("assistant model call cancelled", extra={"member_id": member.id})
        raise
    except Exception:
        await keep_reservation()
        log.exception("assistant model call broke", extra={"member_id": member.id})
        raise _Refused(FAILED) from None
    for part in reply.usage:
        if not known_model(part.model):
            # Priced at the ceiling, which holds the cap; the table needs the model's prices.
            log.error(
                "assistant model not in the price table", extra={"model": part.model[:64], "member_id": member.id}
            )
    cost = total_cost_micros(reply.usage)
    async with write_transaction(db):
        await repo.settle(household_id, usage_id, _combined(reply.model, reply.usage), cost, assistant.clock())
        await db.commit()
    if cost > reserved:  # pragma: no cover - the bound makes this impossible; say so if it ever isn't
        log.error("assistant call cost more than its reservation", extra={"member_id": member.id})
    return reply


async def _keep(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    key: str,
    private: bool,
    hours: int,
    sent: Pseudonyms,
    asked: Turn,
    answered: Turn,
) -> None:
    """Add one exchange to the stored conversation. It reads the conversation again under the
    write lock, so an exchange saved by another message in the same thread meanwhile is kept,
    and this one goes after it. A member that one labeled first keeps that label, and anyone
    new gets the next number; the exchange's labels are rewritten to match."""
    household_id = member.household_id
    repo = AssistantRepository(db)
    async with write_transaction(db):
        if hours == 0:
            await repo.delete_conversation(household_id, key)
            await db.commit()
            return
        now = assistant.clock()
        latest = await repo.conversation(household_id, key)
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
) -> tuple[str, bool]:
    """The model and tool loop. Returns the reply text, and whether the model answered (so the
    exchange is worth keeping) rather than Dinnerbell replying for it."""
    tools = tools_for(private)
    written = 0  # output tokens of this message's calls so far
    for _ in range(MAX_MODEL_CALLS):
        request = build_request(assistant.config.model, tools, messages, keeps_history=keeps_history)
        try:
            reply = await _call(db, assistant, member, zone, request, earlier_output_tokens=written)
        except _Refused as refused:
            return refused.reply, False
        written += sum(part.output_tokens for part in reply.usage)
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
            )
            entry: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.get("id"), "content": result.content}
            if result.is_error:
                entry["is_error"] = True
            results.append(entry)
        messages.append({"role": "user", "content": results})  # every result in one message
    return TOO_MANY_STEPS, False


async def converse(
    db: AsyncSession, assistant: Assistant, member: Member, key: str, *, private: bool, text: str
) -> Reply | None:
    """Answer ``member``'s ``text`` in the conversation ``key`` (``domain.assistant.web_key`` or
    ``slack_key``). ``private`` is True for the web page and DMs, False for a channel thread.

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
    answer, answered = await _run_loop(
        db, assistant, member, zone, _messages(sent, current), private=private, keeps_history=hours > 0
    )
    if answered:
        await _keep(db, assistant, member, key, private, hours, pseudonyms, current, Turn("assistant", answer))
    names = {m.id: m.display_name for m in await MemberRepository(db).list(household.id)}
    log.info("assistant replied", extra={"member_id": member.id, "answered": answered})
    return Reply(pseudonyms.restore_names(answer, names))
