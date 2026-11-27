# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin > Health (#339): the notifications that failed, with their reasons, and the audit log
(SEC-9), for an admin without a shell. Read only. What /healthz reports comes from
``services.health.check_health``; this module adds what /healthz leaves out because it names
people."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.events import EventRepository
from dinnerbell.db.outbox import FailedEntry, OutboxRepository
from dinnerbell.db.push import ClosedPush, PushRepository
from dinnerbell.db.repositories import AuditEntry, AuditRepository, MemberRepository
from dinnerbell.domain.events import Event
from dinnerbell.domain.members import Member
from dinnerbell.domain.notifications import PayloadError, from_payload
from dinnerbell.services.common import require_admin
from dinnerbell.services.push import ROUTINE_CLOSES

LIMIT = 50
"""How many failed notifications, and how many audit rows, the page lists."""
ADMINS_ONLY = "Only household admins can see Dinnerbell's health."

KIND_WORDS = {
    "event_cancelled": "Call called off",
    "response_reminder": "Reminder to answer",
    "link_needed": "Slack link needed",
    "badges_earned": "Badges earned",
}
"""Notification kinds whose plain words aren't their names with spaces."""

SUBJECTS = {
    "auth": "Sign-in",
    "bootstrap": "Setup",
    "catalog": "Catalog",
    "event": "Call",
    "guest": "Guest",
    "history": "History",
    "household": "Household",
    "instance": "Instance",
    "meal": "Meal",
    "member": "Member",
}
ACTION_WORDS = {
    "auth.sign_in_refused": "Sign-in refused",
    "event.cancelled": "Call called off",
    "event.everyone_answered": "Everyone answered",
    "event.voting": "Voting opened",
    "member.admin_added": "Admin added from the host",
}
"""Audit actions whose plain words aren't their subject and verb with spaces."""

HOST_ACTIONS = frozenset({"member.admin_added", "history.reset", "instance.scrubbed"})
"""Actions someone runs with a shell on the host: ``dinnerbell admin add``, ``reset-history``,
and ``scrub-test``. Other rows without an actor are Dinnerbell's own, such as the scheduler's."""


@dataclass(frozen=True, slots=True)
class FailedNotification:
    at: datetime
    via: str
    """"Slack" for the outbox, "Web push" for the push queue."""
    kind: str
    """The kind in plain words."""
    recipient: str
    """Who it was for: a member's name, "The channel", or a placeholder."""
    reason: str
    event_id: int | None
    event: Event | None
    """The call it was about, if it still exists: the page names it by slot and day."""


@dataclass(frozen=True, slots=True)
class AuditLine:
    at: datetime
    who: str
    action: str
    """The action as stored, such as ``member.invited``, for searching the logs."""
    words: str
    """The action in plain words, such as "Member invited"."""
    target: str | None
    """What it was done to, in words, or None."""
    target_path: str | None
    """A page about the target, when there's one."""
    event: Event | None
    """The call the row is about, if it still exists: the page names it by slot and day."""


@dataclass(frozen=True, slots=True)
class HealthDetails:
    failed: list[FailedNotification]
    audit: list[AuditLine]


async def health_details(db: AsyncSession, actor: Member) -> HealthDetails:
    """The newest ``LIMIT`` failed notifications and audit rows of the actor's household, newest
    first. Forbidden unless the actor is an admin."""
    require_admin(actor, ADMINS_ONLY)
    household_id = actor.household_id
    names = {m.id: m.display_name for m in await MemberRepository(db).list(household_id)}
    outbox = await OutboxRepository(db).recent_failures(household_id, LIMIT)
    pushes = await PushRepository(db).recent_closed(household_id, LIMIT, except_reasons=ROUTINE_CLOSES)
    failed = [_from_outbox(row, names) for row in outbox] + [_from_push(row, names) for row in pushes]
    failed = sorted(failed, key=lambda f: f.at, reverse=True)[:LIMIT]
    rows = await AuditRepository(db).recent(household_id, LIMIT)
    call_ids = {f.event_id for f in failed if f.event_id is not None} | _ids(rows, "event")
    events = await _events(db, household_id, call_ids)
    failed = [replace(f, event=events.get(f.event_id)) if f.event_id is not None else f for f in failed]
    audit = await _audit_lines(db, household_id, rows, names, events)
    return HealthDetails(failed, audit)


