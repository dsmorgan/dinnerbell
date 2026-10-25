# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notifications: what happened, as typed domain data, for a channel adapter to deliver
(spec sections 9 and 13.4, FR-33, FR-34). docs/channels.md is the adapter's contract.

Each notification carries IDs, names, times, items, and links as paths; nothing here knows
about Slack. Services write notifications to the outbox in the same transaction as the change
they describe. Three keys steer delivery:

- ``idempotency_key``: unique per household; writing the same key twice stores it once.
- ``message_key``: names a message that later notifications edit in place. A post with a
  ``message_key`` creates it; an update (``is_update``) edits it. Undelivered updates to the
  same message coalesce: only the latest state is sent.
- ``thread_key``: the ``message_key`` of the message to thread a post under.
"""

from __future__ import annotations

import types
from dataclasses import MISSING, dataclass, fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, ClassVar, Protocol, Union, get_args, get_origin, get_type_hints

from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.events import AssignmentRole, EndReason, EventKind, EventState, ProposalSource, RoleOffer, Status
from dinnerbell.domain.meals import Slot

# Building blocks ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonInfo:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class EventInfo:
    """The event a notification is about. ``path`` is the event page, relative to the base
    URL (FR-37). Times are UTC; ``timezone`` is the household's, for display."""

    id: int
    household_id: int
    slot: Slot
    day: date
    state: EventState
    run: int
    path: str
    timezone: str
    respond_deadline: datetime
    vote_deadline: datetime | None = None
    coordinator: PersonInfo | None = None
    end_reason: EndReason | None = None
    ended_by: PersonInfo | None = None
    """Who called the call off (FR-56), when it's cancelled."""
    end_note: str | None = None
    """The reason they gave, if any."""
    early: bool = False
    """Posted before its day (issue #59), so messages say which day its deadline is on."""
    kind: EventKind = EventKind.CALL
    """A call or an offer (FR-4)."""

    @property
    def key(self) -> str:
        return f"event:{self.id}:run{self.run}"


@dataclass(frozen=True, slots=True)
class ItemInfo:
    """A catalog item, or a free-text proposal (``id`` None) not yet in the catalog."""

    id: int | None
    name: str
    kind: ItemKind
    url: str | None = None
    order_url: str | None = None
    cuisines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RosterEntry:
    member: PersonInfo
    status: Status
    guests: int = 0
    guest_needs: tuple[str, ...] = ()
    eat_from: time | None = None
    eat_until: time | None = None
    offers: tuple[RoleOffer, ...] = ()
    suggestions: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ProposalInfo:
    id: int
    rank: int
    item: ItemInfo
    mode: Mode
    reasons: tuple[str, ...]
    source: ProposalSource
    proposed_by: PersonInfo | None = None
    voters: tuple[PersonInfo, ...] = ()

    @property
    def votes(self) -> int:
        return len(self.voters)


@dataclass(frozen=True, slots=True)
class ParticipantInfo:
    member: PersonInfo
    guests: int = 0


@dataclass(frozen=True, slots=True)
class AssignmentInfo:
    role: AssignmentRole
    member: PersonInfo


@dataclass(frozen=True, slots=True)
class OfferInfo:
    """An offer (FR-4): who's making or getting what, how, by when, and how many seats are left.
    ``capacity`` is the spare servings; ``taken`` counts everyone in but the offerer, guests
    included (``domain.offers.seats_taken``). Guests' dietary needs are listed so whoever
    cooks or orders can plan."""

    offerer: PersonInfo
    item: ItemInfo
    mode: Mode
    at: datetime
    capacity: int
    taken: int
    dietary_notes: tuple[str, ...] = ()

    @property
    def seats_left(self) -> int:
        return max(self.capacity - self.taken, 0)

    @property
    def full(self) -> bool:
        return self.seats_left == 0


