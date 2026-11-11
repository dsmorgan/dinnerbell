# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coordinating through the assistant, in private only (#252, spec 6.14, **Tools** and
**Drafts**): look up the call a member coordinates, lock a pick or break a tie, shuffle the picks,
change the plan as Slack's **Adjust** does, hand off coordinating, mark the meal done, and call
off a call (FR-56).

``tools`` offers these only in a private conversation, the web page or a DM, never a channel
thread (``private_only``), so a housemate can't steer a coordinator's request (ADR 0009).

Each runs as ``tool_actor`` of the member, with a member's role, and the services' own checks
decide who may: the coordinator, and for a call-off the member who started an ad-hoc call too.
An admin who isn't the coordinator is refused, as any member is. Each draft checks now, in the
order the use case does, so someone who may not act learns nothing about the call's people. The
use case checks again on **Confirm**, under the write lock, after the draft is used and its call's
run and state are checked.

People are named only by the conversation's labels, such as "Member 2": a label resolves to a
member at draft time from the conversation's pseudonym map, never a guest, and the service then
refuses anyone it would, such as someone deactivated or not in. ``look_up`` gives the coordinator
what the call page shows them that the tools need: who's in, by label, with the roles they offered,
how many guests are in, which picks are tied, and the decision. Never anyone's maybe or out, vote,
or tally. Nothing here returns a name, and no title or line on a card holds one.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import EventRepository
from dinnerbell.domain.assistant import Pseudonyms
from dinnerbell.domain.assistant_coordinating import (
    PlanChange,
    call_off_lines,
    decide_lines,
    decide_title,
    done_lines,
    hand_off_lines,
    plan_lines,
    plan_title,
    shuffle_lines,
)
from dinnerbell.domain.assistant_drafts import Draft, DraftKind, day_words
from dinnerbell.domain.catalog import MODE_ORDER, CatalogItem, Mode, name_key
from dinnerbell.domain.events import (
    LEAD_ROLE,
    MAX_SHUFFLES,
    MODE_ROLES,
    OPEN_STATES,
    AssignmentRole,
    Event,
    EventKind,
    EventRuleViolation,
    EventState,
    RoleOffer,
    can_do,
    check_can_adjust,
    check_can_break_tie,
    check_can_cancel,
    check_can_lock,
    check_can_mark_done,
    check_can_shuffle,
    check_handoff,
    check_run,
    clean_cancel_note,
    default_roles,
    is_coordinator,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls, coordinator
from dinnerbell.services.assistant.acting import ToolRefusal
from dinnerbell.services.assistant.call_drafts import NOTHING_TO_DO, ActionConfirmed, DraftSpec, find_pick, pick_name
from dinnerbell.services.assistant.candidates import CallRef, call_basics
from dinnerbell.services.call_context import CallContext, load
from dinnerbell.services.calls import handoff_target
from dinnerbell.services.coordinator import Adjustment, PartlyAdjusted, check_assignee, check_mode
from dinnerbell.services.errors import NotFound

Labeler = Callable[[int], str]
Use = Callable[[], Awaitable[None]]

NOT_YOURS = (
    "The member doesn't coordinate an open call and didn't start one, so there's no call to do that on. Say so: "
    "only a call's coordinator can, and the member who started an ad-hoc call can call it off."
)
ONLY_THE_COORDINATOR = (
    "Only a call's coordinator can look this up, and the member doesn't coordinate that call. Say so."
)
NOT_A_LABEL = (
    "{label} isn't anyone this call can use. Call look_up_coordinating for who's in, and name people only by the "
    "labels it returns, such as Member 2."
)
"""The same for a label nobody has and a guest's: the model never learns who the guests are."""
LABEL_ONLY = "Name a person by a label such as Member 2, or send an empty string for nobody."
NOT_A_CATALOG_PICK = (
    "That pick isn't a catalog item yet, so its roles wait for the decision. Draft the lock alone, then ask again."
)
NO_PICKUP = "Only pickup has someone who picks it up. Send pickup as the way, or an empty string for pickup."
NO_SUCH_PLACE = (
    "The catalog has no active place or home meal by that name. Look it up with look_up_catalog_items. A new name "
    "goes through Adjust in Slack or Change the plan on the web."
)
TWO_PLACES = "A restaurant and a home meal share that name. Send the way too: cook for the home meal."
OFFER_NOT_A_CALL = "That's an offer, not a call. Use draft_take_back_offer for the member's own offer."
CHANGED = "The call changed since the draft. Ask the assistant for a new draft."
OFFER_WORDS = {RoleOffer.COOK: "cook", RoleOffer.PICKUP: "pickup", RoleOffer.ORDER: "order"}
_LABEL = re.compile(r"Member (\d{1,6})")


def _stale(message: str = CHANGED) -> EventRuleViolation:
    return EventRuleViolation(message, code="stale_draft")


def _words(event: Event, today: date) -> tuple[str, Slot]:
    return day_words(event.day, today), event.slot


# Which call, and whom a label names --------------------------------------------------------------


async def coordinated_call(
    db: AsyncSession, actor: Member, ref: CallRef, *, default_call_id: int | None, today: date
) -> Event:
    """The call a coordinating tool means: the one the member names, in any state, then the
    conversation's call, then the only open call the member coordinates or started. With several,
    ``ToolRefusal`` lists them and asks which. Only the member's own calls are listed, so the
    refusal says nothing about anyone else's."""
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
    mine = [
        e
        for e in await events.for_today(household_id, today)
        if e.kind is EventKind.CALL
        and e.state in OPEN_STATES
        and (ref.day is None or e.day == ref.day)
        and (is_coordinator(e, actor) or e.created_by == actor.id)
    ]
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise ToolRefusal(NOT_YOURS)
    listed = "; ".join(
        f"{e.slot.value} on {e.day.isoformat()} ({day_words(e.day, today)}, {e.state.label.lower()})" for e in mine
    )
    raise ToolRefusal(f"The member has several open calls: {listed}. Ask which, then call again with its slot and day.")


def person(ctx: CallContext, labels: Pseudonyms, label: str) -> int | None:
    """The member ``label`` names in this conversation, such as "Member 2", or None for an empty
    string. Only a household member the conversation's map holds: a label nobody has, or a guest's,
    gets ``NOT_A_LABEL``. The service decides the rest, such as someone deactivated or not in."""
    label = " ".join(label.split())
    if not label:
        return None
    match = _LABEL.fullmatch(label)
    if match is None:
        raise ToolRefusal(LABEL_ONLY)
    number = int(match.group(1))
    member_id = next((m for m, n in labels.numbers.items() if n == number), None)
    member = ctx.members.get(member_id) if member_id is not None else None
    if member is None or member.is_guest:
        raise ToolRefusal(NOT_A_LABEL.format(label=label))
    return member.id


def _can_do(ctx: CallContext, item: CatalogItem, mode: Mode, who: int, label: str) -> None:
    """``who`` can do ``mode``'s role for ``item`` (``check_assignee``), with the refusal naming
    them by ``label``: ``check_assignee``'s own names them."""
    if not can_do(item, mode, who):
        raise EventRuleViolation(f"{item.name} has a cook list, and {label} isn't on it.", code="cook_list")
    check_assignee(ctx, item, mode, who)


def _named(mode: Mode, lead: int | None, pickup: int | None, label: Labeler) -> dict[AssignmentRole, str]:
    """The roles the member named someone for, by label."""
    named: dict[AssignmentRole, str] = {}
    if lead is not None:
        named[LEAD_ROLE[mode]] = label(lead)
    if pickup is not None:
        named[AssignmentRole.PICKUP] = label(pickup)
    return named


def _holder(ctx: CallContext, member_id: int, label: Labeler) -> str:
    return "a guest" if ctx.is_guest(member_id) else label(member_id)


# Look up ----------------------------------------------------------------------------------------


async def look_up(db: AsyncSession, actor: Member, event: Event, *, label: Labeler, today: date) -> dict[str, Any]:
    """What the coordinator sees on the call page that the tools need, for their own call only:
    who's in by label with their role offers, how many guests are in, while voting the picks with
    whether each is tied and the shuffles left, and once decided the plan with who does each role.
    No maybe or out, no vote, and no tally. Anyone else gets ``ONLY_THE_COORDINATOR``, and nothing."""
    ctx = await load(db, event)
    event = ctx.event
    if not actor.active or not is_coordinator(event, actor):
        raise ToolRefusal(ONLY_THE_COORDINATOR)
    members = [r for r in ctx.participants if (m := ctx.members.get(r.member_id)) is not None and m.is_member]
    who = [
        {"who": label(r.member_id), "offers": [w for role, w in OFFER_WORDS.items() if role in r.offers]}
        for r in sorted(members, key=lambda r: r.member_id)
    ]
    found: dict[str, Any] = {
        "call": call_basics(ctx, today),
        "you_coordinate": True,
        "in": sorted(who, key=lambda p: int(p["who"].split()[-1])),
        "guests_in": sum(1 for r in ctx.participants if ctx.is_guest(r.member_id)),
    }
    if event.state is EventState.VOTING:
        tied = cc.tied_ids(ctx)
        found["picks"] = [
            {"pick": p.id, "name": pick_name(ctx, p), "mode": p.mode.value, "tied": p.id in tied} for p in ctx.picks
        ]
        found["shuffles_left"] = cc.shuffles_left(ctx)
    item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
    if event.state is EventState.DECIDED and item is not None and event.decided_mode is not None:
        meal = event.meal_time.astimezone(ctx.zone) if event.meal_time is not None else None
        found["decision"] = {
            "name": item.name,
            "mode": event.decided_mode.value,
            "ways": [m.value for m in MODE_ORDER if m in item.modes],
            "meal_time": f"{meal:%H:%M}" if meal is not None else None,
            "roles": {a.role.value: _holder(ctx, a.member_id, label) for a in ctx.assignments if a.role in ROLES},
            "waiting_for_you_to_choose_roles": event.roles_due_at is not None,
        }
    return found


ROLES = (AssignmentRole.COOK, AssignmentRole.ORDER, AssignmentRole.PICKUP)


# Deciding a pick (FR-15, FR-16) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Roles:
    """Roles the member named with a lock: the way, who cooks or orders, and who picks up, by label."""

    way: Mode | None = None
    lead: str = ""
    pickup: str = ""

    @property
    def any(self) -> bool:
        return self.way is not None or bool(self.lead) or bool(self.pickup)


async def decide(
    db: AsyncSession,
    actor: Member,
    event: Event,
    *,
    handle: int,
    name: str,
    mode: Mode | None,
    tie: bool,
    roles: Roles,
    labels: Pseudonyms,
    label: Labeler,
    today: date,
) -> DraftSpec:
    """Lock a pick (``calls.lock``) or break a tie with it (``coordinator.break_tie``), checked as
    each checks, the coordinator's say first, so nobody else learns which picks exist. With
    ``roles``, **Assign roles** runs after (``coordinator.choose_roles``): each person named must
    be able to do it, and each role the way needs must have someone."""
    ctx = await load(db, event)
    event = ctx.event
    if tie:
        check_can_break_tie(event, actor)
    else:
        check_can_lock(event, actor)
    pick, shown = find_pick(ctx, handle, name, mode)
    if tie and pick.id not in cc.tied_ids(ctx):
        raise EventRuleViolation(coordinator.NOT_TIED, code="not_tied")
    when, slot = _words(event, today)
    payload: dict[str, Any] = {"proposal_id": pick.id, "name": shown, "mode": pick.mode.value, "tie": tie}
    named: dict[AssignmentRole, str] = {}
    way: Mode | None = None
    if roles.any:
        item = ctx.items.get(pick.item_id) if pick.item_id is not None else None
        if item is None:
            raise ToolRefusal(NOT_A_CATALOG_PICK)
        chosen = roles.way or pick.mode
        check_mode(item, chosen)
        lead, pickup = person(ctx, labels, roles.lead), person(ctx, labels, roles.pickup)
        if pickup is not None and chosen is not Mode.PICKUP:
            raise ToolRefusal(NO_PICKUP)
        _check_roles(ctx, item, chosen, lead, pickup, label)
        way = chosen if chosen is not pick.mode else None
        named = _named(chosen, lead, pickup, label)
        payload |= {"way": way.value if way else None, "lead": lead, "pickup": pickup, "item_id": item.id}
    lines = decide_lines(shown, pick.mode, tie=tie, slot=slot, when=when, way=way, roles=named)
    return DraftSpec(DraftKind.DECIDE, payload, decide_title(shown, pick.mode, tie=tie), lines, event)


def _check_roles(
    ctx: CallContext, item: CatalogItem, mode: Mode, lead: int | None, pickup: int | None, label: Labeler
) -> None:
    """As ``choose_roles`` checks: whoever is named can do it, and each role ``mode`` needs has
    someone, named or the first volunteer."""
    for who in (lead, pickup):
        if who is not None:
            _can_do(ctx, item, mode, who, label(who))
    found = default_roles(mode, ctx.coverage, item.cook_ids, lead=lead, pickup=pickup)
    for role in MODE_ROLES[mode]:
        if role not in found:
            verb = coordinator.ROLE_VERB[role]
            raise EventRuleViolation(coordinator.NO_ONE_CHOSEN.format(verb=verb), code="no_one_chosen")


# Shuffle (FR-61) --------------------------------------------------------------------------------


async def shuffle(db: AsyncSession, actor: Member, event: Event, *, today: date) -> DraftSpec:
    """Shuffle the picks (``coordinator.shuffle``), as it checks: the coordinator's say, the
    shuffles left, and that something can be redrawn."""
    ctx = await load(db, event)
    event = ctx.event
    check_can_shuffle(event, actor)
    if not cc.shuffle_picks(ctx):
        raise EventRuleViolation(coordinator.NOTHING_TO_SHUFFLE, code="nothing_to_shuffle")
    when, slot = _words(event, today)
    voted = any(ctx.votes.get(p.id) for p in ctx.proposals)
    lines = shuffle_lines(anyone_voted=voted, left_after=max(MAX_SHUFFLES - event.shuffles - 1, 0))
    title = f"Shuffle the picks for {when}'s {slot.value}"
    return DraftSpec(DraftKind.SHUFFLE, {"shuffles": event.shuffles}, title, lines, event)


# Change the plan, as Adjust does (spec 5.7) ----------------------------------------------------


def _wall(ctx: CallContext, at: datetime) -> time:
    local = at.astimezone(ctx.zone)
    return time(local.hour, local.minute)


def as_opened(ctx: CallContext) -> Adjustment:
    """The plan as it is now, as **Adjust** opens with it."""
    event = ctx.event
    mode = event.decided_mode
    holders = {a.role: a.member_id for a in ctx.assignments}
    return Adjustment(
        meal_time=_wall(ctx, event.meal_time) if event.meal_time is not None else None,
        assignee_id=holders.get(LEAD_ROLE[mode]) if mode is not None else None,
        coordinator_id=event.coordinator_id,
        mode=mode,
        pickup_id=holders.get(AssignmentRole.PICKUP) if mode is Mode.PICKUP else None,
        item_id=event.decided_item_id,
    )


def adjustment_json(adjustment: Adjustment) -> dict[str, Any]:
    return {
        "meal_time": f"{adjustment.meal_time:%H:%M}" if adjustment.meal_time is not None else None,
        "assignee_id": adjustment.assignee_id,
        "coordinator_id": adjustment.coordinator_id,
        "mode": adjustment.mode.value if adjustment.mode is not None else None,
        "pickup_id": adjustment.pickup_id,
        "item_id": adjustment.item_id,
        "place_mode": adjustment.place_mode.value if adjustment.place_mode is not None else None,
    }


def adjustment_from(raw: Mapping[str, Any]) -> Adjustment:
    def number(name: str) -> int | None:
        value = raw.get(name)
        return int(value) if value is not None else None

    def way(name: str) -> Mode | None:
        value = raw.get(name)
        return Mode(str(value)) if value else None

    meal = raw.get("meal_time")
    return Adjustment(
        meal_time=time.fromisoformat(str(meal)) if meal else None,
        assignee_id=number("assignee_id"),
        coordinator_id=number("coordinator_id"),
        mode=way("mode"),
        pickup_id=number("pickup_id"),
        item_id=number("item_id"),
        place_mode=way("place_mode"),
    )


async def _place(db: AsyncSession, actor: Member, name: str, way: Mode | None) -> CatalogItem | None:
    if not name:
        return None
    found = [i for i in await CatalogRepository(db).items(actor.household_id) if i.active and i.key == name_key(name)]
    if not found:
        raise ToolRefusal(NO_SUCH_PLACE)
    if len(found) > 1 and way is not None:  # a restaurant and a home meal: the way says which
        found = [i for i in found if way in i.modes] or found
    if len(found) > 1:
        raise ToolRefusal(TWO_PLACES)
    return found[0]


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """What the member asked to change: each part empty or None to leave it."""

    meal_time: time | None = None
    place: str = ""
    way: Mode | None = None
    lead: str = ""
    pickup: str = ""


async def plan(
    db: AsyncSession,
    actor: Member,
    event: Event,
    request: PlanRequest,
    *,
    labels: Pseudonyms,
    label: Labeler,
    today: date,
    now: datetime,
) -> DraftSpec:
    """Change the meal time, the place (a catalog item), the way, or who cooks, orders, or picks
    up, as **Adjust** does (``coordinator.plan_adjustment``, which only reads): the coordinator
    alone, once it's decided, checked first. The draft holds the plan as it is (``as_opened``),
    so Confirm refuses a part someone changed since rather than writing over it."""
    ctx = await load(db, event)
    event = ctx.event
    check_can_adjust(event, actor)
    opened = as_opened(ctx)
    current = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
    if current is None or opened.mode is None:  # pragma: no cover - a decided call has both
        raise EventRuleViolation("There's no decision to change.", code="not_decided")
    item = await _place(db, actor, request.place, request.way)
    if item is not None and item.id == current.id:
        item = None  # the place it has: another way is a switch of way, as Adjust does it
    lead, pickup = person(ctx, labels, request.lead), person(ctx, labels, request.pickup)
    there = item or current
    way = request.way or (opened.mode if opened.mode in there.modes else None)
    if pickup is not None and way is not Mode.PICKUP:
        raise ToolRefusal(NO_PICKUP)
    for who in (lead, pickup):
        if who is not None and way is not None:
            _can_do(ctx, there, way, who, label(who))
    if item is not None:
        wanted = Adjustment(
            meal_time=request.meal_time,
            assignee_id=lead,
            pickup_id=pickup,
            item_id=item.id,
            place_mode=request.way,
        )
    else:
        wanted = Adjustment(meal_time=request.meal_time, assignee_id=lead, mode=request.way, pickup_id=pickup)
    changes = await coordinator.plan_adjustment(db, actor, event.id, wanted, opened=opened, run=event.run)
    if changes.empty:
        raise ToolRefusal(NOTHING_TO_DO)
    meal = (opened.meal_time, changes.meal_time) if changes.meal_time and opened.meal_time else None
    place = (current.name, item.name) if changes.place_asked and item is not None else None
    if place is not None:
        new_way = changes.place_mode
        switched = (opened.mode, new_way) if new_way is not None and new_way is not opened.mode else None
    else:
        switched = (opened.mode, changes.mode) if changes.mode is not None else None
    named = _named(switched[1] if switched else opened.mode, lead, pickup, label)
    change = PlanChange(meal_time=meal, place=place, way=switched, roles=named)
    when, slot = _words(event, today)
    lines = plan_lines(change, ahead=cc.before_the_meal(event, now))
    payload = {"wanted": adjustment_json(wanted), "opened": adjustment_json(opened)}
    return DraftSpec(DraftKind.PLAN, payload, plan_title(slot=slot, when=when), lines, event)


# Hand off, mark done, call off ----------------------------------------------------------------


async def hand_off(
    db: AsyncSession, actor: Member, event: Event, to: str, *, labels: Pseudonyms, label: Labeler, today: date
) -> DraftSpec:
    """Hand coordinating to the member ``to`` labels (``calls.hand_off``), checked as its
    ``handoff_target`` checks: the coordinator's say first, then someone active who isn't a
    guest, and once it's decided, someone who's in."""
    ctx = await load(db, event)
    event = ctx.event
    if not is_coordinator(event, actor):
        check_handoff(event, actor, actor)  # its refusal for anyone but the coordinator, before the label
    target = person(ctx, labels, to)
    if target is None:
        raise ToolRefusal(LABEL_ONLY)
    handoff_target(ctx, actor, target)
    when, slot = _words(event, today)
    shown = label(target)
    lines = hand_off_lines(shown, slot=slot, when=when, decided=event.state is EventState.DECIDED)
    title = f"Hand off coordinating {when}'s {slot.value} to {shown}"
    return DraftSpec(DraftKind.HAND_OFF, {"to": target}, title, lines, event)


async def mark_done(db: AsyncSession, actor: Member, event: Event, *, today: date, now: datetime) -> DraftSpec:
    """Mark the meal done (``calls.mark_done``), as ``check_can_mark_done`` checks: the
    coordinator, or someone who's in, once it's meal time."""
    ctx = await load(db, event)
    event = ctx.event
    check_can_mark_done(event, actor, ctx.response_of(actor.id), now=now)
    item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
    name = item.name if item is not None else "the meal"
    when, slot = _words(event, today)
    title = f"Mark {when}'s {slot.value} done"
    return DraftSpec(
        DraftKind.MARK_DONE, {"state": event.state.value}, title, done_lines(name, slot=slot, when=when), event
    )


async def call_off(db: AsyncSession, actor: Member, event: Event, note: str, *, today: date) -> DraftSpec:
    """Call off a call (``calls.cancel_call``, FR-56), as ``check_can_cancel`` checks: the
    coordinator, or the member who started an ad-hoc call until it's decided. An offer is the
    offerer's to take back (``draft_take_back_offer``). The reason is the member's own words,
    cleaned as the service cleans it, and the card shows it."""
    if event.is_offer:
        raise ToolRefusal(OFFER_NOT_A_CALL)
    check_can_cancel(event, actor)
    reason = clean_cancel_note(note)
    when, slot = _words(event, today)
    lines = call_off_lines(
        slot=slot,
        when=when,
        posted=event.state is not EventState.SCHEDULED,
        decided=event.state is EventState.DECIDED,
        note=reason,
    )
    payload = {"note": reason, "state": event.state.value}
    return DraftSpec(DraftKind.CALL_OFF, payload, f"Call off {when}'s {slot.value} call", lines, event)


# Confirm ----------------------------------------------------------------------------------------


def _plan_changed(exc: EventRuleViolation | NotFound) -> EventRuleViolation | NotFound:
    """A refusal at Confirm in the assistant's words, and naming nobody: Adjust's for a part
    changed since says to open the modal again, and ``check_assignee``'s names the person."""
    code = getattr(exc, "code", None)
    if code == "plan_changed":
        return _stale()
    if code == "cook_list":
        return EventRuleViolation("Someone you named isn't on that home meal's cook list.", code="cook_list")
    return exc


async def confirm(
    db: AsyncSession, actor: Member, draft: Draft, *, today: date, now: datetime, use: Use
) -> ActionConfirmed:
    """**Confirm** on a coordinating draft, as ``actor``, who is ``tool_actor`` of its member: the
    use case the call page or Slack's button runs. Under the write lock it uses the draft first,
    so a second Confirm hears it's used, then checks the call's run and what the draft expects,
    then runs the use case, which commits the draft's use with its change, or rolls both back.
    The use case checks everything again, the coordinator's say included."""
    payload = draft.payload
    async with write_transaction(db):
        await use()
        event = await EventRepository(db).get(actor.household_id, draft.event_id or 0)
        if event is None:
            raise NotFound(cc.NO_SUCH_CALL)
        check_run(event, draft.run)
        when, slot = _words(event, today)
        run = draft.run
        if draft.kind is DraftKind.DECIDE:
            proposal_id, tie = int(payload["proposal_id"]), bool(payload["tie"])
            if tie:
                await coordinator.break_tie(db, actor, event.id, proposal_id, run=run, now=now)
            else:
                await calls.lock(db, actor, event.id, proposal_id, run=run, now=now)
            decided = f"Confirmed. {when.capitalize()}'s {slot.value} call is decided on {payload['name']}."
        elif draft.kind is DraftKind.SHUFFLE:
            if event.shuffles != int(payload["shuffles"]):
                raise _stale("The picks were shuffled since the draft. Look at them again first.")
            done = await coordinator.shuffle(db, actor, event.id, run=run, now=now)
            left = "1 shuffle" if done.shuffles_left == 1 else f"{done.shuffles_left} shuffles"
            return ActionConfirmed(event.id, f"Confirmed. {done.replaced} picks replaced; {left} left.")
        elif draft.kind is DraftKind.PLAN:
            wanted, opened = adjustment_from(payload["wanted"]), adjustment_from(payload["opened"])
            try:
                await coordinator.adjust(db, actor, event.id, wanted, opened=opened, run=run or 0, now=now)
            except PartlyAdjusted as exc:
                return ActionConfirmed(event.id, f"Partly confirmed: some of the plan changed, but not all. {exc}")
            except (EventRuleViolation, NotFound) as exc:
                raise _plan_changed(exc) from None
            return ActionConfirmed(event.id, f"Confirmed. The plan for {when}'s {slot.value} is changed.")
        elif draft.kind is DraftKind.HAND_OFF:
            await calls.hand_off(db, actor, event.id, int(payload["to"]), run=run, now=now)
            return ActionConfirmed(event.id, f"Confirmed. You handed off coordinating {when}'s {slot.value}.")
        elif draft.kind is DraftKind.MARK_DONE:
            if event.state.value != payload["state"]:
                raise _stale()
            await calls.mark_done(db, actor, event.id, now=now)
            return ActionConfirmed(event.id, f"Confirmed. {when.capitalize()}'s {slot.value} is done.")
        else:
            if event.state.value != payload["state"]:
                raise _stale()
            note = payload.get("note")
            await calls.cancel_call(db, actor, event.id, note=str(note) if note else None, run=run, now=now)
            return ActionConfirmed(event.id, f"Confirmed. {when.capitalize()}'s {slot.value} call is called off.")
    return await _assign_after(db, actor, draft, decided, now=now)


async def _assign_after(
    db: AsyncSession, actor: Member, draft: Draft, decided: str, *, now: datetime
) -> ActionConfirmed:
    """**Assign roles** after a lock that named them (``coordinator.choose_roles``), in its own
    transaction: the lock stands whatever happens. Only on the place and way it locked."""
    payload = draft.payload
    event_id = draft.event_id or 0
    if payload.get("item_id") is None:
        return ActionConfirmed(event_id, decided)
    way = payload.get("way")
    lead, pickup = payload.get("lead"), payload.get("pickup")
    try:
        await coordinator.choose_roles(
            db,
            actor,
            event_id,
            mode=Mode(str(way)) if way else None,
            member_id=int(lead) if lead is not None else None,
            pickup_id=int(pickup) if pickup is not None else None,
            run=draft.run,
            as_coordinator=True,
            expect=coordinator.still_showing(int(payload["item_id"]), Mode(str(payload["mode"]))),
            now=now,
        )
    except (EventRuleViolation, NotFound) as exc:
        return ActionConfirmed(event_id, f"{decided} The roles weren't assigned: {_plan_changed(exc)}")
    return ActionConfirmed(event_id, f"{decided} The roles are assigned.")
