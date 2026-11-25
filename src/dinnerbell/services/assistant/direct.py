# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acting directly, with **Undo** (#374, spec 6.14, **Acting at once**).

In a private conversation, the web page or a DM, a draft that ``acts_directly`` allows runs at
once instead of waiting for **Confirm**: an answer, a vote, a role, a suggestion, an answer for a
guest, and changes to the member's own data that tighten or tell nobody. ``act`` stores it as a
draft row marked ``direct``, uses it at once through the same Confirm path a draft takes, with
the same checks, then stores what it did and what **Undo** puts back. A refusal deletes the row,
so nothing is left behind but the tool's error.

``undo`` is what **Undo** on the record runs, from the web or Slack. It never calls the model, so
it works at the cap and at Conversation hours 0. Only the record's member may, once, within
``UNDO_MINUTES``, while the assistant is available, and only while what the action changed is as
it left it: anything moved since is refused, and nothing changes. It runs the inverse through the
use case the page or the buttons call, as ``tool_actor`` of the member, in one transaction with
marking the record undone, so a refusal leaves the record as it was.

A first answer has no **Undo**: Dinnerbell can't un-answer anyone. Its record says so, and the
member, or the assistant, answers again while answers are open.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.assistant import AssistantRepository
from dinnerbell.db.engine import write_transaction
from dinnerbell.db.events import EventRepository
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.domain.assistant_coordinating import plain
from dinnerbell.domain.assistant_drafts import (
    CALL_ACTIONS,
    ROLE_DOING,
    UNDO_MINUTES,
    Draft,
    DraftKind,
    day_words,
    done_text,
    expires_at,
    undone_text,
)
from dinnerbell.domain.events import Details, Event, EventRuleViolation, Response, RoleOffer, Status, check_run
from dinnerbell.domain.members import Member
from dinnerbell.services import calls
from dinnerbell.services.assistant import draft_log, drafts, own_drafts
from dinnerbell.services.assistant.acting import tool_actor
from dinnerbell.services.assistant.call_drafts import ActionConfirmed, DraftSpec, check_same_pick
from dinnerbell.services.assistant.drafts import TURNED_OFF, DraftUnavailable, DraftView
from dinnerbell.services.assistant.runtime import AssistantConfig, available
from dinnerbell.services.call_context import NO_SUCH_CALL, CallContext, load
from dinnerbell.services.clock import Clock
from dinnerbell.services.errors import Forbidden, NotFound

log = logging.getLogger(__name__)

NO_SUCH_RECORD = "That's gone. Change it yourself where you'd change it on the web."
UNDONE = "That's undone already."
UNDO_CLOSED = f"Undo works for {UNDO_MINUTES} minutes, and that's over. Change it yourself instead."
NO_UNDO = "It was your first answer to this call, so there's no Undo. Answer again to change it."
"""A record's line, and Undo's refusal, for an action that answered a call the member hadn't:
Dinnerbell can't un-answer anyone."""
NO_UNDO_SAVED = "There's no Undo for this one. Change it yourself on the web."
"""A record's line when its Undo couldn't be stored: rare, and logged."""
MOVED_CALL = "That changed since the assistant did it, so Undo changed nothing. Change it on the call's page."
MOVED_OWN = "That changed since the assistant did it, so Undo changed nothing. Change it on Preferences."
MOVED_FEEDBACK = (
    "That changed since the assistant did it, so Undo changed nothing. Change it on the meal's feedback form."
)
STALE = "stale_draft"
"""The code every Confirm path gives a change made meanwhile, which Undo words as moved."""


def _moved(kind: DraftKind) -> EventRuleViolation:
    if kind is DraftKind.FEEDBACK:
        text = MOVED_FEEDBACK
    elif kind in CALL_ACTIONS:
        text = MOVED_CALL
    else:
        text = MOVED_OWN
    return EventRuleViolation(text, code="undo_moved")


# Records ----------------------------------------------------------------------------------------


def view(record: Draft, now: datetime) -> DraftView:
    """A direct action's record card (#374): **Done:**, its title, what it did, and **Undo** while
    it works. Item names only, never a person's: no direct kind names people."""
    return DraftView(
        id=record.id,
        kind=record.kind.value,
        title=record.title,
        confirm_lines=record.lines,
        record=True,
        undo=record.undo_open(now),
        undone=record.undone_at is not None,
    )