@dataclass(frozen=True, slots=True)
class DecisionInfo:
    """FR-18: what was decided, with the head count and guests' dietary needs (FR-20)."""

    proposal: ProposalInfo
    meal_time: datetime
    head_count: int
    participants: tuple[ParticipantInfo, ...]
    assignments: tuple[AssignmentInfo, ...]
    dietary_notes: tuple[str, ...] = ()


# Notifications --------------------------------------------------------------------------------


class _Notification:
    """Behavior every notification shares. Subclasses are frozen dataclasses with an ``event``."""

    __slots__ = ()
    kind: ClassVar[str]
    is_update: ClassVar[bool] = False
    message_suffix: ClassVar[str | None] = None
    thread_suffix: ClassVar[str | None] = None
    needs_thread: ClassVar[bool] = False
    """A reply that means nothing without the message it threads under. When that message
    never landed, the outbox closes the row unsent instead of posting it at the top level."""

    event: EventInfo

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}"

    @property
    def message_key(self) -> str | None:
        return None if self.message_suffix is None else f"{self.event.key}:{self.message_suffix}"

    @property
    def thread_key(self) -> str | None:
        return None if self.thread_suffix is None else f"{self.event.key}:{self.thread_suffix}"

    @property
    def recipient_id(self) -> int | None:
        """The member to message directly, or None for a channel post."""
        return None

    @property
    def household_id(self) -> int:
        return self.event.household_id

    @property
    def event_id(self) -> int | None:
        """The event the notification is about, or None for one about the household."""
        return self.event.id


@dataclass(frozen=True, slots=True)
class CallOpened(_Notification):
    """Call opens: a channel post mentioning every active member (``mentions``)."""

    kind: ClassVar[str] = "call_opened"
    message_suffix: ClassVar[str | None] = "call"
    event: EventInfo
    mentions: tuple[PersonInfo, ...]
    roster: tuple[RosterEntry, ...] = ()
    waiting_on: tuple[PersonInfo, ...] = ()
    head_count: int = 0


@dataclass(frozen=True, slots=True)
class CallUpdated(_Notification):
    """The call message's new state: roster, coordinator, deadlines, or the event's state."""

    kind: ClassVar[str] = "call_updated"
    is_update: ClassVar[bool] = True
    message_suffix: ClassVar[str | None] = "call"
    event: EventInfo
    roster: tuple[RosterEntry, ...] = ()
    waiting_on: tuple[PersonInfo, ...] = ()
    head_count: int = 0


@dataclass(frozen=True, slots=True)
class ResponseReminder(_Notification):
    """Response window closing: one direct message to a member who hasn't answered (FR-11)."""

    kind: ClassVar[str] = "response_reminder"
    event: EventInfo
    member: PersonInfo
    guest: bool = False
    """The member is a guest invited to the call (#38): In and Out only."""

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}"

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class MaybeResolved(_Notification):
    """A maybe became out at the deadline; the member is told (FR-9)."""

    kind: ClassVar[str] = "maybe_resolved"
    event: EventInfo
    member: PersonInfo

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}"

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class ProposalsReady(_Notification):
    """Proposals ready: a post threaded under the call, with a vote button per proposal."""

    kind: ClassVar[str] = "proposals_ready"
    message_suffix: ClassVar[str | None] = "proposals"
    thread_suffix: ClassVar[str | None] = "call"
    event: EventInfo
    proposals: tuple[ProposalInfo, ...]
    head_count: int = 0


@dataclass(frozen=True, slots=True)
class ProposalsUpdated(_Notification):
    """The proposals message's new state: votes, added proposals, or voting closed."""

    kind: ClassVar[str] = "proposals_updated"
    is_update: ClassVar[bool] = True
    message_suffix: ClassVar[str | None] = "proposals"
    event: EventInfo
    proposals: tuple[ProposalInfo, ...]
    head_count: int = 0


@dataclass(frozen=True, slots=True)
class DecisionMade(_Notification):
    """Decision made: a post threaded under the call. ``locked_by`` is who locked it (FR-15),
    or None when the vote decided."""

    kind: ClassVar[str] = "decision_made"
    message_suffix: ClassVar[str | None] = "decision"
    thread_suffix: ClassVar[str | None] = "call"
    event: EventInfo
    decision: DecisionInfo
    locked_by: PersonInfo | None = None


