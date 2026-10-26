# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dinner calls: the event state machine, responses, coverage, decisions, and meal time.

Spec sections 5.1 to 5.5, 6.1 to 6.4, and 8. Everything here is pure: callers pass the clock
reading and the household time zone in.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from dinnerbell.domain.catalog import CatalogItem, Mode
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Member

MAX_GUESTS = 20
MAX_NOTE_LENGTH = 200
MAX_SUGGESTION_LENGTH = 100
MAX_SUGGESTIONS = 3
MIN_HEAD_COUNT = 2
DONE_AFTER_MEAL = timedelta(hours=2)
EARLY_CLOSE_QUIET = timedelta(minutes=10)
"""How long responses must sit unchanged before a call with people in closes early: time to
add guests, an eat window, a suggestion, or an offer to cook after tapping In."""
MAX_DEADLINE_SHIFT_MINUTES = 240
AD_HOC_DAYS_AHEAD = 7
MAX_PROPOSALS = 10
"""Proposals per call, ranked and added together: enough choice, and a Slack message that fits."""
MAX_ADDED_PER_MEMBER = 3
CANCEL_NOTE_MAX = 140
"""A call-off reason: one line, short enough for a Slack notification preview."""
QUARTER_HOUR = timedelta(minutes=15)
LATEST_EAT_TIME = time(23, 45)
"""The latest eat time: later ones round the meal time up past midnight, off the call's day."""


class EventState(StrEnum):
    SCHEDULED = "scheduled"
    COLLECTING = "collecting"
    VOTING = "voting"
    DECIDED = "decided"
    DONE = "done"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"

    @property
    def is_open(self) -> bool:
        """Still in progress: not done, abandoned, or cancelled."""
        return self in OPEN_STATES

    @property
    def label(self) -> str:
        return STATE_LABELS[self]


OPEN_STATES = frozenset({EventState.SCHEDULED, EventState.COLLECTING, EventState.VOTING, EventState.DECIDED})
STATE_LABELS = {
    EventState.SCHEDULED: "Scheduled",
    EventState.COLLECTING: "Who's in?",
    EventState.VOTING: "Voting",
    EventState.DECIDED: "Decided",
    EventState.DONE: "Done",
    EventState.ABANDONED: "On your own",
    EventState.CANCELLED: "Cancelled",
}

TRANSITIONS: Mapping[EventState, frozenset[EventState]] = {
    # Scheduled to abandoned: its whole window passed while the server was down (missed).
    EventState.SCHEDULED: frozenset({EventState.COLLECTING, EventState.CANCELLED, EventState.ABANDONED}),
    # Collecting to decided: an offer is on once its time comes (FR-4); a call always votes first.
    EventState.COLLECTING: frozenset(
        {EventState.VOTING, EventState.DECIDED, EventState.ABANDONED, EventState.CANCELLED}
    ),
    # Back to collecting (today) or scheduled (a later day) is the admin's re-run (FR-6).
    EventState.VOTING: frozenset(
        {EventState.DECIDED, EventState.ABANDONED, EventState.CANCELLED, EventState.COLLECTING, EventState.SCHEDULED}
    ),
    EventState.DECIDED: frozenset({EventState.DONE, EventState.CANCELLED, EventState.COLLECTING, EventState.SCHEDULED}),
    # Re-runs: to collecting for today, to scheduled for a later day (FR-3 timing).
    EventState.ABANDONED: frozenset({EventState.COLLECTING, EventState.SCHEDULED}),
    EventState.CANCELLED: frozenset({EventState.COLLECTING, EventState.SCHEDULED}),
    EventState.DONE: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, current: EventState, target: EventState) -> None:
        super().__init__(f"An event can't go from {current.value} to {target.value}.")
        self.current = current
        self.target = target


class EventRuleViolation(Exception):
    """An action the call rules forbid right now. The message is written for the person who tried it."""


class DuplicatePick(EventRuleViolation):
    """A second pick for a place the call already has (#77). The use cases refuse it first,
    naming the pick; this is the backstop where every pick is written."""


CALL_FINISHED = "This call already finished."
STALE_CALL = "That can't happen anymore."
OFFER_JOIN_INSTEAD = "This is an offer: tap Count me in to join it."
OFFER_CHANGE = "To change an offer, call it off and post a new one."
NO_COORDINATOR = "An offer has no coordinator: whoever made it is in charge."
GUESTS_DONT = "Guests can answer and pitch in, but not that."
GUEST_NOT_INVITED = "You haven't been invited to this one. Ask a household member to invite you."
"""For a guest at a rule only members may use (#38). ``domain.guests`` has the full matrix."""
NOT_IN_TO_ADD = "You're not in for this call. Joining late is coming soon. Ask someone who's in to add it."
"""Only someone who's in can add a pick once voting has started (#74); joining late is #76."""
ALREADY_A_PICK = "That's already a pick on the call. Vote for it instead."


class StaleRun(EventRuleViolation):
    """The person acted on an earlier run of a call that has since been started again."""


WINDOW_MOVED = "The call has moved on since then, so nothing changed."


class WindowMoved(EventRuleViolation):
    """Close now was confirmed for a window that's no longer the open one."""


def check_run(event: Event, run: int | None) -> None:
    """``run`` is the run of the call the person saw, or None when their surface doesn't say.
    A call-off, Close now, or restart from a page left open across a restart would otherwise
    act on the newer run, which the person never saw."""
    if run is not None and run != event.run:
        raise StaleRun(STALE_CALL)


