# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification types (domain/notifications.py): keys, audiences, and the outbox payload round trip."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from dinnerbell.domain import notifications as n
from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.events import AssignmentRole, EndReason, EventState, ProposalSource, RoleOffer, Status
from dinnerbell.domain.meals import Slot

WHEN = datetime(2026, 10, 22, 21, 30, tzinfo=UTC)
ALEX, SAM = n.PersonInfo(1, "Alex"), n.PersonInfo(2, "Sam")
EVENT = n.EventInfo(
    id=7,
    household_id=1,
    slot=Slot.DINNER,
    day=date(2026, 10, 22),
    state=EventState.VOTING,
    run=2,
    path="/events/7",
    timezone="America/Chicago",
    respond_deadline=WHEN,
    vote_deadline=WHEN,
    coordinator=ALEX,
)
ITEM = n.ItemInfo(3, "Siam Garden", ItemKind.RESTAURANT, "https://siam.example", None, ("Thai",))
PROPOSAL = n.ProposalInfo(4, 1, ITEM, Mode.PICKUP, ("Suggested by Sam.",), ProposalSource.SYSTEM, None, (ALEX, SAM))
FREE = n.ProposalInfo(
    5, 2, n.ItemInfo(None, "Pho Place", ItemKind.RESTAURANT), Mode.PICKUP, (), ProposalSource.MEMBER, SAM
)
ROSTER = (
    n.RosterEntry(ALEX, Status.IN, 2, ("Vegan",), time(18), time(20), (RoleOffer.PICKUP,), ("Tacos",), "late"),
    n.RosterEntry(SAM, Status.MAYBE),
)
DECISION = n.DecisionInfo(
    proposal=PROPOSAL,
    meal_time=WHEN,
    head_count=4,
    participants=(n.ParticipantInfo(ALEX, 2), n.ParticipantInfo(SAM)),
    assignments=(n.AssignmentInfo(AssignmentRole.PICKUP, SAM), n.AssignmentInfo(AssignmentRole.COORDINATOR, ALEX)),
    dietary_notes=("Vegan",),
)

SCHEDULE = n.SlotScheduleInfo(True, time(15), 90, 45, 15, time(18, 30), (Mode.PICKUP, Mode.DELIVERY))

EXAMPLES: list[tuple[n.Notification, str, str | None, str | None, int | None]] = [
    (n.CallOpened(EVENT, (ALEX, SAM), ROSTER, (SAM,), 3), "event:7:run2:call_opened", "event:7:run2:call", None, None),
    (n.CallUpdated(EVENT, ROSTER, (), 3), "event:7:run2:call_updated", "event:7:run2:call", None, None),
    (n.ResponseReminder(EVENT, SAM), "event:7:run2:response_reminder:member2", None, None, 2),
    (n.MaybeResolved(EVENT, SAM), "event:7:run2:maybe_resolved:member2", None, None, 2),
    (
        n.ProposalsReady(EVENT, (PROPOSAL, FREE), 3),
        "event:7:run2:proposals_ready",
        "event:7:run2:proposals",
        "event:7:run2:call",
        None,
    ),
    (n.ProposalsUpdated(EVENT, (PROPOSAL,), 3), "event:7:run2:proposals_updated", "event:7:run2:proposals", None, None),
    (
        n.DecisionMade(EVENT, DECISION, ALEX),
        "event:7:run2:decision_made",
        "event:7:run2:decision",
        "event:7:run2:call",
        None,
    ),
    (n.DecisionUpdated(EVENT, DECISION, ALEX), "event:7:run2:decision_updated", "event:7:run2:decision", None, None),
    (
        n.AssignmentMade(EVENT, 11, SAM, (AssignmentRole.PICKUP, AssignmentRole.COORDINATOR), DECISION),
        f"event:7:run2:assignment_made:member{SAM.id}:11",
        None,
        None,
        2,
    ),
    (n.CallAbandoned(EVENT, EndReason.TOO_FEW, 1), "event:7:run2:call_abandoned", None, None, None),
    (n.EventCancelled(EVENT, (ALEX,), SAM), "event:7:run2:event_cancelled", None, "event:7:run2:call", None),
    (
        n.EventCancelled(EVENT, (ALEX,), SAM, "Pizza at Grandma's", (ALEX, SAM)),
        "event:7:run2:event_cancelled",
        None,
        "event:7:run2:call",
        None,
    ),
    (
        n.AssignmentCancelled(EVENT, SAM, (AssignmentRole.PICKUP,), DECISION, ALEX, "Snow day"),
        f"event:7:run2:assignment_cancelled:member{SAM.id}",
        None,
        None,
        2,
    ),
    (
        n.StartedCallCancelled(replace(EVENT, ended_by=ALEX, end_note="Away"), SAM, ALEX),
        f"event:7:run2:started_call_cancelled:member{SAM.id}",
        None,
        None,
        2,
    ),
    (
        n.ScheduleChanged(
            household_id=1,
            change_id="abc123",
            slot=Slot.DINNER,
            changed_by=SAM,
            before=SCHEDULE,
            after=replace(SCHEDULE, call_time=time(14), modes=(Mode.COOK,)),
            starts_on=date(2026, 10, 23),
            path="/schedule",
        ),
        "schedule:dinner:abc123",
        None,
        None,
        None,
    ),
    (n.CallDiscarded(EVENT, "event:7:run1:call"), "event:7:run1:call:call_discarded", "event:7:run1:call", None, None),
    (
        n.FeedbackReminder(replace(EVENT, state=EventState.DONE), SAM, "Siam Garden", date(2026, 10, 29)),
        f"event:7:run2:feedback_reminder:member{SAM.id}",
        None,
        None,
        2,
    ),
]


