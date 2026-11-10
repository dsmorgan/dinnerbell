# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's drafts (spec 6.14, **Drafts**): making them, **Confirm**, and **Cancel**.

A draft tool checks what it can now and stores the draft with what **Confirm** will do, worked
out with the services' own rules (``domain.assistant_drafts``). Nothing else changes until the
member confirms. ``confirm_draft`` is what Confirm calls, from the web or Slack: it uses the
draft and runs the same service as the form, as ``tool_actor(member)``, in one transaction, so
a double Confirm can't post twice and a refusal leaves the draft as it was. The service checks
everything again, so a stale draft gets the service's own error and nothing changes.

A catalog draft's Confirm opens the web import preview, which only adds: the import re-plans
with ``add_only``, so an item that appeared meanwhile is left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import EventRepository, ResponseRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant import assistant_settings
from dinnerbell.domain.assistant_drafts import (
    DRAFT_MINUTES,
    MAX_OPEN_DRAFTS,
    Draft,
    DraftKind,
    Narrowing,
    OfferOutcome,
    catalog_lines,
    clean_catalog,
    day_words,
    expires_at,
    offer_lines,
    offer_outcome,
    proposal_lines,
)
from dinnerbell.domain.catalog import Mode, kind_for, name_key
from dinnerbell.domain.catalog_file import parse_catalog
from dinnerbell.domain.catalog_import import ImportPlan
from dinnerbell.domain.events import (
    MODE_OFFER,
    Event,
    EventRuleViolation,
    EventState,
    Proposal,
    can_do,
    check_can_join_late,
    check_cook_list,
    check_proposal_room,
    with_late_join,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.offers import OfferPath, check_capacity, offer_time
from dinnerbell.domain.schedule import slot_schedule
from dinnerbell.services import calls, offers
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.assistant.acting import ToolRefusal, tool_actor
from dinnerbell.services.assistant.candidates import Labeler, find_candidates
from dinnerbell.services.clock import Clock
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.offers import OfferInput, Posted
from dinnerbell.services.variety import household_today

log = logging.getLogger(__name__)

NO_SUCH_DRAFT = "That draft is gone. Ask the assistant again."
USED = "That draft was already confirmed or cancelled."
EXPIRED = "That draft expired. Ask the assistant again for a new one."
TURNED_OFF = "The assistant is turned off, so its drafts can't be confirmed."
TOO_MANY = f"The member has {MAX_OPEN_DRAFTS} drafts waiting already. Ask them to confirm or cancel those first."
NOT_A_CANDIDATE = (
    "That isn't one of this call's candidates in that mode. Call find_candidates, and draft only "
    "a name and mode it returns. Don't say why something isn't there: you don't know."
)
MAX_ERRORS_SHOWN = 10


class DraftUnavailable(EventRuleViolation):
    """A draft that can't be confirmed or cancelled now: used, expired, or the assistant is off."""

    code = "draft_unavailable"


@dataclass(frozen=True, slots=True)
class DraftView:
    """A draft as an entry point shows it, on a card with **Confirm** and **Cancel**. Item
    names only, never a person's."""

    id: int
    kind: str
    title: str
    confirm_lines: tuple[str, ...]


def view(draft: Draft) -> DraftView:
    return DraftView(id=draft.id, kind=draft.kind.value, title=draft.title, confirm_lines=draft.lines)


def summary(draft: Draft, label: str) -> str:
    """What a draft tool tells the model: the draft's ID and what Confirm does, not its payload."""
    return (
        f"Draft {draft.id} is waiting for {label} to confirm: {draft.title}. Confirm will: {' '.join(draft.lines)} "
        f"Dinnerbell shows the member Confirm and Cancel on it for {DRAFT_MINUTES} minutes. "
        "Nothing has changed yet, so don't say it's done."
    )


# Making drafts ----------------------------------------------------------------------------------


async def _store(
    db: AsyncSession,
    actor: Member,
    key: str,
    kind: DraftKind,
    payload: dict[str, Any],
    title: str,
    lines: tuple[str, ...],
    *,
    event: Event | None,
    now: datetime,
) -> Draft:
    repo = AssistantRepository(db)
    async with write_transaction(db):
        if await repo.waiting_drafts(actor.household_id, actor.id, now) >= MAX_OPEN_DRAFTS:
            raise ToolRefusal(TOO_MANY)
        draft = await repo.add_draft(
            actor.household_id,
            member_id=actor.id,
            conversation_key=key,
            kind=kind,
            payload=payload,
            title=title,
            lines=lines,
            event_id=event.id if event is not None else None,
            run=event.run if event is not None else None,
            now=now,
            expires_at=expires_at(now),
        )
        await db.commit()
    log.info("assistant drafted", extra={"member_id": actor.id, "kind": kind.value, "draft_id": draft.id})
    return draft


async def _today(db: AsyncSession, household_id: int, now: datetime) -> tuple[date, ZoneInfo]:
    household = await HouseholdRepository(db).get(household_id)
    if household is None:  # pragma: no cover - members cascade with their household
        raise NotFound("No such household.")
    return household_today(household, now), ZoneInfo(household.timezone)


async def draft_proposal(
    db: AsyncSession, actor: Member, key: str, event: Event, name: str, mode: Mode, *, label: Labeler, now: datetime
) -> Draft:
    """A proposal of one of ``event``'s candidates (``find_candidates``) in ``mode``, for
    ``actor``. Only a candidate can be drafted, so a vetoed or unmet item never gets this far,
    and the refusal says nothing about why."""
    found = await find_candidates(db, actor, event, Narrowing(mode=mode), label=label, now=now)
    match = next((c for c in found.found if name_key(c.item.name) == name_key(name)), None)
    if match is None or mode not in match.modes:
        raise ToolRefusal(NOT_A_CANDIDATE)
    ctx = found.ctx
    mine = ctx.response_of(actor.id)
    join_late = mine is None or not mine.is_in
    seen = mine
    if join_late:
        check_can_join_late(ctx.event, actor, mine)
        seen = with_late_join(mine, actor.id, now)
    check_proposal_room(ctx.proposals, actor)
    item = ctx.items[match.item.id]
    volunteers = can_do(item, mode, actor.id) and (seen is None or MODE_OFFER[mode] not in seen.offers)
    today, _ = await _today(db, actor.household_id, now)
    lines = proposal_lines(
        slot=ctx.event.slot,
        when=day_words(ctx.event.day, today),
        join_late=join_late,
        volunteers=volunteers,
        mode=mode,
    )
    payload = {"item_id": item.id, "mode": mode.value, "join_late": join_late}
    title = f"Propose {item.name} ({mode.label.lower()})"
    return await _store(db, actor, key, DraftKind.PROPOSAL, payload, title, lines, event=ctx.event, now=now)


@dataclass(frozen=True, slots=True)
class OfferRequest:
    """An offer the model asked for: an existing catalog item by name, and what ``post_offer``
    needs. ``at`` None means the slot's usual meal time."""

    name: str
    mode: Mode
    slot: Slot
    day: date
    at: time | None
    capacity: int


async def draft_offer(db: AsyncSession, actor: Member, key: str, request: OfferRequest, *, now: datetime) -> Draft:
    """An offer of an existing catalog item, never free text, checked now as ``post_offer``
    would check it, with what it will do to the slot's call that day (FR-5)."""
    today, zone = await _today(db, actor.household_id, now)
    item = await CatalogRepository(db).item_by_name(actor.household_id, kind_for(request.mode), request.name)
    if item is None or not item.active:
        raise ToolRefusal(
            "The catalog has no such item for that mode. Look it up with look_up_catalog_items; "
            "a new item needs a catalog draft first."
        )
    if request.mode not in item.modes:
        raise EventRuleViolation(f"{item.name} isn't available by {request.mode.label.lower()}.", code="unavailable")
    check_cook_list(item, request.mode, actor, ask="Ask a cook to offer it")
    at = request.at
    if at is None:
        at = slot_schedule(await household_settings(db, actor.household_id), request.slot).meal_time
    moment = offer_time(request.day, at, zone=zone, today=today, now=now)
    capacity = check_capacity(request.capacity)
    call = await EventRepository(db).call_for(actor.household_id, request.slot, request.day)
    outcome = offer_outcome(call, request.mode)
    if outcome is OfferOutcome.JOIN and call is not None and call.state is EventState.VOTING:
        mine = next(
            (r for r in await ResponseRepository(db).for_event(actor.household_id, call.id) if r.member_id == actor.id),
            None,
        )
        if mine is None or not mine.is_in:
            raise ToolRefusal(
                f"Voting has started on that {request.slot.value} call and the member isn't in it, so an offer "
                "can't join it."
            )
    when = day_words(request.day, today)
    lines = offer_lines(outcome, slot=request.slot, when=when, state=call.state if call else None, mode=request.mode)
    local = moment.astimezone(zone)
    payload = {
        "item_id": item.id,
        "mode": request.mode.value,
        "slot": request.slot.value,
        "day": request.day.isoformat(),
        "at": f"{local:%H:%M}",
        "capacity": capacity,
        "outcome": outcome.value,
    }
    clock = f"{local:%I:%M %p}".lstrip("0")
    title = f"Offer to {request.mode.verb} {item.name}, {request.slot.value} {when} at {clock}"
    return await _store(db, actor, key, DraftKind.OFFER, payload, title, lines, event=call, now=now)


async def draft_catalog_items(db: AsyncSession, actor: Member, key: str, text: str, *, now: datetime) -> Draft:
    """New catalog items from ``text``, in the catalog file format. Entries naming an item the
    catalog has are dropped and listed, and ``cooks`` and ``active`` are stripped; the draft
    holds the cleaned file."""
    parsed = parse_catalog(text)
    if parsed.errors:
        shown = "; ".join(str(e) for e in parsed.errors[:MAX_ERRORS_SHOWN])
        raise ToolRefusal(f"That isn't a valid catalog file: {shown}. Fix it and try again.")
    items = await CatalogRepository(db).items(actor.household_id)
    clean = clean_catalog(parsed, items)
    if not clean.added:
        raise ToolRefusal(f"The catalog has all of those already: {', '.join(clean.dropped)}. Nothing to add.")
    plan = await catalog_service.preview_import(db, actor.household_id, clean.text, add_only=True)
    if not plan.ok:
        shown = "; ".join(str(e) for e in plan.errors[:MAX_ERRORS_SHOWN])
        raise ToolRefusal(f"That file can't be imported: {shown}. Fix it and try again.")
    count = len(clean.added)
    title = f"Add {count} {'item' if count == 1 else 'items'} to the catalog"
    payload = {"content": clean.text, "added": list(clean.added), "dropped": list(clean.dropped)}
    return await _store(db, actor, key, DraftKind.CATALOG, payload, title, catalog_lines(clean), event=None, now=now)


async def waiting(db: AsyncSession, member: Member, key: str, now: datetime) -> list[DraftView]:
    """``member``'s drafts in the conversation ``key`` that still wait for Confirm, oldest first,
    for an entry point that draws the conversation again."""
    return [view(d) for d in await AssistantRepository(db).drafts_waiting(member.household_id, member.id, key, now)]


# Confirm and Cancel -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalConfirmed:
    event_id: int
    proposal: Proposal


@dataclass(frozen=True, slots=True)
class OfferConfirmed:
    posted: Posted


@dataclass(frozen=True, slots=True)
class CatalogReady:
    """What an entry point needs to open the web import preview for a catalog draft. It signs
    ``sha256``, ``fingerprint``, and ``member_id`` as the import preview does, marks the token
    add-only, and applies with ``apply_import(..., add_only=True)``."""

    content: str
    plan: ImportPlan
    sha256: str
    fingerprint: str
    member_id: int
    add_only: bool = True


Confirmed = ProposalConfirmed | OfferConfirmed | CatalogReady


async def _mine(db: AsyncSession, member: Member, draft_id: int) -> Draft:
    """``member``'s draft, or NotFound: someone else's draft looks the same as none."""
    if not member.is_member:
        raise Forbidden("Only active members can confirm drafts.")
    draft = await AssistantRepository(db).draft(member.household_id, draft_id)
    if draft is None or draft.member_id != member.id:
        raise NotFound(NO_SUCH_DRAFT)
    return draft


def _check_usable(draft: Draft, now: datetime) -> None:
    if draft.used_at is not None:
        raise DraftUnavailable(USED)
    if draft.expired(now):
        raise DraftUnavailable(EXPIRED)


async def _use(db: AsyncSession, draft: Draft, member: Member, now: datetime) -> None:
    """Use the draft in the caller's write transaction, or raise why it can't be."""
    if not await AssistantRepository(db).use_draft(member.household_id, draft.id, member.id, now):
        latest = await AssistantRepository(db).draft(member.household_id, draft.id)
        if latest is None:  # pragma: no cover - deleted between the read and the write
            raise NotFound(NO_SUCH_DRAFT)
        _check_usable(latest, now)
        raise DraftUnavailable(USED)  # pragma: no cover - use_draft found it unusable


async def confirm_draft(db: AsyncSession, clock: Clock, member: Member, draft_id: int) -> Confirmed:
    """**Confirm** on ``member``'s draft: use it, and run the service the form or button runs,
    as ``tool_actor(member)``, in one transaction. A refusal from the service comes through
    unchanged and leaves the draft unused. Raises NotFound for a draft that isn't the member's,
    and ``DraftUnavailable`` for one used, expired, or with the assistant turned off."""
    now = clock()
    draft = await _mine(db, member, draft_id)
    _check_usable(draft, now)
    if not assistant_settings(await household_settings(db, member.household_id)).enabled:
        raise DraftUnavailable(TURNED_OFF)
    actor = tool_actor(member)
    if draft.kind is DraftKind.PROPOSAL:
        return await _confirm_proposal(db, actor, draft, now)
    if draft.kind is DraftKind.OFFER:
        return await _confirm_offer(db, actor, draft, now)
    return await _confirm_catalog(db, actor, draft, now)


async def _confirm_proposal(db: AsyncSession, actor: Member, draft: Draft, now: datetime) -> ProposalConfirmed:
    if draft.event_id is None:  # pragma: no cover - every proposal draft names its call
        raise NotFound(NO_SUCH_DRAFT)
    payload = draft.payload
    async with write_transaction(db):
        await _use(db, draft, actor, now)
        # ``add_proposal`` locks the call in this transaction, then commits the draft's use with
        # the proposal, or rolls both back.
        proposal = await calls.add_proposal(
            db,
            actor,
            draft.event_id,
            Mode(str(payload["mode"])),
            item_id=int(payload["item_id"]),
            join_late=bool(payload["join_late"]),
            run=draft.run,
            now=now,
        )
    return ProposalConfirmed(draft.event_id, proposal)


async def _confirm_offer(db: AsyncSession, actor: Member, draft: Draft, now: datetime) -> OfferConfirmed:
    payload = draft.payload
    mode = Mode(str(payload["mode"]))
    data = OfferInput(
        slot=Slot(str(payload["slot"])),
        day=date.fromisoformat(str(payload["day"])),
        mode=mode,
        at=time.fromisoformat(str(payload["at"])),
        capacity=int(payload["capacity"]),
        item_id=int(payload["item_id"]),
    )
    promised = OfferOutcome(str(payload["outcome"]))

    async def expect(call: Event | None, path: OfferPath) -> None:
        """Still what the card said, word for word, on the same run of the same call; then use
        the draft. A call that moved from answers to voting reads differently, so it's stale too."""
        now_outcome = offer_outcome(call, mode)
        same_call = (call.id, call.run) == (draft.event_id, draft.run) if call is not None else draft.event_id is None
        today, _ = await _today(db, actor.household_id, now)
        line = offer_lines(
            now_outcome, slot=data.slot, when=day_words(data.day, today), state=call.state if call else None, mode=mode
        )[0]
        if now_outcome is not promised or not same_call or line != draft.lines[0]:
            raise EventRuleViolation(
                f"That changed since the draft. Now: {line} Ask the assistant for a new draft.", code="stale_draft"
            )
        await _use(db, draft, actor, now)

    posted = await offers.post_offer(db, actor, data, now=now, expect=expect)
    return OfferConfirmed(posted)


async def _confirm_catalog(db: AsyncSession, actor: Member, draft: Draft, now: datetime) -> CatalogReady:
    """Open the preview, unless the import can't run or would add nothing now: then the draft
    stays unused, with nothing to show."""
    content = str(draft.payload["content"])
    async with write_transaction(db):
        plan = await catalog_service.preview_import(db, actor.household_id, content, add_only=True)
        if not plan.ok:
            raise EventRuleViolation(
                "The catalog changed, so that draft can't be imported now. Ask the assistant for a new one.",
                code="stale_draft",
            )
        if not plan.items_to_create:
            raise EventRuleViolation("The catalog has all of those now, so there's nothing to add.", code="stale_draft")
        await _use(db, draft, actor, now)
        await db.commit()
    return CatalogReady(
        content=content,
        plan=plan,
        sha256=catalog_service.content_digest(content),
        fingerprint=plan.fingerprint(),
        member_id=actor.id,
    )


async def cancel_draft(db: AsyncSession, clock: Clock, member: Member, draft_id: int) -> None:
    """**Cancel** on ``member``'s draft: it can't be confirmed afterward. Works with the
    assistant turned off too. Raises as ``confirm_draft`` does for a draft that isn't theirs, or
    is used or expired."""
    now = clock()
    draft = await _mine(db, member, draft_id)
    _check_usable(draft, now)
    async with write_transaction(db):
        await _use(db, draft, member, now)
        await db.commit()
    log.info("assistant draft cancelled", extra={"member_id": member.id, "draft_id": draft.id})
