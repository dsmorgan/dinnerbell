# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read models for the Today page and event pages (spec section 11, FR-37).

Permissions come from the same domain checks the use cases enforce, so a page never offers a
button whose action would be refused, and hiding a button is never the only protection.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import EventRepository
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain import events as rules
from dinnerbell.domain import notifications as note
from dinnerbell.domain import offers
from dinnerbell.domain.catalog import CatalogItem, Mode, Tag, TagKind, for_meal_first, meal_hint
from dinnerbell.domain.events import Event, EventRuleViolation, EventState, Response, Window
from dinnerbell.domain.guests import GuestAction, can_answer_for, check_can_invite, check_guest
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services import guests as guest_rules
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
    join: bool = False
    """Join an open offer that has a seat left (FR-4): ``offers.join``."""
    add_guest: bool = False
    """Bring one more guest to an open offer with a seat left: ``offers.add_guest``."""
    drop_guest: bool = False
    """Bring one guest fewer to an open offer (#149): ``offers.drop_guest``."""
    leave: bool = False
    """Drop out of an open offer they're in, unless it's theirs: ``offers.leave``."""
    invite: bool = False
    """Invite a guest to the call (#38): ``guests.invite``."""
    join_late: bool = False
    """Join while voting after missing the answers (#76): ``calls.join_late``. A vote or a pick
    from them joins them late too, once they say yes (``join_late=True``)."""
    late_roles: bool = False
    """Offer a role as a late joiner taking part, while voting (#76): ``calls.offer_role``."""
    switch_late: bool = False
    """Flip late joiners' switches (#76): ``calls.switch_late_joiner``."""
    shuffle: bool = False
    """Shuffle the picks (FR-61): ``coordinator.shuffle``. Only while a shuffle is left and would
    change something."""
    break_tie: bool = False
    """Choose among picks tied for the most votes (FR-16): ``coordinator.break_tie``."""
    choose_roles: bool = False
    """Switch the decided pick's mode and assign its role (FR-61): ``coordinator.choose_roles``."""
    adjust: bool = False
    """**Adjust** in Slack (spec 5.7): the meal time, who does the role, and handing off, for the
    coordinator only: ``coordinator.adjust``."""


@dataclass(frozen=True, slots=True)
class RolesChoice:
    """What the coordinator chooses from when assigning roles (FR-61): the pick's ways of getting
    it, who's in, and who offered for each."""

    mode: Mode
    modes: tuple[Mode, ...]
    people: tuple[note.PersonInfo, ...]
    volunteers: dict[Mode, tuple[int, ...]]
    """Who offered for each way, best first, to preselect."""
    due_at: datetime | None = None
    """While set, the automatic assignment waits for the choice until then."""


@dataclass(frozen=True, slots=True)
class GuestView:
    """A guest invited to the call, as the viewer sees them (#38). ``answerable`` when the
    viewer answers for them: a guest without Slack whom they invited, or any such guest for an
    admin."""

    person: note.PersonInfo
    registered: bool
    invited_by: note.PersonInfo | None
    answer: Response | None
    answerable: bool
    removable: bool
    needs: tuple[str, ...] = ()
    """The guest's dietary needs, by name: only for whoever answers for them."""


