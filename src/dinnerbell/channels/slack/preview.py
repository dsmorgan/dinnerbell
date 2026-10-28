# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sample messages for ``dinnerbell slack preview``, and the cases the golden tests pin down.

Paste the printed JSON into Slack's Block Kit Builder (app.slack.com/block-kit-builder) to see
a message without a workspace. Samples use a fixed household: Alex (admin, coordinating), Sam
(cooks), Kim, and Lee. Lee has no Slack user, so Lee shows as a plain name, not a mention.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.blocks import Directory, with_version_footer
from dinnerbell.channels.slack.messages import (
    CardGuest,
    RenderContext,
    posted_reply,
    reminder_message,
    render,
    schedule_reply,
    started_reply,
    your_answer,
)
from dinnerbell.channels.slack.views import (
    HomeLinks,
    cancel_modal,
    close_modal,
    details_modal,
    feedback_modal,
    home_view,
    invite_modal,
    log_modal,
    offer_modal,
    proposal_modal,
)
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
    Window,
)
from dinnerbell.domain.feedback import FeedbackInput, MealFacts, PlaceIssue, Rating
from dinnerbell.domain.meals import MemberRef, Slot
from dinnerbell.domain.members import Capabilities, Member, Role
from dinnerbell.domain.schedule import DEFAULT_SLOTS
from dinnerbell.services.event_views import EventView, Permissions, ProposalView, TodayView
from dinnerbell.services.feedback import Awaiting, FeedbackForm
from dinnerbell.services.meals import LogForm
from dinnerbell.services.offers import OfferForm, SlotDefaults

BASE_URL = "https://dinnerbell.example.com"
ZONE = "America/Chicago"
DAY = date(2026, 10, 22)

ALEX = note.PersonInfo(1, "Alex")
SAM = note.PersonInfo(2, "Sam")
KIM = note.PersonInfo(3, "Kim")
LEE = note.PersonInfo(4, "Lee")
PAT = note.PersonInfo(9, "Pat")
"""A guest (#38)."""
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
REASON = "Pizza at Grandma's"
CANCELLED = replace(event(EventState.CANCELLED, coordinator=ALEX), ended_by=ALEX, end_note=REASON)
EARLY = replace(
    event(), day=date(2026, 10, 23), respond_deadline=datetime(2026, 10, 23, 21, 30, tzinfo=UTC), early=True
)
"""Posted the evening before its day (issue #59)."""

LUNCH_OFFER = replace(
    event(),
    id=9,
    slot=Slot.LUNCH,
    path="/events/9",
    respond_deadline=datetime(2026, 10, 22, 17, 30, tzinfo=UTC),
    kind=EventKind.OFFER,
)
"""Alex's lunch offer: picking up Siam Garden at 12:30 PM, with 3 servings spare."""
OFFER = note.OfferInfo(ALEX, SIAM, Mode.PICKUP, datetime(2026, 10, 22, 17, 30, tzinfo=UTC), capacity=3, taken=2)
OFFER_ROSTER = (
    note.RosterEntry(ALEX, Status.IN),
    note.RosterEntry(SAM, Status.IN, guests=1, guest_needs=("Vegetarian",)),
)
FULL_OFFER = replace(OFFER, taken=3, dietary_notes=("Vegetarian",))
FULL_ROSTER = (*OFFER_ROSTER, note.RosterEntry(KIM, Status.IN))

# Joining late (#76): Lee joined late and takes part; Kim joined late, and Alex took her off.
LATE_ROSTER = (
    *ROSTER[:2],
    note.RosterEntry(LEE, Status.IN, offers=(RoleOffer.ORDER,), late=True),
    note.RosterEntry(KIM, Status.OUT, late=True),
)
LATE_JOINERS = (note.LateJoinerInfo(KIM, taking_part=False), note.LateJoinerInfo(LEE))
LATE_PROPOSALS = (
    replace(PROPOSALS[0], voters=(ALEX, LEE), uncounted=(LEE.id,)),
    replace(
        PROPOSALS[1],
        voters=(SAM,),
        warnings=("⚠ A guest of someone who joined late needs Vegetarian, and this isn't marked that way.",),
    ),
    PROPOSALS[2],
)
LATE_DECISION = replace(
    DECISION,
    proposal=replace(
        DECISION.proposal,
        warnings=("⚠ A guest of someone who joined late needs Vegetarian, and this isn't marked that way.",),
    ),
    head_count=4,
    participants=(*DECISION.participants, note.ParticipantInfo(LEE, late=True)),
)