def friendly(exc: IllegalTransition) -> EventRuleViolation:
    """``exc`` as a message for the person whose action lost a race, such as a call-off that
    arrived just after the scheduler marked the meal done."""
    return EventRuleViolation(STALE_CALL if exc.current.is_open else CALL_FINISHED)


class InvalidResponse(ValueError):
    """Response details that can never be saved. The message is safe to show."""


def transition(current: EventState, target: EventState) -> EventState:
    """Return ``target`` if the state machine allows it, or raise IllegalTransition."""
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(current, target)
    return target


class EventKind(StrEnum):
    CALL = "call"
    OFFER = "offer"
    """Someone announcing a meal they're making or getting, with seats to spare (FR-4,
    ``domain.offers``)."""


class EndReason(StrEnum):
    TOO_FEW = "too_few"
    NO_COVERAGE = "no_coverage"
    MISSED = "missed"
    """The server was down until it was too late to decide for that day; ended quietly."""


class Status(StrEnum):
    IN = "in"
    OUT = "out"
    MAYBE = "maybe"


class RoleOffer(StrEnum):
    """Roles a member can offer in a response. Coordinating is a claim on the event instead."""

    COOK = "cook"
    PICKUP = "pickup"
    ORDER = "order"


class AssignmentRole(StrEnum):
    COOK = "cook"
    PICKUP = "pickup"
    ORDER = "order"
    COORDINATOR = "coordinator"


class ProposalSource(StrEnum):
    SYSTEM = "system"
    COORDINATOR = "coordinator"
    MEMBER = "member"


MODE_ROLE = {Mode.COOK: AssignmentRole.COOK, Mode.PICKUP: AssignmentRole.PICKUP, Mode.DELIVERY: AssignmentRole.ORDER}
MODE_OFFER = {Mode.COOK: RoleOffer.COOK, Mode.PICKUP: RoleOffer.PICKUP, Mode.DELIVERY: RoleOffer.ORDER}
"""The role a member offers by adding a pick in ``mode``: whoever adds a pick does it (#74)."""


class Window(StrEnum):
    RESPOND = "respond"
    VOTE = "vote"


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    household_id: int
    kind: EventKind
    slot: Slot
    day: date
    state: EventState
    run: int
    opens_at: datetime
    respond_deadline: datetime
    reminder_minutes: int
    vote_minutes: int
    created_by: int | None = None
    coordinator_id: int | None = None
    vote_deadline: datetime | None = None
    reminder_sent_at: datetime | None = None
    decided_item_id: int | None = None
    decided_mode: Mode | None = None
    meal_time: datetime | None = None
    decided_at: datetime | None = None
    done_at: datetime | None = None
    end_reason: EndReason | None = None
    locked_by: int | None = None
    """Who locked the decision (FR-15), or None when the vote decided it."""
    ended_by: int | None = None
    """Who called the call off (FR-56), or None. A restart or re-run clears it."""
    end_note: str | None = None
    """The reason they gave, if any (``clean_cancel_note``)."""
    capacity: int | None = None
    """An offer's spare servings: how many others, guests included, can join it (FR-4). None
    for a call. An offer keeps its item, mode, and time in ``decided_item_id``,
    ``decided_mode``, and ``meal_time`` from the start, and closes at ``respond_deadline``,
    its time."""

    @property
    def is_offer(self) -> bool:
        return self.kind is EventKind.OFFER

    @property
    def remind_at(self) -> datetime:
        return self.respond_deadline - timedelta(minutes=self.reminder_minutes)

    @property
    def path(self) -> str:
        return f"/events/{self.id}"

    @property
    def key(self) -> str:
        """The prefix for this run's notification keys. A re-run starts fresh messages."""
        return f"event:{self.id}:run{self.run}"


@dataclass(frozen=True, slots=True)
class Response:
    member_id: int
    status: Status
    guests: int = 0
    guest_needs: frozenset[int] = frozenset()
    eat_from: time | None = None
    eat_until: time | None = None
    offers: frozenset[RoleOffer] = frozenset()
    note: str | None = None
    maybe_at_deadline: bool = False
    responded_at: datetime | None = None
    updated_at: datetime | None = None
    """The last change to the response, for the early close (``closes_early``)."""

    @property
    def is_in(self) -> bool:
        return self.status is Status.IN

    @property
    def head_count(self) -> int:
        return 1 + self.guests if self.is_in else 0


@dataclass(frozen=True, slots=True)
class Suggestion:
    member_id: int
    item_id: int | None = None
    free_text: str | None = None


@dataclass(frozen=True, slots=True)
class Proposal:
    """A candidate for a call: a catalog item, or free text with a cuisine (FR-13)."""

    id: int
    event_id: int
    mode: Mode
    rank: int
    source: ProposalSource
    item_id: int | None = None
    free_text: str | None = None
    free_cuisine_id: int | None = None
    score: float | None = None
    reasons: tuple[str, ...] = ()
    proposed_by: int | None = None


@dataclass(frozen=True, slots=True)
class Assignment:
    id: int
    member_id: int
    role: AssignmentRole