@dataclass(frozen=True, slots=True)
class DecisionUpdated(_Notification):
    """The decision message's new state: meal time, assignments, or done. ``locked_by`` is who
    locked it, as on ``DecisionMade``, so every edit can still say so."""

    kind: ClassVar[str] = "decision_updated"
    is_update: ClassVar[bool] = True
    message_suffix: ClassVar[str | None] = "decision"
    event: EventInfo
    decision: DecisionInfo
    locked_by: PersonInfo | None = None


@dataclass(frozen=True, slots=True)
class AssignmentMade(_Notification):
    """Assignment: one direct message to an assignee listing all their ``roles`` (FR-20), such
    as cooking and coordinating. ``assignment_id`` is the assignment that prompted it: at the
    decision, the member's first; later, the new one. The key names the member and that
    assignment, so a retried transition stores it once and a corrected assignment messages its
    new assignee."""

    kind: ClassVar[str] = "assignment_made"
    event: EventInfo
    assignment_id: int
    member: PersonInfo
    roles: tuple[AssignmentRole, ...]
    decision: DecisionInfo
    via: PersonInfo | None = None
    """For a guest without Slack (#38), the member who invited them, who gets the message."""

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}:{self.assignment_id}"

    @property
    def recipient_id(self) -> int | None:
        return self.via.id if self.via is not None else self.member.id


@dataclass(frozen=True, slots=True)
class CallAbandoned(_Notification):
    """Abandoned: the on-your-own channel post (spec 5.4)."""

    kind: ClassVar[str] = "call_abandoned"
    event: EventInfo
    reason: EndReason
    head_count: int = 0
    needs_unmet: bool = False
    """Nothing fit, and it was the dietary needs that ruled out what else would have: say so,
    without saying whose (#38)."""


@dataclass(frozen=True, slots=True)
class EventCancelled(_Notification):
    """Called off (FR-56): a reply threaded under the call message. ``audience`` is who to
    mention: everyone who was in or maybe, plus the coordinator. ``participants`` (who was in)
    predates it. A call that was never posted sends none, and the outbox drops one whose call
    message never landed (``needs_thread``)."""

    kind: ClassVar[str] = "event_cancelled"
    thread_suffix: ClassVar[str | None] = "call"
    needs_thread: ClassVar[bool] = True
    event: EventInfo
    participants: tuple[PersonInfo, ...] = ()
    cancelled_by: PersonInfo | None = None
    note: str | None = None
    audience: tuple[PersonInfo, ...] | None = None
    """None only on rows queued before it existed; mention ``participants`` for those."""


@dataclass(frozen=True, slots=True)
class AssignmentCancelled(_Notification):
    """A decided call was called off: one direct message to each member assigned to cook,
    pick up, or order, so nobody shops or drives for nothing. ``roles`` are theirs."""

    kind: ClassVar[str] = "assignment_cancelled"
    event: EventInfo
    member: PersonInfo
    roles: tuple[AssignmentRole, ...]
    decision: DecisionInfo
    cancelled_by: PersonInfo | None = None
    note: str | None = None
    via: PersonInfo | None = None
    """For a guest without Slack (#38), the member who invited them, who gets the message."""

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}"

    @property
    def recipient_id(self) -> int | None:
        return self.via.id if self.via is not None else self.member.id


@dataclass(frozen=True, slots=True)
class StartedCallCancelled(_Notification):
    """Someone else called off a call ``member`` started before it was ever posted, so there's
    no thread to reply in: one direct message to the starter instead."""

    kind: ClassVar[str] = "started_call_cancelled"
    event: EventInfo
    member: PersonInfo
    cancelled_by: PersonInfo
    note: str | None = None

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}"

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class SlotScheduleInfo:
    """A slot's standing schedule (FR-1), as a schedule change shows it. Times are wall-clock
    times in the household time zone."""

    enabled: bool
    call_time: time
    response_minutes: int
    vote_minutes: int
    reminder_minutes: int
    meal_time: time
    modes: tuple[Mode, ...]


