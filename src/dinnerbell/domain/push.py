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
        # Direct messages spec 6.15 doesn't name. A call-off pushes to everyone in, so the
        # assignees' own call-off message would only repeat it.
        note.MaybeResolved.kind,
        note.AssignmentCancelled.kind,
        note.StartedCallCancelled.kind,
        note.PlaceChanged.kind,
        note.OfferFull.kind,
        note.LateJoinChanged.kind,
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
    if notification.recipient_id is not None:
        return (notification.recipient_id,)
    people: tuple[note.PersonInfo, ...] = ()
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
PUSH_HOSTS = frozenset({"fcm.googleapis.com", "updates.push.services.mozilla.com"})
"""Push services by exact host: Chrome and other Chromium browsers on Android and desktop (FCM),
and Firefox (Mozilla's autopush)."""
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


def check_subscription(endpoint: str, p256dh: str, auth: str) -> SubscriptionKeys:
    """What the browser sent, checked, or InvalidSubscription."""
    if not known_push_service(endpoint):
        raise InvalidSubscription(NOT_A_PUSH_SERVICE)
    try:
        point, secret = _b64decode(p256dh), _b64decode(auth)
    except ValueError:
        raise InvalidSubscription(BAD_KEYS) from None
    if len(point) != 65 or point[0] != 4 or len(secret) != 16:
        raise InvalidSubscription(BAD_KEYS)
    return SubscriptionKeys(endpoint, p256dh.rstrip("="), auth.rstrip("="))


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

    def payload(self, base_url: str) -> bytes:
        """Declarative Web Push (WebKit): iOS 18.4 and later shows it without the service worker,
        which reads the same ``notification`` on Android and older iOS."""
        navigate = base_url.rstrip("/") + self.path
        body = self.body
        while True:
            data = {
                "web_push": 8030,
                "notification": {"title": self.title, "body": body, "navigate": navigate, "lang": "en", "dir": "ltr"},
            }
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) <= MAX_PAYLOAD_BYTES or not body:
                return encoded
            body = body[: len(body) // 2].rstrip() + "…" if len(body) > 2 else ""


def _clock(at: datetime, zone: ZoneInfo, now: datetime) -> str:
    local = at.astimezone(zone)
    text = local.strftime("%I:%M %p").lstrip("0")
    return text if local.date() == now.astimezone(zone).date() else f"{local:%A} {text}"


def _roles(roles: tuple[AssignmentRole, ...]) -> str:
    words = [_ROLE_WORDS[r] for r in roles]
    return " and ".join(words) if len(words) <= 2 else ", ".join(words[:-1]) + ", and " + words[-1]


def message(notification: note.Notification, now: datetime) -> PushMessage:
    """What a push of ``notification`` says: the meal, the time, and what to do, and never
    another member's answer or anything a member wrote. A tap opens the call's page."""
    if isinstance(notification, note.GuestJoined):
        return PushMessage(
            "A guest joined", f"{notification.guest.name} joined. Keep or remove them.", "/admin/members"
        )
    if isinstance(
        notification, note.ScheduleChanged | note.BadgesEarned | note.MonthlyRecap | note.LinkNeeded
    ):  # pragma: no cover - never pushed
        raise ValueError(notification.kind)
    event = notification.event
    zone = ZoneInfo(event.timezone)
    meal = event.slot.value.capitalize()
    lower = event.slot.value

    def at(when: datetime | None) -> str:
        return _clock(when, zone, now) if when is not None else ""

    title, body = f"{meal}", "Open Dinnerbell to see what's next."
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
        who = "your guest is" if notification.via is not None else "you're"
        title = f"{meal}: {who} {_roles(notification.roles)}"
        body = f"{decision.proposal.item.name}, at {at(decision.meal_time)}."
    elif isinstance(notification, note.RolesToChoose):
        title, body = f"{meal}: choose who does it", f"Assign the roles by {at(notification.due_at)}."
    elif isinstance(notification, note.RoleVacated):
        title, body = f"{meal}: a role needs someone", f"Pick someone else for {_roles(notification.roles)}."
    elif isinstance(notification, note.CoordinatorPicked | note.CoordinatingGiven):
        title, body = f"{meal}: you're coordinating", "You can hand it off on the call's page."
    elif isinstance(notification, note.AssignmentEnded):
        who = "Your guest is" if notification.via is not None else "You're"
        title, body = f"{meal}: plans changed", f"{who} no longer {_roles(notification.roles)}."
    elif isinstance(notification, note.FeedbackReminder):
        title, body = f"How was {lower}?", f"Tell the household about {notification.item} by {notification.closes:%A}."
    return PushMessage(title, body, event.path)


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
    """RFC 8030 ``Topic``, shared by every push about one run of one call, so a newer one waiting
    at the push service replaces the older. None for a push about no call."""
    if event_id is None:
        return None
    digest = hashlib.sha256(f"dinnerbell:{household_id}:{event_id}:{run}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()[:TOPIC_LENGTH]


def topic_for(notification: note.Notification) -> str | None:
    event = getattr(notification, "event", None)
    if not isinstance(event, note.EventInfo) or notification.event_id is None:
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
