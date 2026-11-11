# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's drafts (spec 6.14, **Drafts**): making them, **Confirm**, and **Cancel**.

A draft tool checks what it can now and stores the draft with what **Confirm** will do, worked
out with the services' own rules (``domain.assistant_drafts``). Nothing else changes until the
member confirms. ``confirm_draft`` is what Confirm calls, from the web or Slack: it uses the
draft and runs the same service as the form, as ``tool_actor(member)``, in one transaction, so
a double Confirm can't post twice and a refusal leaves the draft as it was. The service checks
everything again, so a stale draft gets the service's own error and nothing changes.

A call action's draft (#251) comes from ``call_drafts``, which says what Confirm does and runs
its use case; ``store_action`` stores it.

A catalog draft's Confirm opens the web import preview, which only adds: the import re-plans
with ``add_only``, so an item that appeared meanwhile is left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import EventRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository
from dinnerbell.domain.assistant_drafts import (
    CALL_ACTIONS,
    DRAFT_MINUTES,
    MAX_OPEN_DRAFTS,
    CleanCatalog,
    Draft,
    DraftKind,
    Narrowing,
    OfferOutcome,
    OfferPlan,
    catalog_lines,
    clean_catalog,
    day_words,
    expires_at,
    offer_lines,
    offer_outcome,
    proposal_lines,
)
from dinnerbell.domain.catalog import CatalogItem, Mode, kind_for, name_key
from dinnerbell.domain.catalog_file import ParsedCatalog, parse_catalog
from dinnerbell.domain.catalog_import import ImportPlan
from dinnerbell.domain.events import (
    MODE_OFFER,
    Event,
    EventRuleViolation,
    EventState,
    Proposal,
    ProposalSource,
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
from dinnerbell.services import calls, lifecycle, offers, picks
from dinnerbell.services import catalog as catalog_service
from dinnerbell.services.assistant import call_drafts
from dinnerbell.services.assistant.acting import ToolRefusal, tool_actor
from dinnerbell.services.assistant.call_drafts import ActionConfirmed, DraftSpec
from dinnerbell.services.assistant.candidates import Labeler, as_proposer, find_candidates
from dinnerbell.services.assistant.runtime import AssistantConfig, available
from dinnerbell.services.call_context import load
from dinnerbell.services.clock import Clock
from dinnerbell.services.errors import Forbidden, NotFound
from dinnerbell.services.offers import OfferInput, Posted
from dinnerbell.services.preferences import group_preferences
from dinnerbell.services.variety import household_today

log = logging.getLogger(__name__)

NO_SUCH_DRAFT = "That draft is gone. Ask the assistant again."
USED = "That draft was already confirmed or cancelled."
EXPIRED = "That draft expired. Ask the assistant again for a new one."
TURNED_OFF = "The assistant is turned off, so its drafts can't be confirmed."
"""Also while the operator's key or flag is missing: the member can't tell those apart."""
TOO_MANY = f"The member has {MAX_OPEN_DRAFTS} drafts waiting already. Ask them to confirm or cancel those first."
NOT_A_CANDIDATE = (
    "That isn't one of this call's candidates in that mode. Call find_candidates, and draft only "
    "a name and mode it returns. Don't say why something isn't there: you don't know."
)
NOT_A_PICK = (
    "That can't join this call's vote in that mode. Offer it for another meal or day, or draft a proposal of "
    "a name and mode find_candidates returns. Don't say why: you don't know."
)
"""An offer that would join a vote as a pick the call's filters refuse: as ``NOT_A_CANDIDATE``,
it says nothing about whose stance or need it misses."""
MAX_ERRORS_SHOWN = 10
IMPORT_LINK_LIFETIME = timedelta(hours=24)
"""How long a confirmed catalog draft is kept, and its import link works: a link from Slack's
Confirm can sit there a while before the member opens it."""


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


async def _offer_plan(
    db: AsyncSession,
    actor: Member,
    call: Event | None,
    item: CatalogItem,
    request: OfferRequest,
    *,
    today: date,
    zone: ZoneInfo,
    now: datetime,
    drafting: bool,
) -> OfferPlan:
    """What posting the offer will do, by ``post_offer``'s own rules (FR-5). For a join, it
    checks the place's pick as ``offers._attach`` does: refused, with the service's words, when
    the call has it in the same mode or someone else added it, and otherwise taken over in the
    offer's mode. ``drafting`` adds the refusal ``_attach`` gives a voter who isn't in, and, for
    a vote, ``NOT_A_PICK`` when the pick misses the call's filters, as a proposal draft does
    (ADR 0005); Confirm leaves both to the service. While answers are open, who's in can still
    change, so the filters wait for Confirm, and the draft says the same whoever answers (FR-29)."""
    outcome = offer_outcome(call, request.mode)
    if call is None or outcome is not OfferOutcome.JOIN:
        schedule = slot_schedule(await household_settings(db, actor.household_id), request.slot)
        deadline = schedule.opens_at(request.day, zone) + schedule.response_window
        scheduled = schedule.enabled and (request.day > today or now < deadline)
        if call is None:
            return OfferPlan(outcome, scheduled=scheduled)
        return OfferPlan(outcome, call.id, call.run, call.state, scheduled=scheduled)
    ctx = await load(db, call)
    mine = ctx.response_of(actor.id)
    if drafting and call.state is EventState.VOTING and (mine is None or not mine.is_in):
        raise ToolRefusal(
            f"Voting has started on that {request.slot.value} call and the member isn't in it, so an offer "
            "can't join it."
        )
    candidate = Proposal(0, call.id, request.mode, 0, ProposalSource.MEMBER, item_id=item.id, proposed_by=actor.id)
    existing = picks.picked(ctx, candidate)
    if existing is not None and (existing.mode is request.mode or existing.proposed_by not in (None, actor.id)):
        raise picks.already_a_pick(ctx, existing)
    if drafting and call.state is EventState.VOTING:
        group = await group_preferences(db, actor.household_id, ctx.veto_people)
        if lifecycle.misfit(as_proposer(ctx, actor, now), candidate, group) is not None:
            raise ToolRefusal(NOT_A_PICK)
    return OfferPlan(outcome, call.id, call.run, call.state, takes_over=existing.mode if existing else None)


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
    plan = await _offer_plan(db, actor, call, item, request, today=today, zone=zone, now=now, drafting=True)
    when = day_words(request.day, today)
    lines = offer_lines(plan, slot=request.slot, when=when, mode=request.mode, item=item.name)
    local = moment.astimezone(zone)
    payload = {
        "item_id": item.id,
        "mode": request.mode.value,
        "slot": request.slot.value,
        "day": request.day.isoformat(),
        "at": f"{local:%H:%M}",
        "capacity": capacity,
        "plan": plan.to_json(),
    }
    if plan.outcome is OfferOutcome.JOIN:  # a pick on the call: its time and servings are the call's
        title = f"Offer to {request.mode.verb} {item.name} for {when}'s {request.slot.value} call"
    else:
        clock = f"{local:%I:%M %p}".lstrip("0")
        title = f"Offer to {request.mode.verb} {item.name}, {request.slot.value} {when} at {clock}"
    return await _store(db, actor, key, DraftKind.OFFER, payload, title, lines, event=call, now=now)


async def store_action(db: AsyncSession, actor: Member, key: str, spec: DraftSpec, *, now: datetime) -> Draft:
    """Store a call action ``call_drafts`` drafted and checked, against its call."""
    payload = dict(spec.payload)
    return await _store(db, actor, key, spec.kind, payload, spec.title, spec.lines, event=spec.event, now=now)


async def draft_catalog_items(db: AsyncSession, actor: Member, key: str, text: str, *, now: datetime) -> Draft:
    """New catalog items from ``text``, in the catalog file format. Entries naming an item the
    catalog has are dropped and listed, and ``cooks`` and ``active`` are stripped; the draft
    holds the cleaned file."""
    parsed = parse_catalog(text)
    if parsed.errors:
        shown = "; ".join(str(e) for e in parsed.errors[:MAX_ERRORS_SHOWN])
        raise ToolRefusal(f"That isn't a valid catalog file: {shown}. Fix it and try again.")
    draft, _ = await draft_parsed_catalog(db, actor, key, parsed, now=now)
    return draft


LINK_NOT_IMPORTABLE = "The items read from that page can't be imported. Add them on the Catalog page instead."


async def draft_parsed_catalog(
    db: AsyncSession, actor: Member, key: str, parsed: ParsedCatalog, *, now: datetime, from_page: bool = False
) -> tuple[Draft, CleanCatalog]:
    """A catalog draft of ``parsed``, which has no errors: entries the catalog has are dropped
    and listed, and ``cooks`` and ``active`` are stripped. ``from_page`` is True for entries a
    drafting call read from a web page: then a file the import refuses gets a fixed refusal,
    because the import's errors quote the file, and the page wrote it."""
    items = await CatalogRepository(db).items(actor.household_id)
    clean = clean_catalog(parsed, items)
    if not clean.added:
        raise ToolRefusal(f"The catalog has all of those already: {', '.join(clean.dropped)}. Nothing to add.")
    plan = await catalog_service.preview_import(db, actor.household_id, clean.text, add_only=True)
    if not plan.ok:
        if from_page:
            raise ToolRefusal(LINK_NOT_IMPORTABLE)
        shown = "; ".join(str(e) for e in plan.errors[:MAX_ERRORS_SHOWN])
        raise ToolRefusal(f"That file can't be imported: {shown}. Fix it and try again.")
    count = len(clean.added)
    title = f"Add {count} {'item' if count == 1 else 'items'} to the catalog"
    payload = {"content": clean.text, "added": list(clean.added), "dropped": list(clean.dropped)}
    lines = catalog_lines(clean)
    return await _store(db, actor, key, DraftKind.CATALOG, payload, title, lines, event=None, now=now), clean


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


Confirmed = ProposalConfirmed | OfferConfirmed | CatalogReady | ActionConfirmed


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


async def _use(
    db: AsyncSession,
    draft: Draft,
    member: Member,
    now: datetime,
    *,
    confirmed: bool = True,
    keep_until: datetime | None = None,
) -> None:
    """Use the draft in the caller's write transaction, or raise why it can't be: a Confirm, or
    a Cancel with ``confirmed`` False. ``keep_until`` keeps it past its 30 minutes, as a
    confirmed catalog draft is kept for its import link."""
    repo = AssistantRepository(db)
    used = await repo.use_draft(
        member.household_id, draft.id, member.id, now, confirmed=confirmed, keep_until=keep_until
    )
    if not used:
        latest = await AssistantRepository(db).draft(member.household_id, draft.id)
        if latest is None:  # pragma: no cover - deleted between the read and the write
            raise NotFound(NO_SUCH_DRAFT)
        _check_usable(latest, now)
        raise DraftUnavailable(USED)  # pragma: no cover - use_draft found it unusable


async def confirm_draft(
    db: AsyncSession, clock: Clock, config: AssistantConfig, member: Member, draft_id: int
) -> Confirmed:
    """**Confirm** on ``member``'s draft: use it, and run the service the form or button runs,
    as ``tool_actor(member)``, in one transaction. A refusal from the service comes through
    unchanged and leaves the draft unused. Raises NotFound for a draft that isn't the member's,
    and ``DraftUnavailable`` for one used or expired, or while the assistant isn't available:
    the key, the flag, and Admin's switch must all be set (``runtime.available``)."""
    now = clock()
    draft = await _mine(db, member, draft_id)
    _check_usable(draft, now)
    if not await available(db, config, member):
        raise DraftUnavailable(TURNED_OFF)
    actor = tool_actor(member)
    if draft.kind is DraftKind.PROPOSAL:
        return await _confirm_proposal(db, actor, draft, now)
    if draft.kind is DraftKind.OFFER:
        return await _confirm_offer(db, actor, draft, now)
    if draft.kind in CALL_ACTIONS:
        return await _confirm_action(db, actor, draft, now)
    return await _confirm_catalog(db, actor, draft, now)


async def _confirm_action(db: AsyncSession, actor: Member, draft: Draft, now: datetime) -> ActionConfirmed:
    today, _ = await _today(db, actor.household_id, now)
    if draft.kind is DraftKind.CALL:  # ``start_call`` takes the write lock itself, and uses the draft in it

        async def use() -> None:
            await _use(db, draft, actor, now)

        return await call_drafts.confirm_call(db, actor, draft, today=today, now=now, use=use)
    async with write_transaction(db):
        await _use(db, draft, actor, now)
        # The use case locks the call in this transaction, then commits the draft's use with its
        # change, or rolls both back (``call_drafts.confirm``).
        return await call_drafts.confirm(db, actor, draft, today=today, now=now)


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
    promised = OfferPlan.from_json(payload["plan"])
    request = OfferRequest("", mode, data.slot, data.day, data.at, data.capacity)

    async def expect(call: Event | None, path: OfferPath) -> None:
        """Still what the card said: the same outcome on the same run of the same call, in the
        same state, taking over the same pick. Then use the draft."""
        today, zone = await _today(db, actor.household_id, now)
        item = await CatalogRepository(db).item(actor.household_id, int(payload["item_id"]))
        if item is None:  # pragma: no cover - post_offer refused it already
            raise NotFound(NO_SUCH_DRAFT)
        plan = await _offer_plan(db, actor, call, item, request, today=today, zone=zone, now=now, drafting=False)
        if not promised.same(plan):
            line = offer_lines(plan, slot=data.slot, when=day_words(data.day, today), mode=mode, item=item.name)[0]
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
        await _use(db, draft, actor, now, keep_until=now + IMPORT_LINK_LIFETIME)
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
        await _use(db, draft, member, now, confirmed=False)
        await db.commit()
    log.info("assistant draft cancelled", extra={"member_id": member.id, "draft_id": draft.id})


async def ready_for_import(
    db: AsyncSession, config: AssistantConfig, member: Member, draft_id: int, *, now: datetime
) -> CatalogReady:
    """The import preview for ``member``'s catalog draft that Confirm used, re-planned add-only
    against the catalog now: for the web link a Slack Confirm gives (``CatalogReady``'s import,
    reached by a GET). Only a confirmed draft opens it: a draft its member cancelled, or **Start
    over** cancelled, never does, whatever link names it. Anything else raises NotFound, the same
    for each: someone else's draft, one waiting, cancelled, or gone, another kind, a guest or
    inactive member, the assistant not available, or kept past ``IMPORT_LINK_LIFETIME`` (the
    purge may not have run yet)."""
    repo = AssistantRepository(db)
    draft = await repo.draft(member.household_id, draft_id) if member.is_member else None
    if draft is None or draft.member_id != member.id or draft.kind is not DraftKind.CATALOG:
        raise NotFound(NO_SUCH_DRAFT)
    if draft.used_at is None or draft.confirmed_at is None:
        raise NotFound(NO_SUCH_DRAFT)
    if draft.expired(now):
        raise NotFound(NO_SUCH_DRAFT)
    if not await available(db, config, member):
        raise NotFound(NO_SUCH_DRAFT)
    content = str(draft.payload["content"])
    plan = await catalog_service.preview_import(db, member.household_id, content, add_only=True)
    return CatalogReady(
        content=content,
        plan=plan,
        sha256=catalog_service.content_digest(content),
        fingerprint=plan.fingerprint(),
        member_id=member.id,
    )
