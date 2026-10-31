# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the coordinator can do that others can't (FR-16, FR-61, #87): break a tie, shuffle the
picks, and choose the winning pick's mode and who does its role; and, in Slack, adjust the plan
once it's decided (spec 5.7).

Each is the coordinator's, or an admin's when nobody coordinates, or a guest does (a guest
coordinator chooses roles, which is logistics, but not the plan, FR-58). Each takes the event's
lock, checks the run the person saw, and is audited and logged (#75). Everything is scoped to the
actor's household.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.events import AssignmentRepository, EventRepository, ProposalRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import CatalogItem, Mode
from dinnerbell.domain.events import (
    MODE_ROLE,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    ProposalSource,
    can_do,
    check_can_adjust,
    check_can_break_tie,
    check_can_choose_roles,
    check_can_shuffle,
    check_run,
    friendly,
)
from dinnerbell.domain.guests import GuestAction
from dinnerbell.domain.members import Member
from dinnerbell.domain.ranking import rank
from dinnerbell.services import activity, lifecycle
from dinnerbell.services import call_context as cc
from dinnerbell.services import guests as guest_rules
from dinnerbell.services.call_context import CallContext, locked_event, publish
from dinnerbell.services.calls import hand_off, handoff_target, meal_at, set_meal_time
from dinnerbell.services.common import audit
from dinnerbell.services.errors import NotFound
from dinnerbell.services.preferences import group_preferences

NOT_TIED = "That proposal isn't tied for the most votes."
NOTHING_TO_SHUFFLE = "Nothing can be shuffled: every proposal was added, offered, suggested, or has votes."
NOTHING_ELSE_FITS = "No other place fits for everyone who's in, so the proposals stay as they are."


def _target(event_id: int) -> str:
    return f"event:{event_id}"


def _coordinator(ctx: CallContext) -> Member | None:
    coordinator_id = ctx.event.coordinator_id
    return ctx.members.get(coordinator_id) if coordinator_id is not None else None


# Break the tie (FR-16) --------------------------------------------------------------------------


async def break_tie(
    db: AsyncSession, actor: Member, event_id: int, proposal_id: int, *, run: int | None = None, now: datetime
) -> None:
    """**Break the tie**: choose among the picks sharing the most votes, even without having voted
    (``check_can_break_tie``). It decides the call now, as a lock does, and the decision says who
    chose. A pick that isn't tied is refused; a vote that moved since the person looked changes
    what's tied, so a stale choice can't decide a pick that is no longer in the tie."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.BREAK_TIE)
        check_run(event, run)
        ctx = await cc.load(db, event)
        capacity = check_can_break_tie(event, actor, _coordinator(ctx))
        proposal = ctx.proposal(proposal_id)
        if proposal is None:
            raise NotFound("No such proposal.")
        activity.about(**activity.pick(ctx.items, proposal))
        if proposal.id not in cc.tied_ids(ctx):
            raise EventRuleViolation(NOT_TIED, code="not_tied")
        await lifecycle.decide(db, event, now, locked=proposal, actor=actor, tie_chosen=True)
        activity.action(
            db,
            "break_tie",
            event,
            actor,
            after=EventState.DECIDED,
            capacity=capacity.value,
            **activity.pick(ctx.items, proposal),
        )


# Shuffle the picks (FR-61) ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Shuffled:
    replaced: int
    kept: int
    shuffles_left: int


async def shuffle(db: AsyncSession, actor: Member, event_id: int, *, run: int | None = None, now: datetime) -> Shuffled:
    """**Shuffle picks**: redraw the ranked picks (``domain.events.shuffle_candidates``), with
    the next seed (``ranking_seed(..., shuffle=n)``), at most ``MAX_SHUFFLES`` times a call.

    With nobody having voted, every ranked pick is redrawn; otherwise those with no votes at all.
    Picks a member added, an offer made, or someone suggested stay, and so does every pick with a
    vote. The redraw holds to every hard filter ranking does, and to one pick per place (#77), as
    it is the same ranking. It leaves out the places just drawn away, unless nothing else fits.
    A shuffle that would change nothing is refused and isn't counted. The proposals message
    says who shuffled and how many were replaced, and a note goes in the call's thread. Audited
    as ``event.shuffled``; logged as ``shuffle`` with the counts."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.SHUFFLE)
        check_run(event, run)
        ctx = await cc.load(db, event)
        capacity = check_can_shuffle(event, actor, _coordinator(ctx))
        out = cc.shuffle_picks(ctx)
        if not out:
            raise EventRuleViolation(NOTHING_TO_SHUFFLE, code="nothing_to_shuffle")
        voted = any(ctx.votes.get(p.id) for p in ctx.proposals)
        out_items = frozenset(p.item_id for p in out if p.item_id is not None)
        kept_items = frozenset(p.item_id for p in ctx.proposals if p.item_id is not None) - out_items
        group = await group_preferences(db, actor.household_id, ctx.veto_people)
        number = event.shuffles + 1
        base = await lifecycle.ranking_snapshot(db, ctx, group, taken=kept_items | out_items, shuffle=number)
        fresh = rank(replace(base, limit=len(out)))
        if not fresh:
            raise EventRuleViolation(NOTHING_ELSE_FITS, code="nothing_else_fits")
        # Short of places, the rest come from those just drawn away, redrawn with the same seed.
        fresh_items = frozenset(r.item_id for r in fresh)
        back = (
            rank(replace(base, limit=len(out) - len(fresh), taken=kept_items | fresh_items))
            if len(fresh) < len(out)
            else []
        )
        retained = frozenset(r.item_id for r in back)
        dropped = [p for p in out if p.item_id not in retained]
        repo = ProposalRepository(db)
        slots = sorted(p.rank for p in dropped)
        for proposal in dropped:
            await repo.delete(actor.household_id, proposal.id)
        for position, choice in zip(slots, fresh, strict=False):
            await repo.add(
                household_id=actor.household_id,
                event_id=event.id,
                item_id=choice.item_id,
                mode=choice.mode,
                rank=position,
                score=choice.score,
                reasons=choice.reasons,
                source=ProposalSource.SYSTEM,
                now=now,
            )
        replaced = len(fresh)
        line = f"{actor.display_name} shuffled the proposals ({replaced} replaced)."
        event = await EventRepository(db).save(replace(event, shuffles=number, shuffle_note=line), now)
        ctx = await cc.load(db, event)
        by = ctx.person(actor.id)
        await publish(
            db, ctx, cc.proposals_updated(ctx), note.PicksShuffled(cc.event_info(ctx), by, replaced, number), now=now
        )
        counts: dict[str, Any] = {
            "replaced": replaced,
            "kept": len(ctx.proposals) - replaced,
            "shuffle": number,
            "voted": voted,
        }
        await audit(
            db, actor.household_id, actor, "event.shuffled", _target(event.id), counts | {"as": capacity.value}, now
        )
        activity.action(db, "shuffle", event, actor, capacity=capacity.value, **counts)
        return Shuffled(replaced, len(ctx.proposals) - replaced, cc.shuffles_left(ctx))


# Choose the mode and assign the roles (FR-61) ---------------------------------------------------


NO_ONE_CHOSEN = "Pick who will {verb} it."


@dataclass(frozen=True, slots=True)
class Held:
    """The message to someone just assigned, held back by ``choose_roles(tell=False)``: who, the
    assignment, and the coordinator who assigned them, if they hadn't offered."""

    member_id: int
    assignment_id: int
    by_id: int | None


def check_assignee(ctx: CallContext, item: CatalogItem, mode: Mode, who: int) -> Member:
    """Who may do the decided pick's role: an active member or guest who's in, and on the cook
    list of a home meal that has one."""
    target = ctx.members.get(who)
    if target is None or not target.active:
        raise NotFound("No such member.")
    response = ctx.response_of(who)
    if response is None or not response.is_in:
        raise EventRuleViolation("Pick someone who's in.", code="not_in")
    if not can_do(item, mode, who):
        raise EventRuleViolation(
            f"{item.name} has a cook list, and {target.display_name} isn't on it.", code="cook_list"
        )
    return target


async def choose_roles(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    mode: Mode | None = None,
    member_id: int | None = None,
    run: int | None = None,
    as_coordinator: bool = False,
    tell: bool = True,
    now: datetime,
) -> Held | None:
    """**Assign roles**: switch the winning pick to another way of getting it (``mode``, for a
    place that allows more than one) and say who does it (``member_id``, someone who's in; the
    first volunteer when None). Allowed to the coordinator, a guest coordinator too, and an admin
    (``check_can_choose_roles``), while the call is decided. It ends the coordinator's time to
    choose (``Event.roles_due_at``): the assignment is theirs now.

    Whoever is assigned is told. If they hadn't offered for the role, the message says the
    coordinator assigned it; they can't refuse in the app, but they can tell the coordinator.
    Audited as ``event.roles_assigned``; logged as ``assign_roles``.

    From Slack's Adjust, ``as_coordinator`` allows only the coordinator (spec 5.7), and with
    ``tell`` False the message to whoever is assigned is held and returned, for ``adjust`` to
    send once a hand-off in the same save is done."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.MANAGE_DECISION)
        check_run(event, run)
        check_can_choose_roles(event, actor)
        if as_coordinator:
            check_can_adjust(event, actor)
        ctx = await cc.load(db, event)
        item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
        winner, current = ctx.winner, event.decided_mode
        if item is None or winner is None or current is None:  # pragma: no cover - a decided call has all three
            raise EventRuleViolation("There's no decision to change.", code="not_decided")
        chosen_mode = mode or current
        if chosen_mode not in item.modes:
            raise EventRuleViolation(f"{item.name} isn't available by {chosen_mode.label.lower()}.", code="unavailable")
        volunteers = ctx.coverage.volunteers(chosen_mode, item.cook_ids)
        who = member_id if member_id is not None else (volunteers[0] if volunteers else None)
        if who is None:
            raise EventRuleViolation(NO_ONE_CHOSEN.format(verb=chosen_mode.verb), code="no_one_chosen")
        target = check_assignee(ctx, item, chosen_mode, who)
        role = MODE_ROLE[chosen_mode]
        assignments = AssignmentRepository(db)
        if chosen_mode is not current:
            await ProposalRepository(db).set_mode(actor.household_id, winner.id, chosen_mode)
            await assignments.unassign(actor.household_id, event.id, MODE_ROLE[current])
        was_pending = event.roles_due_at is not None
        had = next((a for a in ctx.assignments if a.role is role), None)
        changed = chosen_mode is not current or had is None or had.member_id != who
        event = await EventRepository(db).save(replace(event, decided_mode=chosen_mode, roles_due_at=None), now)
        assigned = await assignments.assign(actor.household_id, event.id, role, who, now) if changed else None
        if assigned is None and had is not None and was_pending:
            assigned = had  # the automatic choice stands; they hadn't been told yet
        ctx = await cc.load(db, event)
        decision = cc.decision_info(ctx)
        sent: list[note.Notification] = [*cc.decision_updated(ctx), cc.message_updated(ctx)]
        held: Held | None = None
        if assigned is not None and decision is not None:
            by = actor.id if who not in volunteers and who != actor.id else None
            if tell:
                sent.append(
                    cc.assignment_made(ctx, who, assigned.id, decision, ctx.person(by) if by is not None else None)
                )
            else:
                held = Held(who, assigned.id, by)
        await publish(db, ctx, *sent, now=now)
        payload = {
            "role": role.value,
            "member_id": who,
            "mode": chosen_mode.value,
            "switched_from": current.value if chosen_mode is not current else None,
            "volunteered": who in volunteers,
        }
        await audit(db, actor.household_id, actor, "event.roles_assigned", _target(event.id), payload, now)
        activity.action(
            db,
            "assign_roles",
            event,
            actor,
            role=role.value,
            to_member_id=who,
            to_member=target.display_name,
            switched=chosen_mode is not current,
            volunteered=who in volunteers,
            item=item,
            item_id=item.id,
            mode=chosen_mode,
        )
        return held


# Adjust the plan in Slack (spec 5.7) ------------------------------------------------------------

PLAN_CHANGED = "The plan changed since you opened this. Open Adjust again."


@dataclass(frozen=True, slots=True)
class Adjustment:
    """The coordinator's **Adjust** modal: the meal time (a wall time on the call's date), who
    does the decided pick's role, and who coordinates. As asked for, None leaves that part alone;
    as opened, it's what the modal showed."""

    meal_time: time | None = None
    assignee_id: int | None = None
    coordinator_id: int | None = None

    @property
    def empty(self) -> bool:
        return self.meal_time is None and self.assignee_id is None and self.coordinator_id is None


class AdjustmentRefused(EventRuleViolation):
    """One part of an **Adjust** the rules refuse, so the modal can show why next to it. ``part``
    is ``meal_time``, ``assignee``, or ``coordinator``."""

    def __init__(self, part: str, refusal: EventRuleViolation | NotFound) -> None:
        super().__init__(str(refusal), code=getattr(refusal, "code", "not_found"))
        self.part = part


class PartlyAdjusted(EventRuleViolation):
    """A part of an **Adjust** was refused after earlier parts were saved, as when someone left
    in between: ``saved`` is what changed, and the message says why the rest didn't."""

    def __init__(self, saved: Adjustment, refusal: Exception) -> None:
        shown = friendly(refusal) if isinstance(refusal, IllegalTransition) else refusal
        super().__init__(str(shown), code=getattr(shown, "code", "not_found"))
        self.saved = saved


def _part(part: str, check: Callable[[], object]) -> None:
    try:
        check()
    except (EventRuleViolation, NotFound) as exc:
        raise AdjustmentRefused(part, exc) from exc


def _wall(ctx: CallContext, at: datetime) -> time:
    local = at.astimezone(ctx.zone)
    return time(local.hour, local.minute)


def _asked(part: str, wanted: object, opened: object, current: object) -> bool:
    """Whether the person changed ``part`` in the modal: it differs from what the modal opened
    with. A part changed elsewhere since is refused, so the modal never writes back over it."""
    if wanted is None or wanted == opened:
        return False
    if current != opened:
        raise AdjustmentRefused(part, EventRuleViolation(PLAN_CHANGED, code="plan_changed"))
    return True


async def plan_adjustment(
    db: AsyncSession, actor: Member, event_id: int, wanted: Adjustment, *, opened: Adjustment, run: int
) -> Adjustment:
    """What ``adjust`` would change: each part of ``wanted`` that differs from what the modal
    opened with (``opened``), checked with the rules the use cases apply. It only reads, so the
    Slack modal can ask before it closes and show a refusal next to its field
    (``AdjustmentRefused``). A refusal of the whole thing, such as from someone who isn't the
    coordinator, is an ``EventRuleViolation``."""
    event = await EventRepository(db).get(actor.household_id, event_id)
    if event is None:
        raise NotFound(cc.NO_SUCH_CALL)
    activity.about(event)  # for the refusal's log line, as under the event's lock
    return await _plan(db, await cc.load(db, event), actor, wanted, opened, run)


async def _plan(
    db: AsyncSession, ctx: CallContext, actor: Member, wanted: Adjustment, opened: Adjustment, run: int
) -> Adjustment:
    event = ctx.event
    await guest_rules.gate(db, event, actor, GuestAction.MANAGE_DECISION)
    check_run(event, run)
    check_can_adjust(event, actor)
    item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
    mode = event.decided_mode
    if item is None or mode is None or event.meal_time is None:  # pragma: no cover - a decided call has all three
        raise EventRuleViolation("There's no decision to change.", code="not_decided")
    meal = wanted.meal_time
    if _asked("meal_time", meal, opened.meal_time, _wall(ctx, event.meal_time)) and meal is not None:
        at: time = meal
        _part("meal_time", lambda: meal_at(ctx, at))
    else:
        meal = None
    current = next((a.member_id for a in ctx.assignments if a.role is MODE_ROLE[mode]), None)
    assignee = wanted.assignee_id
    if _asked("assignee", assignee, opened.assignee_id, current) and assignee is not None:
        who: int = assignee
        _part("assignee", lambda: check_assignee(ctx, item, mode, who))
    else:
        assignee = None
    lead = wanted.coordinator_id
    if _asked("coordinator", lead, opened.coordinator_id, event.coordinator_id) and lead is not None:
        await guest_rules.gate(db, event, actor, GuestAction.HAND_OFF)
        to: int = lead
        _part("coordinator", lambda: handoff_target(ctx, actor, to))
    else:
        lead = None
    return Adjustment(meal, assignee, lead)


async def adjust(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    wanted: Adjustment,
    *,
    opened: Adjustment,
    run: int,
    now: datetime,
) -> Adjustment:
    """**Adjust** in Slack (spec 5.7): the coordinator changes the meal time, who does the role,
    and who coordinates, in one modal. Only the parts they changed from what the modal opened
    with change, each through the use case the web uses for it, which checks its rules again
    under the event's lock, the coordinator's say included, and is audited:
    ``calls.set_meal_time``, ``choose_roles``, then ``calls.hand_off``, last, since handing off
    ends the actor's say over the rest. Returns what changed; an empty ``Adjustment`` changed
    nothing and sent nothing. A part refused after an earlier one was saved raises
    ``PartlyAdjusted``.

    With a hand-off in the same save, the message to whoever gets the role waits until after it,
    so it names the new coordinator; when that's the same person, the hand-off's own message
    lists both roles, so there's only one."""
    changes = await plan_adjustment(db, actor, event_id, wanted, opened=opened, run=run)
    saved = Adjustment()
    held: Held | None = None
    try:
        if changes.meal_time is not None:
            await set_meal_time(db, actor, event_id, changes.meal_time, run=run, as_coordinator=True, now=now)
            saved = replace(saved, meal_time=changes.meal_time)
        if changes.assignee_id is not None:
            held = await choose_roles(
                db,
                actor,
                event_id,
                member_id=changes.assignee_id,
                run=run,
                as_coordinator=True,
                tell=changes.coordinator_id is None,
                now=now,
            )
            saved = replace(saved, assignee_id=changes.assignee_id)
        if changes.coordinator_id is not None:
            await hand_off(db, actor, event_id, changes.coordinator_id, run=run, now=now)
            saved = replace(saved, coordinator_id=changes.coordinator_id)
    except (EventRuleViolation, NotFound, IllegalTransition) as exc:
        if saved.empty:
            raise
        raise PartlyAdjusted(saved, exc) from exc
    finally:
        if held is not None and held.member_id != saved.coordinator_id:
            await _tell_assigned(db, actor, event_id, held, now=now)
    return saved


async def _tell_assigned(db: AsyncSession, actor: Member, event_id: int, held: Held, *, now: datetime) -> None:
    """Send the message ``choose_roles`` held, naming the call as it is now."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await cc.load(db, event)
        decision = cc.decision_info(ctx)
        if event.state is not EventState.DECIDED or decision is None:
            return
        by = ctx.person(held.by_id) if held.by_id is not None else None
        await publish(db, ctx, cc.assignment_made(ctx, held.member_id, held.assignment_id, decision, by), now=now)