def test_every_notification_type_has_an_example() -> None:
    assert {type(example[0]) for example in EXAMPLES} == set(n.NOTIFICATION_TYPES.values())
    assert {"call_updated", "proposals_updated", "decision_updated", "call_discarded"} == n.UPDATE_KINDS


@pytest.mark.parametrize(("notification", "key", "message", "thread", "recipient"), EXAMPLES)
def test_keys_and_audience(
    notification: n.Notification, key: str, message: str | None, thread: str | None, recipient: int | None
) -> None:
    assert notification.idempotency_key == key
    assert notification.message_key == message
    assert notification.thread_key == thread
    assert notification.recipient_id == recipient


@pytest.mark.parametrize("notification", [example[0] for example in EXAMPLES])
def test_payload_round_trips_through_json(notification: n.Notification) -> None:
    import json

    payload = json.loads(json.dumps(n.to_payload(notification)))
    assert n.from_payload(notification.kind, payload) == notification


def test_only_a_call_off_needs_its_thread() -> None:
    """The outbox drops a reply whose call message never landed only for a call-off: the other
    threaded posts still mean something at the top level."""
    assert {kind for kind, cls in n.NOTIFICATION_TYPES.items() if cls.needs_thread} == {"event_cancelled"}


def test_a_call_off_queued_before_note_and_audience_still_decodes() -> None:
    old = n.to_payload(n.EventCancelled(EVENT, (ALEX,), SAM))
    del old["note"], old["audience"]
    del old["event"]["ended_by"], old["event"]["end_note"]
    assert n.from_payload("event_cancelled", old) == n.EventCancelled(EVENT, (ALEX,), SAM)


def test_payload_is_plain_domain_data() -> None:
    payload = n.to_payload(n.DecisionMade(EVENT, DECISION))
    assert payload["event"]["path"] == "/events/7"
    assert payload["decision"]["meal_time"] == "2026-10-22T21:30:00+00:00"
    assert payload["decision"]["proposal"]["item"]["url"] == "https://siam.example"
    assert payload["decision"]["proposal"]["voters"] == [{"id": 1, "name": "Alex"}, {"id": 2, "name": "Sam"}]
    assert PROPOSAL.votes == 2


@pytest.mark.parametrize(
    ("kind", "payload", "message"),
    [
        ("no_such_kind", {}, "unknown notification kind"),
        ("maybe_resolved", {"event": n.to_payload(n.MaybeResolved(EVENT, SAM))["event"]}, "bad maybe_resolved"),
        ("call_abandoned", n.to_payload(n.CallAbandoned(EVENT, EndReason.TOO_FEW)) | {"reason": "bored"}, "bad"),
        ("call_abandoned", n.to_payload(n.CallAbandoned(EVENT, EndReason.TOO_FEW)) | {"head_count": "2"}, "bad"),
    ],
)
def test_bad_payloads_raise(kind: str, payload: dict[str, Any], message: str) -> None:
    with pytest.raises(n.PayloadError, match=message):
        n.from_payload(kind, payload)