DINNER_SCHEDULE = note.SlotScheduleInfo(
    enabled=True,
    call_time=time(15, 0),
    response_minutes=90,
    vote_minutes=45,
    reminder_minutes=15,
    meal_time=time(18, 30),
    modes=(Mode.PICKUP, Mode.DELIVERY),
)

NOTIFICATIONS: dict[str, Callable[[], note.Notification]] = {
    "call_opened": lambda: note.CallOpened(event(), (ALEX, KIM, LEE, SAM), waiting_on=(ALEX, KIM, LEE, SAM)),
    "call_opened_early": lambda: note.CallOpened(EARLY, (ALEX, KIM, LEE, SAM), waiting_on=(ALEX, KIM, LEE, SAM)),
    "call_updated": lambda: note.CallUpdated(event(), ROSTER, (LEE,), head_count=3),
    "call_updated_voting": lambda: note.CallUpdated(VOTING, ROSTER, (), head_count=3),
    "call_updated_voting_no_coordinator": lambda: note.CallUpdated(event(EventState.VOTING), ROSTER, (), head_count=3),
    "call_updated_decided": lambda: note.CallUpdated(DECIDED, ROSTER, (), head_count=3),
    "call_updated_done": lambda: note.CallUpdated(event(EventState.DONE, coordinator=ALEX), ROSTER, (), 3),
    "call_updated_abandoned": lambda: note.CallUpdated(event(EventState.ABANDONED), ROSTER[2:], (LEE,), 0),
    "call_updated_missed": lambda: note.CallUpdated(
        replace(event(EventState.ABANDONED), end_reason=EndReason.MISSED), ROSTER, (LEE,), 3
    ),
    "call_updated_cancelled": lambda: note.CallUpdated(CANCELLED, ROSTER, (LEE,), 3),
    "response_reminder": lambda: note.ResponseReminder(event(), KIM),
    "maybe_resolved": lambda: note.MaybeResolved(VOTING, KIM),
    "proposals_ready": lambda: note.ProposalsReady(VOTING, tuple(replace(p, voters=()) for p in PROPOSALS), 3),
    "proposals_updated": lambda: note.ProposalsUpdated(VOTING, PROPOSALS, 3),
    "proposals_updated_closed": lambda: note.ProposalsUpdated(DECIDED, PROPOSALS, 3),
    "decision_made": lambda: note.DecisionMade(DECIDED, DECISION, locked_by=ALEX),
    "decision_made_by_vote": lambda: note.DecisionMade(DECIDED, DELIVERY),
    "decision_updated": lambda: note.DecisionUpdated(DECIDED, DECISION, locked_by=ALEX),
    "decision_updated_cancelled": lambda: note.DecisionUpdated(CANCELLED, DECISION),
    "decision_updated_done": lambda: note.DecisionUpdated(event(EventState.DONE, coordinator=ALEX), DECISION),
    "assignment_made": lambda: note.AssignmentMade(DECIDED, 31, SAM, (AssignmentRole.COOK,), DECISION),
    "assignment_made_two_roles": lambda: note.AssignmentMade(
        DECIDED, 33, SAM, (AssignmentRole.COOK, AssignmentRole.COORDINATOR), TWO_ROLES
    ),
    "assignment_made_order": lambda: note.AssignmentMade(DECIDED, 32, LEE, (AssignmentRole.ORDER,), DELIVERY),
    "call_abandoned": lambda: note.CallAbandoned(event(EventState.ABANDONED), EndReason.TOO_FEW, head_count=1),
    "call_abandoned_no_coverage": lambda: note.CallAbandoned(event(EventState.ABANDONED), EndReason.NO_COVERAGE, 3),
    "call_abandoned_needs_unmet": lambda: note.CallAbandoned(
        event(EventState.ABANDONED), EndReason.NO_COVERAGE, 3, needs_unmet=True
    ),
    "event_cancelled": lambda: note.EventCancelled(
        CANCELLED, (ALEX, SAM), cancelled_by=ALEX, note=REASON, audience=(ALEX, KIM, SAM)
    ),
    "event_cancelled_breakfast": lambda: note.EventCancelled(
        replace(event(EventState.CANCELLED), slot=Slot.BREAKFAST), (ALEX, SAM), cancelled_by=ALEX, audience=(ALEX, SAM)
    ),
    "event_cancelled_no_reason": lambda: note.EventCancelled(
        event(EventState.CANCELLED), (ALEX, SAM), cancelled_by=ALEX, audience=(ALEX, KIM, LEE, SAM)
    ),
    "assignment_cancelled": lambda: note.AssignmentCancelled(
        CANCELLED, SAM, (AssignmentRole.COOK,), DECISION, cancelled_by=ALEX, note=REASON
    ),
    "started_call_cancelled": lambda: note.StartedCallCancelled(
        replace(event(EventState.CANCELLED), day=date(2026, 10, 23)), KIM, cancelled_by=ALEX
    ),
    "schedule_changed": lambda: note.ScheduleChanged(
        household_id=1,
        change_id="0123456789abcdef",
        slot=Slot.DINNER,
        changed_by=note.PersonInfo(9, "<!channel> & *Co*"),
        before=DINNER_SCHEDULE,
        after=replace(DINNER_SCHEDULE, call_time=time(11, 0), response_minutes=60, modes=(Mode.COOK, Mode.PICKUP)),
        starts_on=date(2026, 10, 23),
        path="/schedule",
        kept_today_at=time(15, 0),
    ),
    "call_discarded": lambda: note.CallDiscarded(DECIDED, "event:7:run1:call"),
    "offer_posted": lambda: note.OfferPosted(LUNCH_OFFER, replace(OFFER, taken=0), OFFER_ROSTER[:1], 1),
    "offer_updated": lambda: note.OfferUpdated(
        LUNCH_OFFER, replace(OFFER, dietary_notes=("Vegetarian",)), OFFER_ROSTER, 3
    ),
    "offer_updated_full": lambda: note.OfferUpdated(LUNCH_OFFER, FULL_OFFER, FULL_ROSTER, 4),
    "offer_updated_closed": lambda: note.OfferUpdated(
        replace(LUNCH_OFFER, state=EventState.DECIDED), FULL_OFFER, FULL_ROSTER, 4
    ),
    "offer_updated_done": lambda: note.OfferUpdated(
        replace(LUNCH_OFFER, state=EventState.DONE), FULL_OFFER, FULL_ROSTER, 4
    ),
    "offer_updated_cancelled": lambda: note.OfferUpdated(
        replace(LUNCH_OFFER, state=EventState.CANCELLED, ended_by=ALEX, end_note="Siam is closed today"),
        OFFER,
        OFFER_ROSTER,
        3,
    ),
    "offer_full": lambda: note.OfferFull(LUNCH_OFFER, ALEX, FULL_OFFER),
    "offer_cancelled": lambda: note.EventCancelled(
        replace(LUNCH_OFFER, state=EventState.CANCELLED), (ALEX, SAM), cancelled_by=ALEX, audience=(ALEX, SAM)
    ),
    "guest_invited": lambda: note.GuestInvited(event(), PAT, KIM),
    "guest_joined": lambda: note.GuestJoined(
        1, ALEX, note.PersonInfo(9, "Pat <Slack> & *Co*"), "slack", slack_user_id="U07PAT"
    ),
    "guest_joined_web": lambda: note.GuestJoined(
        1, ALEX, PAT, "web", unlinked=(KIM,), slack_user_id="U07PAT", slack_email="pat@example.com"
    ),
    "guest_joined_unlinked": lambda: note.GuestJoined(
        1, ALEX, PAT, "slack", unlinked=(KIM, LEE), slack_user_id="U07PAT", slack_email="pat@example.com"
    ),
    "guest_joined_slack_guest": lambda: note.GuestJoined(
        1, ALEX, PAT, "slack", slack_user_id="U07PAT", slack_email="pat@example.com", slack_guest_account=True
    ),
    "response_reminder_guest": lambda: note.ResponseReminder(event(), PAT, guest=True),
    "role_vacated": lambda: note.RoleVacated(DECIDED, ALEX, PAT, (AssignmentRole.PICKUP,)),
    "link_needed": lambda: note.LinkNeeded(
        1, ALEX, KIM, "U09KIM", "Kim <K> & *Co*", "join", slack_email="kim@example.com"
    ),
    "link_needed_hourly": lambda: note.LinkNeeded(1, ALEX, KIM, "U09KIM", "", "hourly", slack_email="kim@example.com"),
    "link_needed_hourly_admin": lambda: note.LinkNeeded(
        1, ALEX, ALEX, "U01ALEX", "", "hourly", slack_email="alex@example.com", member_is_admin=True
    ),
    "assignment_made_for_guest": lambda: note.AssignmentMade(
        DECIDED, 34, PAT, (AssignmentRole.PICKUP,), replace(DECISION, dietary_notes=("Vegetarian",)), via=KIM
    ),
    "assignment_cancelled_for_guest": lambda: note.AssignmentCancelled(
        CANCELLED, PAT, (AssignmentRole.PICKUP,), DECISION, cancelled_by=ALEX, note=REASON, via=KIM
    ),
    "call_updated_voting_late": lambda: note.CallUpdated(VOTING, LATE_ROSTER, (), head_count=4),
    "proposals_updated_late": lambda: note.ProposalsUpdated(VOTING, LATE_PROPOSALS, 4, LATE_JOINERS),
    "proposals_updated_late_closed": lambda: note.ProposalsUpdated(DECIDED, LATE_PROPOSALS, 4, LATE_JOINERS[1:]),
    "decision_made_late": lambda: note.DecisionMade(DECIDED, LATE_DECISION),
    "late_join_changed": lambda: note.LateJoinChanged(VOTING, KIM, False, ALEX, VOTING.respond_deadline),
    "late_join_changed_back": lambda: note.LateJoinChanged(VOTING, KIM, True, ALEX, VOTING.respond_deadline),
    "feedback_reminder": lambda: note.FeedbackReminder(
        event(EventState.DONE, coordinator=ALEX), KIM, "Sam's Tacos", date(2026, 10, 29)
    ),
}