@dataclass(frozen=True, slots=True)
class Details:
    """The Details form (FR-8), already parsed. Validated by ``check_details``."""

    guests: int = 0
    guest_needs: frozenset[int] = frozenset()
    eat_from: time | None = None
    eat_until: time | None = None
    suggestion_item_ids: tuple[int, ...] = ()
    suggestion_texts: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ProposalTally:
    """What the decision rule needs to know about one proposal."""

    id: int
    rank: int
    covered: bool
    voters: frozenset[int] = field(default_factory=frozenset)


# Responses -------------------------------------------------------------------------------------


def _is_control(char: str) -> bool:
    return unicodedata.category(char).startswith("C")


def clean_text(value: str | None, limit: int, what: str) -> str | None:
    text = " ".join((value or "").split())
    if not text:
        return None
    if len(text) > limit:
        raise InvalidResponse(f"{what} is at most {limit} characters.")
    if any(_is_control(char) for char in text):
        raise InvalidResponse(f"{what} can't contain control characters.")
    return text


def check_details(details: Details, *, dietary_tag_ids: Iterable[int], active_item_ids: Iterable[int]) -> Details:
    """Validate the Details form and return it with text normalized."""
    if not 0 <= details.guests <= MAX_GUESTS:
        raise InvalidResponse(f"Guests run from 0 to {MAX_GUESTS}.")
    if not details.guest_needs <= set(dietary_tag_ids):
        raise InvalidResponse("Guest needs must be dietary tags. Reload the page and try again.")
    if any(t is not None and t > LATEST_EAT_TIME for t in (details.eat_from, details.eat_until)):
        raise InvalidResponse("Pick eating times by 11:45 PM.")
    if details.eat_from and details.eat_until and details.eat_until < details.eat_from:
        raise InvalidResponse("The latest time to eat can't be before the earliest.")
    if not set(details.suggestion_item_ids) <= set(active_item_ids):
        raise InvalidResponse("That suggestion isn't in the catalog anymore. Reload the page and try again.")
    cleaned = (clean_text(v, MAX_SUGGESTION_LENGTH, "A suggestion") for v in details.suggestion_texts)
    texts = tuple(t for t in cleaned if t)
    if len(set(details.suggestion_item_ids)) + len(texts) > MAX_SUGGESTIONS:
        raise InvalidResponse(f"Suggest up to {MAX_SUGGESTIONS} things.")
    return Details(
        guests=details.guests,
        guest_needs=details.guest_needs if details.guests else frozenset(),  # no guests, no guest needs
        eat_from=details.eat_from,
        eat_until=details.eat_until,
        suggestion_item_ids=tuple(dict.fromkeys(details.suggestion_item_ids)),
        suggestion_texts=texts,
        note=clean_text(details.note, MAX_NOTE_LENGTH, "A note"),
    )


def check_can_respond(event: Event, actor: Member, *, invited: bool = False) -> None:
    """FR-7: members respond while the response window is open. An offer is joined instead
    (``domain.offers``), where seats are counted. A guest responds only to a call they're
    ``invited`` to (#38); ``domain.guests`` has the rest of what a guest may do."""
    if not actor.active:
        raise EventRuleViolation("Only active members can respond.")
    if actor.is_guest and not invited:
        raise EventRuleViolation(GUEST_NOT_INVITED)
    if event.is_offer:
        raise EventRuleViolation(OFFER_JOIN_INSTEAD)
    if event.state is not EventState.COLLECTING:
        raise EventRuleViolation("Responses are closed for this call.")


def with_status(current: Response | None, member_id: int, status: Status, now: datetime) -> Response:
    """A new status. Saying out withdraws role offers: someone who isn't there can't cook."""
    if current is None:
        return Response(member_id=member_id, status=status, responded_at=now)
    offers = frozenset() if status is Status.OUT else current.offers
    return replace(current, status=status, offers=offers)


def with_offer(
    current: Response | None, member_id: int, role: RoleOffer, offered: bool, now: datetime
) -> Response | None:
    """Offer or withdraw a role; None when there's nothing to change. Offering to cook, pick
    up, or order counts you in unless you said maybe: the person doing the work is at the meal."""
    if not offered:
        return None if current is None else replace(current, offers=current.offers - {role})
    status = Status.MAYBE if current is not None and current.status is Status.MAYBE else Status.IN
    base = with_status(current, member_id, status, now)
    return replace(base, offers=base.offers | {role})


def with_details(current: Response | None, member_id: int, details: Details, now: datetime) -> Response:
    """Details without a status yet count the member in."""
    base = current or Response(member_id=member_id, status=Status.IN, responded_at=now)
    return replace(
        base,
        guests=details.guests,
        guest_needs=details.guest_needs,
        eat_from=details.eat_from,
        eat_until=details.eat_until,
        note=details.note,
    )


def resolve_maybes(responses: Iterable[Response]) -> list[Response]:
    """FR-9: unresolved maybes become out at the deadline. Returns only the changed responses."""
    return [
        replace(r, status=Status.OUT, offers=frozenset(), maybe_at_deadline=True)
        for r in responses
        if r.status is Status.MAYBE
    ]


def head_count(responses: Iterable[Response]) -> int:
    """Members who are in, plus their guests."""
    return sum(r.head_count for r in responses)


def veto_participants(responses: Iterable[Response]) -> frozenset[int]:
    """Whose vetoes apply (FR-9, FR-28): members in, and maybes, including maybes the deadline
    turned out, because they might still turn up."""
    return frozenset(r.member_id for r in responses if r.status is not Status.OUT or r.maybe_at_deadline)


