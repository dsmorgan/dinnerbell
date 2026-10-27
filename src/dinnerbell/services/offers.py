# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offers (spec 5.2, 5.3, FR-4, FR-5): post one, join it, bring guests, and drop out.

Posting decides what the offer does to that slot's call on that day (``domain.offers.
offer_path``): it joins an open call as a pick, it replaces a scheduled call when it's a cooking
offer, and otherwise it stands on its own. Everything a post reads and writes happens in one
``write_transaction``, so a scheduler tick opening the call at the same moment can't slip in
between the read and the write.

Calling an offer off, marking it done, and feedback on it go through ``calls`` and ``feedback``
like a call's; the rules there know offers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository, ItemValues
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import AssignmentRepository, EventRepository, ProposalRepository, ResponseRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import (
    DEFAULT_MEALS,
    CatalogItem,
    InvalidCatalogInput,
    ItemKind,
    Mode,
    Tag,
    TagKind,
    for_meal_first,
    meal_hint,
    normalize_item_name,
    sorted_modes,
)
from dinnerbell.domain.events import (
    MODE_ROLE,
    Event,
    EventRuleViolation,
    EventState,
    Proposal,
    ProposalSource,
    Status,
    check_cook_list,
    check_proposal_room,
    check_run,
    clean_cancel_note,
    is_coordinator,
    transition,
    with_status,
)
from dinnerbell.domain.guests import GuestAction
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.offers import (
    DEFAULT_CAPACITY,
    MAX_OPEN_OFFERS,
    OfferPath,
    check_can_join,
    check_can_leave,
    check_capacity,
    joined,
    offer_path,
    offer_time,
    seats_left,
)
from dinnerbell.domain.ranking import usual_choice
from dinnerbell.domain.schedule import slot_schedules
from dinnerbell.services import activity, picks
from dinnerbell.services import call_context as cc
from dinnerbell.services import guests as guest_rules
from dinnerbell.services.call_context import CallContext, load, locked_event, publish
from dinnerbell.services.common import audit
from dinnerbell.services.errors import NotFound
from dinnerbell.services.lifecycle import misfit
from dinnerbell.services.preferences import group_preferences
from dinnerbell.services.variety import household_today

NO_SUCH_OFFER = "No such offer."
DOING = {Mode.COOK: "cook it", Mode.PICKUP: "pick it up", Mode.DELIVERY: "order it"}


@dataclass(frozen=True, slots=True)
class OfferInput:
    """What someone offers (FR-4): a catalog item, or a new one by name with its cuisine; how
    it arrives; the day and wall-clock time; and the spare servings."""

    slot: Slot
    day: date
    mode: Mode
    at: time
    capacity: int
    item_id: int | None = None
    free_text: str | None = None
    cuisine_id: int | None = None


@dataclass(frozen=True, slots=True)
class Posted:
    """What posting did: ``path`` says whether the offer stands on its own (``event_id`` is the
    offer), joined the call as a pick (``event_id`` is the call), or replaced the call."""

    path: OfferPath
    event_id: int
    slot: Slot
    day: date
    item_name: str


@dataclass(frozen=True, slots=True)
class SlotDefaults:
    mode: Mode
    at: time


@dataclass(frozen=True, slots=True)
class OfferForm:
    """What the offer form starts with: today, the slot most likely meant, each slot's usual
    mode and meal time (FR-1), and the catalog to pick from, items for that slot's meal first
    (#72)."""

    today: date
    slot: Slot
    defaults: dict[Slot, SlotDefaults]
    capacity: int
    items: tuple[CatalogItem, ...]
    cuisine_tags: tuple[Tag, ...]

    def meal_hint(self, item: CatalogItem) -> str | None:
        """ "usually dinner" beside an item that isn't for the slot the form starts on. It's
        still offerable: people know better than the catalog (#72)."""
        return meal_hint(item, self.slot.meal)


