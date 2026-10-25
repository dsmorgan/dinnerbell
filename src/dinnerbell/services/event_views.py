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
    cancel: bool = False
    """Call it off (FR-56): ``calls.cancel_call``."""
    close_window: Window | None = None
    """The window Close now would close (``calls.close_window``): respond reads "Start the vote
    now", vote reads "Decide now". None when the actor can't, or once the window's deadline has
    passed."""
    restart: bool = False
    """Start a called-off call again (FR-56): ``calls.start_call`` for its slot and day."""
    post_now: bool = False
    """Post a scheduled call now instead of at its usual time (issue #59): ``calls.post_now``."""


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

    @property
    def current(self) -> list[EventView]:
        """Calls that are posted or over: today's, and any posted for another day."""
        return [v for v in self.events if v.event.state is not EventState.SCHEDULED]

    @property
    def upcoming(self) -> list[EventView]:
        """Calls scheduled and not posted yet, soonest first (issue #59)."""
        return [v for v in self.events if v.event.state is EventState.SCHEDULED]


def _allowed(check: Callable[[], object]) -> bool:
    try:
        check()
    except EventRuleViolation:
        return False
    return True


def _window(event: Event) -> Window | None:
    if event.state is EventState.COLLECTING:
        return Window.RESPOND
    return Window.VOTE if event.state is EventState.VOTING else None


def permissions(ctx: CallContext, actor: Member, today: date, now: datetime, *, any_time: bool = False) -> Permissions:
    """What ``actor`` may do. ``any_time``, on a test instance only (#60), lifts the too-late
    rule for starting a called-off call again, as ``calls.start_call`` does there."""
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
        mark_done=_allowed(lambda: rules.check_can_mark_done(event, actor, mine, now=now)),
        admin=admin,
        rerun=admin
        and _allowed(lambda: rules.check_rerun(event, today, now=now, too_late_after=ctx.too_late_for(today))),
        move_window=_window(event) if admin else None,
        cancel=_allowed(lambda: rules.check_can_cancel(event, actor)),
        close_window=_closable(event, actor, now),
        restart=actor.active and _allowed(lambda: _check_restart(ctx, today, now, any_time)),
        post_now=event.state is EventState.SCHEDULED
        and _allowed(lambda: rules.check_can_post_now(event, actor, now=now)),
    )


def _closable(event: Event, actor: Member, now: datetime) -> Window | None:
    """The window Close now would close, while its deadline is still ahead. From the deadline
    until the scheduler's next tick moves the call on, the service refuses to close it
    (``check_expected_window``), so the button would only say the call moved on."""
    try:
        window = rules.check_can_close_window(event, actor)
        rules.check_expected_window(event, window, window, now=now)
    except EventRuleViolation:
        return None
    return window


def _check_restart(ctx: CallContext, today: date, now: datetime, any_time: bool) -> None:
    """What ``calls.start_call`` checks before it starts a called-off call again."""
    rules.check_ad_hoc_day(ctx.event.day, today)
    rules.check_restart(ctx.event, today, now=now, too_late_after=ctx.too_late_for(today), any_time=any_time)


def view(ctx: CallContext, actor: Member, today: date, now: datetime, *, any_time: bool = False) -> EventView:
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
        can=permissions(ctx, actor, today, now, any_time=any_time),
        members=tuple(ctx.person(m.id) for m in by_name),
        dietary_tags=tuple(t for t in tags if t.kind is TagKind.DIETARY),
        cuisine_tags=tuple(t for t in tags if t.kind is TagKind.CUISINE),
        items=tuple(sorted((i for i in ctx.items.values() if i.active), key=lambda i: i.key)),
    )


async def today_for(db: AsyncSession, actor: Member, *, now: datetime) -> date:
    """Today's date in the actor's household."""
    return await _today(db, actor, now)


async def _today(db: AsyncSession, actor: Member, now: datetime) -> date:
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    return household_today(household, now)


async def event_view(
    db: AsyncSession, actor: Member, event_id: int, *, now: datetime, any_time: bool = False
) -> EventView:
    """``any_time``: see ``permissions``."""
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    return view(await load(db, event), actor, await _today(db, actor, now), now, any_time=any_time)


async def calls_on(db: AsyncSession, actor: Member, day: date) -> list[Event]:
    """The actor's household's calls on ``day``, in slot order, open or not."""
    events = EventRepository(db)
    found = [await events.call_for(actor.household_id, slot, day) for slot in Slot]
    return [event for event in found if event is not None]


async def scheduled_call(db: AsyncSession, actor: Member, slot: Slot, day: date) -> Event | None:
    """The actor's household's call for ``slot`` on ``day`` if it's scheduled and not posted
    yet, for starting a call with "post it now" when it exists already (issue #59)."""
    event = await EventRepository(db).call_for(actor.household_id, slot, day)
    return event if event is not None and event.state is EventState.SCHEDULED else None


async def today_view(db: AsyncSession, actor: Member, *, now: datetime, any_time: bool = False) -> TodayView:
    """``any_time``: see ``permissions``."""
    today = await _today(db, actor, now)
    events = await EventRepository(db).for_today(actor.household_id, today)
    return TodayView(
        today=today,
        events=[view(await load(db, event), actor, today, now, any_time=any_time) for event in events],
        slots=tuple(Slot),
    )
