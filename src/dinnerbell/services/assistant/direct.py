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
from datetime import date, datetime, time, timedelta
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
from dinnerbell.domain.assistant_own import level_label
from dinnerbell.domain.away import Away
from dinnerbell.domain.events import (
    Event,
    EventRuleViolation,
    Response,
    RoleOffer,
    Status,
    check_details,
    check_run,
    drops_coordinating,
    with_details,
    with_offer,
    with_status,
    with_usual_offers,
)
from dinnerbell.domain.feedback import FeedbackInput, PlaceIssue, Rating
from dinnerbell.domain.members import Member
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services import call_context as cc
from dinnerbell.services import calls
from dinnerbell.services import preferences as preference_service
from dinnerbell.services.assistant import call_drafts, draft_log, drafts, own_drafts
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
NO_UNDO_GUEST = "It was {name}'s first answer to this call, so there's no Undo. Answer for them again to change it."
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
    else:
        why = " ".join(record.lines[1:2]).replace("your ", "their ").replace("You ", "They ")
        shows = f"Dinnerbell shows {label} a record of it with no Undo: {why}"
    return f"Done at once, not a draft: {record.title}. {did} {shows} Tell them briefly that it's done."


OVER_THE_LIMIT = "It's a draft, not done at once, because this message did {most} things at once already."


# Acting -----------------------------------------------------------------------------------------