# Modals and the Home tab ---------------------------------------------------------------------------

TAGS = (Tag(41, 1, TagKind.DIETARY, "Vegetarian"), Tag(42, 1, TagKind.DIETARY, "Gluten-free"))
CUISINES = (Tag(43, 1, TagKind.CUISINE, "Mexican"), Tag(44, 1, TagKind.CUISINE, "Thai"))
ITEMS = (
    CatalogItem(11, 1, ItemKind.HOME, "Sam's Tacos", frozenset({Mode.COOK}), frozenset({43}), frozenset({2})),
    CatalogItem(12, 1, ItemKind.RESTAURANT, "Siam Garden", frozenset({Mode.PICKUP}), frozenset({44}), frozenset()),
)


def _event_view(state: EventState, mine: Response | None, can: Permissions) -> EventView:
    info = CANCELLED if state is EventState.CANCELLED else event(state, coordinator=ALEX)
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


KIM_LATE = Response(member_id=KIM.id, status=Status.IN, offers=frozenset({RoleOffer.PICKUP}), late=True)
"""Kim joined late and offered to pick up (#76)."""
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
CALLED_OFF_VIEW = _event_view(EventState.CANCELLED, SAM_RESPONSE, Permissions(restart=True))
TOMORROW = date(2026, 10, 23)
_LUNCH = _event_view(EventState.SCHEDULED, None, Permissions(post_now=True, cancel=True))
SCHEDULED_VIEW = replace(
    _LUNCH,
    event=replace(
        _LUNCH.event,
        id=8,
        slot=Slot.LUNCH,
        day=TOMORROW,
        opens_at=datetime(2026, 10, 23, 15, 30, tzinfo=UTC),
        respond_deadline=datetime(2026, 10, 23, 16, 30, tzinfo=UTC),
        coordinator_id=None,
    ),
    info=replace(
        event(EventState.SCHEDULED),
        id=8,
        slot=Slot.LUNCH,
        day=TOMORROW,
        path="/events/8",
        respond_deadline=datetime(2026, 10, 23, 16, 30, tzinfo=UTC),
    ),
)
"""Tomorrow's lunch, scheduled for 10:30 AM, which Sam may post now or call off."""
BREAKFAST_CALLED_OFF = replace(
    CALLED_OFF_VIEW,
    event=replace(CALLED_OFF_VIEW.event, id=9, slot=Slot.BREAKFAST),
    info=replace(CALLED_OFF_VIEW.info, id=9, slot=Slot.BREAKFAST, path="/events/9"),
)
"""Today's breakfast, called off, which anyone may start again (#58)."""
ORIGIN = ids.ViewOrigin(7, "C0DINNER")
CALL_OFF_ORIGIN = replace(ORIGIN, run=1)
"""Call it off and Close now name the run they were opened for, as ``Interactions`` writes them."""