@dataclass(frozen=True, slots=True)
class ProposalView:
    info: note.ProposalInfo
    voted: bool
    not_for_me: bool = False
    """The viewer joined late and this pick misses their own vetoes or dietary needs (#76).
    Only they see it (FR-29)."""


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
    offer: note.OfferInfo | None = None
    """The offer, when the event is one (FR-4)."""
    guests: tuple[GuestView, ...] = ()
    """Guests invited to the call (#38)."""
    invitable: tuple[note.PersonInfo, ...] = ()
    """Saved guests the viewer could invite: only for a member who may invite (#38)."""
    late: tuple[note.LateJoinerInfo, ...] = ()
    """Who joined late (#76), with the coordinator's switches."""
    shuffles_left: int = 0
    """Shuffles a call has left (FR-61); 0 when none would change anything."""
    roles: RolesChoice | None = None
    """What **Assign roles** offers, for someone who may use it."""

    @property
    def id(self) -> int:
        return self.event.id

    def meal_hint(self, item: CatalogItem) -> str | None:
        """ "usually dinner" beside an item that isn't for the call's meal (#72). ``items`` lists
        those last, and still offers them: people know better than the catalog."""
        return meal_hint(item, self.event.slot.meal)

    @property
    def not_for_me(self) -> tuple[str, ...]:
        """The picks that don't fit the viewer, a late joiner, by name (#76): for their eyes only."""
        return tuple(p.info.item.name for p in self.proposals if p.not_for_me)

    @property
    def picked_item_ids(self) -> frozenset[int]:
        """Catalog items that are picks already, which the Add a proposal list marks (#77)."""
        return frozenset(p.info.item.id for p in self.proposals if p.info.item.id is not None)


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


def _offer_permissions(ctx: CallContext, actor: Member, now: datetime) -> Permissions:
    """What ``actor`` may do with an offer: the same checks ``offers`` and ``calls`` make."""
    event, mine = ctx.event, ctx.response_of(actor.id)
    seats = offers.seats_left(event, ctx.responses) > 0
    open_to_them = _allowed(lambda: offers.check_can_join(event, actor))
    inside = mine is not None and mine.is_in
    return Permissions(
        join=open_to_them and seats and not inside,
        add_guest=open_to_them and seats,
        drop_guest=open_to_them and inside and mine is not None and mine.guests > 0,
        leave=inside and _allowed(lambda: offers.check_can_leave(event, actor)),
        mark_done=_allowed(lambda: rules.check_can_mark_done(event, actor, mine, now=now)),
        admin=actor.active and actor.is_admin,
        cancel=_allowed(lambda: rules.check_can_cancel(event, actor)),
    )


def _guest_may(ctx: CallContext, actor: Member, action: GuestAction) -> bool:
    return _allowed(lambda: check_guest(ctx.event, actor, action, invited=ctx.invited(actor.id)))


def _guest_permissions(base: Permissions, ctx: CallContext, actor: Member) -> Permissions:
    """``base`` cut down to what ``domain.guests`` lets an invited guest do (#38)."""
    may = functools.partial(_guest_may, ctx, actor)
    return Permissions(
        respond=base.respond and may(GuestAction.ANSWER),  # never joins late, or switches (#76)
        claim=base.claim and may(GuestAction.COORDINATE),
        hand_off=base.hand_off and may(GuestAction.HAND_OFF),
        manage=base.manage and may(GuestAction.MANAGE_DECISION),
        mark_done=base.mark_done and may(GuestAction.MARK_DONE),
        close_window=base.close_window if may(GuestAction.CLOSE_WINDOW) else None,
        choose_roles=base.choose_roles and may(GuestAction.MANAGE_DECISION),
        adjust=base.adjust and may(GuestAction.MANAGE_DECISION),
    )


def permissions(ctx: CallContext, actor: Member, today: date, now: datetime, *, any_time: bool = False) -> Permissions:
    """What ``actor`` may do. ``any_time``, on a test instance only (#60), lifts the too-late
    rule for starting a called-off call again, as ``calls.start_call`` does there. A guest gets
    only what ``domain.guests`` allows them (#38)."""
    if actor.is_guest:
        return _guest_permissions(_member_permissions(ctx, actor, today, now, any_time=any_time), ctx, actor)
    return _member_permissions(ctx, actor, today, now, any_time=any_time)