def needs_reminder(member: Member, response: Response | None) -> bool:
    """FR-11: one direct message to active members who haven't answered at all. Maybes are not
    nudged again, and members who muted reminders are skipped."""
    return member.active and response is None and not member.mute_dm_reminders


def wants_feedback_reminder(member: Member) -> bool:
    """FR-48: the feedback reminder goes to active members who turned it on. Muting direct-
    message reminders wins: a muted member gets no reminder of any kind."""
    return member.active and member.feedback_reminder and not member.mute_dm_reminders


# Coverage (FR-17) -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Coverage:
    """Who offered what, among members who are in and active."""

    cooks: tuple[int, ...] = ()
    pickups: tuple[int, ...] = ()
    orderers: tuple[int, ...] = ()

    def covers(self, mode: Mode, cook_list: frozenset[int]) -> bool:
        return bool(self.volunteers(mode, cook_list))

    def volunteers(self, mode: Mode, cook_list: frozenset[int]) -> tuple[int, ...]:
        """Members who could take ``mode``'s role, best first. A cook must be on the item's
        cook list when it has one; members on the list come first either way."""
        if mode is Mode.COOK:
            if cook_list:
                return tuple(m for m in self.cooks if m in cook_list)
            return self.cooks
        return self.pickups if mode is Mode.PICKUP else self.orderers


def coverage(responses: Iterable[Response], members: Mapping[int, Member]) -> Coverage:
    """FR-17 coverage from responses, in the order members first answered. Delivery needs any
    participating member who offered to order or can order."""
    ordered = sorted(
        (r for r in responses if r.is_in and r.member_id in members and members[r.member_id].active),
        key=lambda r: (r.responded_at is None, r.responded_at.timestamp() if r.responded_at else 0.0, r.member_id),
    )
    cooks = tuple(r.member_id for r in ordered if RoleOffer.COOK in r.offers)
    pickups = tuple(r.member_id for r in ordered if RoleOffer.PICKUP in r.offers)
    offered_order = [r.member_id for r in ordered if RoleOffer.ORDER in r.offers]
    can_order = [r.member_id for r in ordered if members[r.member_id].capabilities.can_order]
    orderers = tuple(dict.fromkeys(offered_order + can_order))
    return Coverage(cooks=cooks, pickups=pickups, orderers=orderers)


# Decisions (FR-16) -----------------------------------------------------------------------------


def choose_winner(
    tallies: Iterable[ProposalTally],
    *,
    coordinator_id: int | None,
    admin_ids: frozenset[int],
    admins_next: bool = False,
) -> ProposalTally | None:
    """The proposal that wins at the vote deadline, or None if none is covered.

    Most votes wins. Ties go to the proposal the coordinator voted for; with no coordinator, to
    the one more admins voted for; then to ranking order. ``admins_next`` lets admins break a
    tie ``coordinator_id``'s vote didn't: with a guest coordinating, ``coordinator_id`` is whoever
    invited them, and admins come after them (#38).
    """
    eligible = [t for t in tallies if t.covered]
    if not eligible:
        return None

    def key(t: ProposalTally) -> tuple[int, int, int, int]:
        coordinator_pick = 1 if coordinator_id is not None and coordinator_id in t.voters else 0
        admin_picks = len(t.voters & admin_ids) if coordinator_id is None or admins_next else 0
        return (-len(t.voters), -coordinator_pick, -admin_picks, t.rank)

    return min(eligible, key=key)


def assign_roles(mode: Mode, volunteers: tuple[int, ...], coordinator_id: int | None) -> dict[AssignmentRole, int]:
    """Assignments for a decision: the first volunteer takes the mode's role (the coordinator
    can change it afterward), and the coordinator is assigned as coordinator."""
    assigned: dict[AssignmentRole, int] = {}
    if volunteers:
        assigned[MODE_ROLE[mode]] = volunteers[0]
    if coordinator_id is not None:
        assigned[AssignmentRole.COORDINATOR] = coordinator_id
    return assigned


def round_up_to_quarter(moment: datetime) -> datetime:
    """Round up to the next quarter hour; a time already on one stays put."""
    epoch = datetime(2000, 1, 1, tzinfo=moment.tzinfo)
    quarters = math.ceil((moment - epoch) / QUARTER_HOUR)
    return epoch + quarters * QUARTER_HOUR


def meal_time(
    responses: Iterable[Response], *, day: date, zone: tzinfo, default: time, not_before: datetime
) -> datetime:
    """FR-19: the latest "earliest" across participants' eat windows, rounded up to the
    quarter hour. With no eat windows, the slot's usual meal time. Never before ``not_before``
    (the decision), so a decision can't land in the past."""
    earliest = [r.eat_from for r in responses if r.is_in and r.eat_from is not None]
    wall = max(earliest) if earliest else default
    candidate = datetime.combine(day, wall, tzinfo=zone).astimezone(UTC)
    return round_up_to_quarter(max(candidate, not_before.astimezone(UTC)))


# Who may do what ------------------------------------------------------------------------------


def is_coordinator(event: Event, actor: Member) -> bool:
    return event.coordinator_id is not None and event.coordinator_id == actor.id