async def offer_form(db: AsyncSession, actor: Member, *, now: datetime) -> OfferForm:
    """Lunch until its usual meal time, then dinner; each with its schedule's first default
    mode and usual meal time, so lunch offers start as pickup at 12:30 (FR-1)."""
    guest_rules.refuse_guest(actor, GuestAction.OFFER)
    household = await HouseholdRepository(db).get(actor.household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    schedules = slot_schedules(await household_settings(db, household.id))
    defaults = {
        slot: SlotDefaults(sorted_modes(schedule.modes)[0] if schedule.modes else Mode.PICKUP, schedule.meal_time)
        for slot, schedule in schedules.items()
    }
    local = now.astimezone(ZoneInfo(household.timezone))
    slot = Slot.LUNCH if local.time() < schedules[Slot.LUNCH].meal_time else Slot.DINNER
    catalog = CatalogRepository(db)
    items = for_meal_first(
        sorted((i for i in await catalog.items(household.id) if i.active), key=lambda i: i.key), slot.meal
    )
    cuisines = sorted((t for t in await catalog.tags(household.id) if t.kind is TagKind.CUISINE), key=lambda t: t.key)
    return OfferForm(local.date(), slot, defaults, DEFAULT_CAPACITY, tuple(items), tuple(cuisines))


# Posting --------------------------------------------------------------------------------------


async def post_offer(db: AsyncSession, actor: Member, data: OfferInput, *, now: datetime) -> Posted:
    """Post ``data`` for ``actor`` (FR-4, FR-5), and commit. Audited."""
    if not actor.active:
        raise EventRuleViolation("Only active members can make an offer.", code="inactive")
    activity.about(item=data.free_text, item_id=data.item_id, mode=data.mode)
    guest_rules.refuse_guest(actor, GuestAction.OFFER)
    async with write_transaction(db):
        household = await HouseholdRepository(db).get(actor.household_id)
        if household is None:  # pragma: no cover - members cascade with their household
            raise NotFound("No such household.")
        today = household_today(household, now)
        at = offer_time(data.day, data.at, zone=ZoneInfo(household.timezone), today=today, now=now)
        capacity = check_capacity(data.capacity)
        item = await _offered_item(db, actor, data, now)
        call = await EventRepository(db).call_for(actor.household_id, data.slot, data.day)
        path = offer_path(call, data.mode)
        if path is OfferPath.ATTACH and call is not None:
            posted = await _attach(db, actor, call, item, data.mode, now)
        else:
            posted = await _post(
                db, actor, data, item, at, capacity, replacing=call if path is OfferPath.REPLACE else None, now=now
            )
        await db.commit()
    return posted


async def _offered_item(db: AsyncSession, actor: Member, data: OfferInput, now: datetime) -> CatalogItem:
    """The catalog item offered: one picked, one that already has the typed name, or a new one
    made from the name and cuisine, audited as ``catalog.item_created``."""
    catalog, household_id = CatalogRepository(db), actor.household_id
    if (data.item_id is None) == (not (data.free_text or "").strip()):
        raise EventRuleViolation(
            "Pick something from the catalog, or type something new, but not both.", code="both_given"
        )
    if data.item_id is not None:
        item = await catalog.item(household_id, data.item_id)
        if item is None or not item.active:
            raise EventRuleViolation("That isn't in the catalog anymore. Reload and try again.", code="not_in_catalog")
    else:
        kind = ItemKind.HOME if data.mode is Mode.COOK else ItemKind.RESTAURANT
        try:
            name = normalize_item_name(data.free_text or "")
        except InvalidCatalogInput as exc:
            raise EventRuleViolation(str(exc), code="invalid_name") from None
        found = await catalog.item_by_name(household_id, kind, name)
        if found is not None and not found.active:
            raise EventRuleViolation("That isn't in the catalog anymore. Reload and try again.", code="not_in_catalog")
        if found is None:
            tags = {t.id: t for t in await catalog.tags(household_id)}
            cuisine = tags.get(data.cuisine_id) if data.cuisine_id is not None else None
            if cuisine is None or cuisine.kind is not TagKind.CUISINE:
                raise EventRuleViolation("Pick a cuisine for it, so variety and vetoes still work.", code="no_cuisine")
            values = ItemValues(
                kind=kind,
                name=name,
                modes=frozenset({data.mode}),
                meals=DEFAULT_MEALS | {data.slot.meal},  # offered for this meal (#72)
                tag_ids=frozenset({cuisine.id}),
                cook_ids=frozenset(),
                url=None,
                order_url=None,
                notes=None,
                default_servings=None,
                active=True,
            )
            found = await catalog.add_item(household_id, values, created_by=actor.id, now=now)
            payload = {"name": name, "kind": kind.value, "from_offer": True}
            await audit(db, household_id, actor, "catalog.item_created", f"item:{found.id}", payload, now)
        item = found
    if data.mode not in item.modes:
        raise EventRuleViolation(f"{item.name} isn't available by {data.mode.label.lower()}.", code="unavailable")
    check_cook_list(item, data.mode, actor, ask="Ask a cook to offer it")
    return item


async def _post(
    db: AsyncSession,
    actor: Member,
    data: OfferInput,
    item: CatalogItem,
    at: datetime,
    capacity: int,
    *,
    replacing: Event | None,
    now: datetime,
) -> Posted:
    """A standalone offer, posted in the channel. Its offerer is in, and assigned the role the
    mode needs, so feedback can thank them as the cook."""
    events = EventRepository(db)
    mine = [o for o in await events.offers_for(actor.household_id, data.slot, data.day) if o.created_by == actor.id]
    if sum(1 for o in mine if o.state.is_open) >= MAX_OPEN_OFFERS:
        raise EventRuleViolation(
            f"You have {MAX_OPEN_OFFERS} {data.slot.value} offers on for that day already. Call one off first.",
            code="too_many_offers",
        )
    offer = await events.add_offer(
        household_id=actor.household_id,
        slot=data.slot,
        day=data.day,
        created_by=actor.id,
        item_id=item.id,
        mode=data.mode,
        at=at,
        capacity=capacity,
        now=now,
    )
    await AssignmentRepository(db).assign(actor.household_id, offer.id, MODE_ROLE[data.mode], actor.id, now)
    offerer = with_status(None, actor.id, Status.IN, now)
    await ResponseRepository(db).save(actor.household_id, offer.id, offerer, now)
    if replacing is not None:
        await _replace(db, actor, replacing, offer, now)
    ctx = await load(db, offer)
    await publish(db, ctx, cc.offer_posted(ctx), now=now)
    payload = {
        "slot": data.slot.value,
        "date": data.day.isoformat(),
        "item_id": item.id,
        "mode": data.mode.value,
        "at": at.isoformat(),
        "capacity": capacity,
        "replaced": replacing.id if replacing is not None else None,
    }
    await audit(db, actor.household_id, actor, "event.offered", f"event:{offer.id}", payload, now)
    replaced = replacing.id if replacing is not None else None
    activity.action(
        db, "offer_posted", offer, actor, started=True, item=item, mode=data.mode, capacity=capacity, replaced=replaced
    )
    path = OfferPath.REPLACE if replacing is not None else OfferPath.NEW
    return Posted(path, offer.id, data.slot, data.day, item.name)


async def _replace(db: AsyncSession, actor: Member, call: Event, offer: Event, now: datetime) -> None:
    """FR-5: a cooking offer posted before the call replaces it. The call was scheduled and not
    posted, so nothing goes to the channel; whoever started it hears, as with any call-off of a
    scheduled call. Audited as ``event.replaced_by_offer``."""
    if not await EventRepository(db).lock(call.household_id, call.id, now):  # pragma: no cover - just read
        raise NotFound(cc.NO_SUCH_CALL)
    reason = clean_cancel_note(f"{actor.display_name} is cooking instead.")
    ended = replace(call, state=transition(call.state, EventState.CANCELLED), ended_by=actor.id, end_note=reason)
    ended = await EventRepository(db).save(ended, now)
    ctx = await load(db, ended)
    starter = ctx.members.get(call.created_by) if call.created_by is not None else None
    if starter is not None and starter.active and starter.id != actor.id:
        info = cc.event_info(ctx)
        await publish(
            db, ctx, note.StartedCallCancelled(info, ctx.person(starter.id), ctx.person(actor.id), reason), now=now
        )
    payload = {"offer_id": offer.id, "from": call.state.value}
    await audit(db, call.household_id, actor, "event.replaced_by_offer", f"event:{call.id}", payload, now)


async def _attach(db: AsyncSession, actor: Member, call: Event, item: CatalogItem, mode: Mode, now: datetime) -> Posted:
    """FR-5: an offer for a slot with an open call becomes a pick on it. The offerer is in and
    offers the role it takes (``picks.volunteer``). While responses are open that's an answer
    like any other; once voting has started, only someone already in can add one, since a new
    person's vetoes would change what the picks must pass (FR-29). The pick passes the same
    checks as an added proposal.

    A call has one pick per place (#77). An offer for a place that ranking proposed, or that the
    offerer added, in another mode takes that pick over: its mode becomes the offer's, the
    offerer is its proposer, and its votes stay, since they're for the place. Another member's
    pick isn't taken over, since it would then depend on the offerer being in
    (``lifecycle._keep_attached``) instead of on them; that, and an offer in the same mode, is
    refused, as an added proposal is. Audited as ``event.offer_attached``, with ``took_over`` naming the mode a pick
    had before."""
    events = EventRepository(db)
    if not await events.lock(call.household_id, call.id, now):  # pragma: no cover - just read
        raise NotFound(cc.NO_SUCH_CALL)
    ctx = await load(db, call)
    mine = ctx.response_of(actor.id)
    voting = call.state is EventState.VOTING
    if voting and (mine is None or not mine.is_in):
        raise EventRuleViolation(
            f"Voting has started on the {call.slot.value} call, and you're not in it, so your offer can't join it.",
            code="not_in",
        )
    source = ProposalSource.COORDINATOR if is_coordinator(call, actor) else ProposalSource.MEMBER
    reasons = (f"{actor.display_name} offered to {DOING[mode]}.",)
    candidate = Proposal(0, call.id, mode, 0, source, item_id=item.id, reasons=reasons, proposed_by=actor.id)
    existing = picks.picked(ctx, candidate)
    if existing is not None and (existing.mode is mode or existing.proposed_by not in (None, actor.id)):
        raise picks.already_a_pick(ctx, existing)
    # A pick taken over adds no row, but becomes the offerer's (MAX_ADDED_PER_MEMBER).
    check_proposal_room([p for p in ctx.proposals if existing is None or p.id != existing.id], actor)
    if existing is not None:
        candidate = replace(
            existing,
            mode=mode,
            item_id=item.id,
            free_text=None,
            free_cuisine_id=None,
            reasons=reasons + _still_true(existing),
            proposed_by=actor.id,
        )
    ctx = await picks.volunteer(db, ctx, actor, mode, join=not voting, now=now)
    group = await group_preferences(db, call.household_id, ctx.veto_people)
    problem = misfit(ctx, candidate, group)
    if problem is not None:
        raise problem
    proposals = ProposalRepository(db)
    if existing is None:
        proposal = await proposals.add(
            household_id=call.household_id,
            event_id=call.id,
            item_id=item.id,
            mode=mode,
            rank=await proposals.next_rank(call.household_id, call.id),
            source=source,
            reasons=reasons,
            proposed_by=actor.id,
            now=now,
        )
    else:
        proposal = candidate
        await proposals.take_over(
            call.household_id, existing.id, mode=mode, item_id=item.id, reasons=candidate.reasons, proposed_by=actor.id
        )
    await picks.announce(db, call, now=now)
    payload: dict[str, object] = {"proposal_id": proposal.id, "item_id": item.id, "mode": mode.value}
    if existing is not None:
        payload["took_over"] = existing.mode.value
    await audit(db, call.household_id, actor, "event.offer_attached", f"event:{call.id}", payload, now)
    took_over = existing.mode.value if existing is not None else None
    activity.action(db, "offer_attached", call, actor, item=item, mode=mode, took_over=took_over)
    return Posted(OfferPath.ATTACH, call.id, call.slot, call.day, item.name)


def _still_true(existing: Proposal) -> tuple[str, ...]:
    """``existing``'s reasons that stay true when an offer changes its mode: not that its old
    mode is a usual choice, nor that someone offered to get it that way."""
    stale = (usual_choice(existing.mode), f" offered to {DOING[existing.mode]}.")
    return tuple(r for r in existing.reasons if r != stale[0] and not r.endswith(stale[1]))


# Joining -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seat:
    """Where the member stands on an offer after a tap: in or not, with how many guests, and
    how many seats are left."""

    joined: bool
    guests: int
    seats_left: int
    item_name: str
    offerer: bool = False
    """It's the member's own offer, so they're in without taking a seat and can't drop out."""


async def _offer_context(db: AsyncSession, event: Event, run: int | None, actor: Member) -> CallContext:
    if not event.is_offer:
        raise NotFound(NO_SUCH_OFFER)
    guest_rules.refuse_guest(actor, GuestAction.JOIN_OFFER)  # a member brings a guest with +1 guest
    check_run(event, run)
    return await load(db, event)


async def _seat(db: AsyncSession, event: Event, member_id: int) -> Seat:
    ctx = await load(db, event)
    mine = ctx.response_of(member_id)
    item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
    name = item.name if item is not None else "the offer"
    joined_now = mine is not None and mine.is_in
    guests = mine.guests if mine and joined_now else 0
    return Seat(joined_now, guests, seats_left(event, ctx.responses), name, offerer=event.created_by == member_id)


async def _changed(db: AsyncSession, before: CallContext, actor: Member, now: datetime) -> None:
    """Publish the offer message's new state, and tell whoever made it once the last seat goes."""
    event = before.event
    ctx = await load(db, event)
    sent: list[note.Notification] = [cc.offer_updated(ctx)]
    was_full = seats_left(event, before.responses) == 0
    offerer = ctx.members.get(event.created_by) if event.created_by is not None else None
    filled = not was_full and seats_left(event, ctx.responses) == 0
    if filled and offerer is not None and offerer.active and offerer.id != actor.id:
        sent.append(note.OfferFull(cc.event_info(ctx), ctx.person(offerer.id), cc.offer_info(ctx)))
    await publish(db, ctx, *sent, now=now)


async def _join_locked(
    db: AsyncSession,
    actor: Member,
    before: CallContext,
    *,
    guests: int,
    guest_needs: frozenset[int],
    now: datetime,
) -> Seat:
    event = before.event
    check_can_join(event, actor)
    dietary = [t.id for t in before.tags.values() if t.kind is TagKind.DIETARY]
    answer = joined(
        event, before.responses, actor.id, guests=guests, guest_needs=guest_needs, dietary_tag_ids=dietary, now=now
    )
    await ResponseRepository(db).save(actor.household_id, event.id, answer, now)
    await _changed(db, before, actor, now)
    activity.action(db, "join", event, actor, guests=guests)
    return await _seat(db, event, actor.id)


async def _leave_locked(db: AsyncSession, actor: Member, before: CallContext, now: datetime) -> Seat:
    event = before.event
    check_can_leave(event, actor)
    mine = before.response_of(actor.id)
    if mine is not None and mine.is_in:
        out = replace(with_status(mine, actor.id, Status.OUT, now), guests=0, guest_needs=frozenset())
        await ResponseRepository(db).save(actor.household_id, event.id, out, now)
        await _changed(db, before, actor, now)
        activity.action(db, "leave", event, actor)
    return await _seat(db, event, actor.id)


async def join(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    guests: int = 0,
    guest_needs: frozenset[int] = frozenset(),
    run: int | None = None,
    now: datetime,
) -> Seat:
    """Join the offer with ``guests``, or change how many guests you bring and what they need,
    within the seats left (FR-4, FR-8). ``run``, when given, is the run the person saw."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        before = await _offer_context(db, event, run, actor)
        return await _join_locked(db, actor, before, guests=guests, guest_needs=guest_needs, now=now)


async def add_guest(db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime) -> Seat:
    """**+1 guest**: one more guest, counting the member in if they weren't."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        before = await _offer_context(db, event, run, actor)
        mine = before.response_of(actor.id)
        inside = mine is not None and mine.is_in
        guests = mine.guests if mine is not None and inside else 0
        needs = mine.guest_needs if mine is not None and inside else frozenset()
        return await _join_locked(db, actor, before, guests=guests + 1, guest_needs=needs, now=now)


async def leave(db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime) -> Seat:
    """Drop out of an offer, with any guests, freeing their seats."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        return await _leave_locked(db, actor, await _offer_context(db, event, run, actor), now)


async def toggle_join(db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime) -> Seat:
    """**Count me in** on the shared offer message: join, or drop out if already in, reading
    the member's answer under the offer's lock, so a double tap ends where it started.
    Whoever made the offer is always in; their tap changes nothing."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        before = await _offer_context(db, event, run, actor)
        mine = before.response_of(actor.id)
        if mine is not None and mine.is_in:
            if event.created_by == actor.id:
                check_can_join(event, actor)
                return await _seat(db, event, actor.id)
            return await _leave_locked(db, actor, before, now)
        return await _join_locked(db, actor, before, guests=0, guest_needs=frozenset(), now=now)