def _member_permissions(
    ctx: CallContext, actor: Member, today: date, now: datetime, *, any_time: bool = False
) -> Permissions:
    if ctx.event.is_offer:
        return _offer_permissions(ctx, actor, now)
    event, mine = ctx.event, ctx.response_of(actor.id)
    admin = actor.active and actor.is_admin
    return Permissions(
        respond=_allowed(lambda: rules.check_can_respond(event, actor, invited=ctx.invited(actor.id))),
        claim=_allowed(lambda: rules.check_claim_coordinator(event, actor, invited=ctx.invited(actor.id))),
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
        invite=_allowed(lambda: check_can_invite(event, actor)),
        join_late=_joins_late(event, actor, mine),
        late_roles=mine is not None
        and mine.late
        and _allowed(lambda: rules.check_can_offer_role(event, actor, mine))
        and event.state is EventState.VOTING,
        switch_late=_allowed(lambda: rules.check_can_switch_late(event, actor, _coordinator(ctx))),
        shuffle=bool(cc.shuffles_left(ctx))
        and _allowed(lambda: rules.check_can_shuffle(event, actor, _coordinator(ctx))),
        break_tie=bool(cc.tied_ids(ctx))
        and _allowed(lambda: rules.check_can_break_tie(event, actor, _coordinator(ctx))),
        choose_roles=event.state is EventState.DECIDED and _allowed(lambda: rules.check_can_choose_roles(event, actor)),
        adjust=_allowed(lambda: rules.check_can_adjust(event, actor)),
    )


def _joins_late(event: Event, actor: Member, mine: Response | None) -> bool:
    try:
        return rules.check_can_join_late(event, actor, mine)
    except EventRuleViolation:
        return False


def _coordinator(ctx: CallContext) -> Member | None:
    coordinator_id = ctx.event.coordinator_id
    return ctx.members.get(coordinator_id) if coordinator_id is not None else None


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


def _needs(ctx: CallContext, guest: Member) -> tuple[str, ...]:
    return tuple(sorted((ctx.tags[t].name for t in guest.dietary_needs if t in ctx.tags), key=str.casefold))


def _guest_views(ctx: CallContext, actor: Member) -> tuple[GuestView, ...]:
    if ctx.event.is_offer:
        return ()
    found: list[GuestView] = []
    collecting = ctx.event.state is EventState.COLLECTING
    for guest in ctx.guests:
        inviter = ctx.invitations.get(guest.id)
        found.append(
            GuestView(
                person=ctx.person(guest.id),
                registered=guest.is_registered_guest,
                invited_by=ctx.person(inviter) if inviter is not None else None,
                answer=ctx.response_of(guest.id),
                answerable=collecting and can_answer_for(guest, actor, inviter),
                removable=collecting and actor.is_member and (actor.is_admin or inviter == actor.id),
                needs=_needs(ctx, guest) if can_answer_for(guest, actor, inviter) else (),
            )
        )
    return tuple(found)


def _invitable(ctx: CallContext, actor: Member) -> tuple[note.PersonInfo, ...]:
    if not _allowed(lambda: check_can_invite(ctx.event, actor)):
        return ()
    saved = [m for m in ctx.members.values() if m.is_guest and m.active and not ctx.invited(m.id)]
    return tuple(ctx.person(m.id) for m in sorted(saved, key=lambda m: m.display_name.casefold()))


def _roles_choice(ctx: CallContext) -> RolesChoice | None:
    """The decided pick's ways of getting it and the people to assign, for **Assign roles**."""
    event = ctx.event
    item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
    if event.is_offer or item is None or event.decided_mode is None:
        return None
    modes = tuple(m for m in Mode if m in item.modes)
    people = [ctx.members[r.member_id] for r in ctx.participants if r.member_id in ctx.members]
    return RolesChoice(
        mode=event.decided_mode,
        modes=modes,
        people=tuple(ctx.person(m.id) for m in sorted(people, key=lambda m: m.display_name.casefold()) if m.active),
        volunteers={m: ctx.coverage.volunteers(m, item.cook_ids) for m in modes},
        due_at=event.roles_due_at,
    )