@dataclass(frozen=True, slots=True)
class ScheduleChanged:
    """A member changed a slot's standing schedule (FR-44): a channel post naming them, with
    what changed. It's about the household, not an event, so it has no ``event``; the outbox
    stores it with no event ID, and it holds back nothing. ``starts_on`` is the first day whose
    call follows the new schedule. ``kept_today_at`` is set when today's call keeps the old
    call time because the new one would have skipped it. ``change_id`` makes each change its
    own post, and matches the change's audit row."""

    kind: ClassVar[str] = "schedule_changed"
    is_update: ClassVar[bool] = False
    needs_thread: ClassVar[bool] = False
    household_id: int
    change_id: str
    slot: Slot
    changed_by: PersonInfo
    before: SlotScheduleInfo
    after: SlotScheduleInfo
    starts_on: date
    path: str
    kept_today_at: time | None = None

    @property
    def idempotency_key(self) -> str:
        return f"schedule:{self.slot.value}:{self.change_id}"

    @property
    def message_key(self) -> str | None:
        return None

    @property
    def thread_key(self) -> str | None:
        return None

    @property
    def recipient_id(self) -> int | None:
        return None

    @property
    def event_id(self) -> int | None:
        return None


@dataclass(frozen=True, slots=True)
class CallDiscarded(_Notification):
    """An admin discarded the call on a test instance (#60): one of its messages, the one whose
    ``message_key`` is ``target``, now says so. ``event`` is the call as it was; it's gone, so
    the outbox row refers to no event."""

    kind: ClassVar[str] = "call_discarded"
    is_update: ClassVar[bool] = True
    event: EventInfo
    target: str

    @property
    def idempotency_key(self) -> str:
        return f"{self.target}:{self.kind}"

    @property
    def message_key(self) -> str | None:
        return self.target

    @property
    def event_id(self) -> int | None:
        """None: the call is gone, so the outbox row refers to no event (as ``ScheduleChanged``)."""
        return None


@dataclass(frozen=True, slots=True)
class FeedbackReminder(_Notification):
    """Feedback reminder (FR-48): one direct message per done meal to a member who ate it and
    turned the reminder on. ``item`` names what they ate, and ``closes`` is the last day they
    can give feedback, in the household's time zone."""

    kind: ClassVar[str] = "feedback_reminder"
    event: EventInfo
    member: PersonInfo
    item: str
    closes: date

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}"

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class OfferPosted(_Notification):
    """Offer posted (FR-4): a channel post with the offer, its roster, and join buttons. Its
    message is the event's only one, so it shares the call message's key (``…:call``): a
    call-off threads under it, and a discard on a test instance edits it."""

    kind: ClassVar[str] = "offer_posted"
    message_suffix: ClassVar[str | None] = "call"
    event: EventInfo
    offer: OfferInfo
    roster: tuple[RosterEntry, ...] = ()
    head_count: int = 0


@dataclass(frozen=True, slots=True)
class OfferUpdated(_Notification):
    """The offer message's new state: who joined, seats left, closed, done, or called off."""

    kind: ClassVar[str] = "offer_updated"
    is_update: ClassVar[bool] = True
    message_suffix: ClassVar[str | None] = "call"
    event: EventInfo
    offer: OfferInfo
    roster: tuple[RosterEntry, ...] = ()
    head_count: int = 0


