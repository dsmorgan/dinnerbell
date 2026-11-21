# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web push (FR-53, ADR 0010, spec 6.15): which notifications push and to whom, what a push
says, and the rules for subscriptions and the push service's answers. Pure: the encryption, the
VAPID signature, and the HTTP call are in ``channels.webpush``; the queue is in ``db.push``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import AssignmentRole, EventState
from dinnerbell.domain.members import Member, NotifyVia

# What pushes ----------------------------------------------------------------------------------

DIRECT_PUSHES = frozenset(
    {
        note.RolesToChoose.kind,  # role requests
        note.RoleVacated.kind,
        note.AssignmentMade.kind,  # assignments
        note.CoordinatorPicked.kind,
        note.CoordinatingGiven.kind,  # hand-offs
        note.AssignmentEnded.kind,
        note.ResponseReminder.kind,  # reminders
        note.FeedbackReminder.kind,
        note.GuestJoined.kind,  # the admin's guest-join notices
        note.PlaceChanged.kind,  # changes to the member's own plan
        note.MaybeResolved.kind,
        note.LateJoinChanged.kind,
        note.StartedCallCancelled.kind,  # call-offs that tell the member themselves
        note.AssignmentCancelled.kind,
    }
)
"""Direct messages that push to a member on **Web app** or **Both** (spec 6.15, "What pushes")."""
CHANNEL_PUSHES = frozenset(
    {note.CallOpened.kind, note.ProposalsReady.kind, note.DecisionMade.kind, note.EventCancelled.kind}
)
"""Channel posts that push to the web-app members they concern."""
PUSHED_KINDS = DIRECT_PUSHES | CHANNEL_PUSHES
NOT_PUSHED_KINDS = frozenset(
    {
        # Edits to Slack messages.
        note.CallUpdated.kind,
        note.ProposalsUpdated.kind,
        note.DecisionUpdated.kind,
        note.OfferUpdated.kind,
        note.CallDiscarded.kind,
        # Badge posts and the recap.
        note.BadgesEarned.kind,
        note.MonthlyRecap.kind,
        # Channel posts nobody acts on from a phone, or that spec 6.15 doesn't name.
        note.CallAbandoned.kind,
        note.OfferPosted.kind,
        note.PicksShuffled.kind,
        note.ScheduleChanged.kind,
        # Direct messages about offers, which aren't calls a phone acts on.
        note.OfferFull.kind,
        note.LinkNeeded.kind,  # about Slack accounts
        note.GuestInvited.kind,  # to a guest, and guests never get pushes
    }
)
"""Every other kind, named so that a new kind has to be classified on purpose (a test checks)."""
REMINDER_KINDS = frozenset({note.ResponseReminder.kind, note.FeedbackReminder.kind})
"""Pushes that ``mute_dm_reminders`` turns off, as it does their Slack messages (FR-11, FR-48)."""
URGENT_KINDS = frozenset({note.CallOpened.kind})


def pushes(kind: str) -> bool:
    return kind in PUSHED_KINDS


def audience(notification: note.Notification) -> tuple[int, ...]:
    """The members a pushed ``notification`` concerns, before their choices are checked: the
    direct message's recipient (for a guest's, the member answering for them), or a channel
    post's members at the moment it was queued. A call opening: every member it invites. Voting
    opening and the decision: everyone in. A call off: everyone in or maybe, and the coordinator,
    but not whoever called it off."""
    if notification.kind not in PUSHED_KINDS:
        return ()
    if notification.kind in DIRECT_PUSHES:
        recipient = notification.recipient_id
        if recipient is None:  # pragma: no cover - every direct message has a recipient
            raise ValueError(f"{notification.kind} has no recipient")
        return (recipient,)
    people: tuple[note.PersonInfo, ...]
    if isinstance(notification, note.CallOpened):
        people = notification.mentions
    elif isinstance(notification, note.ProposalsReady):
        people = notification.participants
    elif isinstance(notification, note.DecisionMade):
        people = tuple(p.member for p in notification.decision.participants)
    elif isinstance(notification, note.EventCancelled):
        by = notification.cancelled_by.id if notification.cancelled_by else None
        everyone = notification.audience if notification.audience is not None else notification.participants
        people = tuple(p for p in everyone if p.id != by)
    else:  # pragma: no cover - CHANNEL_PUSHES names only the four above
        raise ValueError(f"no audience for {notification.kind}")
    return tuple(dict.fromkeys(p.id for p in people))