async def records(db: AsyncSession, member: Member, key: str, now: datetime) -> list[DraftView]:
    """``member``'s direct actions in the conversation ``key`` that the purge hasn't deleted,
    oldest first, for a page drawn again."""
    found = await AssistantRepository(db).records(member.household_id, member.id, key, now)
    return [view(r, now) for r in found]


async def record(db: AsyncSession, member: Member, record_id: int, now: datetime) -> DraftView | None:
    """``member``'s direct action ``record_id`` as its card shows it, or None for anyone else's,
    a draft, or one gone."""
    found = await AssistantRepository(db).draft(member.household_id, record_id) if member.is_member else None
    if found is None or found.member_id != member.id or not found.direct or found.confirmed_at is None:
        return None
    return view(found, now)


def summary(record: Draft, label: str) -> str:
    """What a direct action tells the model: what it did, and that it's done. Never its payload."""
    did = " ".join(record.lines[:1])
    if record.undoable:
        shows = f"Dinnerbell shows {label} a record of it with Undo for {UNDO_MINUTES} minutes."
    elif NO_UNDO not in record.lines:
        shows = f"Dinnerbell shows {label} a record of it, with no Undo."
    else:
        shows = (
            f"Dinnerbell shows {label} a record of it. It was their first answer to the call, so it has no Undo; "
            "answering again changes it."
        )
    return f"Done at once, not a draft: {record.title}. {did} {shows} Tell them briefly that it's done."


OVER_THE_LIMIT = "It's a draft, not done at once, because this message did {most} things at once already."


# Acting -----------------------------------------------------------------------------------------


def _snap(response: Response | None) -> dict[str, Any] | None:
    if response is None:
        return None
    return {"status": response.status.value, "offers": sorted(o.value for o in response.offers)}


def _suggested(ctx: CallContext, member_id: int) -> dict[str, list[Any]]:
    theirs = [s for s in ctx.suggestions if s.member_id == member_id]
    return {
        "items": sorted(s.item_id for s in theirs if s.item_id is not None),
        "texts": sorted(s.free_text for s in theirs if s.free_text),
    }


async def _call(db: AsyncSession, actor: Member, event_id: int | None) -> CallContext:
    event = await EventRepository(db).get(actor.household_id, event_id) if event_id is not None else None
    if event is None:
        raise NotFound(NO_SUCH_CALL)
    return await load(db, event)


def _whose(kind: DraftKind, payload: Mapping[str, Any], actor: Member) -> int:
    """The person whose answer a call action changes: the member's, or their guest's."""
    return int(payload["guest_id"]) if kind is DraftKind.GUEST_ANSWER else actor.id


async def _before(db: AsyncSession, actor: Member, spec: DraftSpec) -> Any:
    """What a call action's Undo needs from before it runs: the answer it changes, or, for a
    suggestion, whether the member had answered. Own data carries its own in the payload."""
    if spec.kind not in CALL_ACTIONS or spec.kind is DraftKind.VOTE:
        return None
    ctx = await _call(db, actor, spec.event.id if spec.event is not None else None)
    return ctx.response_of(_whose(spec.kind, spec.payload, actor))