@dataclass(frozen=True, slots=True)
class OfferFull(_Notification):
    """Offer full: one direct message to whoever made it, once per offer, when the last seat goes."""

    kind: ClassVar[str] = "offer_full"
    event: EventInfo
    member: PersonInfo
    offer: OfferInfo

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class GuestInvited(_Notification):
    """A member invited a guest with Slack to a call (#38): one direct message to the guest,
    with In and Out buttons that name the run, like the reminder's."""

    kind: ClassVar[str] = "guest_invited"
    event: EventInfo
    member: PersonInfo
    invited_by: PersonInfo

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}"

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class GuestJoined:
    """Someone joined from the channel as a guest (#38 addendum): one direct message to each
    active admin (``admin``), with **Make member** and **Remove**. It's about the household, not
    an event, like ``ScheduleChanged``. ``via`` is ``slack`` or ``web``."""

    kind: ClassVar[str] = "guest_joined"
    is_update: ClassVar[bool] = False
    needs_thread: ClassVar[bool] = False
    household_id: int
    admin: PersonInfo
    guest: PersonInfo
    via: str
    unlinked: tuple[PersonInfo, ...] = ()
    """Members, not admins, with no Slack account yet, who the guest might really be."""
    slack_user_id: str = ""
    slack_email: str | None = None
    """The email Slack shares for the guest, so the admin can tell who they really are: anyone
    can take any display name."""
    slack_guest_account: bool = False

    @property
    def idempotency_key(self) -> str:
        return f"join:{self.guest.id}:admin{self.admin.id}"

    @property
    def message_key(self) -> str | None:
        return None

    @property
    def thread_key(self) -> str | None:
        return None

    @property
    def recipient_id(self) -> int | None:
        return self.admin.id

    @property
    def event_id(self) -> int | None:
        return None


@dataclass(frozen=True, slots=True)
class RoleVacated(_Notification):
    """A guest left a decided call (#38: an admin removed them) while assigned to cook, pick up,
    or order: one direct message to ``member``, the coordinator or, with none, each admin, to
    pick someone else. ``left`` is the guest; ``roles`` are the ones nobody has now."""

    kind: ClassVar[str] = "role_vacated"
    event: EventInfo
    member: PersonInfo
    left: PersonInfo
    roles: tuple[AssignmentRole, ...]

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}:left{self.left.id}"

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class LinkNeeded:
    """A Slack account Dinnerbell can't link on its own (#38): one direct message to each
    active admin (``admin``). ``member`` holds the email the Slack account has; ``reason`` is
    ``join`` (they tried to join from the channel) or ``hourly`` (the email match found the Slack
    account linked to someone else already). About the household, like ``GuestJoined``."""

    kind: ClassVar[str] = "link_needed"
    is_update: ClassVar[bool] = False
    needs_thread: ClassVar[bool] = False
    household_id: int
    admin: PersonInfo
    member: PersonInfo
    slack_user_id: str
    slack_name: str
    reason: str
    slack_email: str | None = None
    """The email that matched: ``member``'s, which the Slack account has too."""
    member_is_admin: bool = False
    """An admin sets their own Slack member ID; a guest is never linked to one."""

    @property
    def idempotency_key(self) -> str:
        return f"link:{self.reason}:{self.member.id}:{self.slack_user_id}:admin{self.admin.id}"

    @property
    def message_key(self) -> str | None:
        return None

    @property
    def thread_key(self) -> str | None:
        return None

    @property
    def recipient_id(self) -> int | None:
        return self.admin.id

    @property
    def event_id(self) -> int | None:
        return None


Notification = Union[  # noqa: UP007 - a runtime union, used for isinstance checks and payload decoding
    CallOpened,
    CallUpdated,
    ResponseReminder,
    MaybeResolved,
    ProposalsReady,
    ProposalsUpdated,
    DecisionMade,
    DecisionUpdated,
    AssignmentMade,
    CallAbandoned,
    EventCancelled,
    AssignmentCancelled,
    StartedCallCancelled,
    ScheduleChanged,
    CallDiscarded,
    FeedbackReminder,
    OfferPosted,
    OfferUpdated,
    OfferFull,
    GuestInvited,
    GuestJoined,
    RoleVacated,
    LinkNeeded,
]
NOTIFICATION_TYPES: dict[str, type[Notification]] = {cls.kind: cls for cls in get_args(Notification)}
UPDATE_KINDS = frozenset(kind for kind, cls in NOTIFICATION_TYPES.items() if cls.is_update)


