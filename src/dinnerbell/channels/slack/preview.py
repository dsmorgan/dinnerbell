# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sample messages for ``dinnerbell slack preview``, and the cases the golden tests pin down.

Paste the printed JSON into Slack's Block Kit Builder (app.slack.com/block-kit-builder) to see
a message without a workspace. Samples use a fixed household: Alex (admin, coordinating), Sam
(cooks), Kim, and Lee. Lee has no Slack user, so Lee shows as a plain name, not a mention.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.blocks import Directory
from dinnerbell.channels.slack.messages import RenderContext, render
from dinnerbell.channels.slack.views import HomeLinks, details_modal, home_view, proposal_modal
from dinnerbell.domain import notifications as note
from dinnerbell.domain.catalog import CatalogItem, ItemKind, Mode, Tag, TagKind
from dinnerbell.domain.events import (
    AssignmentRole,
    EndReason,
    Event,
    EventKind,
    EventState,
    ProposalSource,
    Response,
    RoleOffer,
    Status,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.services.event_views import EventView, Permissions, ProposalView, TodayView

BASE_URL = "https://dinnerbell.example.com"
ZONE = "America/Chicago"
DAY = date(2026, 10, 22)

ALEX = note.PersonInfo(1, "Alex")
SAM = note.PersonInfo(2, "Sam")
KIM = note.PersonInfo(3, "Kim")
LEE = note.PersonInfo(4, "Lee")
DIRECTORY = Directory({1: "U01ALEX", 2: "U02SAM", 3: "U03KIM"})

TACOS = note.ItemInfo(11, "Sam's Tacos", ItemKind.HOME, cuisines=("Mexican",))
SIAM = note.ItemInfo(
    12,
    "Siam Garden",
    ItemKind.RESTAURANT,
    url="https://siam.example/menu",
    order_url="https://siam.example/order",
    cuisines=("Thai",),
)
TAQUERIA = note.ItemInfo(13, "Taqueria <Lupita> & Co", ItemKind.RESTAURANT, cuisines=("Mexican",))


def event(state: EventState = EventState.COLLECTING, *, coordinator: note.PersonInfo | None = None) -> note.EventInfo:
    return note.EventInfo(
        id=7,
        household_id=1,
        slot=Slot.DINNER,
        day=DAY,
        state=state,
        run=1,
        path="/events/7",
        timezone=ZONE,
        respond_deadline=datetime(2026, 10, 22, 21, 30, tzinfo=UTC),
        vote_deadline=datetime(2026, 10, 22, 22, 15, tzinfo=UTC) if state is not EventState.COLLECTING else None,
        coordinator=coordinator,
    )


ROSTER = (
    note.RosterEntry(ALEX, Status.IN, offers=(RoleOffer.PICKUP,)),
    note.RosterEntry(
        SAM,
        Status.IN,
        guests=1,
        guest_needs=("Vegetarian",),
        eat_from=time(18, 40),
        offers=(RoleOffer.COOK,),
        suggestions=("Sam's Tacos",),
        note="Home late, but I'll cook if it's easy.",
    ),
    note.RosterEntry(KIM, Status.MAYBE),
)
PROPOSALS = (
    note.ProposalInfo(
        21,
        1,
        TACOS,
        Mode.COOK,
        ("Suggested by Sam.", "New to the household."),
        ProposalSource.SYSTEM,
        voters=(ALEX, SAM),
    ),
    note.ProposalInfo(22, 2, SIAM, Mode.PICKUP, ("Alex can pick up.",), ProposalSource.SYSTEM, voters=(LEE,)),
    note.ProposalInfo(23, 3, TAQUERIA, Mode.DELIVERY, ("Added by Kim.",), ProposalSource.MEMBER, proposed_by=KIM),
)
DECISION = note.DecisionInfo(
    proposal=PROPOSALS[0],
    meal_time=datetime(2026, 10, 22, 23, 45, tzinfo=UTC),
    head_count=3,
    participants=(note.ParticipantInfo(ALEX), note.ParticipantInfo(SAM, guests=1)),
    assignments=(
        note.AssignmentInfo(AssignmentRole.COOK, SAM),
        note.AssignmentInfo(AssignmentRole.COORDINATOR, ALEX),
    ),
    dietary_notes=("Vegetarian",),
)
DELIVERY = replace(
    DECISION,
    proposal=note.ProposalInfo(22, 2, SIAM, Mode.DELIVERY, ("Lee can order.",), ProposalSource.SYSTEM, voters=(LEE,)),
    assignments=(note.AssignmentInfo(AssignmentRole.ORDER, LEE),),
    dietary_notes=(),
)
TWO_ROLES = replace(
    DECISION,
    assignments=(note.AssignmentInfo(AssignmentRole.COOK, SAM), note.AssignmentInfo(AssignmentRole.COORDINATOR, SAM)),
)
WAITING = (KIM, LEE)
VOTING = event(EventState.VOTING, coordinator=ALEX)
DECIDED = event(EventState.DECIDED, coordinator=ALEX)

NOTIFICATIONS: dict[str, Callable[[], note.Notification]] = {
    "call_opened": lambda: note.CallOpened(event(), (ALEX, KIM, LEE, SAM), waiting_on=(ALEX, KIM, LEE, SAM)),
    "call_updated": lambda: note.CallUpdated(event(), ROSTER, (LEE,), head_count=3),
    "call_updated_voting": lambda: note.CallUpdated(VOTING, ROSTER, (), head_count=3),
    "call_updated_decided": lambda: note.CallUpdated(DECIDED, ROSTER, (), head_count=3),
    "call_updated_done": lambda: note.CallUpdated(event(EventState.DONE, coordinator=ALEX), ROSTER, (), 3),
    "call_updated_abandoned": lambda: note.CallUpdated(event(EventState.ABANDONED), ROSTER[2:], (LEE,), 0),
    "call_updated_missed": lambda: note.CallUpdated(
        replace(event(EventState.ABANDONED), end_reason=EndReason.MISSED), ROSTER, (LEE,), 3
    ),
    "call_updated_cancelled": lambda: note.CallUpdated(event(EventState.CANCELLED), ROSTER, (LEE,), 3),
    "response_reminder": lambda: note.ResponseReminder(event(), KIM),
    "maybe_resolved": lambda: note.MaybeResolved(VOTING, KIM),
    "proposals_ready": lambda: note.ProposalsReady(VOTING, tuple(replace(p, voters=()) for p in PROPOSALS), 3),
    "proposals_updated": lambda: note.ProposalsUpdated(VOTING, PROPOSALS, 3),
    "proposals_updated_closed": lambda: note.ProposalsUpdated(DECIDED, PROPOSALS, 3),
    "decision_made": lambda: note.DecisionMade(DECIDED, DECISION, locked_by=ALEX),
    "decision_made_by_vote": lambda: note.DecisionMade(DECIDED, DELIVERY),
    "decision_updated": lambda: note.DecisionUpdated(DECIDED, DECISION, locked_by=ALEX),
    "decision_updated_done": lambda: note.DecisionUpdated(event(EventState.DONE, coordinator=ALEX), DECISION),
    "assignment_made": lambda: note.AssignmentMade(DECIDED, 31, SAM, (AssignmentRole.COOK,), DECISION),
    "assignment_made_two_roles": lambda: note.AssignmentMade(
        DECIDED, 33, SAM, (AssignmentRole.COOK, AssignmentRole.COORDINATOR), TWO_ROLES
    ),
    "assignment_made_order": lambda: note.AssignmentMade(DECIDED, 32, LEE, (AssignmentRole.ORDER,), DELIVERY),
    "call_abandoned": lambda: note.CallAbandoned(event(EventState.ABANDONED), EndReason.TOO_FEW, head_count=1),
    "call_abandoned_no_coverage": lambda: note.CallAbandoned(event(EventState.ABANDONED), EndReason.NO_COVERAGE, 3),
    "event_cancelled": lambda: note.EventCancelled(event(EventState.CANCELLED), (ALEX, SAM), cancelled_by=ALEX),
}


# Modals and the Home tab ---------------------------------------------------------------------------

TAGS = (Tag(41, 1, TagKind.DIETARY, "Vegetarian"), Tag(42, 1, TagKind.DIETARY, "Gluten-free"))
CUISINES = (Tag(43, 1, TagKind.CUISINE, "Mexican"), Tag(44, 1, TagKind.CUISINE, "Thai"))
ITEMS = (
    CatalogItem(11, 1, ItemKind.HOME, "Sam's Tacos", frozenset({Mode.COOK}), frozenset({43}), frozenset({2})),
    CatalogItem(12, 1, ItemKind.RESTAURANT, "Siam Garden", frozenset({Mode.PICKUP}), frozenset({44}), frozenset()),
)


def _event_view(state: EventState, mine: Response | None, can: Permissions) -> EventView:
    info = event(state, coordinator=ALEX)
    base = Event(
        id=7,
        household_id=1,
        kind=EventKind.CALL,
        slot=Slot.DINNER,
        day=DAY,
        state=state,
        run=1,
        opens_at=datetime(2026, 10, 22, 20, 0, tzinfo=UTC),
        respond_deadline=info.respond_deadline,
        reminder_minutes=15,
        vote_minutes=45,
        coordinator_id=ALEX.id,
        vote_deadline=info.vote_deadline,
    )
    return EventView(
        event=base,
        info=info,
        roster=ROSTER,
        waiting_on=(LEE,),
        head_count=3,
        mine=mine,
        my_suggestion_ids=(11,) if mine else (),
        my_suggestion_texts=("Pho",) if mine else (),
        proposals=tuple(ProposalView(p, voted=False) for p in PROPOSALS) if state is EventState.VOTING else (),
        decision=DECISION if state is EventState.DECIDED else None,
        can=can,
        members=(ALEX, KIM, LEE, SAM),
        dietary_tags=TAGS,
        cuisine_tags=CUISINES,
        items=ITEMS,
    )


SAM_RESPONSE = Response(
    member_id=SAM.id,
    status=Status.IN,
    guests=1,
    guest_needs=frozenset({41}),
    eat_from=time(18, 40),
    eat_until=time(19, 30),
    offers=frozenset({RoleOffer.COOK}),
    note="Home late.",
)
COLLECTING_VIEW = _event_view(EventState.COLLECTING, SAM_RESPONSE, Permissions(respond=True, claim=False))
VOTING_VIEW = _event_view(EventState.VOTING, SAM_RESPONSE, Permissions(vote=True, propose=True))
DECIDED_VIEW = _event_view(EventState.DECIDED, SAM_RESPONSE, Permissions(mark_done=True))
ORIGIN = ids.ViewOrigin(7, "C0DINNER")

VIEWS: dict[str, Callable[[], dict[str, Any]]] = {
    "details_modal": lambda: details_modal(COLLECTING_VIEW, ORIGIN),
    "details_modal_empty": lambda: details_modal(
        _event_view(EventState.COLLECTING, None, Permissions(respond=True)), ORIGIN
    ),
    "proposal_modal": lambda: proposal_modal(VOTING_VIEW, ORIGIN),
    "home": lambda: home_view(TodayView(DAY, [COLLECTING_VIEW], tuple(Slot)), HomeLinks(BASE_URL)),
    "home_decided": lambda: home_view(
        TodayView(DAY, [DECIDED_VIEW], tuple(Slot)), HomeLinks(BASE_URL), notice="Marked done. Thanks!"
    ),
    "home_empty": lambda: home_view(TodayView(DAY, [], tuple(Slot)), HomeLinks(BASE_URL)),
}

KINDS = (*NOTIFICATIONS, *VIEWS)


def preview(kind: str, base_url: str = BASE_URL) -> dict[str, Any]:
    """The Block Kit JSON for sample ``kind``: a message payload for notifications, or the view
    for a modal or the Home tab. Raises KeyError for an unknown kind."""
    if kind in VIEWS:
        return VIEWS[kind]()
    message = render(NOTIFICATIONS[kind](), RenderContext(base_url, DIRECTORY))
    return message.payload()