def wants_push(member: Member, kind: str) -> bool:
    """Whether ``member`` gets a push of ``kind`` while the web app is on: an active member or
    admin, never a guest, on **Web app** or **Both**, who hasn't muted it if it's a reminder."""
    if not member.is_member or member.delivery not in (NotifyVia.WEB, NotifyVia.BOTH):
        return False
    return not (kind in REMINDER_KINDS and member.mute_dm_reminders)


# Staleness ------------------------------------------------------------------------------------

CHANNEL_STEPS: dict[str, frozenset[EventState]] = {
    note.CallOpened.kind: frozenset({EventState.COLLECTING}),
    note.ProposalsReady.kind: frozenset({EventState.VOTING}),
    note.DecisionMade.kind: frozenset({EventState.DECIDED}),
    note.EventCancelled.kind: frozenset({EventState.CANCELLED}),
}
"""The states a channel post's push still makes sense in: "Are you in?" once the call went to a
vote, or "It's decided" once it was called off, only confuses."""
MOVED_ON = "superseded: the call moved on"
STARTED_AGAIN = "superseded: the call started again"


def channel_post_stale(kind: str, event: note.EventInfo, state: EventState | None, run: int | None) -> str | None:
    """Why a channel post's push about ``event`` is no longer worth sending, given the call's
    current ``state`` and ``run`` (None when it's gone), or None."""
    if state is None or run != event.run:
        return STARTED_AGAIN
    if state is EventState.CANCELLED and kind != note.EventCancelled.kind:
        return note.SUPERSEDED_BY_CALL_OFF
    steps = CHANNEL_STEPS.get(kind)
    return MOVED_ON if steps is not None and state not in steps else None


# Subscriptions --------------------------------------------------------------------------------

MAX_SUBSCRIPTIONS = 10
"""Devices per member. A new one past this replaces their oldest."""
MAX_ENDPOINT_LENGTH = 1024
PUSH_HOSTS = frozenset({"fcm.googleapis.com", "jmt17.google.com", "updates.push.services.mozilla.com"})
"""Push services by exact host: Chrome and other Chromium browsers on Android and desktop (FCM,
and ``jmt17.google.com``, which some Chrome builds have used), and Firefox (Mozilla's
autopush)."""
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")
"""Push services by domain: Safari on iPhone, iPad, and Mac (``web.push.apple.com``), and Edge
on Windows (WNS, such as ``wns2-par02p.notify.windows.com``)."""


class InvalidSubscription(ValueError):
    """A subscription Dinnerbell won't store. The message is written for the member."""


NOT_A_PUSH_SERVICE = "That browser's push service isn't one Dinnerbell knows. Try Safari, Chrome, Firefox, or Edge."


def known_push_service(endpoint: str) -> bool:
    """Whether ``endpoint`` is an HTTPS URL on a push service's host, on the default port, with
    no credentials, so a subscription can't make Dinnerbell post anywhere else (SSRF)."""
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH or not endpoint.isascii() or not endpoint.isprintable():
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.scheme != "https" or port not in (None, 443) or parts.username or parts.password or parts.fragment:
        return False
    if any(c in endpoint for c in " \\"):
        return False
    if host in PUSH_HOSTS:
        return True
    return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in PUSH_HOST_SUFFIXES)


def _b64decode(text: str) -> bytes:
    if not text or not all(c.isalnum() or c in "-_" for c in text.rstrip("=")):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text.rstrip("=") + "=" * (-len(text.rstrip("=")) % 4))


@dataclass(frozen=True, slots=True)
class SubscriptionKeys:
    endpoint: str
    p256dh: str
    """The browser's P-256 public key, an uncompressed point, base64url."""
    auth: str
    """The browser's 16-byte authentication secret, base64url."""


MAX_KEY_LENGTH = 200