CALL_OFF_SUPERSEDES = frozenset({AssignmentMade.kind, ResponseReminder.kind})
"""Direct messages a call-off makes wrong: "You're cooking" or "Are you in?" must never arrive
after "It's called off". The call-off closes these unsent, and the drainer checks again just
before sending one."""
SUPERSEDED_BY_CALL_OFF = "superseded by a call-off"


# Delivery -------------------------------------------------------------------------------------


NULL_CHANNEL_ID = "null"
"""The channel ID in ``NullChannel``'s made-up message references. Never a real channel's."""


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Where a channel put a message, for later updates and threading. Opaque to Dinnerbell."""

    channel_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ref: MessageRef | None


class Channel(Protocol):
    """A channel adapter (spec 13.4). ``channels/base.py`` re-exports it for adapters.

    ``deliver`` posts a new message: to the channel, or directly to ``recipient_id``. ``thread``
    is where to thread it, when the notification has a ``thread_key`` and that message exists.
    ``update`` edits the message at ``ref`` to show the notification's state. Either may raise;
    the outbox retries with backoff.
    """

    async def deliver(self, notification: Notification, *, thread: MessageRef | None = None) -> DeliveryResult: ...

    async def update(self, ref: MessageRef, notification: Notification) -> None: ...


# Payloads -------------------------------------------------------------------------------------


class PermanentDeliveryError(Exception):
    """Raised by a channel when retrying can't help, such as a deleted channel or a member who
    left the workspace. The outbox gives up on the row at once instead of retrying."""


class ChannelUnavailable(Exception):
    """The channel can't be reached right now: an outage, a timeout, or a rate limit. The
    drainer retries the row with backoff, like any failure, and ends its pass at once, so an
    outage can't tie up a drain for minutes with one timeout per row."""


class HeldDelivery(Exception):
    """The channel isn't ready for this notification yet, such as while its setup is broken.
    The drainer puts the row back without counting the attempt, and tries it again after
    ``retry_after`` seconds."""

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PayloadError(ValueError):
    """An outbox payload that doesn't decode to its notification type."""


def to_payload(notification: Notification) -> dict[str, Any]:
    """The notification as JSON-ready data. ``from_payload(kind, ...)`` reverses it."""
    encoded: dict[str, Any] = _encode(notification)
    return encoded


def from_payload(kind: str, payload: dict[str, Any]) -> Notification:
    cls = NOTIFICATION_TYPES.get(kind)
    if cls is None:
        raise PayloadError(f"unknown notification kind {kind!r}")
    try:
        decoded: Notification = _decode(cls, payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise PayloadError(f"bad {kind} payload: {exc}") from None
    return decoded


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_encode(v) for v in value]
    return value


_SCALARS: dict[Any, Any] = {
    datetime: datetime.fromisoformat,
    date: date.fromisoformat,
    time: time.fromisoformat,
}


def _optional(field: Any) -> bool:
    return field.default is not MISSING or field.default_factory is not MISSING


def _decode(tp: Any, data: Any) -> Any:
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        options = [a for a in get_args(tp) if a is not type(None)]
        return None if data is None else _decode(options[0], data)
    if origin is tuple:
        return tuple(_decode(get_args(tp)[0], item) for item in data)
    if isinstance(tp, type) and is_dataclass(tp):
        if not isinstance(data, dict):
            raise TypeError(f"expected an object for {tp.__name__}")
        hints = get_type_hints(tp)
        # A field added later has a default, so rows written before it still decode; a field
        # without one must be there (KeyError, so the row fails safe).
        values = {
            f.name: _decode(hints[f.name], data[f.name]) for f in fields(tp) if f.name in data or not _optional(f)
        }
        return tp(**values)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(data)
    if tp in _SCALARS:
        return _SCALARS[tp](data)
    if tp in (int, str, bool, float) and isinstance(data, tp):
        return data
    raise TypeError(f"expected {tp}, got {type(data).__name__}")
