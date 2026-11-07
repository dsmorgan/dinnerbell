# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the coordinator can do that others can't (FR-16, FR-61, #87): break a tie, shuffle the
picks, move the decision time (#198), and choose the winning pick's mode and who does its role;
and, in Slack, adjust the plan once it's decided (spec 5.7).

Each is the coordinator's and every admin's alike (#179). A guest coordinator chooses roles,
which is logistics, but not the plan (FR-58). Each takes the event's
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
    LEAD_ROLE,
    MODE_ROLES,
    WORK_ROLES,
    AssignmentRole,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    Proposal,
    ProposalSource,
    Window,
    can_do,
    check_can_adjust,
    check_can_break_tie,
    check_can_choose_roles,
    check_can_move_decision_time,
    check_can_set_way,
    check_can_shuffle,
    check_guest_keeps_voting_open,
    check_run,
    default_roles,
    friendly,
    takeout_ways,
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
        capacity = check_can_break_tie(event, actor)
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
        capacity = check_can_shuffle(event, actor)
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


# Set a takeout pick's way during the vote (#178) -----------------------------------------------

NOT_BOTH_WAYS = "That place has one way to get it, so there's nothing to choose."


def _way_changed(proposal: Proposal) -> str:
    """Why a clear for a way that isn't the chosen one anymore is refused (#212)."""
    if proposal.mode_chosen:
        return f"Someone made it {proposal.mode.value} since you looked."
    return "Someone set it back to either way since you looked."


async def set_way(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    proposal_id: int,
    mode: Mode | None,
    *,
    clears: Mode | None = None,
    run: int | None = None,
    now: datetime,
) -> None:
    """Set a takeout pick whose place allows both to pickup or delivery, while voting
    (``check_can_set_way``): the coordinator, a guest coordinator too, or an admin. Setting it,
    even to the way it has, settles it, so the decision doesn't ask the coordinator about it
    again (``domain.events.way_unsettled``). A way nobody who's in can do is refused, as the
    decision would pass the pick over. The proposals message updates in place. Audited as
    ``event.way_set``; logged as ``set_way``.

    ``mode=None`` clears it (#212): the pick keeps the way it has, but nobody chose it, so the
    decision asks again. The same people may, with the same checks except coverage, which only a
    way being set needs. ``clears`` is the way the person saw chosen: the web and Slack send it,
    and if that way isn't the chosen one anymore, because someone set another way or cleared it
    meanwhile, the clear is refused (``way_changed``) and nothing is written. A clear without
    ``clears`` of a way that's open already is accepted, and audited, as setting the way a pick
    has is. The audit's ``mode`` is the way set, or None for a clear, and ``switched_from`` is
    the way the pick had, chosen or not."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.SET_WAY)
        check_run(event, run)
        check_can_set_way(event, actor)
        ctx = await cc.load(db, event)
        proposal = ctx.proposal(proposal_id)
        if proposal is None:
            raise NotFound("No such proposal.")
        activity.about(**activity.pick(ctx.items, proposal))
        item = ctx.items.get(proposal.item_id) if proposal.item_id is not None else None
        ways = takeout_ways(item.modes) if item is not None else ()
        if (
            item is None
            or not ways
            or (mode is not None and mode not in ways)
            or (clears is not None and clears not in ways)
        ):
            raise EventRuleViolation(NOT_BOTH_WAYS, code="not_both_ways")
        if mode is None and clears is not None and not (proposal.mode_chosen and proposal.mode is clears):
            raise EventRuleViolation(_way_changed(proposal), code="way_changed")
        proposals = ProposalRepository(db)
        if mode is None:
            await proposals.clear_mode(actor.household_id, proposal.id)
        else:
            refusal = lifecycle.uncovered(ctx.coverage, mode, item.cook_ids)
            if refusal is not None:
                raise refusal
            await proposals.set_mode(actor.household_id, proposal.id, mode)
        ctx = await cc.load(db, event)
        await publish(db, ctx, cc.proposals_updated(ctx), now=now)
        payload = {
            "proposal_id": proposal.id,
            "mode": mode.value if mode is not None else None,
            "switched_from": proposal.mode.value,
        }
        await audit(db, actor.household_id, actor, "event.way_set", _target(event.id), payload, now)
        activity.action(
            db,
            "set_way",
            event,
            actor,
            switched=mode is not None and mode is not proposal.mode,
            cleared=mode is None,
            **(activity.pick(ctx.items, proposal) | {"mode": mode if mode is not None else proposal.mode}),
        )


# Move the decision time (#198, FR-6) ------------------------------------------------------------


async def move_decision_time(
    db: AsyncSession, actor: Member, event_id: int, minutes: int, *, run: int | None = None, now: datetime
) -> datetime:
    """Move the decision time ``minutes`` later (negative is earlier) while voting
    (``check_can_move_decision_time``): the coordinator, a guest coordinator too, or an admin. It
    is the admin's vote-window move (``call_admin.move_deadline``), with its limits and updated
    messages: a time that lands in the past closes voting at once, except for a guest
    (``check_guest_keeps_voting_open``). Returns the new decision time. Audited as
    ``event.deadline_moved``, with the capacity; logged as ``move_deadline``."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.MOVE_DECISION_TIME)
        check_run(event, run)
        capacity = check_can_move_decision_time(event, actor, now=now)
        check_guest_keeps_voting_open(event, actor, minutes, now=now)
        current, moved = await lifecycle.move_window(db, event, Window.VOTE, minutes, now)
        window = Window.VOTE.value
        payload = {"window": window, "from": current.isoformat(), "to": moved.isoformat(), "as": capacity.value}
        await audit(db, actor.household_id, actor, "event.deadline_moved", _target(event.id), payload, now)
        activity.action(db, "move_deadline", event, actor, window=window, to=moved.isoformat(), capacity=capacity.value)
    await lifecycle.advance(db, actor.household_id, event_id, now)
    return moved


# Choose the mode and assign the roles (FR-61) ---------------------------------------------------


NO_ONE_CHOSEN = "Pick who will {verb}."
ROLE_VERB = {AssignmentRole.COOK: "cook it", AssignmentRole.ORDER: "order it", AssignmentRole.PICKUP: "pick it up"}


@dataclass(frozen=True, slots=True)
class Held:
    """The message to someone ``choose_roles`` just assigned: who, the assignment, and the
    coordinator who assigned them, if they hadn't offered. ``choose_roles`` returns one for each
    person it told, or, with ``tell=False``, held back for ``adjust`` to send."""

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


def check_mode(item: CatalogItem, mode: Mode) -> None:
    """The decided place can be had ``mode``'s way (FR-61): the catalog says it supports it."""
    if mode not in item.modes:
        raise EventRuleViolation(f"{item.name} isn't available by {mode.label.lower()}.", code="unavailable")


def _roles(
    ctx: CallContext, item: CatalogItem, mode: Mode, lead: int | None, pickup: int | None
) -> dict[AssignmentRole, int]:
    """Who does each of ``mode``'s roles: ``lead`` cooks or orders, ``pickup`` picks up, and the
    first volunteers do what nobody was picked for (``default_roles``, FR-17, #178). ``pickup``
    counts only for a pickup."""
    roles = default_roles(mode, ctx.coverage, item.cook_ids, lead=lead, pickup=pickup if mode is Mode.PICKUP else None)
    for role in MODE_ROLES[mode]:
        if role not in roles:
            raise EventRuleViolation(NO_ONE_CHOSEN.format(verb=ROLE_VERB[role]), code="no_one_chosen")
    return roles


async def choose_roles(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    mode: Mode | None = None,
    member_id: int | None = None,
    pickup_id: int | None = None,
    run: int | None = None,
    as_coordinator: bool = False,
    tell: bool = True,
    expect: Callable[[CallContext], object] | None = None,
    now: datetime,
) -> tuple[Held, ...]:
    """**Assign roles**: switch the winning pick to another way of getting it (``mode``, for a
    place that allows more than one) and say who does its roles: ``member_id`` cooks or orders,
    and, for a pickup, ``pickup_id`` picks up (#178). Each is someone who's in, or the first
    volunteer when None; one person can take every role. Switching to delivery drops the pickup
    person; the orderer stays. Allowed to the coordinator, a guest coordinator too, and an admin
    (``check_can_choose_roles``), while the call is decided. It ends the coordinator's time to
    choose (``Event.roles_due_at``): the assignment is theirs now.

    Whoever is assigned is told, once, about all their roles. If they hadn't offered for a role,
    the message says the coordinator assigned it; they can't refuse in the app, but they can tell
    the coordinator. Whoever loses a role before the meal, and has none new, is told too. Returns
    the message to each person told, or held. Audited as ``event.roles_assigned``; logged as
    ``assign_roles``.

    From Slack's Adjust, ``as_coordinator`` allows only the coordinator (spec 5.7), ``expect``
    checks under the lock that the call is still as the modal showed it, and with ``tell`` False
    the messages to whoever is assigned are held, not sent, for ``adjust`` to send once a
    hand-off in the same save is done."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.MANAGE_DECISION)
        check_run(event, run)
        check_can_choose_roles(event, actor)
        if as_coordinator:
            check_can_adjust(event, actor)
        ctx = await cc.load(db, event)
        if expect is not None:
            expect(ctx)
        item = ctx.items.get(event.decided_item_id) if event.decided_item_id is not None else None
        winner, current = ctx.winner, event.decided_mode
        if item is None or winner is None or current is None:  # pragma: no cover - a decided call has all three
            raise EventRuleViolation("There's no decision to change.", code="not_decided")
        chosen_mode = mode or current
        check_mode(item, chosen_mode)
        roles = _roles(ctx, item, chosen_mode, member_id, pickup_id)
        targets = {role: check_assignee(ctx, item, chosen_mode, who) for role, who in roles.items()}
        offered = {role: ctx.coverage.volunteers(chosen_mode, role, item.cook_ids) for role in roles}
        before = {a.role: a for a in ctx.assignments if a.role in WORK_ROLES}
        assignments = AssignmentRepository(db)
        if chosen_mode is not current:
            await ProposalRepository(db).set_mode(actor.household_id, winner.id, chosen_mode)
        for role in before:
            if role not in roles:  # a role the new way doesn't have, such as pickup for delivery
                await assignments.unassign(actor.household_id, event.id, role)
        was_pending = event.roles_due_at is not None
        event = await EventRepository(db).save(replace(event, decided_mode=chosen_mode, roles_due_at=None), now)
        made: dict[int, int] = {}  # each person told, keyed to their first new assignment
        for role, who in roles.items():
            had = before.get(role)
            if had is None or had.member_id != who:
                made.setdefault(who, (await assignments.assign(actor.household_id, event.id, role, who, now)).id)
            elif was_pending:  # the automatic choice stands; they hadn't been told yet
                made.setdefault(who, had.id)
        lost: dict[int, list[AssignmentRole]] = {}
        for role, had in before.items():
            if roles.get(role) != had.member_id:
                lost.setdefault(had.member_id, []).append(role)
        ctx = await cc.load(db, event)
        decision = cc.decision_info(ctx)
        sent: list[note.Notification] = [*cc.decision_updated(ctx), cc.message_updated(ctx)]
        held: list[Held] = []
        ahead = cc.before_the_meal(event, now)  # a correction afterward tells nobody (#154)
        if ahead and not was_pending:
            for gone, gone_roles in lost.items():
                if gone not in made:  # told their new roles instead, such as a cook who now orders
                    sent += cc.assignment_ended(ctx, gone, tuple(gone_roles), actor, now)
        if decision is not None and ahead:
            for who, assignment_id in made.items():
                theirs = [role for role, person in roles.items() if person == who]
                unasked = any(who not in offered[role] for role in theirs)
                by = actor.id if unasked and who != actor.id else None
                if tell:
                    sent.append(
                        cc.assignment_made(
                            ctx, who, assignment_id, decision, ctx.person(by) if by is not None else None
                        )
                    )
                held.append(Held(who, assignment_id, by))
        await publish(db, ctx, *sent, now=now)
        lead_role = LEAD_ROLE[chosen_mode]
        lead = roles[lead_role]
        payload = {
            "role": lead_role.value,
            "member_id": lead,
            "mode": chosen_mode.value,
            "switched_from": current.value if chosen_mode is not current else None,
            "volunteered": lead in offered[lead_role],
            "assignments": {role.value: who for role, who in roles.items()},
            "dropped": [role.value for role in before if role not in roles],
        }
        await audit(db, actor.household_id, actor, "event.roles_assigned", _target(event.id), payload, now)
        pickup = roles.get(AssignmentRole.PICKUP)
        activity.action(
            db,
            "assign_roles",
            event,
            actor,
            role=lead_role.value,
            to_member_id=lead,
            to_member=targets[lead_role].display_name,
            pickup_member_id=pickup,
            switched=chosen_mode is not current,
            volunteered=lead in offered[lead_role],
            item=item,
            item_id=item.id,
            mode=chosen_mode,
        )
        return tuple(held)


# Adjust the plan in Slack (spec 5.7) ------------------------------------------------------------

PLAN_CHANGED = "The plan changed since you opened this. Open Adjust again."


@dataclass(frozen=True, slots=True)
class Adjustment:
    """The coordinator's **Adjust** modal: the meal time (a wall time on the call's date), who
    cooks or orders the decided pick (``assignee_id``), who coordinates, the way of getting it
    (``mode``: cook, pickup, or delivery), and who picks it up (``pickup_id``, #178). As asked
    for, None leaves that part alone; as opened, it's what the modal showed, with ``mode`` None
    for a modal opened before Adjust could switch it, and ``pickup_id`` None for one opened
    before it showed the pickup person."""

    meal_time: time | None = None
    assignee_id: int | None = None
    coordinator_id: int | None = None
    mode: Mode | None = None
    pickup_id: int | None = None

    @property
    def empty(self) -> bool:
        return (
            self.meal_time is None
            and self.assignee_id is None
            and self.coordinator_id is None
            and self.mode is None
            and self.pickup_id is None
        )


class AdjustmentRefused(EventRuleViolation):
    """One part of an **Adjust** the rules refuse, so the modal can show why next to it. ``part``
    is ``meal_time``, ``mode``, ``assignee``, ``pickup``, or ``coordinator``."""

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


def _changed_since(part: str) -> AdjustmentRefused:
    return AdjustmentRefused(part, EventRuleViolation(PLAN_CHANGED, code="plan_changed"))


def _asked(part: str, wanted: object, opened: object, current: object) -> bool:
    """Whether the person changed ``part`` in the modal: it differs from what the modal opened
    with. A part changed elsewhere since is refused, so the modal never writes back over it."""
    if wanted is None or wanted == opened:
        return False
    if current != opened:
        raise _changed_since(part)
    return True


def _meal_now(ctx: CallContext) -> time | None:
    return _wall(ctx, ctx.event.meal_time) if ctx.event.meal_time is not None else None


def _role_now(ctx: CallContext) -> tuple[Mode | None, int | None, int | None]:
    """The decided pick's way of getting it, who cooks or orders it, and who picks it up."""
    mode = ctx.event.decided_mode
    holders = {a.role: a.member_id for a in ctx.assignments}
    lead = holders.get(LEAD_ROLE[mode]) if mode is not None else None
    pickup = holders.get(AssignmentRole.PICKUP) if mode is Mode.PICKUP else None
    return mode, lead, pickup


def _role_as_opened(ctx: CallContext, opened: Adjustment) -> bool:
    mode, lead, pickup = _role_now(ctx)
    return (opened.mode is None or mode == opened.mode) and lead == opened.assignee_id and pickup == opened.pickup_id


def _role_part(ctx: CallContext, opened: Adjustment) -> str:
    """Which field of the roles changed since the modal opened: the way, who cooks or orders, or
    who picks up (#178)."""
    mode, lead, _ = _role_now(ctx)
    if opened.mode is not None and mode != opened.mode:
        return "mode"
    return "assignee" if lead != opened.assignee_id else "pickup"


def _still(opened: Adjustment, part: str) -> Callable[[CallContext], None]:
    """For each locked step of ``adjust``: the part it saves is still as the modal opened with,
    so a change committed since ``plan_adjustment`` read the call is refused, not overwritten."""

    def check(ctx: CallContext) -> None:
        same = {
            "meal_time": lambda: _meal_now(ctx) == opened.meal_time,
            "role": lambda: _role_as_opened(ctx, opened),
            "coordinator": lambda: ctx.event.coordinator_id == opened.coordinator_id,
        }[part]()
        if not same:
            raise _changed_since(_role_part(ctx, opened) if part == "role" else part)

    return check


async def plan_adjustment(
    db: AsyncSession, actor: Member, event_id: int, wanted: Adjustment, *, opened: Adjustment, run: int
) -> Adjustment:
    """What ``adjust`` would change: each part of ``wanted`` that differs from what the modal
    opened with (``opened``), checked with the rules the use cases apply. It only reads, so the
    Slack modal can ask before it closes and show a refusal next to its field
    (``AdjustmentRefused``). A refusal of the whole thing, such as from someone who isn't the
    coordinator, is an ``EventRuleViolation``.

    The way of getting it, who cooks or orders, and who picks up are one part, the roles:
    changing any saves them all, as **Assign roles** does, and each person must be able to do
    their role that way (FR-17, as ``choose_roles`` checks). A role nobody was picked for goes to
    its first volunteer (#178)."""
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
    current = event.decided_mode
    if item is None or current is None or event.meal_time is None:  # pragma: no cover - a decided call has all three
        raise EventRuleViolation("There's no decision to change.", code="not_decided")
    meal = wanted.meal_time
    if _asked("meal_time", meal, opened.meal_time, _meal_now(ctx)) and meal is not None:
        at: time = meal
        _part("meal_time", lambda: meal_at(ctx, at))
    else:
        meal = None
    mode_asked = wanted.mode is not None and opened.mode is not None and wanted.mode != opened.mode
    who_asked = wanted.assignee_id is not None and wanted.assignee_id != opened.assignee_id
    pickup_asked = wanted.pickup_id is not None and wanted.pickup_id != opened.pickup_id
    mode: Mode | None = None
    assignee: int | None = None
    pickup: int | None = None
    if mode_asked or who_asked or pickup_asked:
        if not _role_as_opened(ctx, opened):
            raise _changed_since("mode" if mode_asked else "assignee" if who_asked else "pickup")
        chosen: Mode = wanted.mode if mode_asked and wanted.mode is not None else current
        _part("mode", lambda: check_mode(item, chosen))
        try:
            roles = _roles(ctx, item, chosen, wanted.assignee_id, wanted.pickup_id)
        except EventRuleViolation as exc:
            missing = AssignmentRole.PICKUP if chosen is Mode.PICKUP and wanted.pickup_id is None else None
            raise AdjustmentRefused("pickup" if missing else "assignee", exc) from exc
        for role, who in roles.items():
            try:
                check_assignee(ctx, item, chosen, who)
            except (EventRuleViolation, NotFound) as exc:
                raise AdjustmentRefused("pickup" if role is AssignmentRole.PICKUP else "assignee", exc) from exc
        assignee, pickup = roles[LEAD_ROLE[chosen]], roles.get(AssignmentRole.PICKUP)
        mode = chosen if mode_asked else None
    lead = wanted.coordinator_id
    if _asked("coordinator", lead, opened.coordinator_id, event.coordinator_id) and lead is not None:
        await guest_rules.gate(db, event, actor, GuestAction.HAND_OFF)
        to: int = lead
        _part("coordinator", lambda: handoff_target(ctx, actor, to))
    else:
        lead = None
    return Adjustment(meal, assignee, lead, mode, pickup)


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
    """**Adjust** in Slack (spec 5.7): the coordinator changes the meal time, the way of getting
    it and who does its roles, and who coordinates, in one modal. Only the parts they changed
    from what the modal opened with change, each through the use case the web uses for it,
    which checks its rules again under the event's lock, the coordinator's say included, and
    is audited: ``calls.set_meal_time``, ``choose_roles``, then ``calls.hand_off``, last, since
    handing off ends the actor's say over the rest. Each step also checks, under its lock, that
    its part is still as the modal opened with (``_still``), so a change saved elsewhere since
    ``plan_adjustment`` read the call is refused rather than written over. Returns what changed;
    an empty ``Adjustment`` changed nothing and sent nothing. A part refused after an earlier
    one was saved raises ``PartlyAdjusted``.

    With a hand-off in the same save, the message to whoever gets a role waits until after it,
    so it names the new coordinator; when that's the same person, the hand-off's own message
    lists their roles, so there's only one."""
    changes = await plan_adjustment(db, actor, event_id, wanted, opened=opened, run=run)
    saved = Adjustment()
    held: tuple[Held, ...] = ()
    try:
        if changes.meal_time is not None:
            await set_meal_time(
                db,
                actor,
                event_id,
                changes.meal_time,
                run=run,
                as_coordinator=True,
                expect=_still(opened, "meal_time"),
                now=now,
            )
            saved = replace(saved, meal_time=changes.meal_time)
        if changes.assignee_id is not None:
            tell = changes.coordinator_id is None
            chosen = await choose_roles(
                db,
                actor,
                event_id,
                mode=changes.mode,
                member_id=changes.assignee_id,
                pickup_id=changes.pickup_id,
                run=run,
                as_coordinator=True,
                tell=tell,
                expect=_still(opened, "role"),
                now=now,
            )
            held = () if tell else chosen
            saved = replace(saved, assignee_id=changes.assignee_id, mode=changes.mode, pickup_id=changes.pickup_id)
        if changes.coordinator_id is not None:
            await hand_off(
                db, actor, event_id, changes.coordinator_id, run=run, expect=_still(opened, "coordinator"), now=now
            )
            saved = replace(saved, coordinator_id=changes.coordinator_id)
    except (EventRuleViolation, NotFound, IllegalTransition) as exc:
        if saved.empty:
            raise
        raise PartlyAdjusted(saved, exc) from exc
    finally:
        for message in held:
            if message.member_id != saved.coordinator_id:
                await _tell_assigned(db, actor, event_id, message, now=now)
    return saved


async def _tell_assigned(db: AsyncSession, actor: Member, event_id: int, held: Held, *, now: datetime) -> None:
    """Send the message ``choose_roles`` held, naming the call as it is now."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        ctx = await cc.load(db, event)
        decision = cc.decision_info(ctx)
        if event.state is not EventState.DECIDED or decision is None or not cc.before_the_meal(event, now):
            return
        by = ctx.person(held.by_id) if held.by_id is not None else None
        await publish(db, ctx, cc.assignment_made(ctx, held.member_id, held.assignment_id, decision, by), now=now)