def check_subscription(endpoint: str, p256dh: str, auth: str) -> SubscriptionKeys:
    """What the browser sent, checked, or InvalidSubscription: a known push service's endpoint,
    a P-256 public key that is a point on the curve, and a 16-byte authentication secret."""
    if not known_push_service(endpoint):
        raise InvalidSubscription(NOT_A_PUSH_SERVICE)
    if len(p256dh) > MAX_KEY_LENGTH or len(auth) > MAX_KEY_LENGTH:
        raise InvalidSubscription(BAD_KEYS)
    try:
        point, secret = _b64decode(p256dh), _b64decode(auth)
        if len(point) != 65 or point[0] != 4 or len(secret) != 16:
            raise ValueError("wrong length")
        if not on_p256(point):
            raise ValueError("not a point on P-256")
    except ValueError:
        raise InvalidSubscription(BAD_KEYS) from None
    return SubscriptionKeys(endpoint, p256dh.rstrip("="), auth.rstrip("="))


def endpoint_host(endpoint: str) -> str:
    """The host of an endpoint Dinnerbell refused, for its log line: never the path, which
    identifies the device. At most 100 characters, and "?" when there's none to read."""
    try:
        host = urlsplit(endpoint).hostname or ""
    except ValueError:
        host = ""
    host = "".join(c for c in host if c.isascii() and c.isprintable())[:100]
    return host or "?"


P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
"""P-256's field prime and curve constant b (SEC 2, FIPS 186-5); a is -3."""


def on_p256(point: bytes) -> bool:
    """Whether ``point`` is an uncompressed point on P-256: 0x04, then x and y below p, with
    y² ≡ x³ - 3x + b (mod p). Plain integers, so the domain needs no crypto library."""
    if len(point) != 65 or point[0] != 4:
        return False
    x, y = int.from_bytes(point[1:33], "big"), int.from_bytes(point[33:], "big")
    if not (0 <= x < P256_P and 0 <= y < P256_P):
        return False
    return (y * y - (x * x * x - 3 * x + P256_B)) % P256_P == 0


class PushTooLarge(ValueError):
    """A payload too big for one record. The device is fine: close the push, keep the device."""


class UndeliverableKeys(Exception):
    """A stored subscription's keys can't encrypt a push. Retrying can't help: delete it."""


BAD_KEYS = "The browser sent keys Dinnerbell can't use. Turn notifications off and on again."


@dataclass(frozen=True, slots=True)
class PushTarget:
    """A device to push to. The endpoint identifies the device: log ``subscription_id`` only."""

    subscription_id: int
    endpoint: str
    p256dh: str
    auth: str


# What a push says -----------------------------------------------------------------------------

MAX_PAYLOAD_BYTES = 3000
"""The JSON's limit, well inside one 4,096-byte aes128gcm record with its 86-byte header."""
MIN_TTL = timedelta(seconds=60)
MAX_TTL = timedelta(hours=24)
TOPIC_LENGTH = 24
"""Characters of base64url: 18 bytes of SHA-256, inside RFC 8030's 32."""