EATERS = (MemberRef(1, "Alex"), MemberRef(2, "Sam"), MemberRef(3, "Kim"))
CLOSES = date(2026, 10, 29)
HOME_MEAL = MealFacts(51, DAY, Slot.DINNER, 11, "Sam's Tacos", ItemKind.HOME, Mode.COOK, MemberRef(2, "Sam"), EATERS, 7)
TAKEOUT = MealFacts(52, DAY, Slot.DINNER, 13, TAQUERIA.name, ItemKind.RESTAURANT, Mode.DELIVERY, None, EATERS, 7)
KIM_SAID = FeedbackInput(Rating.LOVED, Rating.NOT_AGAIN, frozenset({PlaceIssue.COLD, PlaceIssue.LATE}))
AWAITING = tuple(
    Awaiting(60 + n, 7 + n, 1, DAY - timedelta(days=n), Slot.DINNER, name, CLOSES - timedelta(days=n))
    for n, name in enumerate(("Sam's Tacos", "Siam Garden", "Pho <Saigon>", "Taqueria", "Curry Night", "Pizza"))
)
FEEDBACK_ORIGIN = replace(ORIGIN, run=1)
_OFFER_BASE = _event_view(EventState.COLLECTING, None, Permissions(join=True, add_guest=True))
OFFER_VIEW = replace(
    _OFFER_BASE,
    event=replace(
        _OFFER_BASE.event,
        id=9,
        kind=EventKind.OFFER,
        slot=Slot.LUNCH,
        coordinator_id=None,
        created_by=ALEX.id,
        capacity=3,
        decided_item_id=12,
        decided_mode=Mode.PICKUP,
        meal_time=OFFER.at,
        respond_deadline=OFFER.at,
    ),
    info=LUNCH_OFFER,
    roster=OFFER_ROSTER,
    offer=OFFER,
)
"""Alex's lunch offer as Sam sees it on Home, before joining."""
OFFER_JOINED_VIEW = replace(
    OFFER_VIEW, mine=Response(SAM.id, Status.IN, guests=1), can=Permissions(add_guest=True, leave=True)
)
OFFER_FORM = OfferForm(
    DAY,
    Slot.LUNCH,
    {
        Slot.LUNCH: SlotDefaults(Mode.PICKUP, time(12, 30)),
        Slot.DINNER: SlotDefaults(Mode.PICKUP, time(18, 30)),
    },
    3,
    ITEMS,
    CUISINES,
)
SAVED_GUESTS = tuple(
    Member(9 + n, 1, name, None, Role.GUEST, Capabilities(), True) for n, name in enumerate(("Pat", "Jo <dev>"))
)