async def _events(db: AsyncSession, household_id: int, event_ids: set[int]) -> dict[int, Event]:
    """The calls that still exist, by ID: one primary-key read each, at most twice ``LIMIT``."""
    repo = EventRepository(db)
    found: dict[int, Event] = {}
    for event_id in sorted(event_ids):
        event = await repo.get(household_id, event_id)
        if event is not None:
            found[event_id] = event
    return found


def kind_words(kind: str) -> str:
    return KIND_WORDS.get(kind) or kind.replace("_", " ").capitalize()


def action_words(action: str) -> str:
    if action in ACTION_WORDS:
        return ACTION_WORDS[action]
    subject, _, verb = action.partition(".")
    if subject not in SUBJECTS or not verb:
        return action
    words = " ".join("Slack" if word == "slack" else word for word in verb.split("_"))
    return f"{SUBJECTS[subject]} {words}"


def _name(names: dict[int, str], member_id: int | None) -> str:
    if member_id is None:
        return "Unknown"
    return names.get(member_id, "A former member")


def _from_outbox(row: FailedEntry, names: dict[int, str]) -> FailedNotification:
    try:
        recipient_id: int | None = getattr(from_payload(row.kind, row.payload), "recipient_id", None)
        recipient = "The channel" if recipient_id is None else _name(names, recipient_id)
    except PayloadError:  # the reason it failed, often: the page still lists it
        recipient = "Unknown"
    reason = row.reason or "No reason recorded"
    return FailedNotification(row.failed_at, "Slack", kind_words(row.kind), recipient, reason, row.event_id, None)


def _from_push(row: ClosedPush, names: dict[int, str]) -> FailedNotification:
    reason = row.reason or "No reason recorded"
    return FailedNotification(
        row.closed_at, "Web push", kind_words(row.kind), _name(names, row.member_id), reason, row.event_id, None
    )


def _ids(rows: list[AuditEntry], kind: str) -> set[int]:
    found: set[int] = set()
    for row in rows:
        prefix, _, value = (row.target or "").partition(":")
        if prefix == kind and value.isdigit():
            found.add(int(value))
    return found


async def _audit_lines(
    db: AsyncSession, household_id: int, rows: list[AuditEntry], names: dict[int, str], events: dict[int, Event]
) -> list[AuditLine]:
    """Each row with its actor and target in words. One query for the items' and tags' names,
    so the cost stays with the ``LIMIT``."""
    catalog = await CatalogRepository(db).names(household_id, _ids(rows, "item"), _ids(rows, "tag"))
    lines = []
    for row in rows:
        if row.actor_member_id is not None:
            who = _name(names, row.actor_member_id)
        else:
            who = "The host" if row.action in HOST_ACTIONS else "Dinnerbell"
        target, path, event = _target(row.target, names, events, catalog.items, catalog.tags)
        lines.append(AuditLine(row.created_at, who, row.action, action_words(row.action), target, path, event))
    return lines


def _target(
    target: str | None,
    names: dict[int, str],
    events: dict[int, Event],
    items: dict[int, str],
    tags: dict[int, str],
) -> tuple[str | None, str | None, Event | None]:
    """(words, page, call) for a stored target such as ``member:12``. Never anything but the
    target itself: the row's payload isn't read."""
    if not target:
        return None, None, None
    kind, _, value = target.partition(":")
    number = int(value) if value.isdigit() else None
    if kind == "household":
        return "The household", None, None
    if kind == "slack":
        return ("A Slack account" if value == "redacted" else f"Slack user {value}"), None, None
    if number is None:
        return target, None, None
    if kind == "member":
        return _name(names, number), (f"/admin/members/{number}" if number in names else None), None
    if kind == "event":
        event = events.get(number)
        return (f"Call {number}" if event is None else None), (event.path if event else None), event
    if kind == "item":
        name = items.get(number)
        return name or f"Catalog item {number}", (f"/catalog/items/{number}" if name else None), None
    if kind == "tag":
        return tags.get(number) or f"Tag {number}", None, None
    if kind == "meal":
        return f"Meal {number}", None, None
    return target, None, None