def view(ctx: CallContext, actor: Member, today: date, now: datetime, *, any_time: bool = False) -> EventView:
    # Who can be handed coordinating or assigned a role: members, and guests invited to the call.
    by_name = sorted([*ctx.active_members, *ctx.guests], key=lambda m: m.display_name.casefold())
    mine = [s for s in ctx.suggestions if s.member_id == actor.id]
    tags = sorted(ctx.tags.values(), key=lambda t: t.key)
    active = sorted((i for i in ctx.items.values() if i.active), key=lambda i: i.key)
    voting = ctx.event.state is EventState.VOTING
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
            ProposalView(
                cc.proposal_info(ctx, p),
                actor.id in ctx.votes.get(p.id, []),
                not_for_me=voting and not cc.fits_late_joiner(ctx, p, actor.id),
            )
            for p in ctx.proposals
        ),
        decision=cc.decision_info(ctx),
        can=permissions(ctx, actor, today, now, any_time=any_time),
        members=tuple(ctx.person(m.id) for m in by_name),
        dietary_tags=tuple(t for t in tags if t.kind is TagKind.DIETARY),
        cuisine_tags=tuple(t for t in tags if t.kind is TagKind.CUISINE),
        items=tuple(for_meal_first(active, ctx.event.slot.meal)),  # for the call's meal first (#72)
        offer=cc.offer_info(ctx) if ctx.event.is_offer else None,
        guests=_guest_views(ctx, actor),
        invitable=_invitable(ctx, actor),
        late=cc.late_joiners(ctx),
        shuffles_left=cc.shuffles_left(ctx),
        roles=_roles_choice(ctx) if ctx.event.state is EventState.DECIDED else None,
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
    """``any_time``: see ``permissions``. A guest sees only calls they're invited to (#38)."""
    await guest_rules.check_event_visible(db, actor, event_id)
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    return view(await load(db, event), actor, await _today(db, actor, now), now, any_time=any_time)


async def calls_on(db: AsyncSession, actor: Member, day: date) -> list[Event]:
    """The actor's household's calls on ``day``, in slot order, open or not."""
    events = EventRepository(db)
    found = [await events.call_for(actor.household_id, slot, day) for slot in Slot]
    return [event for event in found if event is not None]


def may_cancel(event: Event, actor: Member) -> bool:
    """Whether ``actor`` may call off ``event`` now (FR-56): ``calls.cancel_call``'s rule."""
    return _allowed(functools.partial(rules.check_can_cancel, event, actor))


async def offers_on(db: AsyncSession, actor: Member, day: date) -> list[Event]:
    """The open offers on ``day`` that ``actor`` may call off (#149): their own, or any for an
    admin, in slot order, so ``/dinnerbell cancel`` finds them as well as calls."""
    events = EventRepository(db)
    found: list[Event] = []
    for slot in Slot:
        for event in await events.offers_for(actor.household_id, slot, day):
            if event.state.is_open and may_cancel(event, actor):
                found.append(event)
    return found


async def scheduled_call(db: AsyncSession, actor: Member, slot: Slot, day: date) -> Event | None:
    """The actor's household's call for ``slot`` on ``day`` if it's scheduled and not posted
    yet, for starting a call with "post it now" when it exists already (issue #59)."""
    event = await EventRepository(db).call_for(actor.household_id, slot, day)
    return event if event is not None and event.state is EventState.SCHEDULED else None


async def today_view(db: AsyncSession, actor: Member, *, now: datetime, any_time: bool = False) -> TodayView:
    """``any_time``: see ``permissions``."""
    today = await _today(db, actor, now)
    events = await EventRepository(db).for_today(actor.household_id, today)
    if actor.is_guest:  # only the calls they're invited to (#38)
        invited = set(await guest_rules.invited_events(db, actor)) if actor.active else set()
        events = [e for e in events if e.id in invited]
    return TodayView(
        today=today,
        events=[view(await load(db, event), actor, today, now, any_time=any_time) for event in events],
        slots=tuple(Slot),
    )