def check_claim_coordinator(event: Event, actor: Member, *, invited: bool = False) -> None:
    """A guest claims coordinating only on a call they're ``invited`` to (#38)."""
    if not actor.active:
        raise EventRuleViolation("Only active members can coordinate.")
    if actor.is_guest and not invited:
        raise EventRuleViolation(GUEST_NOT_INVITED)
    if event.is_offer:
        raise EventRuleViolation(NO_COORDINATOR)
    if event.state not in {EventState.COLLECTING, EventState.VOTING}:
        raise EventRuleViolation("You can claim coordinating only while the call is open.")
    if event.coordinator_id is not None:
        raise EventRuleViolation("Someone is already coordinating. They can hand it off to you.")


def check_assign_coordinator(event: Event) -> None:
    """FR-10: the admin assigns, replaces, or clears the coordinator of a posted, open call."""
    if event.is_offer:
        raise EventRuleViolation(NO_COORDINATOR)
    if event.state not in OPEN_STATES - {EventState.SCHEDULED}:
        raise EventRuleViolation("This call is over.")


def check_handoff(event: Event, actor: Member, target: Member) -> None:
    if event.is_offer:
        raise EventRuleViolation(NO_COORDINATOR)
    if not is_coordinator(event, actor):
        raise EventRuleViolation("Only the coordinator can hand off coordinating.")
    if event.state not in {EventState.COLLECTING, EventState.VOTING, EventState.DECIDED}:
        raise EventRuleViolation("This call is over.")
    if not target.active or target.id == actor.id:
        raise EventRuleViolation("Pick another active member.")


def check_can_vote(event: Event, actor: Member, response: Response | None) -> None:
    if actor.is_guest:
        raise EventRuleViolation(GUESTS_DONT)
    if event.state is not EventState.VOTING:
        raise EventRuleViolation("Voting is closed.")
    if not actor.active or not ((response is not None and response.is_in) or is_coordinator(event, actor)):
        raise EventRuleViolation("Only people who are in can vote.")


def check_can_propose(event: Event, actor: Member) -> None:
    if actor.is_guest:
        raise EventRuleViolation(GUESTS_DONT)
    if event.state is not EventState.VOTING:
        raise EventRuleViolation("You can add proposals only while voting is open.")
    if not actor.active:
        raise EventRuleViolation("Only active members can add proposals.")


def check_proposal_room(proposals: Iterable[Proposal], actor: Member) -> None:
    """FR-13 within limits: at most ``MAX_PROPOSALS`` per call, ``MAX_ADDED_PER_MEMBER`` from one member."""
    existing = list(proposals)
    if len(existing) >= MAX_PROPOSALS:
        raise EventRuleViolation(f"This call already has {MAX_PROPOSALS} proposals. Vote for one of them.")
    if sum(1 for p in existing if p.proposed_by == actor.id) >= MAX_ADDED_PER_MEMBER:
        raise EventRuleViolation(f"You've added {MAX_ADDED_PER_MEMBER} proposals to this call already.")


def check_in_to_add(mine: Response | None) -> None:
    """Once voting has started, only someone who's in can add a pick: they'll do it (#74), and a
    new person's vetoes would change what the picks must pass (FR-29)."""
    if mine is None or not mine.is_in:
        raise EventRuleViolation(NOT_IN_TO_ADD)


def with_pick_role(current: Response | None, member_id: int, mode: Mode, now: datetime, *, join: bool) -> Response:
    """The answer of whoever adds a pick in ``mode``, offering the role it needs: whoever adds a
    pick does it (#74), so it's covered (FR-17). The caller checks they can (``can_do``). It
    skips ``check_can_respond``, because a pick can be added after responses close. While
    responses are open (``join``), adding one counts you in, as offering a role does; after
    that, only someone already in can (``check_in_to_add``)."""
    if not join:
        check_in_to_add(current)
    base = with_status(current, member_id, Status.IN, now)
    return replace(base, offers=base.offers | {MODE_OFFER[mode]})


def can_do(item: CatalogItem | None, mode: Mode, member_id: int) -> bool:
    """FR-17: whether a member can take ``mode``'s role for ``item``. Anyone can pick up or
    order; someone can cook an item when it has no cook list or they're on it. Something new
    (``item`` None) has no cook list."""
    return mode is not Mode.COOK or item is None or not item.cook_ids or member_id in item.cook_ids


def not_on_cook_list(item: CatalogItem, ask: str) -> str:
    """Why someone off ``item``'s cook list can't cook it; ``ask`` says who to ask instead."""
    return f"{item.name} has a cook list, and you're not on it. {ask}."


def check_cook_list(item: CatalogItem, mode: Mode, actor: Member, *, ask: str) -> None:
    """An offer to cook ``item`` means the offerer cooks it, so they must be able to
    (``can_do``). ``ask`` ends the refusal, such as "Ask a cook to offer it"."""
    if not can_do(item, mode, actor.id):
        raise EventRuleViolation(not_on_cook_list(item, ask))


def check_can_lock(event: Event, actor: Member) -> None:
    """FR-15: the coordinator, or the admin, locks a proposal during the vote window. A guest
    coordinator can't: picking between proposals isn't theirs (#38)."""
    if actor.is_guest:
        raise EventRuleViolation(GUESTS_DONT)
    if event.state is not EventState.VOTING:
        raise EventRuleViolation("Voting is closed.")
    if not actor.active or not (is_coordinator(event, actor) or actor.is_admin):
        raise EventRuleViolation("Only the coordinator or an admin can lock a proposal.")


