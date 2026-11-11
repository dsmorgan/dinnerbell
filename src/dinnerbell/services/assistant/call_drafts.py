# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drafts of the call actions a member takes in public anyway (#251, spec 6.14, **Tools**):
answering, joining late, voting and taking a vote back, suggesting a catalog item, taking or
giving up a role, taking back an offer, inviting a guest and answering for one, and starting an
ad-hoc call (FR-3).

Each draft function checks now what the use case would refuse, with the domain's own rule
checks and words, and returns a ``DraftSpec``: the change, and what **Confirm** does, in lines
worked out with the same rules (``domain.assistant_drafts``). ``drafts.store_action`` stores it.
``confirm`` is what **Confirm** runs, in ``drafts.confirm_draft``'s transaction after it used
the draft: the use case the web form or Slack button calls, which checks everything again.

None of these filter anything, so a refusal can say why: it's about the member's own answer
or the call's state, never anyone's stance. Nothing here returns another member's answer,
name, or vote. A guest is named only as the member wrote them, and nothing stored about a guest,
such as their dietary needs, goes back to the model. A cooking offer that stands in for a call
gets a fixed refusal, since the service's names the cook.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    call_lines,
    day_words,
    guest_answer_lines,
    invite_lines,
    offer_off_lines,
    role_lines,
    suggestion_lines,
    vote_lines,
)
from dinnerbell.domain.catalog import CatalogItem, Mode, TagKind, name_key
from dinnerbell.domain.events import (
    Details,
    Event,
    EventRuleViolation,
    EventState,
    Proposal,
    Response,
    RoleOffer,
    Status,
    check_ad_hoc_day,
    check_can_cancel,
    check_can_join_late,
    check_can_offer_role,
    check_can_respond,
    check_can_vote,
    check_details,
    check_restart,
    check_run,
    counts_vote,
    drops_coordinating,
    is_coordinator,
    late_vote_counts,
    with_late_join,
    with_offer,
    with_status,
    with_usual_offers,
)
from dinnerbell.domain.guests import GuestInput, can_answer_for, check_can_invite, check_guest_input
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import InvalidMemberInput, Member
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls, lifecycle
from dinnerbell.services import guests as guest_service
from dinnerbell.services.assistant.acting import ToolRefusal
from dinnerbell.services.call_context import CallContext, load
from dinnerbell.services.errors import Forbidden, NotFound

NO_SUCH_PROPOSAL = "That isn't one of this call's picks. Call look_up_call, and send a pick it returns."
NO_SUCH_ITEM = "The catalog has no active item by that name. Look it up with look_up_catalog_items."
NO_OPEN_OFFER = "The member has no open offer for that. Say so."
NOTHING_TO_DO = "That's how it is already, so there's nothing to draft. Say so."
NO_SUCH_GUEST = (
    "That isn't a guest on this call. Use the guest's name exactly as the member wrote it, and don't guess at others."
)
SAME_NAME = "More than one saved guest has that name. Ask the member to invite them on the call's page."
COOKING_OFFER = "A cooking offer stands in for that call, so there's no call to start. Say so."
"""Not ``check_no_cooking_offer``'s words, which name the cook."""


@dataclass(frozen=True, slots=True)
class DraftSpec:
    """A call action ready to store: its kind, what the use case needs, and what Confirm does."""

    kind: DraftKind
    payload: Mapping[str, Any]
    title: str
    lines: tuple[str, ...]
    event: Event | None
    """The call the draft acts on; None for an ad-hoc call that doesn't exist yet."""


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


FREE_TEXT_PICK = "a member's own idea"
"""How a pick typed as free text reads to the model, by its ``pick`` handle: its text is what a
member wrote, and can carry anything, such as someone's name."""


def pick_name(ctx: CallContext, pick: Proposal) -> str:
    """A pick as the model may see it: its catalog item's name, or ``FREE_TEXT_PICK``."""
    if pick.item_id is not None and pick.item_id in ctx.items:
        return ctx.items[pick.item_id].name
    return FREE_TEXT_PICK


