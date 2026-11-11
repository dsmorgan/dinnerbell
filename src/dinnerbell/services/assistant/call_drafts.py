# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drafts of the call actions a member takes in public anyway (#251, spec 6.14, **Tools**):
answering, joining late, voting and taking a vote back, suggesting a catalog item, taking or
giving up a role, and taking back an offer.

Each draft function checks now what the use case would refuse, with the domain's own rule
checks and words, and returns a ``DraftSpec``: the change, and what **Confirm** does, in lines
worked out with the same rules (``domain.assistant_drafts``). ``drafts.store_action`` stores it.
``confirm`` is what **Confirm** runs, in ``drafts.confirm_draft``'s transaction after it used
the draft: the use case the web form or Slack button calls, which checks everything again.

None of these filter anything, so a refusal can say why: it's about the member's own answer
or the call's state, never anyone's stance. Nothing here returns another member's answer,
name, or vote.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.events import EventRepository
from dinnerbell.domain.assistant_drafts import (
    ROLE_DOING,
    Draft,
    DraftKind,
    answer_lines,
    day_words,
    offer_off_lines,
    role_lines,
    suggestion_lines,
    vote_lines,
)
from dinnerbell.domain.catalog import CatalogItem, Mode, name_key
from dinnerbell.domain.events import (
    Details,
    Event,
    EventState,
    Proposal,
    Response,
    RoleOffer,
    Status,
    check_can_cancel,
    check_can_join_late,
    check_can_offer_role,
    check_can_respond,
    check_can_vote,
    check_details,
    check_run,
    counts_vote,
    drops_coordinating,
    is_coordinator,
    late_vote_counts,
    with_late_join,
    with_status,
    with_usual_offers,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls
from dinnerbell.services.assistant.acting import ToolRefusal
from dinnerbell.services.call_context import CallContext, load
from dinnerbell.services.errors import NotFound

NO_SUCH_PROPOSAL = (
    "That isn't one of this call's proposals in that mode. Call look_up_call for its proposals, and use a name "
    "and mode it returns."
)
NO_SUCH_ITEM = "The catalog has no active item by that name. Look it up with look_up_catalog_items."
NO_OPEN_OFFER = "The member has no open offer for that. Say so."
NOTHING_TO_DO = "That's how it is already, so there's nothing to draft. Say so."


@dataclass(frozen=True, slots=True)
class DraftSpec:
    """A call action ready to store: its kind, what the use case needs, and what Confirm does."""

    kind: DraftKind
    payload: Mapping[str, Any]
    title: str
    lines: tuple[str, ...]
    event: Event


@dataclass(frozen=True, slots=True)
class ActionConfirmed:
    """What a confirmed call action did: ``text`` for the member, and the call or offer it
    changed, for a link."""

    event_id: int
    text: str
    offer: bool = False


def _call_words(event: Event, today: date) -> tuple[str, str]:
    """The call's day and meal in words, such as ("today", "dinner")."""
    return day_words(event.day, today), event.slot.value


def _joined(ctx: CallContext, actor: Member, mine: Response | None, now: datetime) -> Response | None:
    """``actor``'s answer once they join late, if they need to and may (``check_can_join_late``,
    which raises otherwise), or None when they're in already."""
    if not check_can_join_late(ctx.event, actor, mine):
        return None
    return with_late_join(mine, actor.id, now)


# Answers and joining late ------------------------------------------------------------------------


async def answer(
    db: AsyncSession, actor: Member, event: Event, status: Status, *, today: date, now: datetime
) -> DraftSpec:
    """In, out, or maybe (``calls.respond``) while answers are open. An In while voting is
    joining late (``calls.join_late``); anything else then gets ``respond``'s refusal."""
    ctx = await load(db, event)
    event = ctx.event
    mine = ctx.response_of(actor.id)
    when, slot = _call_words(event, today)
    if event.state is EventState.VOTING and status is Status.IN:
        if _joined(ctx, actor, mine, now) is None:
            raise ToolRefusal(NOTHING_TO_DO)
        lines = answer_lines(status, slot=event.slot, when=when, join_late=True, vote_counts=late_vote_counts(mine))
        title = f"Join {when}'s {slot} call late"
        return DraftSpec(DraftKind.ANSWER, {"status": status.value, "join_late": True}, title, lines, event)
    check_can_respond(event, actor)
    if mine is not None and mine.status is status and mine.away_until is None:
        raise ToolRefusal(NOTHING_TO_DO)
    base = with_status(mine, actor.id, status, now)
    lines = answer_lines(
        status,
        slot=event.slot,
        when=when,
        usual_offers=with_usual_offers(base, actor).offers != base.offers,
        withdraws_roles=status is Status.OUT and mine is not None and bool(mine.offers),
        coordinating=drops_coordinating(event, base),
    )
    word = {Status.IN: "In", Status.OUT: "Out", Status.MAYBE: "Maybe"}[status]
    title = f"Answer {word} for {when}'s {slot} call"
    return DraftSpec(DraftKind.ANSWER, {"status": status.value, "join_late": False}, title, lines, event)


# Votes -------------------------------------------------------------------------------------------


def _pick(ctx: CallContext, name: str, mode: Mode | None) -> Proposal:
    found = [
        p
        for p in ctx.picks
        if name_key(cc.item_info(ctx, p).name) == name_key(name) and (mode is None or p.mode is mode)
    ]
    if len(found) != 1:
        raise ToolRefusal(NO_SUCH_PROPOSAL)
    return found[0]


async def vote(
    db: AsyncSession,
    actor: Member,
    event: Event,
    name: str,
    mode: Mode | None,
    *,
    voting: bool,
    today: date,
    now: datetime,
) -> DraftSpec:
    """A vote for one of the call's picks (``calls.vote``), or taking it back (``calls.unvote``).
    Someone who isn't in joins late first, as the button that asks them does (``join_late``);
    a coordinator who isn't in votes without joining, as they always could."""
    ctx = await load(db, event)
    event = ctx.event
    mine = ctx.response_of(actor.id)
    pick = _pick(ctx, name, mode)
    item = cc.item_info(ctx, pick).name
    voted = actor.id in ctx.votes.get(pick.id, [])
    seen, join_late = mine, False
    already_in = (mine is not None and mine.is_in) or (actor.active and is_coordinator(event, actor))
    if voting and not already_in and event.state is EventState.VOTING:
        seen = _joined(ctx, actor, mine, now)
        join_late = seen is not None
    check_can_vote(event, actor, seen)
    if voted is voting:
        raise ToolRefusal(NOTHING_TO_DO)
    lines = vote_lines(item, pick.mode, vote=voting, join_late=join_late, vote_counts=counts_vote(seen))
    label = f"{item} ({pick.mode.label.lower()})"
    title = f"Vote for {label}" if voting else f"Take back your vote for {label}"
    payload = {"proposal_id": pick.id, "vote": voting, "join_late": join_late, "name": item}
    return DraftSpec(DraftKind.VOTE, payload, title, lines, event)


# Suggestions -------------------------------------------------------------------------------------


async def _item(db: AsyncSession, actor: Member, name: str) -> CatalogItem:
    found = [i for i in await CatalogRepository(db).items(actor.household_id) if i.active and i.key == name_key(name)]
    if len(found) != 1:
        raise ToolRefusal(NO_SUCH_ITEM)
    return found[0]


def _details(ctx: CallContext, mine: Response | None, member_id: int, add: int) -> Details:
    """The member's Details with ``add`` among their suggestions, everything else as it is."""
    theirs = [s for s in ctx.suggestions if s.member_id == member_id]
    return Details(
        eat_from=mine.eat_from if mine else None,
        eat_until=mine.eat_until if mine else None,
        suggestion_item_ids=(*(s.item_id for s in theirs if s.item_id is not None), add),
        suggestion_texts=tuple(s.free_text for s in theirs if s.free_text),
        note=mine.note if mine else None,
    )


async def suggestion(
    db: AsyncSession, actor: Member, event: Event, name: str, *, today: date, now: datetime
) -> DraftSpec:
    """A catalog item added to the member's suggestions (``calls.save_details``), while answers
    are open. A suggestion has no filter: ranking weighs it, and the vote's filters still apply."""
    ctx = await load(db, event)
    event = ctx.event
    check_can_respond(event, actor)
    item = await _item(db, actor, name)
    mine = ctx.response_of(actor.id)
    if any(s.member_id == actor.id and s.item_id == item.id for s in ctx.suggestions):
        raise ToolRefusal(NOTHING_TO_DO)
    active = [i.id for i in ctx.items.values() if i.active]
    check_details(_details(ctx, mine, actor.id, item.id), active_item_ids=active)
    when, slot = _call_words(event, today)
    lines = suggestion_lines(item.name, slot=event.slot, when=when, counts_in=mine is None)
    title = f"Suggest {item.name} for {when}'s {slot} call"
    return DraftSpec(DraftKind.SUGGESTION, {"item_id": item.id, "name": item.name}, title, lines, event)


# Roles -------------------------------------------------------------------------------------------


async def role(
    db: AsyncSession, actor: Member, event: Event, offered_role: RoleOffer, *, offered: bool, today: date, now: datetime
) -> DraftSpec:
    """Offering a role, or giving it up (``calls.offer_role``). While voting, someone who isn't in
    joins late first (``calls.join_late``), as a late joiner may then offer one."""
    ctx = await load(db, event)
    event = ctx.event
    mine = ctx.response_of(actor.id)
    seen, join_late = mine, False
    if offered and event.state is EventState.VOTING and (mine is None or not mine.is_in):
        seen = _joined(ctx, actor, mine, now)
        join_late = seen is not None
    check_can_offer_role(event, actor, seen)
    has = seen is not None and offered_role in seen.offers
    if has is offered:
        raise ToolRefusal(NOTHING_TO_DO)
    when, slot = _call_words(event, today)
    lines = role_lines(
        offered_role,
        offered=offered,
        slot=event.slot,
        when=when,
        join_late=join_late,
        vote_counts=late_vote_counts(mine),
        counts_in=offered and not join_late and (seen is None or seen.status is Status.OUT),
    )
    doing = ROLE_DOING[offered_role]
    title = f"Offer to {doing} for {when}'s {slot} call" if offered else f"Stop offering to {doing}"
    payload = {"role": offered_role.value, "offered": offered, "join_late": join_late}
    return DraftSpec(DraftKind.ROLE, payload, title, lines, event)


# Taking back an offer ----------------------------------------------------------------------------


async def own_offer(db: AsyncSession, actor: Member, slot: Slot | None, day: date | None, *, today: date) -> Event:
    """The member's own open offer for ``slot`` on ``day``, either of which may be None for any.
    With several, ``ToolRefusal`` lists them and asks which."""
    events = EventRepository(db)
    if slot is not None:
        found = await events.offers_for(actor.household_id, slot, day or today)
    else:
        found = [e for e in await events.for_today(actor.household_id, today) if day is None or e.day == day]
    mine = [e for e in found if e.is_offer and e.created_by == actor.id and e.state.is_open]
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise ToolRefusal(NO_OPEN_OFFER)
    listed = "; ".join(f"{e.slot.value} on {e.day.isoformat()} ({day_words(e.day, today)})" for e in mine)
    raise ToolRefusal(
        f"The member has several open offers: {listed}. Ask which, then call again with its slot and day."
    )


async def offer_off(db: AsyncSession, actor: Member, offer: Event, *, today: date) -> DraftSpec:
    """Taking back the member's own offer: calling it off as its offerer (``calls.cancel_call``)."""
    ctx = await load(db, offer)
    check_can_cancel(ctx.event, actor)
    item = ctx.items.get(offer.decided_item_id) if offer.decided_item_id is not None else None
    name = item.name if item is not None else "your offer"
    when, slot = _call_words(offer, today)
    title = f"Take back your {slot} offer of {name} for {when}"
    return DraftSpec(DraftKind.OFFER_OFF, {}, title, offer_off_lines(item=name, slot=offer.slot, when=when), offer)


# Confirm -----------------------------------------------------------------------------------------


async def confirm(db: AsyncSession, actor: Member, draft: Draft, *, today: date, now: datetime) -> ActionConfirmed:
    """Run the use case ``draft`` stands for, as ``actor``, who is ``tool_actor`` of its member.
    The caller holds the write lock and used the draft in it; the use case locks the call in
    the same transaction and commits both, or rolls both back. A draft for another run of the
    call gets ``check_run``'s refusal first, as a stale Slack card does.

    A role that joins late first commits the late join, with the draft's use, before it offers
    the role: if the call closes in between, the member stays joined, and the card says why."""
    if draft.event_id is None:  # pragma: no cover - every call action names its call
        raise NotFound(cc.NO_SUCH_CALL)
    event = await EventRepository(db).get(actor.household_id, draft.event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    check_run(event, draft.run)
    run, payload = draft.run, draft.payload
    when, slot = _call_words(event, today)
    if draft.kind is DraftKind.ANSWER:
        status = Status(str(payload["status"]))
        if payload.get("join_late"):
            await calls.join_late(db, actor, event.id, run=run, now=now)
            return ActionConfirmed(event.id, f"Confirmed. You joined {when}'s {slot} call late.")
        await calls.respond(db, actor, event.id, status, run=run, now=now)
        word = {Status.IN: "in for", Status.OUT: "out of", Status.MAYBE: "a maybe for"}[status]
        return ActionConfirmed(event.id, f"Confirmed. You're {word} {when}'s {slot} call.")
    if draft.kind is DraftKind.VOTE:
        proposal_id, name = int(payload["proposal_id"]), str(payload["name"])
        if payload.get("vote"):
            await calls.vote(db, actor, event.id, proposal_id, join_late=bool(payload.get("join_late")), now=now)
            return ActionConfirmed(event.id, f"Confirmed. Your vote for {name} is in.")
        await calls.unvote(db, actor, event.id, proposal_id, now=now)
        return ActionConfirmed(event.id, f"Confirmed. You took back your vote for {name}.")
    if draft.kind is DraftKind.SUGGESTION:
        ctx = await load(db, event)
        details = _details(ctx, ctx.response_of(actor.id), actor.id, int(payload["item_id"]))
        await calls.save_details(db, actor, event.id, details, run=run, now=now)
        return ActionConfirmed(event.id, f"Confirmed. {payload['name']} is one of your suggestions.")
    if draft.kind is DraftKind.ROLE:
        chosen, offered = RoleOffer(str(payload["role"])), bool(payload["offered"])
        if payload.get("join_late"):
            await calls.join_late(db, actor, event.id, run=run, now=now)
        await calls.offer_role(db, actor, event.id, chosen, offered, run=run, now=now)
        doing = ROLE_DOING[chosen]
        done = f"You're down to {doing}" if offered else f"You're no longer down to {doing}"
        return ActionConfirmed(event.id, f"Confirmed. {done} for {when}'s {slot} call.")
    if draft.kind is DraftKind.OFFER_OFF:
        await calls.cancel_call(db, actor, event.id, run=run, now=now)
        return ActionConfirmed(event.id, "Confirmed. Your offer is called off.", offer=True)
    raise NotFound(cc.NO_SUCH_CALL)  # pragma: no cover - CALL_ACTIONS names every kind above