def check_can_manage_decision(event: Event, actor: Member) -> None:
    """Change the meal time or who does a role: the coordinator while decided, the admin also
    after the fact (spec 5.7)."""
    if not actor.active:
        raise EventRuleViolation("Only active members can change the decision.")
    if event.is_offer:
        raise EventRuleViolation(OFFER_CHANGE)
    if actor.is_admin and event.state in {EventState.DECIDED, EventState.DONE}:
        return
    if not is_coordinator(event, actor):
        raise EventRuleViolation("Only the coordinator or an admin can change the decision.")
    if event.state is not EventState.DECIDED:
        raise EventRuleViolation("There's no decision to change.")


def check_can_mark_done(event: Event, actor: Member, response: Response | None, *, now: datetime) -> None:
    """FR-21: any participant marks a decided event done; so can the coordinator and admin.
    Members can only once it's meal time, so a stray tap can't end the call early; admins can
    any time, to correct the record."""
    if event.state is not EventState.DECIDED:
        raise EventRuleViolation("Only a decided meal can be marked done.")
    participant = response is not None and response.is_in
    if not actor.active or not (participant or is_coordinator(event, actor) or actor.is_admin):
        raise EventRuleViolation("Only someone at the meal can mark it done.")
    if not actor.is_admin and event.meal_time is not None and now < event.meal_time:
        raise EventRuleViolation("You can mark the meal done once it's meal time.")


class ActingAs(StrEnum):
    """The capacity someone acts in, for the audit row of a call-off or a close: the narrowest
    that allows it."""

    COORDINATOR = "coordinator"
    STARTER = "starter"
    ADMIN = "admin"


STARTER_STATES = frozenset({EventState.SCHEDULED, EventState.COLLECTING, EventState.VOTING})
"""While the member who started a call may still call it off. Once it's decided, people may be
cooking or driving, so only the coordinator or an admin can."""
CLOSABLE_STATES = frozenset({EventState.COLLECTING, EventState.VOTING})


def check_can_cancel(event: Event, actor: Member) -> ActingAs:
    """FR-56: who may call a call off. The coordinator and admins, in any open state; the
    member who started an ad-hoc call (``created_by``), until it's decided. Returns the
    narrowest capacity that allows it, so an admin who coordinates is audited as coordinator.
    Whoever made an offer may call it off until it's done, even once it has closed: they're
    the one cooking or driving."""
    if not actor.active:
        raise EventRuleViolation("Only active members can call off a call.")
    if actor.is_guest:
        raise EventRuleViolation(GUESTS_DONT)
    if not event.state.is_open:
        raise EventRuleViolation(CALL_FINISHED)
    if event.is_offer:
        if event.created_by is not None and event.created_by == actor.id:
            return ActingAs.STARTER
        if actor.is_admin:
            return ActingAs.ADMIN
        raise EventRuleViolation("Only whoever made the offer, or an admin, can call it off.")
    if is_coordinator(event, actor):
        return ActingAs.COORDINATOR
    starter = event.created_by is not None and event.created_by == actor.id
    if starter and event.state in STARTER_STATES:
        return ActingAs.STARTER
    if actor.is_admin:
        return ActingAs.ADMIN
    if starter:
        raise EventRuleViolation("It's decided now, so only the coordinator or an admin can call it off.")
    raise EventRuleViolation("Only the coordinator, whoever started this call, or an admin can call it off.")


def check_can_close_window(event: Event, actor: Member) -> Window:
    """FR-56: the coordinator or an admin closes the open window now: "Start the vote now"
    while collecting, "Decide now" while voting. Returns the window it closes."""
    if not actor.active:
        raise EventRuleViolation("Only active members can close a window.")
    if not event.state.is_open:
        raise EventRuleViolation(CALL_FINISHED)
    if event.is_offer:
        raise EventRuleViolation("An offer closes at its time. Call it off instead if plans changed.")
    if event.state not in CLOSABLE_STATES:
        raise EventRuleViolation("There's no open window to close.")
    if not (is_coordinator(event, actor) or actor.is_admin):
        raise EventRuleViolation("Only the coordinator or an admin can close it now.")
    return Window.RESPOND if event.state is EventState.COLLECTING else Window.VOTE


def check_expected_window(event: Event, window: Window, expected: Window | None, *, now: datetime) -> None:
    """``expected`` is the window the person confirmed closing, or None when their surface
    doesn't say. ``window`` is the one open now. A confirmation that arrives after the call
    moved on, or a second one racing the first, must not close the next window, nor close
    this one again once its deadline has passed and the call is advancing."""
    if expected is None:
        return
    deadline = event.respond_deadline if window is Window.RESPOND else event.vote_deadline
    if window is not expected or (deadline is not None and deadline <= now):
        raise WindowMoved(WINDOW_MOVED)


def clean_cancel_note(text: str | None) -> str | None:
    """A call-off reason as one line of at most ``CANCEL_NOTE_MAX`` characters, or None when
    blank. Whitespace runs become one space and other control characters are dropped, keeping
    the zero-width joiner that emoji sequences use. Longer text is cut, not refused: the reason
    is optional, and refusing it would lose the call-off."""
    kept = "".join(c for c in " ".join((text or "").split()) if c == "\u200d" or not _is_control(c))
    return kept[:CANCEL_NOTE_MAX].strip() or None


