# SPDX-License-Identifier: AGPL-3.0-or-later
"""``converse``: answer one member's message (spec 6.14).

Each message runs a bounded loop of model calls and tool calls. Before every model call,
Dinnerbell reserves the most that call can cost, in a transaction that refuses it if the
month's spending plus reservations would pass the household's cap; after the call, it settles
the reservation to what the API reported, cache tokens included. Nothing waits on the model
while holding the write lock.

A message has ``MESSAGE_SECONDS`` in all, under the web page's wait for a reply, so a member who
gives up and sends it again isn't charged for an answer still running. Each model call, each
tool, and each menu link's fetch with its text extraction get only the time left, and are
cancelled when it runs out; a model call's reservation stays spent, since it may have run. Once
the time is up, the message makes no further calls and runs no further tools, and nothing of it
is kept. Only the database work around them is outside the deadline: reading the conversation
before the first call, settling a reservation, and the reply's few writes after the last.

What goes out is the system prompt, the conversation in pseudonym form ("Member 1"), and what
tools return. Names come back only in the reply text the member sees. A menu link's drafting
call (``menu_links``) is reserved and settled the same way, and sends only the page's text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from dinnerbell.domain.assistant_drafts import MAX_OPEN_DRAFTS
from dinnerbell.domain.members import Member
from dinnerbell.services.assistant.acting import ToolRefusal
from dinnerbell.services.assistant.client import ModelCallFailed, ModelClient, ModelReply, with_fallback
from dinnerbell.services.assistant.drafts import TOO_MANY as TOO_MANY_DRAFTS
from dinnerbell.services.assistant.drafts import DraftView
from dinnerbell.services.assistant.menu_links import CAP_TOO_LOW, MAX_LINK_FETCHES, TOO_MANY_LINKS, PageFetcher
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
MESSAGE_SECONDS = 100.0
"""The most one message runs, its model calls, tools, and menu links together: under the
120 seconds the web page waits (``web.routes.assistant_page.SEND_TIMEOUT_MS``)."""

AT_CAP = (
    "The assistant has reached this month's spending limit, so it's unavailable until next month. "
    "Everything else in Dinnerbell works as usual."
)
CAP_BELOW_ONE_CALL = (
    "The assistant's monthly spending limit is too low for this model to answer a request this size, so waiting "
    "until next month won't help. Ask an admin to raise the cap on Admin > Assistant. Everything else in "
    "Dinnerbell works as usual."
)
"""When one call's reservation is more than the household's whole cap, as with a $1 cap and a
model whose worst case is more: ``AT_CAP`` would be false, since nothing was spent."""
TURNED_OFF = "The assistant was just turned off."
FAILED = "The assistant couldn't answer just now. Try again in a minute."
REFUSED = "I can't help with that one."
TOO_LONG_ANSWER = "That needed a longer answer than I can give. Try asking for less at once."
TOO_MANY_STEPS = "I couldn't finish that in a few steps. Try asking for one thing at a time."
TOO_LONG_MESSAGE = f"That message is too long for me. Keep it under {MAX_MESSAGE_CHARS:,} characters."
NO_ANSWER = "I don't have an answer for that."
OUT_OF_TIME = "I ran out of time on that one. Try asking for less at once."


NamingText = Callable[[Callable[[int], str]], str]
"""A message that names members, such as a Slack message's mentions: given the conversation's
labeler (a member ID to their pseudonym, "Member 2", added to the map if they're new), its text
with each member named by label. So the meaning survives, and no Slack ID or name is sent."""


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


def least_private_call_micros(model: str) -> int:
    """What the smallest private message's first call reserves with ``model``: an empty message,
    no history, and every tool a private conversation offers, by ``_call``'s own reservation. A
    cap below it can't answer any private message (``CAP_BELOW_ONE_CALL``)."""
    request = build_request(model, tools_for(True), _messages((), Turn("user", "", "Member 1")), keeps_history=True)
    return reservation_micros(model, payload_bytes=request_bytes(request), max_tokens=int(request["max_tokens"]))


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
    always settled or released and its transaction always ends. A cancel still ends this task,
    but only once ``cleanup`` is done: whoever waits for the cancelled task, such as shutdown
    before it closes the database, waits for the settle too. The app runs on asyncio."""
    task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # cancelled again: the cleanup still comes first
            except Exception:  # retrieved and logged below; the cancel is what ends this task
                break
        failed = None if task.cancelled() else task.exception()
        if failed is not None:
            # The type only: a database error's message can quote its parameters.
            log.error("assistant cleanup failed while cancelled", extra={"error": type(failed).__name__})
        raise


async def _call(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    zone: ZoneInfo,
    request: Mapping[str, Any],
    *,
    deadline: datetime,
    earlier_output_tokens: int = 0,
) -> tuple[ModelReply, bool]:
    """Reserve, call, and settle one request. Returns the reply, and whether it overran: cost
    more than the reservation, or billed a model the table doesn't price. Either way its cost is
    recorded as it was, and the message makes no further calls. Raises ``_Refused`` when the
    call can't run, including ``OUT_OF_TIME`` once the message's ``deadline`` passes, before
    the call or during it. ``earlier_output_tokens`` is what this message's earlier calls wrote:
    their thinking goes back in this request as signed blocks and bills as input."""
    if _seconds_left(assistant, deadline) <= 0:
        raise _Refused(OUT_OF_TIME)
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
        if reserved > settings.cap_micros:
            log.info("assistant cap below one call", extra={"member_id": member.id})
            raise _Refused(CAP_BELOW_ONE_CALL)
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

    left = _seconds_left(assistant, deadline)  # the reservation may have waited for the lock
    if left <= 0:
        await _shielded(release())
        raise _Refused(OUT_OF_TIME)
    try:
        async with asyncio.timeout(left):
            reply = await assistant.client.create(request, time_left=left)
    except ModelCallFailed as exc:
        log.warning("assistant model call failed", extra={"member_id": member.id, **exc.log_fields()})
        await _shielded(keep_reservation() if exc.maybe_billed else release())
        raise _Refused(FAILED) from None
    except TimeoutError:
        # Cut off at the deadline: the request may be running still, so it stays reserved.
        log.warning("assistant model call ran out of time", extra={"member_id": member.id})
        await _shielded(keep_reservation())
        raise _Refused(OUT_OF_TIME) from None
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


def _seconds_left(assistant: Assistant, deadline: datetime) -> float:
    return (deadline - assistant.clock()).total_seconds()


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
    """Raise ``_Refused`` as ``_call`` would for a menu link's drafting call reserving
    ``reserved``, reserving nothing, or ``ToolRefusal`` when the member has ``MAX_OPEN_DRAFTS``
    waiting already. A reservation over the whole cap gets ``CAP_TOO_LOW``: next month wouldn't
    help."""
    household_id = member.household_id
    async with write_transaction(db):
        settings = assistant_settings(await household_settings(db, household_id))
        if not settings.enabled:
            raise _Refused(TURNED_OFF)
        repo = AssistantRepository(db)
        if await repo.waiting_drafts(household_id, member.id, assistant.clock()) >= MAX_OPEN_DRAFTS:
            raise ToolRefusal(TOO_MANY_DRAFTS)
        if reserved > settings.cap_micros:
            log.info("assistant cap below a menu link's reservation", extra={"member_id": member.id})
            raise _Refused(CAP_TOO_LOW)
        month = month_key(assistant.clock(), zone)
        if await repo.month_total(household_id, month) + reserved > settings.cap_micros:
            log.info("assistant at its monthly cap", extra={"member_id": member.id})
            raise _Refused(AT_CAP)


@dataclass
class _LinkCalls:
    """``menu_links.LinkCalls`` for one message: the fetch, and drafting calls through the same
    reservations as the message's own. ``fetched`` counts the links read toward
    ``MAX_LINK_FETCHES``, whatever came of them; each makes at most one drafting call. ``over``
    records a drafting call that overran, so the message makes no further calls."""

    db: AsyncSession
    assistant: Assistant
    member: Member
    zone: ZoneInfo
    deadline: datetime
    fetched: int = 0
    over: bool = False

    @property
    def model(self) -> str:
        return self.assistant.config.model

    async def fetch(self, url: str) -> FetchedPage:
        """The fetch, and its text extraction, in the time the message has left: past it, the
        model is told so, as for any other fetch error."""
        left = _seconds_left(self.assistant, self.deadline)
        if left <= 0:
            raise ToolRefusal(OUT_OF_TIME)
        try:
            async with asyncio.timeout(left):
                return await self.assistant.fetch(url)
        except TimeoutError:
            log.info("assistant link fetch ran out of time", extra={"member_id": self.member.id})
            raise ToolRefusal(OUT_OF_TIME) from None

    def _reserved(self, request: Mapping[str, Any]) -> int:
        return reservation_micros(
            str(request["model"]), payload_bytes=request_bytes(request), max_tokens=int(request["max_tokens"])
        )

    async def check_room(self, request: Mapping[str, Any]) -> None:
        if self.fetched >= MAX_LINK_FETCHES:
            raise ToolRefusal(TOO_MANY_LINKS)
        if self.over:
            raise ToolRefusal(FAILED)
        if _seconds_left(self.assistant, self.deadline) <= 0:
            raise ToolRefusal(OUT_OF_TIME)
        try:
            await _check_room(self.db, self.assistant, self.member, self.zone, self._reserved(request))
        except _Refused as refused:
            raise ToolRefusal(refused.reply) from None
        self.fetched += 1  # the fetch comes next, and counts whether it works or not

    async def call(self, request: Mapping[str, Any]) -> ModelReply:
        """The drafting call for the link ``check_room`` let through."""
        try:
            reply, overran = await _call(
                self.db, self.assistant, self.member, self.zone, request, deadline=self.deadline
            )
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
        # The member's own words keep their labels' meaning, as mentions put them there.
        asked = Turn(asked.role, sent.relabel(asked.text, pseudonyms, unknown=None), pseudonyms.label(member.id))
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
    deadline: datetime,
) -> tuple[str, bool]:
    """The model and tool loop. Returns the reply text, and whether the model answered (so the
    exchange is worth keeping) rather than Dinnerbell replying for it. Every call stops at
    ``deadline``."""
    tools = tools_for(private)
    written = 0  # output tokens of this message's calls so far
    over = False  # a call billed more attempts than its reservation covered
    links = _LinkCalls(db, assistant, member, zone, deadline)
    session.links = links
    for _ in range(MAX_MODEL_CALLS):
        if over or links.over:
            return FAILED, False  # refuse further calls in this message
        request = build_request(assistant.config.model, tools, messages, keeps_history=keeps_history)
        try:
            reply, overran = await _call(
                db, assistant, member, zone, request, deadline=deadline, earlier_output_tokens=written
            )
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
            left = _seconds_left(assistant, deadline)
            if left <= 0:
                return OUT_OF_TIME, False
            try:
                async with asyncio.timeout(left):
                    result = await run_tool(
                        str(call.get("name", "")),
                        call.get("input"),
                        db=db,
                        member=member,
                        private=private,
                        now=assistant.clock(),
                        session=session,
                    )
            except TimeoutError:
                log.warning("assistant tool ran out of time", extra={"member_id": member.id})
                return OUT_OF_TIME, False
            entry: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.get("id"), "content": result.content}
            if result.is_error:
                entry["is_error"] = True
            results.append(entry)
        messages.append({"role": "user", "content": results})  # every result in one message
    return TOO_MANY_STEPS, False


def _theirs(stored: Conversation, member: Member, *, private: bool) -> bool:
    """Whether ``member`` may add to ``stored``: it's as private as the new message, and a
    private one is their own."""
    return stored.private == private and (not private or stored.owner_member_id == member.id)


async def may_converse(db: AsyncSession, member: Member, key: str, *, private: bool) -> bool:
    """Whether ``converse`` would take ``member``'s message in ``key`` rather than raise
    Forbidden, as of now. Slack asks first, so a refused message shows no working indicator;
    ``converse`` checks again itself."""
    stored = await AssistantRepository(db).conversation(member.household_id, key)
    return stored is None or _theirs(stored, member, private=private)


async def converse(
    db: AsyncSession,
    assistant: Assistant,
    member: Member,
    key: str,
    *,
    private: bool,
    text: str | NamingText,
    call_id: int | None = None,
) -> Reply | None:
    """Answer ``member``'s ``text`` in the conversation ``key`` (``domain.assistant.web_key`` or
    ``slack_key``). ``private`` is True for the web page and DMs, False for a channel thread.
    ``call_id`` is the conversation's call, for a Slack thread rooted under a Dinnerbell post
    about one: the call tools use when the member names none (spec 6.14). ``text`` is the
    message, or a ``NamingText`` that writes it with the conversation's pseudonyms.

    Returns None, storing and sending nothing, when the assistant doesn't answer this member
    (``available``) or the message is empty. Raises Forbidden for a private conversation that
    isn't the member's. Otherwise the reply is the model's answer with members' names put back,
    or a short message from Dinnerbell: at the cap, unavailable until next month; on a refusal,
    a failure, or ``MESSAGE_SECONDS`` passing, a line saying so. The exchange is kept for
    Admin's Conversation hours, in pseudonym form, unless that's 0 or the model didn't answer."""
    deadline = assistant.clock() + timedelta(seconds=MESSAGE_SECONDS)
    if not await available(db, assistant.config, member):
        return None
    check_key(key, private=private, member_id=member.id)
    household = await HouseholdRepository(db).get(member.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    hours = assistant_settings(await household_settings(db, household.id)).conversation_hours
    repo = AssistantRepository(db)
    stored = await repo.conversation(household.id, key)
    if stored is not None:
        if not _theirs(stored, member, private=private):
            raise Forbidden("That conversation belongs to someone else.")
        if stored.expired(assistant.clock(), hours):
            stored = None  # past Conversation hours: never sent, and replaced below
    session = ToolSession(
        key=key,
        pseudonyms=(stored.pseudonyms if stored else Pseudonyms()).with_member(member.id),
        call_id=call_id,
        instance_label=assistant.config.instance_label,
    )
    asked = (text if isinstance(text, str) else text(session.label)).strip()
    if not asked:
        return None
    if len(asked) > MAX_MESSAGE_CHARS:
        return Reply(TOO_LONG_MESSAGE)
    current = Turn("user", asked, session.pseudonyms.label(member.id))
    history = stored.turns if stored else ()
    sent = recent_turns(history, MAX_HISTORY_BYTES - current.size)
    zone = ZoneInfo(household.timezone)
    answer, answered = await _run_loop(
        db,
        assistant,
        member,
        zone,
        _messages(sent, current),
        private=private,
        keeps_history=hours > 0,
        session=session,
        deadline=deadline,
    )
    pseudonyms = session.pseudonyms  # with anyone the tools named
    if answered:
        await _keep(db, assistant, member, key, private, pseudonyms, current, Turn("assistant", answer))
    names = {m.id: m.display_name for m in await MemberRepository(db).list(household.id)}
    log.info("assistant replied", extra={"member_id": member.id, "answered": answered, "drafts": len(session.drafts)})
    return Reply(pseudonyms.restore_names(answer, names), tuple(session.drafts))