def _pick(ctx: CallContext, handle: int, name: str, mode: Mode | None) -> tuple[Proposal, str]:
    """The pick ``handle`` names (``look_up_call``'s ``pick``), or else the one pick with that
    name and mode, with how the draft names it: the name the member gave for a free-text pick
    found by name, since those are their own words, and ``FREE_TEXT_PICK`` otherwise."""
    if handle:
        found = [p for p in ctx.picks if p.id == handle]
    else:
        found = [
            p
            for p in ctx.picks
            if name_key(cc.item_info(ctx, p).name) == name_key(name) and (mode is None or p.mode is mode)
        ]
    if len(found) != 1:
        raise ToolRefusal(NO_SUCH_PROPOSAL)
    pick = found[0]
    shown = pick_name(ctx, pick)
    if shown == FREE_TEXT_PICK and not handle:
        shown = " ".join(name.split())
    return pick, shown


async def vote(
    db: AsyncSession,
    actor: Member,
    event: Event,
    name: str,
    mode: Mode | None,
    *,
    handle: int = 0,
    voting: bool,
    today: date,
    now: datetime,
) -> DraftSpec:
    """A vote for one of the call's picks (``calls.vote``), or taking it back (``calls.unvote``).
    Someone who isn't in joins late first, as the button that asks them does (``join_late``);
    a coordinator who isn't in votes without joining, as they always could. A call that isn't
    voting gets ``check_can_vote``'s refusal before any pick is looked up, so it never says
    which picks a call has."""
    ctx = await load(db, event)
    event = ctx.event
    mine = ctx.response_of(actor.id)
    if event.state is not EventState.VOTING:
        check_can_vote(event, actor, mine)
    pick, item = _pick(ctx, handle, name, mode)
    voted = actor.id in ctx.votes.get(pick.id, [])
    seen, join_late = mine, False
    already_in = (mine is not None and mine.is_in) or (actor.active and is_coordinator(event, actor))
    if voting and not already_in:
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
    joins late first, in the same use case (``offer_role``'s ``join_late``), as a late joiner may
    then offer one."""
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


# Guests (#38) ------------------------------------------------------------------------------------


async def _needs(db: AsyncSession, actor: Member, names: Sequence[str]) -> dict[int, str]:
    """The dietary tags ``names`` name, by ID, with the tags' own names."""
    tags = [t for t in await CatalogRepository(db).tags(actor.household_id) if t.kind is TagKind.DIETARY]
    found: dict[int, str] = {}
    for wanted in names:
        tag = next((t for t in tags if name_key(t.name) == name_key(wanted)), None)
        if tag is None:
            known = ", ".join(sorted((t.name for t in tags), key=name_key)) or "none"
            raise ToolRefusal(f"Dietary needs are the household's dietary tags: {known}.")
        found[tag.id] = tag.name
    return found


async def invite_guest(
    db: AsyncSession, actor: Member, event: Event, name: str, needs: Sequence[str], *, today: date
) -> DraftSpec:
    """Inviting a guest while answers are open (``guests.invite``): the household's saved guest
    with exactly that name, or a new one, with the dietary needs the member named. The model
    sends the name as the member wrote it, and gets back only that."""
    ctx = await load(db, event)
    event = ctx.event
    check_can_invite(event, actor)
    typed = " ".join(name.split())
    saved = [g for g in await guest_service.saved_guests(db, actor) if g.display_name.casefold() == typed.casefold()]
    if len(saved) > 1:
        raise ToolRefusal(SAME_NAME)
    guest = saved[0] if saved else None
    if guest is not None and needs:
        raise ToolRefusal("Dietary needs are for a new guest only. Draft the invitation without them.")
    named = await _needs(db, actor, needs)
    if guest is None:
        dietary = [t.id for t in await CatalogRepository(db).tags(actor.household_id) if t.kind is TagKind.DIETARY]
        check_guest_input(GuestInput(typed, frozenset(named)), dietary_tag_ids=dietary)
    elif guest.id in ctx.invitations:
        raise ToolRefusal(NOTHING_TO_DO)
    when, slot = _call_words(event, today)
    lines = invite_lines(
        typed,
        saved=guest is not None,
        registered=guest is not None and guest.is_registered_guest,
        needs=named.values(),
        slot=event.slot,
        when=when,
    )
    payload = {"guest_id": guest.id if guest else None, "name": typed, "needs": sorted(named)}
    return DraftSpec(DraftKind.GUEST, payload, f"Invite {typed} to {when}'s {slot} call", lines, event)


async def guest_answer(
    db: AsyncSession,
    actor: Member,
    event: Event,
    name: str,
    *,
    status: Status | None,
    offered_role: RoleOffer | None,
    offered: bool,
    today: date,
    now: datetime,
) -> DraftSpec:
    """In or out, or a role, for a guest the member invited who has no Slack
    (``guests.answer_for``). Only their inviter answers for them through the assistant: an
    admin's power to answer for anyone's guest is never a tool's."""
    ctx = await load(db, event)
    event = ctx.event
    typed = " ".join(name.split())
    found = [
        g for g in ctx.members.values() if g.id in ctx.invitations and g.display_name.casefold() == typed.casefold()
    ]
    if len(found) != 1:
        raise ToolRefusal(NO_SUCH_GUEST)
    guest = found[0]
    if not can_answer_for(guest, actor, ctx.invitations[guest.id]):
        raise Forbidden("Only whoever invited them, or an admin, answers for a guest without Slack.")
    check_can_respond(event, guest, invited=True)
    current = ctx.response_of(guest.id)
    if status is not None:
        if current is not None and current.status is status:
            raise ToolRefusal(NOTHING_TO_DO)
    elif offered_role is not None and with_offer(current, guest.id, offered_role, offered, now) in (None, current):
        raise ToolRefusal(NOTHING_TO_DO)
    when, _ = _call_words(event, today)
    lines = guest_answer_lines(
        typed,
        status=status,
        role=offered_role,
        offered=offered,
        withdraws_roles=status is Status.OUT and current is not None and bool(current.offers),
        slot=event.slot,
        when=when,
    )
    if status is not None:
        title = f"Answer {status.value.capitalize()} for {typed}"
    else:
        doing = ROLE_DOING[offered_role or RoleOffer.COOK]
        title = f"Offer {typed} to {doing}" if offered else f"Stop offering {typed} to {doing}"
    payload = {
        "guest_id": guest.id,
        "name": typed,
        "status": status.value if status else None,
        "role": offered_role.value if offered_role else None,
        "offered": offered,
    }
    return DraftSpec(DraftKind.GUEST_ANSWER, payload, title, lines, event)


# Ad-hoc calls (FR-3) -----------------------------------------------------------------------------


async def start_call(
    db: AsyncSession, actor: Member, slot: Slot, day: date, *, today: date, now: datetime
) -> DraftSpec:
    """An ad-hoc call for ``slot`` on ``day`` (``calls.start_call``), or the called-off one
    started again, by ``start_call``'s rules."""
    check_ad_hoc_day(day, today)
    existing = await EventRepository(db).call_for(actor.household_id, slot, day)
    if existing is not None:
        ctx = await load(db, existing)
        check_restart(ctx.event, today, now=now, too_late_after=ctx.too_late_for(today))
    try:
        await lifecycle.check_no_cooking_offer(db, actor.household_id, slot, day)
    except lifecycle.OfferedInstead:
        raise ToolRefusal(COOKING_OFFER) from None
    when = day_words(day, today)
    restart = existing is not None
    lines = call_lines(slot=slot, when=when, now=day == today, restart=restart)
    title = f"Start the {slot.value} call for {when} again" if restart else f"Start a {slot.value} call for {when}"
    return DraftSpec(DraftKind.CALL, {"slot": slot.value, "day": day.isoformat()}, title, lines, existing)


async def confirm_call(
    db: AsyncSession, actor: Member, draft: Draft, *, today: date, now: datetime, use: Callable[[], Awaitable[None]]
) -> ActionConfirmed:
    """**Confirm** on an ad-hoc call: ``calls.start_call``, which takes the write lock itself and
    runs ``use``, after checking a called-off call's run, in its transaction before it starts the
    call. A call someone started meanwhile gets its refusal, and the draft stays unused."""
    slot, day = Slot(str(draft.payload["slot"])), date.fromisoformat(str(draft.payload["day"]))

    async def expect() -> None:
        if draft.event_id is not None:
            event = await EventRepository(db).get(actor.household_id, draft.event_id)
            if event is None:
                raise NotFound(cc.NO_SUCH_CALL)
            check_run(event, draft.run)
        await use()

    event = await calls.start_call(db, actor, slot, day, now=now, expect=expect)
    return ActionConfirmed(event.id, f"Confirmed. The {slot.value} call for {day_words(day, today)} is started.")


# Confirm -----------------------------------------------------------------------------------------


async def confirm(db: AsyncSession, actor: Member, draft: Draft, *, today: date, now: datetime) -> ActionConfirmed:
    """Run the use case ``draft`` stands for, as ``actor``, who is ``tool_actor`` of its member.
    The caller holds the write lock and used the draft in it; the use case locks the call in
    the same transaction and commits both, or rolls both back. A draft for another run of the
    call gets ``check_run``'s refusal first, as a stale Slack card does. A role that joins late
    first does so inside ``offer_role``'s lock, so a refusal rolls back the late join too."""
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
            if await calls.join_late(db, actor, event.id, run=run, now=now):
                return ActionConfirmed(event.id, f"Confirmed. You joined {when}'s {slot} call late.")
            return ActionConfirmed(event.id, f"You're in {when}'s {slot} call already, so nothing changed.")
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
        late = bool(payload.get("join_late"))
        await calls.offer_role(db, actor, event.id, chosen, offered, join_late=late, run=run, now=now)
        doing = ROLE_DOING[chosen]
        done = f"You're down to {doing}" if offered else f"You're no longer down to {doing}"
        return ActionConfirmed(event.id, f"Confirmed. {done} for {when}'s {slot} call.")
    if draft.kind is DraftKind.OFFER_OFF:
        await calls.cancel_call(db, actor, event.id, run=run, now=now)
        return ActionConfirmed(event.id, "Confirmed. Your offer is called off.", offer=True)
    if draft.kind is DraftKind.GUEST:
        guest_id, name = payload.get("guest_id"), str(payload["name"])
        needs = frozenset(int(n) for n in payload.get("needs", []))
        new_guest = None if guest_id is not None else GuestInput(name, needs)
        try:
            await guest_service.invite(
                db,
                actor,
                event.id,
                guest_id=int(guest_id) if guest_id is not None else None,
                new_guest=new_guest,
                run=run,
                now=now,
            )
        except InvalidMemberInput as exc:  # such as a dietary tag deleted since: the card shows it
            raise EventRuleViolation(str(exc), code="invalid_guest") from None
        return ActionConfirmed(event.id, f"Confirmed. {name} is invited to {when}'s {slot} call.")
    if draft.kind is DraftKind.GUEST_ANSWER:
        name, raw_status, raw_role = str(payload["name"]), payload.get("status"), payload.get("role")
        guest_status = Status(str(raw_status)) if raw_status else None
        guest_role = RoleOffer(str(raw_role)) if raw_role else None
        await guest_service.answer_for(
            db,
            actor,
            event.id,
            int(payload["guest_id"]),
            status=guest_status,
            role=guest_role,
            offered=bool(payload["offered"]),
            run=run,
            now=now,
        )
        return ActionConfirmed(event.id, f"Confirmed. {name}'s answer for {when}'s {slot} call is saved.")
    raise NotFound(cc.NO_SUCH_CALL)  # pragma: no cover - CALL_ACTIONS names every kind above