def check_skip_day(day: date, today: date) -> None:
    """An admin skips a daily call from today up to ``AD_HOC_DAYS_AHEAD`` days ahead: the
    days anyone can start a call for."""
    if day < today:
        raise EventRuleViolation("You can skip today's call or a later day's.")
    if day > today + timedelta(days=AD_HOC_DAYS_AHEAD):
        raise EventRuleViolation(f"You can skip a call up to {AD_HOC_DAYS_AHEAD} days ahead.")


def check_ad_hoc_day(day: date, today: date) -> None:
    if day < today:
        raise EventRuleViolation("You can start a call for today or a later day.")
    if day > today + timedelta(days=AD_HOC_DAYS_AHEAD):
        raise EventRuleViolation(f"You can start a call up to {AD_HOC_DAYS_AHEAD} days ahead.")


def check_rerun(event: Event, today: date, *, now: datetime, too_late_after: datetime) -> None:
    """``too_late_after`` is the usual meal time plus two hours on ``today``: past it, a re-run
    of today's call would only be expired again. Offers aren't re-run: post a new one."""
    if event.is_offer:
        raise EventRuleViolation("Only a call can be re-run. Post a new offer instead.")
    if event.state not in {EventState.VOTING, EventState.DECIDED, EventState.ABANDONED, EventState.CANCELLED}:
        raise EventRuleViolation("Only a call that's voting, decided, abandoned, or cancelled can be re-run.")
    if event.day < today:
        raise EventRuleViolation("Only today's or a later call can be re-run.")
    if event.day == today and now >= too_late_after:
        raise EventRuleViolation("It's too late to re-run today's call. Start one for tomorrow instead.")


def check_restart(
    event: Event, today: date, *, now: datetime, too_late_after: datetime, any_time: bool = False
) -> None:
    """FR-56: any member starts a called-off call again, under the re-run's timing rules.
    ``any_time``, on a test instance only (#60), lifts the too-late rule."""
    if event.is_offer:
        raise EventRuleViolation("Only a call can start again. Post a new offer instead.")
    if event.state is not EventState.CANCELLED:
        raise EventRuleViolation(f"There's already a {event.slot.value} call for that day.")
    if event.day < today:  # pragma: no cover - start_call allows only today or later
        raise EventRuleViolation("You can start a call for today or a later day.")
    if event.day == today and now >= too_late_after and not any_time:
        raise EventRuleViolation(
            f"It's too late to start today's {event.slot.value} call again. Start one for tomorrow instead."
        )


def shifted_deadline(current: datetime, minutes: int | None, now: datetime, *, latest: datetime) -> datetime:
    """A window's new deadline: ``minutes`` later (negative is earlier), or now when None.
    Never earlier than now, so shortening past the present closes the window, and never past
    ``latest``, midnight after the call's day, when the call would expire unfinished."""
    if minutes is None:
        return now
    if not -MAX_DEADLINE_SHIFT_MINUTES <= minutes <= MAX_DEADLINE_SHIFT_MINUTES:
        raise EventRuleViolation(f"Move a deadline by at most {MAX_DEADLINE_SHIFT_MINUTES} minutes at a time.")
    moved = max(current + timedelta(minutes=minutes), now)
    if moved > latest:
        raise EventRuleViolation("That would run past midnight, when the call ends.")
    return moved


def check_window_change(event: Event, window: Window) -> datetime:
    """The deadline the admin may move right now, or EventRuleViolation. An offer has no
    windows: it closes at its time."""
    if event.is_offer:
        raise EventRuleViolation("An offer has no window to move. It closes at its time.")
    if window is Window.RESPOND and event.state is EventState.COLLECTING:
        return event.respond_deadline
    if window is Window.VOTE and event.state is EventState.VOTING and event.vote_deadline is not None:
        return event.vote_deadline
    raise EventRuleViolation("That window isn't open.")


def posted_before_its_day(event: Event, zone: tzinfo) -> bool:
    """Whether ``event`` was posted before its day (issue #59). Only posting early opens a call
    before its day, and ``opens_at`` records when responses opened, so it stays true through
    voting, a decision, a call-off, and a re-run or restart, which keep it posted
    (``lifecycle.reopen``). A scheduled call hasn't been posted at all."""
    return event.state is not EventState.SCHEDULED and event.opens_at.astimezone(zone).date() < event.day


def check_can_post_now(event: Event, actor: Member, *, now: datetime) -> bool:
    """Issue #59: any active member, who could start the call, posts a scheduled call now,
    such as tomorrow's lunch the evening before. Returns whether there's anything to post: a
    call that's posted already stays as it is. A finished call, or a scheduled one whose
    response window has passed (the scheduler expires it instead, FR-2), is refused."""
    if not actor.active:
        raise EventRuleViolation("Only active members can post a call.")
    if actor.is_guest:
        raise EventRuleViolation(GUESTS_DONT)
    if event.is_offer:
        raise EventRuleViolation("An offer is posted as soon as it's made.")
    if not event.state.is_open:
        raise EventRuleViolation(CALL_FINISHED)
    if event.state is not EventState.SCHEDULED:
        return False
    if now >= event.respond_deadline:
        raise EventRuleViolation("It's too late to post this call.")
    return True


# Scheduler steps -------------------------------------------------------------------------------


