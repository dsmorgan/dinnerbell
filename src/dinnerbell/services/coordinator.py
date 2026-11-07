# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the coordinator can do that others can't (FR-16, FR-61, #87): break a tie, shuffle the
picks, move the decision time (#198), and choose the winning pick's mode and who does its role;
change the place after the decision (#219), which an admin alone can correct after the meal is
done (#220); and, in Slack, adjust the plan once it's decided (spec 5.7).

Each is the coordinator's and every admin's alike (#179). A guest coordinator chooses roles,
which is logistics, but not the plan (FR-58). Each takes the event's lock, checks the run the
person saw, and is audited and logged (#75). Everything is scoped to the actor's household.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.events import AssignmentRepository, EventRepository, ProposalRepository
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import (
    MODE_ORDER,
    CatalogItem,
    InvalidCatalogInput,
    Mode,
    TagKind,
    kind_for,
    normalize_item_name,
)
from dinnerbell.domain.events import (
    AFTER_DECISION_RANK,
    LEAD_ROLE,
    MODE_ROLES,
    WORK_ROLES,
    ActingAs,
    Assignment,
    AssignmentRole,
    Event,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    Proposal,
    ProposalSource,
    Window,
    can_do,
    check_can_adjust,
    check_can_break_tie,
    check_can_change_place,
    check_can_choose_roles,
    check_can_correct_place,
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
from dinnerbell.domain.meals import MealLog
from dinnerbell.domain.members import Member
from dinnerbell.domain.ranking import rank
from dinnerbell.services import activity, contributions, lifecycle, picks
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
    was: str | None = None
    """The old place, when a change of place gave them the role (#219)."""


def _check_in(ctx: CallContext, who: int) -> Member:
    """``who`` is an active member or guest who's in."""
    target = ctx.members.get(who)
    if target is None or not target.active:
        raise NotFound("No such member.")
    response = ctx.response_of(who)
    if response is None or not response.is_in:
        raise EventRuleViolation("Pick someone who's in.", code="not_in")
    return target


def check_assignee(ctx: CallContext, item: CatalogItem, mode: Mode, who: int) -> Member:
    """Who may do the decided pick's role: an active member or guest who's in, and on the cook
    list of a home meal that has one."""
    target = _check_in(ctx, who)
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


async def _save_roles(
    db: AsyncSession,
    event: Event,
    before: dict[AssignmentRole, Assignment],
    roles: dict[AssignmentRole, int],
    now: datetime,
) -> tuple[dict[int, int], dict[int, list[AssignmentRole]]]:
    """Make ``roles`` who does what: a role the way doesn't have goes, and a new holder is
    assigned. ``before`` is who did what, and ``event`` is the call as it was found, for whether
    the coordinator was still choosing. Returns each person to tell, keyed to their first new
    assignment, and what each person lost."""
    assignments = AssignmentRepository(db)
    for role in before:
        if role not in roles:  # a role the new way doesn't have, such as pickup for delivery
            await assignments.unassign(event.household_id, event.id, role)
    made: dict[int, int] = {}
    for role, who in roles.items():
        had = before.get(role)
        if had is None or had.member_id != who:
            made.setdefault(who, (await assignments.assign(event.household_id, event.id, role, who, now)).id)
        elif event.roles_due_at is not None:  # the automatic choice stands; they hadn't been told yet
            made.setdefault(who, had.id)
    lost: dict[int, list[AssignmentRole]] = {}
    for role, had in before.items():
        if roles.get(role) != had.member_id:
            lost.setdefault(had.member_id, []).append(role)
    return made, lost


def _role_messages(
    ctx: CallContext,
    actor: Member,
    roles: dict[AssignmentRole, int],
    offered: dict[AssignmentRole, tuple[int, ...]],
    made: dict[int, int],
    lost: dict[int, list[AssignmentRole]],
    *,
    was_pending: bool,
    tell: bool,
    was: str | None = None,
    now: datetime,
) -> tuple[list[note.Notification], list[Held]]:
    """The messages to whoever gained or lost a role, before the meal: a correction afterward
    tells nobody (#154). Whoever is assigned hears once, about all their roles, and with ``tell``
    False that message is only held. ``was`` is the old place, for a change of place (#219).
    Returns what to send, and each person told or held."""
    decision = cc.decision_info(ctx)
    sent: list[note.Notification] = []
    held: list[Held] = []
    ahead = cc.before_the_meal(ctx.event, now)
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
                person = ctx.person(by) if by is not None else None
                sent.append(cc.assignment_made(ctx, who, assignment_id, decision, person, was=was))
            held.append(Held(who, assignment_id, by, was))
    return sent, held


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
        if chosen_mode is not current:
            await ProposalRepository(db).set_mode(actor.household_id, winner.id, chosen_mode)
        was_pending = event.roles_due_at is not None
        made, lost = await _save_roles(db, event, before, roles, now)
        event = await EventRepository(db).save(replace(event, decided_mode=chosen_mode, roles_due_at=None), now)
        ctx = await cc.load(db, event)
        told, held = _role_messages(ctx, actor, roles, offered, made, lost, was_pending=was_pending, tell=tell, now=now)
        await publish(db, ctx, *cc.decision_updated(ctx), cc.message_updated(ctx), *told, now=now)
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

PLAN_CHANGED = "The plan changed since you opened this. Open it again."


def still_showing(item_id: int | None, mode: Mode | None) -> Callable[[CallContext], None]:
    """For **Assign roles**, under the lock: the call still has the place and way the modal or
    page showed (#219), so roles chosen for one place never land on another. A modal or page
    drawn before it carried them says None, and isn't held to them."""

    def check(ctx: CallContext) -> None:
        event = ctx.event
        if (item_id is not None and event.decided_item_id != item_id) or (
            mode is not None and event.decided_mode is not mode
        ):
            raise EventRuleViolation(PLAN_CHANGED, code="plan_changed")

    return check


@dataclass(frozen=True, slots=True)
class Adjustment:
    """The coordinator's **Adjust** modal: the meal time (a wall time on the call's date), who
    cooks or orders the decided pick (``assignee_id``), who coordinates, the way of getting it
    (``mode``: cook, pickup, or delivery), who picks it up (``pickup_id``, #178), and the place
    (#219): ``item_id`` from the catalog, or ``new_place`` typed in, with the way to get it there
    (``place_mode``) and a new name's cuisine (``cuisine_id``). As asked for, None leaves that
    part alone; as opened, it's what the modal showed, with ``mode`` None for a modal opened
    before Adjust could switch it, ``pickup_id`` None for one opened before it showed the pickup
    person, and ``item_id`` None for one opened before it showed the place."""

    meal_time: time | None = None
    assignee_id: int | None = None
    coordinator_id: int | None = None
    mode: Mode | None = None
    pickup_id: int | None = None
    item_id: int | None = None
    new_place: str | None = None
    place_mode: Mode | None = None
    cuisine_id: int | None = None

    @property
    def place_asked(self) -> bool:
        """Whether this names a place: ``place_mode`` and ``cuisine_id`` say nothing without one."""
        return self.item_id is not None or self.new_place is not None

    @property
    def empty(self) -> bool:
        return (
            self.meal_time is None
            and self.assignee_id is None
            and self.coordinator_id is None
            and self.mode is None
            and self.pickup_id is None
            and not self.place_asked
        )


class AdjustmentRefused(EventRuleViolation):
    """One part of an **Adjust** the rules refuse, so the modal can show why next to it. ``part``
    is ``meal_time``, ``place``, ``new_place``, ``place_mode``, ``place_cuisine``, ``mode``,
    ``assignee``, ``pickup``, or ``coordinator``."""

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
    same_place = opened.item_id is None or ctx.event.decided_item_id == opened.item_id
    same_way = opened.mode is None or mode == opened.mode
    return same_place and same_way and lead == opened.assignee_id and pickup == opened.pickup_id


def _role_part(ctx: CallContext, opened: Adjustment) -> str:
    """Which field of the roles changed since the modal opened: the place they apply to (#219),
    the way, who cooks or orders, or who picks up (#178)."""
    mode, lead, _ = _role_now(ctx)
    if opened.item_id is not None and ctx.event.decided_item_id != opened.item_id:
        return "place"
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
    its first volunteer (#178). A new place takes all of them with it, in ``change_place``, so the
    way and the roles apply to it (#219): those it would keep or fill aren't in what this returns,
    only whoever the person named."""
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
    if wanted.new_place is not None or wanted.item_id not in {None, opened.item_id}:
        await guest_rules.gate(db, event, actor, GuestAction.CHANGE_PLACE)
    place = await _place_asked(
        db,
        ctx,
        item_id=wanted.item_id,
        name=wanted.new_place,
        mode=wanted.place_mode,
        how=wanted.mode if mode_asked else None,
        opened_item_id=opened.item_id,
        current_item_id=ctx.event.decided_item_id,
    )
    if place is not None:
        if (who_asked or pickup_asked) and not _role_as_opened(ctx, opened):
            raise _changed_since("assignee" if who_asked else "pickup")
        assignee = wanted.assignee_id if who_asked else None
        pickup = wanted.pickup_id if pickup_asked and place.mode is Mode.PICKUP else None
        _check_cuisine(ctx, place, wanted.cuisine_id)
        _check_named(ctx, place, assignee, pickup)
    elif mode_asked or who_asked or pickup_asked:
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
    if place is None:
        return Adjustment(meal, assignee, lead, mode, pickup)
    return Adjustment(
        meal,
        assignee,
        lead,
        None,
        pickup,
        item_id=place.item.id if place.name is None and place.item is not None else None,
        new_place=place.name,
        place_mode=place.mode,
        cuisine_id=wanted.cuisine_id if place.item is None else None,
    )


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
    """**Adjust** in Slack (spec 5.7): the coordinator changes the meal time, the place, the way
    of getting it and who does its roles, and who coordinates, in one modal. Only the parts they
    changed from what the modal opened with change, each through the use case the web uses for
    it, which checks its rules again under the event's lock, the coordinator's say included, and
    is audited: ``calls.set_meal_time``; ``change_place``, which saves the way and the roles with
    it, since they apply to the new place, or else ``choose_roles``; then ``calls.hand_off``,
    last, since handing off ends the actor's say over the rest. Each step also checks, under its
    lock, that its part is still as the modal opened with (``_still``, and ``_place_asked`` for the
    place), so a change saved elsewhere since
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
        if changes.place_asked:
            named = changes.assignee_id is not None or changes.pickup_id is not None
            chosen = await change_place(
                db,
                actor,
                event_id,
                item_id=changes.item_id,
                name=changes.new_place,
                cuisine_id=changes.cuisine_id,
                mode=changes.place_mode,
                opened_item_id=opened.item_id,
                member_id=changes.assignee_id,
                pickup_id=changes.pickup_id,
                run=run,
                as_coordinator=True,
                handing_to=changes.coordinator_id,
                expect=_still(opened, "role") if named else None,
                now=now,
            )
            held = () if changes.coordinator_id is None else chosen
            saved = replace(
                saved,
                item_id=changes.item_id,
                new_place=changes.new_place,
                place_mode=changes.place_mode,
                cuisine_id=changes.cuisine_id,
                assignee_id=changes.assignee_id,
                pickup_id=changes.pickup_id,
            )
        elif changes.assignee_id is not None:
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
        told = cc.assignment_made(ctx, held.member_id, held.assignment_id, decision, by, was=held.was)
        await publish(db, ctx, told, now=now)


# Change the place after the decision (#219, spec 5.7) -------------------------------------------

SAME_PLACE = "That's the place already. Pick another one, or type a new name."
NOT_IN_CATALOG = "That place isn't in the catalog anymore. Pick another one, or type its name."
PICK_THE_WAY = "Pick how to get it."
NOT_A_CUISINE = "Pick a cuisine from the list, or leave it empty."
NO_MEAL_LOGGED = "This meal is no longer in History, so there's nothing to correct."
PLACE_OR_NAME = "Pick a place, or type a new one, but not both. To keep what you typed, pick {current}."


@dataclass(frozen=True, slots=True)
class _Place:
    """The place someone asked for, and the way to get it: a catalog item, or, for a name the
    catalog doesn't have yet, ``item`` None and ``name``."""

    item: CatalogItem | None
    name: str | None
    mode: Mode


def _way_there(item: CatalogItem, how: Mode | None, current: Mode | None) -> Mode:
    """The way to get ``item`` when nobody picked one: the only one it has, or else ``how`` (the
    way picked for the plan as it was), or the way the call has now, if it allows those."""
    ways = [m for m in MODE_ORDER if m in item.modes]
    if len(ways) == 1:
        return ways[0]
    for way in (how, current):
        if way in ways and way is not None:
            return way
    raise AdjustmentRefused("place_mode", EventRuleViolation(PICK_THE_WAY, code="no_way_chosen"))


async def _place_asked(
    db: AsyncSession,
    ctx: CallContext,
    *,
    item_id: int | None,
    name: str | None,
    mode: Mode | None,
    how: Mode | None = None,
    opened_item_id: int | None,
    current_item_id: int | None,
) -> _Place | None:
    """The place asked for, or None when it's the place shown and ``current_item_id``, the place
    as it stands: the call's, or on a correction, the meal's. A place changed elsewhere since it
    was shown is refused, so this never writes back over it. The change records what happened, so
    only the way is checked (``check_mode``): vetoes, dietary needs, the variety window, the meal,
    and who offered don't count here. A new name needs its way, since the way says whether it's a
    home meal or a restaurant."""
    event = ctx.event
    picked = item_id if item_id != opened_item_id else None
    if picked is None and name is None:
        return None
    if picked is not None and name is not None:
        current = ctx.items[current_item_id].name if current_item_id in ctx.items else "the place shown"
        both = EventRuleViolation(PLACE_OR_NAME.format(current=current), code="place_and_name")
        raise AdjustmentRefused("new_place", both)
    if current_item_id != opened_item_id:
        raise _changed_since("place" if picked is not None else "new_place")
    if picked is not None:
        item = ctx.items.get(picked)
        if item is None or not item.active:
            raise AdjustmentRefused("place", NotFound(NOT_IN_CATALOG))
        way = mode or _way_there(item, how, event.decided_mode)
        _part("place_mode", lambda: check_mode(item, way))
        return _Place(item, None, way)
    try:
        clean = normalize_item_name(name or "")
    except InvalidCatalogInput as exc:
        raise AdjustmentRefused("new_place", EventRuleViolation(str(exc), code="invalid_name")) from None
    if mode is None:
        raise AdjustmentRefused("place_mode", EventRuleViolation(PICK_THE_WAY, code="no_way_chosen"))
    found = await CatalogRepository(db).item_by_name(event.household_id, kind_for(mode), clean)
    if found is not None and found.id == current_item_id:
        return None
    if found is not None:
        _part("place_mode", lambda: check_mode(found, mode))
    return _Place(found, clean, mode)


def _check_cuisine(ctx: CallContext, place: _Place, cuisine_id: int | None) -> int | None:
    """The cuisine to give a new name, which is optional: the change records what happened, and
    an admin can add it in the catalog. None for a place the catalog has."""
    if place.item is not None or cuisine_id is None:
        return None
    tag = ctx.tags.get(cuisine_id)
    if tag is None or tag.kind is not TagKind.CUISINE:
        raise AdjustmentRefused("place_cuisine", EventRuleViolation(NOT_A_CUISINE, code="no_cuisine"))
    return cuisine_id


def _check_named(ctx: CallContext, place: _Place, lead: int | None, pickup: int | None) -> None:
    """Whoever the change names for a role is someone who can do it (FR-17), as in **Assign
    roles**. A person who just keeps their role isn't checked again: see ``_roles_there``."""
    item = place.item

    def check(who: int) -> None:
        if item is None:  # something new has no cook list
            _check_in(ctx, who)
        else:
            check_assignee(ctx, item, place.mode, who)

    for part, who in (("assignee", lead), ("pickup", pickup)):
        if who is not None:
            _part(part, functools.partial(check, who))


def _roles_there(ctx: CallContext, place: _Place, lead: int | None, pickup: int | None) -> dict[AssignmentRole, int]:
    """Who does what at the new place. People keep their roles if the new way still has them, and
    a role the new way has that nobody has yet goes to its first volunteer, or to nobody: the
    change records what happened, so it isn't refused for that, and it isn't refused for a cook
    off the new place's cook list. They may well have cooked it, and the coordinator can say
    otherwise. ``lead`` and ``pickup`` are whoever the change names instead."""
    holders = {a.role: a.member_id for a in ctx.assignments}
    keeps_pickup = pickup if pickup is not None else holders.get(AssignmentRole.PICKUP)
    return default_roles(
        place.mode,
        ctx.coverage,
        place.item.cook_ids if place.item is not None else frozenset(),
        lead=lead if lead is not None else holders.get(LEAD_ROLE[place.mode]),
        pickup=keeps_pickup if place.mode is Mode.PICKUP else None,
    )


async def _make_it_the_winner(
    db: AsyncSession, ctx: CallContext, place: _Place, cuisine_id: int | None, now: datetime
) -> tuple[CatalogItem, bool]:
    """Give the new place the proposal row that ``CallContext.winner`` finds, and return its
    catalog item and whether it's new to the catalog.

    A place that was a pick keeps its row, its rank, and its votes: it's chosen now, in this way,
    and whoever added it did suggest where they ate. Any other place gets a row outside the picks
    (``AFTER_DECISION_RANK``) that names nobody, so it earns nobody credit for suggesting it, or
    for adding it to the catalog. A change of place from such a row removes it, so a call has at
    most one, and the free-text path is the one a pick that wins takes. A correction can name the
    place the call already has, in another way (#224), and then the row stays."""
    household_id = ctx.event.household_id
    proposals = ProposalRepository(db)
    target = Proposal(
        0,
        ctx.event.id,
        place.mode,
        AFTER_DECISION_RANK,
        ProposalSource.COORDINATOR,
        item_id=place.item.id if place.item is not None else None,
        free_text=place.name if place.item is None else None,
        free_cuisine_id=cuisine_id,
        mode_chosen=True,
    )
    existing = picks.picked(ctx, target)
    winner = ctx.winner
    if winner is not None and winner.after_decision and (existing is None or existing.id != winner.id):
        await proposals.delete(household_id, winner.id)
    if existing is None:
        row = await proposals.add(
            household_id=household_id,
            event_id=target.event_id,
            mode=target.mode,
            rank=target.rank,
            source=target.source,
            reasons=(),
            now=now,
            item_id=target.item_id,
            free_text=target.free_text,
            free_cuisine_id=target.free_cuisine_id,
            mode_chosen=True,
        )
        if place.item is not None:
            return place.item, False
        return await lifecycle.catalog_free_text(db, ctx, row, now), True
    if place.item is None:  # a free-text pick, which becomes a catalog item as a winning one does
        item = await lifecycle.catalog_free_text(db, ctx, replace(existing, mode=place.mode), now)
        created = True
    else:
        item, created = place.item, False
        if existing.item_id is None:
            await proposals.link_item(household_id, existing.id, item.id)
    await proposals.set_mode(household_id, existing.id, place.mode)
    return item, created


async def change_place(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    item_id: int | None = None,
    name: str | None = None,
    cuisine_id: int | None = None,
    mode: Mode | None = None,
    opened_item_id: int | None,
    member_id: int | None = None,
    pickup_id: int | None = None,
    run: int | None = None,
    as_coordinator: bool = False,
    handing_to: int | None = None,
    expect: Callable[[CallContext], object] | None = None,
    now: datetime,
) -> tuple[Held, ...]:
    """Change where the call is eating, after the decision and until it's done (#219, spec 5.7):
    the coordinator or an admin, never a guest (``check_can_change_place``). The place
    is ``item_id``, an active catalog item, or ``name``, typed in, with an optional cuisine, and
    ``mode`` is a way it allows. ``opened_item_id`` is the place the person saw; one changed
    elsewhere since is refused.

    It records what happened, so nothing but the way can refuse it (``_place_asked``). People keep
    their roles if the way still has them (``_roles_there``); ``member_id`` and ``pickup_id`` name
    someone else, who must be able to do it. While the coordinator is still choosing roles, naming
    nobody leaves that as it is: the time runs on, for the new place. How the old place won, a
    lock or a tie, no longer describes the plan, so the decision stops saying it.

    The call's messages update in place. Before the meal, each person who's in gets one message
    (``cc.place_changed``): whoever gains a role hears in ``AssignmentMade``, which names the place
    and the old one, and whoever loses one hears it in ``PlaceChanged``. From the meal time on it
    corrects the record and messages nobody (#154), but a hand-off in the same save still tells
    whoever takes over. Audited as ``event.place_changed``; logged as
    ``change_place``. ``as_coordinator`` and ``expect`` are for Slack's Adjust, as in
    ``choose_roles``. So is ``handing_to``, whom the same save hands coordinating to: the messages
    about new roles are held for ``adjust`` to send after the hand-off, and the hand-off's own
    message lists that person's roles without the old place, so they still get ``PlaceChanged``."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.CHANGE_PLACE)
        check_run(event, run)
        capacity = check_can_change_place(event, actor)
        if as_coordinator:
            check_can_adjust(event, actor)
        ctx = await cc.load(db, event)
        if expect is not None:
            expect(ctx)
        old, old_mode = ctx.winner, event.decided_mode
        if old is None or old_mode is None:  # pragma: no cover - a decided call has both
            raise EventRuleViolation("There's no decision to change.", code="not_decided")
        place = await _place_asked(
            db,
            ctx,
            item_id=item_id,
            name=name,
            mode=mode,
            opened_item_id=opened_item_id,
            current_item_id=event.decided_item_id,
        )
        if place is None:
            raise EventRuleViolation(SAME_PLACE, code="same_place")
        activity.about(item=place.item or place.name, mode=place.mode)
        new_cuisine = _check_cuisine(ctx, place, cuisine_id)
        pickup = pickup_id if place.mode is Mode.PICKUP else None
        _check_named(ctx, place, member_id, pickup)
        was_pending = event.roles_due_at is not None
        # Naming nobody while the coordinator chooses leaves the choice open; otherwise it ends.
        waits = was_pending and member_id is None and pickup is None
        roles = {} if waits else _roles_there(ctx, place, member_id, pickup)
        cooks = place.item.cook_ids if place.item is not None else frozenset()
        offered = {role: ctx.coverage.volunteers(place.mode, role, cooks) for role in roles}
        before = {} if waits else {a.role: a for a in ctx.assignments if a.role in WORK_ROLES}
        item, created = await _make_it_the_winner(db, ctx, place, new_cuisine, now)
        made, lost = await _save_roles(db, event, before, roles, now)
        event = await EventRepository(db).save(
            replace(
                event,
                decided_item_id=item.id,
                decided_mode=place.mode,
                roles_due_at=event.roles_due_at if waits else None,
                locked_by=None,
                tie_how=None,
                tie_by=None,
            ),
            now,
        )
        was = cc.item_info(ctx, old).name
        ctx = await cc.load(db, event)
        everyone: list[note.PlaceChanged] = []
        if cc.before_the_meal(event, now):
            has_message = set(made) - {handing_to}
            ended = {} if was_pending else lost  # they hadn't been told they had those
            everyone = cc.place_changed(ctx, was, actor, now, has_message=has_message, ended=ended)
        said = {message.member.id for message in everyone}
        # Whoever else lost a role hears as usual, such as someone who isn't in, unless the message
        # would go through the person who just made the change.
        hosted = {who for who in lost if (via := cc.answering_for(ctx, who)) is not None and via.id == actor.id}
        left = {who: gone for who, gone in lost.items() if who not in said | hosted}
        told, held = _role_messages(
            ctx,
            actor,
            roles,
            offered,
            made,
            left,
            was_pending=was_pending,
            tell=handing_to is None,
            was=was,
            now=now,
        )
        await publish(db, ctx, *cc.messages_for_state(ctx), *everyone, *told, now=now)
        dropped = [role.value for role in before if role not in roles]
        payload = {
            "from_item_id": old.item_id,
            "from_mode": old_mode.value,
            "to_item_id": item.id,
            "to_mode": place.mode.value,
            "new_item": created,
            "assignments": {role.value: who for role, who in roles.items()},
            "dropped": dropped,
            "as": capacity.value,
        }
        await audit(db, actor.household_id, actor, "event.place_changed", _target(event.id), payload, now)
        activity.action(
            db,
            "change_place",
            event,
            actor,
            item=item,
            item_id=item.id,
            mode=place.mode,
            from_item_id=old.item_id,
            from_mode=old_mode.value,
            new_item=created,
            dropped=dropped or None,
            capacity=capacity.value,
        )
        return tuple(held)


def _into_line(ctx: CallContext, meal: MealLog, mode: Mode | None, opened_item_id: int | None) -> _Place:
    """A correction that asks for the place the meal already says. That's refused, unless an earlier
    History edit left the call on another place (#224): then it brings the call into line, in a way
    the meal's place allows. It may be out of the catalog by now: the meal already says it."""
    event = ctx.event
    if event.decided_item_id == meal.item_id:
        raise EventRuleViolation(SAME_PLACE, code="same_place")
    if meal.item_id != opened_item_id:
        raise _changed_since("place")
    item = ctx.items[meal.item_id]
    way = mode or _way_there(item, None, event.decided_mode)
    _part("place_mode", lambda: check_mode(item, way))
    return _Place(item, None, way)


async def correct_place(
    db: AsyncSession,
    actor: Member,
    event_id: int,
    *,
    item_id: int | None = None,
    name: str | None = None,
    cuisine_id: int | None = None,
    mode: Mode | None = None,
    opened_item_id: int | None,
    run: int | None = None,
    now: datetime,
) -> None:
    """Correct where a done call ate (#220, spec 5.7): an admin only, on the web, never an offer
    (``check_can_correct_place``). What it accepts is what ``change_place`` accepts, and it treats
    the proposal row and the roles the same way: nothing but the way can refuse the place, people
    keep their roles if the way still has them, and a new role goes to its first volunteer, or to
    nobody. ``opened_item_id`` and ``run`` are what the page showed; one changed since is refused.

    It works from what the meal says, since a History edit before #224 could change the meal and
    leave the call (and its counts) behind. Correcting to the place the call still has moves the
    meal back, and keeps the call's roles and how it won. Correcting to the place the meal already
    has moves the call to it, as any other correction would.

    It changes every stored copy of the place: the call, the meal log's item and cuisines, so
    history, stats, ranking, and the variety window read the new place, and the feedback on the
    meal, so ratings and issues count toward the new place (``FeedbackRepository.move_to``). The
    counts and badges follow (``contributions.recount_place``). A meal an admin deleted from History
    has nothing to correct, and is refused.

    The call's messages update in place, as other corrections do, and it messages nobody. Audited as
    ``event.place_changed`` with ``after_done``, and logged as ``change_place``, likewise."""
    async with locked_event(db, actor.household_id, event_id, now) as event:
        await guest_rules.gate(db, event, actor, GuestAction.CHANGE_PLACE)
        check_run(event, run)
        check_can_correct_place(event, actor)
        old_item, old_mode = event.decided_item_id, event.decided_mode
        if old_item is None or old_mode is None:  # pragma: no cover - a done call has both
            raise EventRuleViolation("There's no decision to change.", code="not_decided")
        household_id = event.household_id
        meals = MealLogRepository(db)
        meal_id = await meals.for_event(household_id, event.id)
        meal = await meals.get(household_id, meal_id) if meal_id is not None else None
        if meal is None:
            raise EventRuleViolation(NO_MEAL_LOGGED, code="no_meal")
        ctx = await cc.load(db, event)
        place = await _place_asked(
            db,
            ctx,
            item_id=item_id,
            name=name,
            mode=mode,
            opened_item_id=opened_item_id,
            current_item_id=meal.item_id,
        )
        if place is None:
            place = _into_line(ctx, meal, mode, opened_item_id)
        activity.about(item=place.item or place.name, mode=place.mode)
        new_cuisine = _check_cuisine(ctx, place, cuisine_id)
        before = {a.role: a for a in ctx.assignments if a.role in WORK_ROLES}
        # Only the meal was out of step: the call already has this place, in this way, so it stays as it is.
        as_it_was = (
            place.item if place.item is not None and (place.item.id, place.mode) == (old_item, old_mode) else None
        )
        if as_it_was is None:
            roles = _roles_there(ctx, place, None, None)
            item, created = await _make_it_the_winner(db, ctx, place, new_cuisine, now)
        else:
            roles = {role: a.member_id for role, a in before.items()}
            item, created = as_it_was, False
        await _save_roles(db, event, before, roles, now)
        corrected = replace(event, decided_item_id=item.id, decided_mode=place.mode)
        if item.id != old_item:  # how the old place won, a lock or a tie, no longer describes the plan
            corrected = replace(corrected, locked_by=None, tie_how=None, tie_by=None)
        event = await EventRepository(db).save(corrected, now)
        ctx = await cc.load(db, event)
        await meals.update(
            household_id,
            meal.id,
            day=meal.day,
            slot=meal.slot,
            item_id=item.id,
            cuisines=lifecycle.meal_cuisines(ctx, item),
            participants=meal.participants,
            now=now,
        )
        moved = await FeedbackRepository(db).move_to(
            household_id, meal.id, item_id=item.id, item_name=item.name, item_kind=item.kind, mode=place.mode
        )
        await publish(db, ctx, *cc.messages_for_state(ctx), now=now)
        dropped = [role.value for role in before if role not in roles]
        payload = {
            "from_item_id": old_item,
            "from_mode": old_mode.value,
            "to_item_id": item.id,
            "to_mode": place.mode.value,
            "new_item": created,
            "assignments": {role.value: who for role, who in roles.items()},
            "dropped": dropped,
            "feedback_moved": moved,
            "after_done": True,
            "as": ActingAs.ADMIN.value,
        }
        if meal.item_id != old_item:  # what the meal said, which an earlier History edit left apart from the call
            payload["from_meal_item_id"] = meal.item_id
        await audit(db, actor.household_id, actor, "event.place_changed", _target(event.id), payload, now)
        activity.action(
            db,
            "change_place",
            event,
            actor,
            item=item,
            item_id=item.id,
            mode=place.mode,
            from_item_id=old_item,
            from_mode=old_mode.value,
            new_item=created,
            dropped=dropped or None,
            feedback_moved=moved,
            after_done=True,
            capacity=ActingAs.ADMIN.value,
        )
        await contributions.recount_place(db, actor, event, now=now)
