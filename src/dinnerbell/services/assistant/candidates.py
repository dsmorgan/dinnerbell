# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which call a tool means, and the candidates for a voting call (spec 6.14, **Tools**).

``find_candidates`` is a read-only use case over ranking's hard filters. It answers only while
a call is voting: the people whose vetoes apply are fixed then, so asking before and after a
housemate answers reveals nothing (FR-29). It runs the filters as ranking does, with the member
asking counted as able to take the role a pick needs, because adding a pick volunteers them for
it (``calls.add_proposal``). Candidates come back in name order, with reasons built only from
variety, novelty, suggestions, the default mode, volunteer fit, and meal fit, from a snapshot
that names people by the conversation's pseudonyms. Nothing says what the filters removed.

Household settings are read directly, through the call's context, never through an admin-only
use case such as ``schedule.ranking_settings``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import EventRepository
from dinnerbell.domain.assistant_drafts import (
    Candidate,
    Narrowing,
    candidate_reasons,
    day_words,
    narrowed,
    reasons_snapshot,
)
from dinnerbell.domain.catalog import name_key
from dinnerbell.domain.events import (
    Event,
    EventKind,
    EventRuleViolation,
    EventState,
    RoleOffer,
    check_can_join_late,
    with_late_join,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.ranking import candidates
from dinnerbell.services.assistant.acting import ToolRefusal
from dinnerbell.services.call_context import CallContext, load
from dinnerbell.services.lifecycle import ranking_snapshot, suggester_ids
from dinnerbell.services.preferences import group_preferences

TOOL_STATES = frozenset({EventState.COLLECTING, EventState.VOTING})
"""The calls a tool picks when the member names none: open for answers or votes."""
NOT_VOTING = (
    "That call isn't voting yet, and candidates come only once voting opens. "
    "Tell the member to ask again then. Don't guess at what will fit."
)
NO_OPEN_CALL = "No call is open for answers or votes right now. Say so, and don't guess at one."
STATE_WORDS = {EventState.COLLECTING: "collecting answers", EventState.VOTING: "voting"}
ROLE_WORDS = (("cook", RoleOffer.COOK), ("pick up", RoleOffer.PICKUP), ("order", RoleOffer.ORDER))

Labeler = Callable[[int], str]
"""The conversation's pseudonym for a member ID, added to its map if they're new."""


@dataclass(frozen=True, slots=True)
class CallRef:
    """The call a member named: a slot, a day, both, or neither."""

    slot: Slot | None = None
    day: date | None = None


def _describe(event: Event, today: date) -> str:
    state = STATE_WORDS.get(event.state, event.state.label.lower())
    return f"{event.slot.value} on {event.day.isoformat()} ({day_words(event.day, today)}, {state})"


async def resolve_call(
    db: AsyncSession, actor: Member, ref: CallRef, *, default_call_id: int | None, today: date
) -> Event:
    """The call a tool means (spec 6.14): the one the member names, then the conversation's
    call, then the only call open for answers or votes. With several open, ``ToolRefusal`` asks
    the model to ask which, listing them."""
    events = EventRepository(db)
    household_id = actor.household_id
    if ref.slot is not None:
        day = ref.day or today
        call = await events.call_for(household_id, ref.slot, day)
        if call is None:
            raise ToolRefusal(f"There's no {ref.slot.value} call on {day.isoformat()} ({day_words(day, today)}).")
        return call
    if ref.day is None and default_call_id is not None:
        call = await events.get(household_id, default_call_id)
        if call is not None and call.kind is EventKind.CALL:
            return call
    found = [
        e
        for e in await events.for_today(household_id, today)
        if e.kind is EventKind.CALL and e.state in TOOL_STATES and (ref.day is None or e.day == ref.day)
    ]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ToolRefusal(NO_OPEN_CALL)
    listed = "; ".join(_describe(e, today) for e in found)
    raise ToolRefusal(
        f"Several calls are open: {listed}. Ask the member which one they mean, then call again with its slot and day."
    )


def call_basics(ctx: CallContext, today: date) -> dict[str, Any]:
    """What the model learns about a call (spec 6.14, **Data sent**): its meal, day, state, meal
    time, and which roles someone who's in offered. No names, no answers, no head count."""
    event = ctx.event
    offered = set().union(*(r.offers for r in ctx.participants if r.member_id in ctx.members))
    at = event.meal_time.astimezone(ctx.zone).time() if event.meal_time else ctx.schedule.meal_time
    return {
        "meal": event.slot.value,
        "day": event.day.isoformat(),
        "when": day_words(event.day, today),
        "state": STATE_WORDS.get(event.state, event.state.label.lower()),
        "meal_time": f"{at:%H:%M}",
        "roles_offered": [words for words, role in ROLE_WORDS if role in offered],
    }


def as_proposer(ctx: CallContext, actor: Member, now: datetime) -> CallContext:
    """``ctx`` as ``calls.add_proposal`` would see it while adding ``actor``'s pick: joined late
    if they aren't in and may, and offering every role, since adding a pick volunteers them for
    the one it needs (``picks.volunteer``). A late joiner's own stances never filter (#76), so
    the people whose vetoes apply don't change. Someone who can't take part counts as they are."""
    mine = ctx.response_of(actor.id)
    joined = mine
    if mine is None or not mine.is_in:
        try:
            check_can_join_late(ctx.event, actor, mine)
        except EventRuleViolation:
            return ctx
        joined = with_late_join(mine, actor.id, now)
    if joined is None:  # pragma: no cover - joining late always answers
        return ctx
    offering = replace(joined, offers=joined.offers | {RoleOffer.COOK, RoleOffer.PICKUP, RoleOffer.ORDER})
    others = [r for r in ctx.responses if r.member_id != actor.id]
    return replace(ctx, responses=[*others, offering])


@dataclass(frozen=True, slots=True)
class Candidates:
    ctx: CallContext
    found: tuple[Candidate, ...]
    """In name order."""


async def find_candidates(
    db: AsyncSession, actor: Member, event: Event, narrowing: Narrowing, *, label: Labeler, now: datetime
) -> Candidates:
    """The candidates for ``event`` that ``actor`` could propose, narrowed by ``narrowing``.
    Read-only. Raises ``ToolRefusal`` unless the call is voting."""
    if event.kind is not EventKind.CALL or event.state is not EventState.VOTING:
        raise ToolRefusal(NOT_VOTING)
    ctx = await load(db, event)
    if ctx.event.state is not EventState.VOTING:  # pragma: no cover - read again just now
        raise ToolRefusal(NOT_VOTING)
    group = await group_preferences(db, actor.household_id, ctx.veto_people)
    taken = frozenset(p.item_id for p in ctx.proposals if p.item_id is not None)
    snapshot = await ranking_snapshot(db, ctx, group, taken=taken)
    proposer = as_proposer(ctx, actor, now)
    filters = replace(snapshot, coverage=proposer.coverage, guest_needs=proposer.guest_needs)
    suggested = suggester_ids(ctx)
    people = {m for ids in suggested.values() for m in ids} | set(snapshot.coverage.cooks)
    clean = reasons_snapshot(snapshot, labels={m: label(m) for m in sorted(people)}, suggesters=suggested)
    found: list[Candidate] = []
    for for_meal in (True, False):
        pairs = narrowed(
            candidates(filters, for_meal=for_meal),
            narrowing,
            last_eaten_days=snapshot.last_eaten_days,
            cuisine_names=snapshot.cuisine_names,
        )
        for item, modes in pairs.values():
            reasons = candidate_reasons(item, modes, clean, for_meal=for_meal)
            found.append(Candidate(item=item, modes=modes, reasons=reasons, for_meal=for_meal))
    found.sort(key=lambda c: (name_key(c.item.name), c.item.id))
    return Candidates(ctx=ctx, found=tuple(found))