_ROLE_WORDS = {
    AssignmentRole.COOK: "cooking",
    AssignmentRole.PICKUP: "picking up",
    AssignmentRole.ORDER: "ordering",
    AssignmentRole.COORDINATOR: "coordinating",
}


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    path: str
    """The page a tap opens, relative to the base URL."""
    tag: str | None = None
    """For a channel post's push, the call's ``topic``: a newer one replaces it on the lock
    screen. None for a direct message, so a device keeps every one."""

    def payload(self, base_url: str) -> bytes:
        """Declarative Web Push (WebKit): iOS 18.4 and later shows it without the service worker,
        which reads the same ``notification`` on Android and older iOS."""
        navigate = base_url.rstrip("/") + self.path
        body = self.body
        while True:
            shown: dict[str, str | bool] = {
                "title": self.title,
                "body": body,
                "navigate": navigate,
                "lang": "en",
                "dir": "ltr",
            }
            if self.tag:
                shown["tag"] = self.tag
                shown["renotify"] = True  # replacing an earlier one still alerts
            encoded = json.dumps({"web_push": 8030, "notification": shown}, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode()) <= MAX_PAYLOAD_BYTES or not body:
                return encoded.encode()
            body = body[: len(body) // 2].rstrip() + "…" if len(body) > 2 else ""


def _clock(at: datetime, zone: ZoneInfo, now: datetime) -> str:
    local = at.astimezone(zone)
    text = local.strftime("%I:%M %p").lstrip("0")
    return text if local.date() == now.astimezone(zone).date() else f"{local:%A} {text}"


def _roles(roles: tuple[AssignmentRole, ...]) -> str:
    words = [_ROLE_WORDS[r] for r in roles]
    return " and ".join(words) if len(words) <= 2 else ", ".join(words[:-1]) + ", and " + words[-1]


_WAYS = {Mode.COOK: "Cooked at home", Mode.PICKUP: "Picked up", Mode.DELIVERY: "Ordered for delivery"}


def message(notification: note.Notification, now: datetime) -> PushMessage:
    """What a push of ``notification`` says: the meal, the time, and what to do, and never
    another member's answer or anything a member wrote. A tap opens the call's page. Every kind
    in ``PUSHED_KINDS`` has its own words; any other raises ValueError."""
    if isinstance(notification, note.GuestJoined):
        return PushMessage(
            "A guest joined", f"{notification.guest.name} joined. Keep or remove them.", "/admin/members"
        )
    if notification.kind not in PUSHED_KINDS or isinstance(
        notification, note.ScheduleChanged | note.BadgesEarned | note.MonthlyRecap | note.LinkNeeded
    ):
        raise ValueError(f"{notification.kind} doesn't push")
    event = notification.event
    zone = ZoneInfo(event.timezone)
    meal = event.slot.value.capitalize()
    lower = event.slot.value

    def at(when: datetime | None) -> str:
        return _clock(when, zone, now) if when is not None else ""

    def whose(via: note.PersonInfo | None) -> str:
        return "your guest is" if via is not None else "you're"

    if isinstance(notification, note.CallOpened | note.ResponseReminder):
        title, body = f"{meal}: are you in?", f"Answer by {at(event.respond_deadline)}."
    elif isinstance(notification, note.ProposalsReady):
        title = f"{meal}: time to vote"
        body = f"Voting closes at {at(event.vote_deadline)}." if event.vote_deadline else "Vote for a pick."
    elif isinstance(notification, note.DecisionMade):
        decision = notification.decision
        title, body = f"{meal} is decided", f"{decision.proposal.item.name}, at {at(decision.meal_time)}."
    elif isinstance(notification, note.EventCancelled):
        title, body = f"{meal} is called off", f"There's no {lower} call now."
    elif isinstance(notification, note.AssignmentMade):
        decision = notification.decision
        title = f"{meal}: {whose(notification.via)} {_roles(notification.roles)}"
        body = f"{decision.proposal.item.name}, at {at(decision.meal_time)}."
    elif isinstance(notification, note.RolesToChoose):
        title, body = f"{meal}: choose who does it", f"Assign the roles by {at(notification.due_at)}."
    elif isinstance(notification, note.RoleVacated):
        title, body = f"{meal}: a role needs someone", f"Pick someone else for {_roles(notification.roles)}."
    elif isinstance(notification, note.CoordinatorPicked | note.CoordinatingGiven):
        title, body = f"{meal}: you're coordinating", "You can hand it off on the call's page."
    elif isinstance(notification, note.AssignmentEnded):
        title = f"{meal}: plans changed"
        body = f"{whose(notification.via).capitalize()} no longer {_roles(notification.roles)}."
    elif isinstance(notification, note.FeedbackReminder):
        title, body = f"How was {lower}?", f"Tell the household about {notification.item} by {notification.closes:%A}."
    elif isinstance(notification, note.PlaceChanged):
        decision = notification.decision
        title = f"{meal} moved to {decision.proposal.item.name}"
        body = f"{_WAYS[decision.proposal.mode]}, at {at(decision.meal_time)}."
        if notification.ended_roles:
            body += f" {whose(notification.via).capitalize()} no longer {_roles(notification.ended_roles)}."
    elif isinstance(notification, note.MaybeResolved):
        title = f"{meal}: you're out for now"
        late = f" before {at(event.vote_deadline)}" if event.vote_deadline else ""
        body = f"Your maybe turned to out when answers closed. Join late{late} if you can make it."
    elif isinstance(notification, note.LateJoinChanged):
        if notification.taking_part:
            title, body = f"{meal}: you're back in", "The coordinator put you back in this call."
        else:
            title, body = f"{meal}: you're out this time", "The coordinator took you off this call."
    elif isinstance(notification, note.StartedCallCancelled):
        title, body = f"{meal} is called off", "Someone called off the call you started. You can start it again."
    elif isinstance(notification, note.AssignmentCancelled):
        title = f"{meal} is called off"
        body = f"{whose(notification.via).capitalize()} no longer {_roles(notification.roles)}."
    else:  # pragma: no cover - a test builds every pushed kind
        raise ValueError(f"no push text for {notification.kind}")
    tag = topic_for(notification)
    return PushMessage(title, body, event.path, tag)


def next_deadline(notification: note.Notification, now: datetime) -> datetime | None:
    """The call's next deadline after ``now``: its answers, its vote, a role choice, or the
    meal. None for a push about no call, or when every deadline has passed."""
    event = getattr(notification, "event", None)
    if not isinstance(event, note.EventInfo):
        return None
    times = [event.respond_deadline, event.vote_deadline, event.plan.meal_time if event.plan else None]
    decision = getattr(notification, "decision", None)
    if isinstance(decision, note.DecisionInfo):
        times.append(decision.meal_time)
    if isinstance(notification, note.RolesToChoose):
        times.append(notification.due_at)
    ahead = [t for t in times if t is not None and t > now]
    return min(ahead) if ahead else None


def ttl(notification: note.Notification, now: datetime) -> int:
    """Seconds the push service keeps the push for an offline device: until the call's next
    deadline, at least a minute, and at most a day (without a deadline, a day)."""
    deadline = next_deadline(notification, now)
    left = MAX_TTL if deadline is None else min(max(deadline - now, MIN_TTL), MAX_TTL)
    return int(left.total_seconds())


def urgency(kind: str) -> str:
    """RFC 8030 ``Urgency``: high for a call opening, which wakes a phone; normal otherwise."""
    return "high" if kind in URGENT_KINDS else "normal"


def topic(household_id: int, event_id: int | None, run: int) -> str | None:
    """RFC 8030 ``Topic`` for one run of one call: 24 characters of base64url. None for no call."""
    if event_id is None:
        return None
    digest = hashlib.sha256(f"dinnerbell:{household_id}:{event_id}:{run}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()[:TOPIC_LENGTH]


def topic_for(notification: note.Notification) -> str | None:
    """The ``Topic`` and notification ``tag`` of a channel post's push, shared by one call's
    opening, voting, decision, and call off, so a newer one replaces the older both at the push
    service and on the lock screen. None for a direct message, so an offline device keeps every
    one that asks something of the member."""
    if notification.kind not in CHANNEL_PUSHES:
        return None
    event = getattr(notification, "event", None)
    if not isinstance(event, note.EventInfo):  # pragma: no cover - every channel post has an event
        return None
    return topic(event.household_id, event.id, event.run)


# The push service's answer --------------------------------------------------------------------


class Outcome(StrEnum):
    SENT = "sent"
    GONE = "gone"
    """404 or 410: the subscription ended. Delete it now."""
    REJECTED = "rejected"
    """401 or 403: the push service refused our signature. Delete it after ``AUTH_STRIKES``."""
    FAILED = "failed"
    """413 or another 4xx: this push can't go. Close the row."""
    RETRY = "retry"
    """429 or 5xx: try again later, honoring Retry-After."""


AUTH_STRIKES = 3
AUTH_WINDOW = timedelta(hours=24)
AUTH_BURST = 3
"""Rejections in a household within ``AUTH_BURST_WINDOW`` that log an error and warn on Admin."""
AUTH_BURST_WINDOW = timedelta(hours=1)


def outcome(status: int) -> Outcome:
    if 200 <= status < 300:
        return Outcome.SENT
    if status in (404, 410):
        return Outcome.GONE
    if status in (401, 403):
        return Outcome.REJECTED
    if status == 429 or status >= 500:
        return Outcome.RETRY
    return Outcome.FAILED


@dataclass(frozen=True, slots=True)
class PushResponse:
    status: int
    retry_after: float | None = None
    """Seconds the push service asked to wait (Retry-After), if any."""


class PushSender(Protocol):
    """Sends one encrypted push (RFC 8291, RFC 8030, RFC 8292). ``channels.webpush`` has the
    real one. Raises on a network failure or a timeout; any HTTP answer is a ``PushResponse``."""

    async def send(
        self, target: PushTarget, payload: bytes, *, ttl: int, urgency: str, topic: str | None
    ) -> PushResponse: ...
