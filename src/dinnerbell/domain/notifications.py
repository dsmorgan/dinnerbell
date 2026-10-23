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
from dinnerbell.domain.events import AssignmentRole, EndReason, EventState, ProposalSource, RoleOffer, Status
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

    @property
    def idempotency_key(self) -> str:
        return f"{self.event.key}:{self.kind}:member{self.member.id}:{self.assignment_id}"

    @property
    def recipient_id(self) -> int | None:
        return self.member.id


@dataclass(frozen=True, slots=True)
class CallAbandoned(_Notification):
    """Abandoned: the on-your-own channel post (spec 5.4)."""

    kind: ClassVar[str] = "call_abandoned"
    event: EventInfo
    reason: EndReason
    head_count: int = 0


@dataclass(frozen=True, slots=True)
class EventCancelled(_Notification):
    """Cancelled by the admin: a post threaded under the call, for the participants."""

    kind: ClassVar[str] = "event_cancelled"
    thread_suffix: ClassVar[str | None] = "call"
    event: EventInfo
    participants: tuple[PersonInfo, ...] = ()
    cancelled_by: PersonInfo | None = None


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
]
NOTIFICATION_TYPES: dict[str, type[Notification]] = {cls.kind: cls for cls in get_args(Notification)}
UPDATE_KINDS = frozenset(kind for kind, cls in NOTIFICATION_TYPES.items() if cls.is_update)


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