async def _undo(db: AsyncSession, actor: Member, record: Draft, before: Any) -> dict[str, Any] | None:
    """What **Undo** puts back, read once the action ran, with what it left, so Undo can tell
    whether anything moved since. None when nothing can be put back: a first answer."""
    kind, payload = record.kind, record.payload
    if kind in (DraftKind.ANSWER, DraftKind.ROLE, DraftKind.GUEST_ANSWER):
        if before is None:
            return None
        whose = _whose(kind, payload, actor)
        left = (await _call(db, actor, record.event_id)).response_of(whose)
        return {"member_id": whose, **(_snap(before) or {}), "after": _snap(left)}
    if kind is DraftKind.VOTE:
        return {"vote": not payload["vote"]}
    if kind is DraftKind.SUGGESTION:
        if before is None:
            return None
        return {"after": _suggested(await _call(db, actor, record.event_id), actor.id)}
    if kind is DraftKind.PREFERENCE:
        return {"level": payload["before"], "after": payload["level"]}
    if kind is DraftKind.AWAY:
        me = await own_drafts.fresh(db, actor)
        today, _ = await drafts.household_day(db, actor.household_id, record.created_at)
        window = me.away_now(today)
        now_away = None if window is None else [window.first.isoformat(), window.last.isoformat()]
        return {"window": payload.get("before"), "after": now_away}
    if kind is DraftKind.NEEDS:
        me = await own_drafts.fresh(db, actor)
        return {"needs": payload["before"], "after": sorted(me.dietary_needs)}
    if kind is DraftKind.CAN_USUALLY:
        return {"flags": payload["before"], "after": payload["after"]}
    saved = await FeedbackRepository(db).get(actor.household_id, int(payload["meal_id"]), actor.id)
    kept = own_drafts.stored_answers(saved.answers if saved else None)
    return {"answers": payload.get("replaced"), "after": own_drafts.stamp(saved), "after_answers": kept}


async def act(db: AsyncSession, member: Member, key: str, spec: DraftSpec, *, now: datetime) -> Draft:
    """Do ``spec`` at once for ``member`` (#374), which ``acts_directly`` allows, as
    ``tool_actor(member)``, through the Confirm path its draft would take, and return its record.
    A refusal deletes the record and comes through unchanged, for the model to read."""
    actor = tool_actor(member)
    repo = AssistantRepository(db)
    before = await _before(db, actor, spec)
    async with write_transaction(db):
        record = await repo.add_draft(
            actor.household_id,
            member_id=actor.id,
            conversation_key=key,
            kind=spec.kind,
            payload=dict(spec.payload),
            title=spec.title,
            lines=spec.lines,
            event_id=spec.event.id if spec.event is not None else None,
            run=spec.event.run if spec.event is not None else None,
            now=now,
            expires_at=expires_at(now),
            direct=True,
        )
        await db.commit()
    try:
        result = await drafts.confirm_direct(db, actor, record, now)
    except Exception:  # refused: the record goes too (a cancel leaves it unused, shown nowhere, for the purge)
        async with write_transaction(db):
            await repo.delete_draft(actor.household_id, record.id)
            await db.commit()
        raise
    text = done_text(result.text) if isinstance(result, ActionConfirmed) else ""
    try:
        undo = await _undo(db, actor, record, before)
    except Exception:  # done already: a record without an Undo, never an error that invites doing it again
        log.exception("assistant direct action's undo not kept", extra={"member_id": actor.id, "draft_id": record.id})
        undo = None
    first_answer = before is None and spec.kind in CALL_ACTIONS and spec.kind is not DraftKind.VOTE
    lines = (plain(text),) if undo is not None else (plain(text), NO_UNDO if first_answer else NO_UNDO_SAVED)
    async with write_transaction(db):
        await repo.keep_record(actor.household_id, record.id, payload={**record.payload, "undo": undo}, lines=lines)
        await db.commit()
    log.info("assistant acted", extra={"member_id": actor.id, "kind": spec.kind.value, "draft_id": record.id})
    kept = await repo.draft(actor.household_id, record.id)
    if kept is None:  # pragma: no cover - only the purge deletes it, 30 minutes on
        raise NotFound(NO_SUCH_RECORD)
    return kept


async def record_done(db: AsyncSession, member: Member, record: Draft, now: datetime) -> None:
    """Add a direct action's entry to its conversation (#363's ``draft_log``), after the message's
    exchange: "Done on …: title. What it did." Logged, never raised, as a used draft's entry is."""
    try:
        outcome = record.lines[0] if record.lines else ""
        await draft_log.record_use(db, member, record, confirmed=True, outcome=outcome, now=now, verb="Done")
    except Exception:
        log.exception("assistant direct action's entry not kept", extra={"member_id": member.id})


# Undo -------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Undone:
    """What **Undo** did: ``text`` for the member, and the call it changed, or where to see it."""

    record: DraftView
    text: str
    event_id: int | None = None
    link: tuple[str, str] | None = None