class Step(StrEnum):
    EXPIRE = "expire"
    OPEN = "open"
    REMIND = "remind"
    CLOSE_RESPONSES = "close_responses"
    CLOSE_VOTING = "close_voting"
    MARK_DONE = "mark_done"


UNDECIDED = frozenset({EventState.SCHEDULED, EventState.COLLECTING, EventState.VOTING})


def stale_after(event: Event, *, usual_meal: time, zone: tzinfo) -> datetime:
    """When an undecided call is too old to finish: the later of the slot's usual meal time
    plus two hours and the call's own vote deadline plus two hours, but never past midnight
    after its day. A late ad-hoc call keeps its evening; a call nobody could reach after a long
    outage doesn't decide dinner at 22:00 or on Saturday."""
    midnight = datetime.combine(event.day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
    usual = datetime.combine(event.day, usual_meal, tzinfo=zone).astimezone(UTC) + DONE_AFTER_MEAL
    natural_end = event.vote_deadline or event.respond_deadline + timedelta(minutes=event.vote_minutes)
    return min(midnight, max(usual, natural_end + DONE_AFTER_MEAL))


def closes_early(
    event: Event,
    now: datetime,
    responses: Iterable[Response],
    active_member_ids: Iterable[int],
    coverage: Coverage,
    guest_ids: Iterable[int] = (),
) -> bool:
    """FR-56: whether a collecting call's responses close before the deadline.

    Every active member must have answered in this run (an answer kept from before a re-run or
    restart doesn't count) and nobody may be a maybe. With everyone out, it closes at once and
    ends on-your-own. With anyone in, it waits until no response has changed for
    ``EARLY_CLOSE_QUIET``, so tapping In first and adding guests or an offer next is never cut
    short; with two or more eating, someone must also be able to cook, pick up, or order
    (``coverage``). Otherwise the call waits for its deadline, or for Close now.

    ``active_member_ids`` are the household's members and admins, whom the call waits for;
    ``guest_ids`` are the guests invited to it (#38). Nobody waits for a guest, but a guest's
    answer counts like anyone's toward who's in, the head count, and the quiet spell.
    """
    active = frozenset(active_member_ids)
    counted = active | frozenset(guest_ids)
    this_run = [
        r for r in responses if r.member_id in counted and r.updated_at is not None and r.updated_at >= event.opens_at
    ]
    answered = {r.member_id for r in this_run} & active
    if not active or answered != active or any(r.status is Status.MAYBE for r in this_run):
        return False
    if not any(r.is_in for r in this_run):
        return True
    last_change = max(r.updated_at for r in this_run if r.updated_at is not None)
    if now - last_change < EARLY_CLOSE_QUIET:
        return False
    return head_count(this_run) < MIN_HEAD_COUNT or bool(coverage.cooks or coverage.pickups or coverage.orderers)


def due_step(
    event: Event,
    now: datetime,
    *,
    stale_after: datetime,
    responses: Iterable[Response] = (),
    active_member_ids: Iterable[int] = (),
    coverage: Coverage | None = None,
    guest_ids: Iterable[int] = (),
) -> Step | None:
    """The next thing the scheduler owes ``event`` at ``now``, if anything.

    After downtime, calling this repeatedly walks an event through every missed step. A
    reminder whose deadline has already passed is skipped: it would only arrive too late. An
    undecided call past ``stale_after``, or a scheduled call whose response window already
    passed, expires instead of being caught up (FR-2). Responses close early by
    ``closes_early``, given the call's responses, the household's active members, and the
    responses' coverage; with none passed, they close at the deadline.
    """
    if event.is_offer:
        # An offer has no reminder and no early close: it takes people until its time (FR-4),
        # then it's on, and done two hours after. Its plan was set when it was posted, so after
        # downtime it catches up to done instead of expiring like an undecided call.
        if event.state is EventState.COLLECTING and now >= event.respond_deadline:
            return Step.CLOSE_RESPONSES
        if (
            event.state is EventState.DECIDED
            and event.meal_time is not None
            and now >= event.meal_time + DONE_AFTER_MEAL
        ):
            return Step.MARK_DONE
        return None
    if event.state in UNDECIDED and now >= stale_after:
        return Step.EXPIRE
    if event.state is EventState.SCHEDULED:
        if now >= event.respond_deadline:
            return Step.EXPIRE
        return Step.OPEN if now >= event.opens_at else None
    if event.state is EventState.COLLECTING:
        early = closes_early(event, now, responses, active_member_ids, coverage or Coverage(), guest_ids)
        if now >= event.respond_deadline or early:
            return Step.CLOSE_RESPONSES
        if event.reminder_sent_at is None and now >= event.remind_at:
            return Step.REMIND
    if event.state is EventState.VOTING and event.vote_deadline is not None and now >= event.vote_deadline:
        return Step.CLOSE_VOTING
    if event.state is EventState.DECIDED and event.meal_time is not None and now >= event.meal_time + DONE_AFTER_MEAL:
        return Step.MARK_DONE
    return None


def should_open_today(
    *, enabled: bool, opens_at: datetime, respond_deadline: datetime, now: datetime, exists: bool
) -> bool:
    """FR-2: open the standing call once its time has come, unless it already exists. A call
    whose response window already closed (the server was down all afternoon) is skipped rather
    than opened only to be abandoned at once."""
    return enabled and not exists and opens_at <= now < respond_deadline