def guest_home(label: str = "") -> dict[str, Any]:
    """Pat's Home as a guest invited to dinner (#38): In and Out, and nothing else of the household's."""
    view = replace(COLLECTING_VIEW, mine=None, can=Permissions(respond=True))
    return home_view(TodayView(DAY, [view], tuple(Slot)), HomeLinks(BASE_URL), label=label, guest=True)


def left_over(days_ago: int) -> EventView:
    """A decided dinner from ``days_ago`` days before ``DAY``, still open because the scheduler
    was down (#51)."""
    day, event_id = DAY - timedelta(days=days_ago), 200 + days_ago
    return replace(
        DECIDED_VIEW,
        event=replace(DECIDED_VIEW.event, id=event_id, day=day),
        info=replace(DECIDED_VIEW.info, id=event_id, day=day, path=f"/events/{event_id}"),
    )


LOG_FORM = LogForm(DAY, SAM.id, ITEMS, (MemberRef(1, "Alex"), MemberRef(3, "Kim"), MemberRef(2, "Sam")))

VIEWS: dict[str, Callable[[], dict[str, Any]]] = {
    "details_modal": lambda: details_modal(COLLECTING_VIEW, ORIGIN),
    "details_modal_empty": lambda: details_modal(
        _event_view(EventState.COLLECTING, None, Permissions(respond=True)), ORIGIN
    ),
    "proposal_modal": lambda: proposal_modal(VOTING_VIEW, ORIGIN),
    "proposal_modal_join_late": lambda: proposal_modal(
        replace(VOTING_VIEW, mine=None, can=Permissions(propose=True, join_late=True)), replace(ORIGIN, run=1)
    ),
    "home_voting_join_late": lambda: home_view(
        TodayView(DAY, [replace(VOTING_VIEW, mine=None, can=Permissions(propose=True, join_late=True))], tuple(Slot)),
        HomeLinks(BASE_URL),
    ),
    "home_voting_late": lambda: home_view(
        TodayView(
            DAY,
            [
                replace(
                    VOTING_VIEW,
                    mine=KIM_LATE,
                    can=Permissions(vote=True, late_roles=True),
                    proposals=(ProposalView(PROPOSALS[1], voted=False, not_for_me=True), *VOTING_VIEW.proposals[2:]),
                )
            ],
            tuple(Slot),
        ),
        HomeLinks(BASE_URL),
    ),
    "cancel_modal": lambda: cancel_modal(COLLECTING_VIEW, CALL_OFF_ORIGIN),
    "cancel_modal_decided": lambda: cancel_modal(DECIDED_VIEW, replace(CALL_OFF_ORIGIN, home=True)),
    "close_modal": lambda: close_modal(COLLECTING_VIEW, replace(CALL_OFF_ORIGIN, window=Window.RESPOND)),
    "close_modal_vote": lambda: close_modal(VOTING_VIEW, replace(CALL_OFF_ORIGIN, window=Window.VOTE)),
    "home": lambda: home_view(TodayView(DAY, [COLLECTING_VIEW], tuple(Slot)), HomeLinks(BASE_URL)),
    "home_decided": lambda: home_view(
        TodayView(DAY, [DECIDED_VIEW], tuple(Slot)), HomeLinks(BASE_URL), notice="Marked done. Thanks!"
    ),
    "home_coordinator": lambda: home_view(
        TodayView(
            DAY,
            [replace(COLLECTING_VIEW, can=Permissions(respond=True, cancel=True, close_window=Window.RESPOND))],
            tuple(Slot),
        ),
        HomeLinks(BASE_URL),
    ),
    "home_called_off": lambda: home_view(
        TodayView(DAY, [CALLED_OFF_VIEW], tuple(Slot)), HomeLinks(BASE_URL), notice="You called off dinner."
    ),
    "home_breakfast_called_off": lambda: home_view(
        TodayView(DAY, [BREAKFAST_CALLED_OFF, COLLECTING_VIEW], tuple(Slot)), HomeLinks(BASE_URL)
    ),
    "home_empty": lambda: home_view(TodayView(DAY, [], tuple(Slot)), HomeLinks(BASE_URL)),
    "home_upcoming": lambda: home_view(
        TodayView(DAY, [COLLECTING_VIEW, SCHEDULED_VIEW], tuple(Slot)), HomeLinks(BASE_URL)
    ),
    "home_feedback": lambda: home_view(TodayView(DAY, [], tuple(Slot)), HomeLinks(BASE_URL), awaiting=AWAITING),
    "home_feedback_one": lambda: home_view(
        TodayView(DAY, [DECIDED_VIEW], tuple(Slot)), HomeLinks(BASE_URL), awaiting=AWAITING[:1]
    ),
    "feedback_modal": lambda: feedback_modal(FeedbackForm(HOME_MEAL, KIM.id, None, CLOSES), FEEDBACK_ORIGIN),
    "feedback_modal_cook": lambda: feedback_modal(FeedbackForm(HOME_MEAL, SAM.id, None, CLOSES), FEEDBACK_ORIGIN),
    "feedback_modal_restaurant": lambda: feedback_modal(
        FeedbackForm(TAKEOUT, KIM.id, KIM_SAID, CLOSES), replace(FEEDBACK_ORIGIN, home=True)
    ),
    "feedback_modal_thanked": lambda: feedback_modal(
        FeedbackForm(HOME_MEAL, KIM.id, FeedbackInput(Rating.FINE, cook_thanks=True, cook_note="Best yet!"), CLOSES),
        FEEDBACK_ORIGIN,
    ),
    "home_offer": lambda: home_view(TodayView(DAY, [OFFER_VIEW, COLLECTING_VIEW], tuple(Slot)), HomeLinks(BASE_URL)),
    "home_offer_joined": lambda: home_view(TodayView(DAY, [OFFER_JOINED_VIEW], tuple(Slot)), HomeLinks(BASE_URL)),
    "offer_modal": lambda: offer_modal(OFFER_FORM, ids.FormOrigin("C0DINNER")),
    "log_modal": lambda: log_modal(LOG_FORM, ids.FormOrigin(None, home=True)),
    "invite_modal": lambda: invite_modal(
        replace(COLLECTING_VIEW, can=Permissions(respond=True, invite=True)), SAVED_GUESTS, CALL_OFF_ORIGIN
    ),
    "home_guest": lambda: guest_home(),
    # Open calls the scheduler left from earlier days, oldest first as the repository lists them.
    "home_left_over": lambda: home_view(
        TodayView(DAY, [*(left_over(n) for n in range(5, 0, -1)), COLLECTING_VIEW], tuple(Slot)), HomeLinks(BASE_URL)
    ),
    "home_guest_empty": lambda: home_view(TodayView(DAY, [], tuple(Slot)), HomeLinks(BASE_URL), guest=True),
    "cancel_modal_offer": lambda: cancel_modal(
        replace(OFFER_VIEW, can=Permissions(cancel=True)), replace(CALL_OFF_ORIGIN, event_id=9)
    ),
    # Interactions adds the footer to every Home tab it publishes.
    "home_version": lambda: with_version_footer(
        home_view(TodayView(DAY, [], tuple(Slot)), HomeLinks(BASE_URL)), "0.3.0+abc1234"
    ),
}