class DraftInstead(Exception):
    """A direct action that would tell someone new, found under the call's lock: it stays a
    draft, and ``reason`` tells the model why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


CLOSES_EARLY = (
    "It's a draft, not done at once: with it, everyone has answered the call, so its answers close early and "
    "Dinnerbell tells the household."
)
ENDS_COORDINATING = (
    "It's a draft, not done at once: the member coordinates that call now, and an out ends that and tells "
    "the household."
)
CLOSES_A_CALL = (
    "It's a draft, not done at once: it would let a call's answers close early, and Dinnerbell would tell the "
    "household."
)
UNDO_CLOSES_A_CALL = (
    "Putting that back would let a call's answers close early, so Undo changed nothing. Change it on Preferences."
)
RATINGS_MOVE = "It's a draft, not done at once: the member's ratings of it count now, and a level stops them for good."
MAYBE_COORDINATING = (
    "You coordinate this call: if you're still a maybe when answers close, you stop coordinating, as on the "
    "call's page."
)


def _response_json(response: Response | None) -> dict[str, Any] | None:
    """A whole answer, as **Undo** puts it back and compares it (#374): everything the call keeps
    of it, but when it last changed."""
    if response is None:
        return None
    return {
        "member_id": response.member_id,
        "status": response.status.value,
        "guests": response.guests,
        "guest_needs": sorted(response.guest_needs),
        "eat_from": response.eat_from.isoformat() if response.eat_from else None,
        "eat_until": response.eat_until.isoformat() if response.eat_until else None,
        "offers": sorted(o.value for o in response.offers),
        "note": response.note,
        "maybe_at_deadline": response.maybe_at_deadline,
        "responded_at": response.responded_at.isoformat() if response.responded_at else None,
        "late": response.late,
        "vote_counts": response.vote_counts,
        "usual_offers_done": response.usual_offers_done,
        # Only an out can be away's, as the call keeps it.
        "away_until": response.away_until.isoformat()
        if response.away_until and response.status is Status.OUT
        else None,
    }


def _response(raw: Mapping[str, Any]) -> Response:
    def moment(value: Any) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value else None

    return Response(
        member_id=int(raw["member_id"]),
        status=Status(str(raw["status"])),
        guests=int(raw["guests"]),
        guest_needs=frozenset(int(n) for n in raw["guest_needs"]),
        eat_from=time.fromisoformat(str(raw["eat_from"])) if raw["eat_from"] else None,
        eat_until=time.fromisoformat(str(raw["eat_until"])) if raw["eat_until"] else None,
        offers=frozenset(RoleOffer(str(o)) for o in raw["offers"]),
        note=raw["note"],
        maybe_at_deadline=bool(raw["maybe_at_deadline"]),
        responded_at=moment(raw["responded_at"]),
        late=bool(raw["late"]),
        vote_counts=bool(raw["vote_counts"]),
        usual_offers_done=bool(raw["usual_offers_done"]),
        away_until=date.fromisoformat(str(raw["away_until"])) if raw["away_until"] else None,
    )


def _suggested(ctx: CallContext, member_id: int) -> dict[str, list[Any]]:
    theirs = [s for s in ctx.suggestions if s.member_id == member_id]
    return {
        "items": sorted(s.item_id for s in theirs if s.item_id is not None),
        "texts": sorted(s.free_text for s in theirs if s.free_text),
    }


def _whose(kind: DraftKind, payload: Mapping[str, Any], actor: Member) -> int:
    """The person whose answer a call action changes: the member's, or their guest's."""
    return int(payload["guest_id"]) if kind is DraftKind.GUEST_ANSWER else actor.id


def _predicted(ctx: CallContext, actor: Member, kind: DraftKind, payload: Mapping[str, Any], now: datetime) -> Any:
    """The answer a call action leaves, by the rules its use case applies, under the same lock:
    ``respond``, ``offer_role``, ``save_details``, or ``guests.answer_for``. None for a vote."""
    whose = _whose(kind, payload, actor)
    mine = ctx.response_of(whose)
    if kind is DraftKind.ANSWER:
        return with_usual_offers(with_status(mine, whose, Status(str(payload["status"])), now), actor)
    if kind is DraftKind.ROLE:
        return with_offer(mine, whose, RoleOffer(str(payload["role"])), bool(payload["offered"]), now) or mine
    if kind is DraftKind.SUGGESTION:
        active = [i.id for i in ctx.items.values() if i.active]
        details = check_details(
            call_drafts.details_with(ctx, mine, whose, int(payload["item_id"])), active_item_ids=active
        )
        new = with_details(mine, whose, details, now)
        return new if mine is not None else with_usual_offers(new, actor)
    if kind is DraftKind.GUEST_ANSWER:
        if payload.get("status"):
            return with_status(mine, whose, Status(str(payload["status"])), now)
        role = RoleOffer(str(payload["role"]))
        return with_offer(mine, whose, role, bool(payload["offered"]), now) or mine
    return None


@dataclass
class _Checked:
    """What ``act``'s check found under the call's lock, for the record."""

    undo: dict[str, Any] | None = None
    extra: tuple[str, ...] = ()


async def _check(
    db: AsyncSession, actor: Member, record: Draft, found: _Checked, *, now: datetime, today: date
) -> None:
    """In the action's transaction, before its use case: whether it still acts at once, and
    what **Undo** puts back and what the action leaves, read where nothing can change them before
    the use case commits. Writes the Undo into the record, so it commits with the action."""
    kind, payload = record.kind, record.payload
    undo: dict[str, Any] | None = None
    if kind in CALL_ACTIONS:
        event = await _event(db, actor, record)
        ctx = await load(db, event)
        if kind is DraftKind.VOTE:
            undo = {"vote": not payload["vote"]}
        else:
            whose = _whose(kind, payload, actor)
            before = ctx.response_of(whose)
            after = _predicted(ctx, actor, kind, payload, now)
            if kind is DraftKind.ANSWER and drops_coordinating(event, after):
                raise DraftInstead(ENDS_COORDINATING)
            if after is not None and await cc.closes_early_with(db, event, after, now):
                raise DraftInstead(CLOSES_EARLY)
            if kind is DraftKind.ANSWER and after.status is Status.MAYBE and event.coordinator_id == actor.id:
                found.extra = (MAYBE_COORDINATING,)
            if before is not None:
                undo = {"answer": _response_json(before), "after": _response_json(after)}
                if kind is DraftKind.SUGGESTION:
                    left = _suggested(ctx, actor.id)
                    added = sorted([*left["items"], int(payload["item_id"])])
                    undo |= {"suggestions": left, "after_suggestions": {**left, "items": added}}
    elif kind is DraftKind.PREFERENCE:
        target_type, target_id = TargetType(str(payload["target_type"])), int(payload["target_id"])
        edited = None
        if target_type is TargetType.ITEM:
            if await preference_service.my_ratings_move(db, actor, target_id, today=today):
                raise DraftInstead(RATINGS_MOVE)
            at = await preference_service.my_item_edit(db, actor, target_id)
            edited = at.isoformat() if at else None
        undo = {"level": payload["before"], "after": payload["level"], "edited_at": edited}
        if target_type is TargetType.ITEM:  # the action's own edit time, for Undo to tell it's still the last
            undo["edited_by_action"] = now.isoformat()
    elif kind is DraftKind.AWAY:
        after = None if payload.get("clear") else [payload["first"], payload["last"]]
        if await _closes_a_call(db, actor, window=after, now=now):
            raise DraftInstead(CLOSES_A_CALL)
        undo = {"window": payload.get("before"), "after": after}
    elif kind is DraftKind.NEEDS:
        undo = {"needs": payload["before"], "after": sorted(int(n) for n in payload["needs"])}
    elif kind is DraftKind.CAN_USUALLY:
        if await _closes_a_call(db, actor, flags=payload["after"], now=now):
            raise DraftInstead(CLOSES_A_CALL)
        undo = {"flags": payload["before"], "after": payload["after"]}
    else:
        answers = FeedbackInput(
            rating=Rating(str(payload["rating"])) if payload.get("rating") else None,
            place_issues=frozenset(PlaceIssue(str(i)) for i in payload.get("issues", [])),
            issue_note=payload.get("issue_note"),
            cook_thanks=bool(payload.get("cook_thanks")),
            cook_note=payload.get("cook_note"),
        )
        saving = None if answers.is_empty else own_drafts.stored_answers(answers)
        undo = {"answers": payload.get("replaced"), "after_answers": saving}
    found.undo = undo
    await AssistantRepository(db).keep_record(
        actor.household_id, record.id, payload={**payload, "undo": undo}, lines=record.lines
    )


_KEEP: Any = object()


async def _closes_a_call(
    db: AsyncSession, actor: Member, *, now: datetime, window: Any = _KEEP, flags: Mapping[str, Any] | None = None
) -> bool:
    """Whether away days as ``window`` (first and last, or None for none), or **I can usually**
    as ``flags``, would let a call taking answers close them early (``own_change_closes_early``):
    the window answering the member Out, or giving back what it answered, or their ordering
    counting toward who covers a call they're in. In the caller's write transaction."""
    me = await own_drafts.fresh(db, actor)
    after = me
    if window is not _KEEP:
        away = None if window is None else Away(date.fromisoformat(str(window[0])), date.fromisoformat(str(window[1])))
        after = replace(after, away=away)
    if flags is not None:
        after = replace(after, capabilities=own_drafts.capabilities(flags))
    return await cc.own_change_closes_early(db, me, after, now)


def _no_undo_line(kind: DraftKind, payload: Mapping[str, Any]) -> str:
    if kind is DraftKind.GUEST_ANSWER:
        return NO_UNDO_GUEST.format(name=payload.get("name", "your guest"))
    return NO_UNDO if kind in CALL_ACTIONS else NO_UNDO_SAVED


async def act(db: AsyncSession, member: Member, key: str, spec: DraftSpec, *, now: datetime) -> Draft:
    """Do ``spec`` at once for ``member`` (#374), which ``acts_directly`` allows, as
    ``tool_actor(member)``, through the Confirm path its draft would take, and return its record.
    Under that path's lock, a call action that would complete the call's answers, or end the
    member's coordinating, and a level on an item their ratings move, raise ``DraftInstead``.
    The record, with what **Undo** puts back, commits with the action. A refusal deletes the
    record and comes through unchanged, for the model to read."""
    actor = tool_actor(member)
    repo = AssistantRepository(db)
    today, _ = await drafts.household_day(db, actor.household_id, now)
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
    found = _Checked()

    async def check() -> None:
        await _check(db, actor, record, found, now=now, today=today)

    try:
        result = await drafts.confirm_direct(db, actor, record, now, check=check)
    except Exception:  # refused: the record goes too (a cancel leaves it unused, shown nowhere, for the purge)
        async with write_transaction(db):
            await repo.delete_draft(actor.household_id, record.id)
            await db.commit()
        raise
    text = plain(done_text(result.text)) if isinstance(result, ActionConfirmed) else ""
    why = (_no_undo_line(spec.kind, spec.payload),) if found.undo is None else ()
    lines = (text, *why, *found.extra)
    kept = replace(record, payload={**record.payload, "undo": found.undo}, lines=lines, used_at=now, confirmed_at=now)
    try:
        async with write_transaction(db):
            await repo.keep_record(actor.household_id, record.id, payload=kept.payload, lines=lines)
            await db.commit()
    except Exception:  # done, and its Undo is stored: only the card's words after a reload differ
        log.exception("assistant direct action's words not kept", extra={"member_id": actor.id, "draft_id": record.id})
    log.info("assistant acted", extra={"member_id": actor.id, "kind": spec.kind.value, "draft_id": record.id})
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
    if not record.undoable:  # its record's second line says why: a first answer, or not saved
        raise DraftUnavailable(record.lines[1] if len(record.lines) > 1 else NO_UNDO_SAVED)
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
        return await _inverse_level(db, actor, record, back, now=now, mark=mark)
    if kind is DraftKind.AWAY:
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
        # Give's own check wants the feedback's save time as it is; the answers, compared under
        # its lock in ``use``, say whether anything moved since the action.
        now_saved = await FeedbackRepository(db).get(actor.household_id, int(payload["meal_id"]), actor.id)
        inverse = {
            "meal_id": payload["meal_id"],
            "item_id": payload["item_id"],
            "rating": answers.get("rating"),
            "issues": answers.get("issues", []),
            "issue_note": answers.get("issue_note"),
            "cook_thanks": bool(answers.get("cook_thanks")),
            "cook_note": answers.get("cook_note"),
            "before": own_drafts.stamp(now_saved),
        }

    async def use() -> None:
        await mark()
        if kind is DraftKind.AWAY and await _closes_a_call(db, actor, window=back.get("window"), now=now):
            raise EventRuleViolation(UNDO_CLOSES_A_CALL, code="undo_closes_early")
        if kind is DraftKind.CAN_USUALLY and await _closes_a_call(db, actor, flags=back["flags"], now=now):
            raise EventRuleViolation(UNDO_CLOSES_A_CALL, code="undo_closes_early")
        if kind is DraftKind.FEEDBACK:
            saved = await FeedbackRepository(db).get(actor.household_id, int(payload["meal_id"]), actor.id)
            if own_drafts.stored_answers(saved.answers if saved else None) != back.get("after_answers"):
                raise _moved(kind)

    done = await own_drafts.confirm(db, actor, replace(record, payload=inverse), now=now, use=use)
    if kind is DraftKind.FEEDBACK and back.get("answers") is not None:
        return replace(done, text="Undone. Your earlier feedback is back as it was.")
    return replace(done, text=undone_text(done.text))


async def _inverse_level(
    db: AsyncSession,
    actor: Member,
    record: Draft,
    back: Mapping[str, Any],
    *,
    now: datetime,
    mark: Callable[[], Awaitable[None]],
) -> ActionConfirmed:
    """The earlier level, and on an item, when the member had last set one, as it was, so a
    rating saved since counts as it would have (FR-55): ``restore_my_preference``."""
    payload = record.payload
    target_type, target_id = TargetType(str(payload["target_type"])), int(payload["target_id"])
    level = Stance.parse(str(back["level"]))
    edited = datetime.fromisoformat(str(back["edited_at"])) if back.get("edited_at") else None
    async with write_transaction(db):
        await mark()
        current = await preference_service.my_stance(db, actor, target_type, target_id)
        if current is None:
            raise NotFound(own_drafts.GONE)
        if current[1].level != back["after"]:
            raise _moved(DraftKind.PREFERENCE)
        if back.get("edited_by_action") is not None:  # set again since, even to the same level: theirs now
            last = await preference_service.my_item_edit(db, actor, target_id)
            if last is None or last != datetime.fromisoformat(str(back["edited_by_action"])):
                raise _moved(DraftKind.PREFERENCE)
        name = await preference_service.restore_my_preference(
            db, actor, target_type, target_id, level, edited_at=edited, now=now
        )
    text = f"Undone. Your level for {name} is {level_label(level)} now."
    return ActionConfirmed(None, text, link=own_drafts.PREFERENCES)


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
        earlier = _response(back["answer"])
        suggestions = None
        if kind is DraftKind.SUGGESTION:
            kept = back["suggestions"]
            suggestions = (tuple(int(i) for i in kept["items"]), tuple(str(t) for t in kept["texts"]))

        def expect(ctx: CallContext) -> None:
            if _response_json(ctx.response_of(earlier.member_id)) != back.get("after"):
                raise _moved(kind)
            if kind is DraftKind.SUGGESTION and _suggested(ctx, actor.id) != back.get("after_suggestions"):
                raise _moved(kind)

        await calls.restore_answer(
            db, actor, event.id, earlier, suggestions=suggestions, run=record.run, now=now, expect=expect
        )
    if kind is DraftKind.SUGGESTION:
        return ActionConfirmed(event.id, f"Undone. {payload['name']} is no longer one of your suggestions.")
    return ActionConfirmed(
        event.id, _restored_text(kind, payload, earlier.status, earlier.offers, when=when, slot=slot)
    )


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