async def _mine(db: AsyncSession, member: Member, record_id: int) -> Draft:
    """``member``'s direct action, or NotFound: someone else's, a draft, and none look the same."""
    if not member.is_member:
        raise Forbidden("Only active members can undo.")
    record = await AssistantRepository(db).draft(member.household_id, record_id)
    if record is None or record.member_id != member.id or not record.direct or record.confirmed_at is None:
        raise NotFound(NO_SUCH_RECORD)
    return record


def _check_open(record: Draft, now: datetime) -> None:
    if record.undone_at is not None:
        raise DraftUnavailable(UNDONE)
    if not record.undoable:
        raise DraftUnavailable(NO_UNDO)
    if not record.undo_open(now):
        raise DraftUnavailable(UNDO_CLOSED)


async def undo(db: AsyncSession, clock: Clock, config: AssistantConfig, member: Member, record_id: int) -> Undone:
    """**Undo** on ``member``'s direct action (#374): run its inverse, as ``tool_actor(member)``,
    with marking it undone, in one transaction. Raises NotFound for a record that isn't theirs,
    ``DraftUnavailable`` for one undone already, with no Undo, past ``UNDO_MINUTES``, or while
    the assistant isn't available, and the use case's refusal, or ``MOVED_*`` for a change made
    since, which leaves the record as it was. Its conversation records it, as Confirm's does."""
    now = clock()
    record = await _mine(db, member, record_id)
    _check_open(record, now)
    if not await available(db, config, member):
        raise DraftUnavailable(TURNED_OFF)
    actor = tool_actor(member)
    repo = AssistantRepository(db)

    async def mark() -> None:
        if not await repo.mark_undone(
            actor.household_id, record.id, actor.id, now, now - timedelta(minutes=UNDO_MINUTES)
        ):
            latest = await repo.draft(actor.household_id, record.id)
            if latest is None:  # pragma: no cover - deleted between the read and the write
                raise NotFound(NO_SUCH_RECORD)
            _check_open(latest, now)
            raise DraftUnavailable(UNDONE)  # pragma: no cover - mark_undone found it used

    try:
        done = await _inverse(db, actor, record, now=now, mark=mark)
    except EventRuleViolation as exc:
        if exc.code == STALE:
            raise _moved(record.kind) from None
        raise
    log.info("assistant action undone", extra={"member_id": actor.id, "draft_id": record.id})
    try:
        await draft_log.record_use(db, member, record, confirmed=True, outcome=done.text, now=now, verb="Undone")
    except Exception:
        log.exception("assistant undo entry not kept", extra={"member_id": member.id, "draft_id": record.id})
    latest = await repo.draft(actor.household_id, record.id)
    return Undone(view(latest or record, now), done.text, done.event_id, done.link)


async def _inverse(
    db: AsyncSession, actor: Member, record: Draft, *, now: datetime, mark: Callable[[], Awaitable[None]]
) -> ActionConfirmed:
    """Run the record's inverse, calling ``mark`` first in its transaction."""
    kind, payload = record.kind, record.payload
    back: Mapping[str, Any] = payload["undo"]
    if kind in CALL_ACTIONS:
        return await _inverse_call(db, actor, record, back, now=now, mark=mark)
    if kind is DraftKind.PREFERENCE:
        inverse = {**payload, "level": back["level"], "before": back["after"]}
    elif kind is DraftKind.AWAY:
        window = back.get("window")
        if window is None:
            inverse = {"first": None, "last": None, "clear": True, "before": back.get("after")}
        else:
            inverse = {"first": window[0], "last": window[1], "clear": False, "before": back.get("after")}
    elif kind is DraftKind.NEEDS:
        inverse = {"needs": back["needs"], "before": back["after"]}
    elif kind is DraftKind.CAN_USUALLY:
        inverse = {"after": back["flags"], "before": back["after"]}
    else:
        answers = back.get("answers") or {}
        inverse = {
            "meal_id": payload["meal_id"],
            "item_id": payload["item_id"],
            "rating": answers.get("rating"),
            "issues": answers.get("issues", []),
            "issue_note": answers.get("issue_note"),
            "cook_thanks": bool(answers.get("cook_thanks")),
            "cook_note": answers.get("cook_note"),
            "before": back.get("after"),
        }

    async def use() -> None:
        await mark()
        if kind is DraftKind.FEEDBACK:  # a save in the same second keeps the stamp, so the answers too
            saved = await FeedbackRepository(db).get(actor.household_id, int(payload["meal_id"]), actor.id)
            if own_drafts.stored_answers(saved.answers if saved else None) != back.get("after_answers"):
                raise _moved(kind)

    done = await own_drafts.confirm(db, actor, replace(record, payload=inverse), now=now, use=use)
    if kind is DraftKind.FEEDBACK and back.get("answers") is not None:
        return replace(done, text="Undone. Your earlier feedback is back as it was.")
    return replace(done, text=undone_text(done.text))