# Your answer cards and the answered reminder ------------------------------------------------------

KIM_IN = Response(member_id=KIM.id, status=Status.IN)
KIM_OFFERS = replace(KIM_IN, offers=frozenset({RoleOffer.COOK, RoleOffer.ORDER}))
CardSample = Callable[[RenderContext], dict[str, Any]]


def _card(
    info: note.EventInfo, mine: Response | None, *, run: int | None = None, notice: str | None = None
) -> CardSample:
    """Kim's card. ``run`` is the run of the card tapped; None for a tap on the call message."""
    return lambda ctx: your_answer(info, mine, KIM.id, ctx, run=run, notice=notice).payload()


def _reminder(info: note.EventInfo, mine: Response | None, run: int) -> CardSample:
    """Kim's reminder after a tap on it, as ``Interactions`` edits it."""
    return lambda ctx: reminder_message(note.ResponseReminder(info, KIM), ctx, mine=mine, run=run).payload()


CARDS: dict[str, CardSample] = {
    "your_answer_in": _card(event(), KIM_IN),
    "your_answer_maybe": _card(event(), replace(KIM_IN, status=Status.MAYBE), run=1),
    "your_answer_out": _card(event(), replace(KIM_IN, status=Status.OUT), run=1),
    "your_answer_roles": _card(event(), KIM_OFFERS, run=1),
    "your_answer_guests": _card(event(), replace(KIM_OFFERS, guests=2), run=1),
    "your_answer_as_guest": lambda ctx: your_answer(event(), KIM_IN, KIM.id, ctx, run=1, guest=True).payload(),
    "your_answer_inviter": lambda ctx: your_answer(
        event(),
        KIM_IN,
        KIM.id,
        ctx,
        run=1,
        can_invite=True,
        guests=(
            CardGuest(9, "Pat", Response(9, Status.IN, offers=frozenset({RoleOffer.PICKUP})), ("Vegetarian",)),
            CardGuest(10, "Jo <dev>", None),
        ),
    ).payload(),
    "guest_invited_answered": lambda ctx: reminder_message(
        note.GuestInvited(event(), PAT, KIM), ctx, mine=Response(9, Status.OUT), run=1
    ).payload(),
    "your_answer_coordinator": _card(event(coordinator=KIM), KIM_OFFERS, run=1),
    "your_answer_other_coordinator": _card(
        event(coordinator=note.PersonInfo(9, "<!channel> & *Co*")),
        KIM_IN,
        run=1,
        notice="Someone is already coordinating. They can hand it off to you.",
    ),
    "your_answer_voting": _card(VOTING, KIM_OFFERS),
    "your_answer_moved_on": _card(VOTING, KIM_OFFERS, run=1),
    "your_answer_called_off": _card(CANCELLED, KIM_IN, run=1),
    "your_answer_join_late": _card(event(EventState.VOTING), replace(KIM_IN, status=Status.OUT), run=1),
    "your_answer_late": _card(VOTING, KIM_LATE, run=1),
    "your_answer_late_not_for_you": lambda ctx: your_answer(
        VOTING, KIM_LATE, KIM.id, ctx, run=1, not_for_you=("Siam Garden", "Taqueria <Lupita> & Co")
    ).payload(),
    "your_answer_late_counts": _card(event(EventState.VOTING), replace(KIM_LATE, vote_counts=True)),
    "your_answer_taken_off": _card(VOTING, replace(KIM_LATE, status=Status.OUT), run=1),
    "your_answer_started_again": _card(replace(event(), run=2), KIM_IN, run=1),
    "response_reminder_answered": _reminder(event(), replace(KIM_IN, status=Status.MAYBE), run=1),
    "response_reminder_moved_on": _reminder(VOTING, KIM_IN, run=1),
    "response_reminder_started_again": _reminder(replace(event(), run=2), KIM_IN, run=1),
    "schedule_reply": lambda ctx: schedule_reply(DEFAULT_SLOTS, ctx).payload(),
    "call_started_reply": lambda ctx: started_reply(
        event(), datetime(2026, 10, 22, 20, 0, tzinfo=UTC), today=DAY, channel_id="C0DINNER", ctx=ctx
    ).payload(),
    "call_scheduled_reply": lambda ctx: started_reply(
        SCHEDULED_VIEW.info, SCHEDULED_VIEW.event.opens_at, today=DAY, channel_id="C0DINNER", ctx=ctx
    ).payload(),
    "call_posted_reply": lambda ctx: posted_reply(
        replace(SCHEDULED_VIEW.info, state=EventState.COLLECTING, early=True), channel_id="C0DINNER", ctx=ctx
    ).payload(),
}
"""Messages that answer a tap or a command rather than a notification. They mention nobody."""

KINDS = (*NOTIFICATIONS, *VIEWS, *CARDS)


def preview(kind: str, base_url: str = BASE_URL) -> dict[str, Any]:
    """The Block Kit JSON for sample ``kind``: a message payload for notifications and cards, or
    the view for a modal or the Home tab. Raises KeyError for an unknown kind."""
    if kind in VIEWS:
        return VIEWS[kind]()
    ctx = RenderContext(base_url, DIRECTORY)
    if kind in CARDS:
        return CARDS[kind](ctx)
    return render(NOTIFICATIONS[kind](), ctx).payload()
