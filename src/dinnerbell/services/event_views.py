# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read models for the Today page and event pages (spec section 11, FR-37).

Permissions come from the same domain checks the use cases enforce, so a page never offers a
button whose action would be refused, and hiding a button is never the only protection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import EventRepository
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain import events as rules
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import CatalogItem, Tag, TagKind
from dinnerbell.domain.events import Event, EventRuleViolation, EventState, Response, Window
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services.call_context import CallContext, load
from dinnerbell.services.errors import NotFound
from dinnerbell.services.variety import household_today


@dataclass(frozen=True, slots=True)
class Permissions:
    respond: bool = False
    claim: bool = False
    hand_off: bool = False
    vote: bool = False
    propose: bool = False
    lock: bool = False
    manage: bool = False
    mark_done: bool = False
    admin: bool = False
    rerun: bool = False
    move_window: Window | None = None


@dataclass(frozen=True, slots=True)
class ProposalView:
    info: note.ProposalInfo
    voted: bool


@dataclass(frozen=True)
class EventView:
    event: Event
    info: note.EventInfo
    roster: tuple[note.RosterEntry, ...]
    waiting_on: tuple[note.PersonInfo, ...]
    head_count: int
    mine: Response | None
    my_suggestion_ids: tuple[int, ...]
    my_suggestion_texts: tuple[str, ...]
    proposals: tuple[ProposalView, ...]
    decision: note.DecisionInfo | None
    can: Permissions
    members: tuple[note.PersonInfo, ...]
    dietary_tags: tuple[Tag, ...]
    cuisine_tags: tuple[Tag, ...]
    items: tuple[CatalogItem, ...]

    @property
    def id(self) -> int:
        return self.event.id


@dataclass(frozen=True)
class TodayView:
    today: date
    events: list[EventView]
    slots: tuple[Slot, ...]


def _allowed(check: Callable[[], None]) -> bool:
    try:
        check()
    except EventRuleViolation:
        return False
    return True


def _window(event: Event) -> Window | None:
    if event.state is EventState.COLLECTING:
        return Window.RESPOND
    return Window.VOTE if event.state is EventState.VOTING else None


def permissions(ctx: CallContext, actor: Member, today: date) -> Permissions:
    event, mine = ctx.event, ctx.response_of(actor.id)
    admin = actor.active and actor.is_admin
    return Permissions(
        respond=_allowed(lambda: rules.check_can_respond(event, actor)),
        claim=_allowed(lambda: rules.check_claim_coordinator(event, actor)),
        hand_off=rules.is_coordinator(event, actor) and event.state.is_open and event.state is not EventState.SCHEDULED,
        vote=_allowed(lambda: rules.check_can_vote(event, actor, mine)),
        propose=_allowed(lambda: rules.check_can_propose(event, actor)),
        lock=_allowed(lambda: rules.check_can_lock(event, actor)),
        manage=_allowed(lambda: rules.check_can_manage_decision(event, actor)),
        mark_done=_allowed(lambda: rules.check_can_mark_done(event, actor, mine)),
        admin=admin,
        rerun=admin and _allowed(lambda: rules.check_rerun(event, today)),
        move_window=_window(event) if admin else None,
    )


def view(ctx: CallContext, actor: Member, today: date) -> EventView:
    by_name = sorted(ctx.active_members, key=lambda m: m.display_name.casefold())
    mine = [s for s in ctx.suggestions if s.member_id == actor.id]
    tags = sorted(ctx.tags.values(), key=lambda t: t.key)
    return EventView(
        event=ctx.event,
        info=cc.event_info(ctx),
        roster=cc.roster(ctx),
        waiting_on=cc.waiting_on(ctx),
        head_count=ctx.head_count,
        mine=ctx.response_of(actor.id),
        my_suggestion_ids=tuple(s.item_id for s in mine if s.item_id is not None),
        my_suggestion_texts=tuple(s.free_text for s in mine if s.free_text is not None),
        proposals=tuple(
            ProposalView(cc.proposal_info(ctx, p), actor.id in ctx.votes.get(p.id, [])) for p in ctx.proposals
        ),
        decision=cc.decision_info(ctx),
        can=permissions(ctx, actor, today),
        members=tuple(ctx.person(m.id) for m in by_name),
        dietary_tags=tuple(t for t in tags if t.kind is TagKind.DIETARY),
        cuisine_tags=tuple(t for t in tags if t.kind is TagKind.CUISINE),
        items=tuple(sorted((i for i in ctx.items.values() if i.active), key=lambda i: i.key)),
    )


async def _today(db: AsyncSession, actor: Member, now: datetime) -> date:
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    return household_today(household, now)


async def event_view(db: AsyncSession, actor: Member, event_id: int, *, now: datetime) -> EventView:
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    return view(await load(db, event), actor, await _today(db, actor, now))


async def today_view(db: AsyncSession, actor: Member, *, now: datetime) -> TodayView:
    today = await _today(db, actor, now)
    events = await EventRepository(db).for_today(actor.household_id, today)
    return TodayView(
        today=today,
        events=[view(await load(db, event), actor, today) for event in events],
        slots=tuple(Slot),
    )