async def _event(db: AsyncSession, actor: Member, record: Draft) -> Event:
    event = await EventRepository(db).get(actor.household_id, record.event_id) if record.event_id else None
    if event is None:
        raise NotFound(NO_SUCH_CALL)
    check_run(event, record.run)
    return event


async def _inverse_call(
    db: AsyncSession,
    actor: Member,
    record: Draft,
    back: Mapping[str, Any],
    *,
    now: datetime,
    mark: Callable[[], Awaitable[None]],
) -> ActionConfirmed:
    kind, payload = record.kind, record.payload
    today, _ = await drafts.household_day(db, actor.household_id, now)
    async with write_transaction(db):
        await mark()
        event = await _event(db, actor, record)
        when, slot = day_words(event.day, today), event.slot.value
        if kind is DraftKind.VOTE:
            ctx = await load(db, event)
            pick = check_same_pick(ctx, payload)
            if (actor.id in ctx.votes.get(pick.id, [])) is not bool(payload["vote"]):
                raise _moved(kind)
            name = str(payload["name"])
            if back["vote"]:
                await calls.vote(db, actor, event.id, pick.id, now=now)
                return ActionConfirmed(event.id, f"Undone. Your vote for {name} is in again.")
            await calls.unvote(db, actor, event.id, pick.id, now=now)
            return ActionConfirmed(event.id, f"Undone. You took back your vote for {name}.")
        if kind is DraftKind.SUGGESTION:
            ctx = await load(db, event)
            if _suggested(ctx, actor.id) != back["after"]:
                raise _moved(kind)
            mine = ctx.response_of(actor.id)
            theirs = [s for s in ctx.suggestions if s.member_id == actor.id]
            item_id = int(payload["item_id"])
            details = Details(
                eat_from=mine.eat_from if mine else None,
                eat_until=mine.eat_until if mine else None,
                suggestion_item_ids=tuple(s.item_id for s in theirs if s.item_id is not None and s.item_id != item_id),
                suggestion_texts=tuple(s.free_text for s in theirs if s.free_text),
                note=mine.note if mine else None,
            )
            await calls.save_details(db, actor, event.id, details, run=record.run, now=now)
            return ActionConfirmed(event.id, f"Undone. {payload['name']} is no longer one of your suggestions.")
        status = Status(str(back["status"]))
        offers = frozenset(RoleOffer(str(o)) for o in back.get("offers", []))

        def expect(current: Response | None) -> None:
            if _snap(current) != back.get("after"):
                raise _moved(kind)

        await calls.restore_answer(
            db,
            actor,
            event.id,
            member_id=int(back["member_id"]),
            status=status,
            offers=offers,
            run=record.run,
            now=now,
            expect=expect,
        )
    return ActionConfirmed(event.id, _restored_text(kind, payload, status, offers, when=when, slot=slot))


STATUS_WORDS = {Status.IN: "in for", Status.OUT: "out of", Status.MAYBE: "a maybe for"}


def _restored_text(
    kind: DraftKind, payload: Mapping[str, Any], status: Status, offers: frozenset[RoleOffer], *, when: str, slot: str
) -> str:
    call = f"{when}'s {slot} call"
    if kind is DraftKind.GUEST_ANSWER:
        return f"Undone. {payload['name']}'s answer for {call} is back as it was."
    doing = " and ".join(ROLE_DOING[r] for r in RoleOffer if r in offers)
    roles = f", down to {doing}" if doing else ""
    return f"Undone. You're {STATUS_WORDS[status]} {call} again{roles}."
