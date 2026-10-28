# SPDX-License-Identifier: AGPL-3.0-or-later
"""The call lifecycle rules in domain/events.py and domain/schedule.py, without a database."""

from __future__ import annotations

import importlib.util
import itertools
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dinnerbell.domain import events as ev
from dinnerbell.domain import schedule as sched
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.events import (
    AssignmentRole,
    Coverage,
    Details,
    Event,
    EventRuleViolation,
    EventState,
    IllegalTransition,
    InvalidResponse,
    ProposalTally,
    Response,
    RoleOffer,
    Status,
    Step,
    wants_feedback_reminder,
)
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.members import Capabilities, Member, Role

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
CHICAGO = ZoneInfo("America/Chicago")


def member(member_id: int, *, admin: bool = False, active: bool = True, **caps: bool) -> Member:
    return Member(
        id=member_id,
        household_id=1,
        display_name=f"M{member_id}",
        email=f"m{member_id}@example.com",
        role=Role.ADMIN if admin else Role.MEMBER,
        capabilities=Capabilities(**caps),
        active=active,
    )


def event(state: EventState = EventState.COLLECTING, **changes: object) -> Event:
    base = Event(
        id=7,
        household_id=1,
        kind=ev.EventKind.CALL,
        slot=Slot.DINNER,
        day=date(2026, 10, 22),
        state=state,
        run=1,
        opens_at=NOW,
        respond_deadline=NOW + timedelta(minutes=90),
        reminder_minutes=15,
        vote_minutes=45,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# State machine ----------------------------------------------------------------------------------


ALLOWED = {
    (EventState.SCHEDULED, EventState.COLLECTING),
    (EventState.SCHEDULED, EventState.CANCELLED),
    (EventState.SCHEDULED, EventState.ABANDONED),
    (EventState.COLLECTING, EventState.VOTING),
    (EventState.COLLECTING, EventState.DECIDED),  # an offer at its time (FR-4)
    (EventState.COLLECTING, EventState.ABANDONED),
    (EventState.COLLECTING, EventState.CANCELLED),
    (EventState.VOTING, EventState.DECIDED),
    (EventState.VOTING, EventState.ABANDONED),
    (EventState.VOTING, EventState.CANCELLED),
    (EventState.VOTING, EventState.COLLECTING),
    (EventState.DECIDED, EventState.DONE),
    (EventState.DECIDED, EventState.CANCELLED),
    (EventState.DECIDED, EventState.COLLECTING),
    (EventState.ABANDONED, EventState.COLLECTING),
    (EventState.CANCELLED, EventState.COLLECTING),
    (EventState.ABANDONED, EventState.SCHEDULED),
    (EventState.VOTING, EventState.SCHEDULED),
    (EventState.DECIDED, EventState.SCHEDULED),
    (EventState.CANCELLED, EventState.SCHEDULED),
}


@pytest.mark.parametrize(("current", "target"), list(itertools.product(EventState, EventState)))
def test_transitions_are_exactly_the_allowed_ones(current: EventState, target: EventState) -> None:
    if (current, target) in ALLOWED:
        assert ev.transition(current, target) is target
    else:
        with pytest.raises(IllegalTransition, match=f"from {current.value} to {target.value}"):
            ev.transition(current, target)


def test_done_is_final_and_open_states() -> None:
    assert ev.TRANSITIONS[EventState.DONE] == frozenset()
    assert {s for s in EventState if s.is_open} == {
        EventState.SCHEDULED,
        EventState.COLLECTING,
        EventState.VOTING,
        EventState.DECIDED,
    }
    assert all(s.label for s in EventState)


# Responses ------------------------------------------------------------------------------------


def test_status_out_withdraws_offers_and_offers_count_you_in() -> None:
    first = ev.with_offer(None, 3, RoleOffer.COOK, True, NOW)
    assert first is not None
    assert (first.status, first.offers, first.responded_at) == (Status.IN, {RoleOffer.COOK}, NOW)
    out = ev.with_status(first, 3, Status.OUT, NOW + timedelta(minutes=1))
    assert (out.status, out.offers, out.responded_at) == (Status.OUT, frozenset(), NOW)
    back = ev.with_offer(out, 3, RoleOffer.PICKUP, True, NOW)
    assert back is not None and back.status is Status.IN and back.offers == {RoleOffer.PICKUP}


def test_a_maybe_who_offers_stays_maybe_and_withdrawing_needs_a_response() -> None:
    maybe = ev.with_status(None, 3, Status.MAYBE, NOW)
    offered = ev.with_offer(maybe, 3, RoleOffer.ORDER, True, NOW)
    assert offered is not None and offered.status is Status.MAYBE and offered.offers == {RoleOffer.ORDER}
    withdrawn = ev.with_offer(offered, 3, RoleOffer.ORDER, False, NOW)
    assert withdrawn is not None and withdrawn.offers == frozenset()
    assert ev.with_offer(None, 3, RoleOffer.ORDER, False, NOW) is None


def test_details_count_you_in_and_keep_status() -> None:
    details = Details(guests=2, guest_needs=frozenset({9}), eat_from=time(18), note="hi")
    fresh = ev.with_details(None, 3, details, NOW)
    assert (fresh.status, fresh.guests, fresh.head_count) == (Status.IN, 2, 3)
    maybe = ev.with_details(ev.with_status(None, 3, Status.MAYBE, NOW), 3, details, NOW)
    assert maybe.status is Status.MAYBE and maybe.head_count == 0


def test_check_details_normalizes_and_rejects() -> None:
    ok = ev.check_details(
        Details(guests=1, suggestion_item_ids=(4, 4), suggestion_texts=("  Tacos  please ", " "), note="  a  b "),
        dietary_tag_ids=[9],
        active_item_ids=[4],
    )
    assert (ok.suggestion_item_ids, ok.suggestion_texts, ok.note) == ((4,), ("Tacos please",), "a b")
    bad = [
        (Details(guests=21), "Guests"),
        (Details(guests=-1), "Guests"),
        (Details(guest_needs=frozenset({8})), "dietary"),
        (Details(eat_from=time(19), eat_until=time(18)), "latest"),
        (Details(suggestion_item_ids=(5,)), "catalog"),
        (Details(suggestion_texts=("x" * 101,)), "at most 100"),
        (Details(suggestion_texts=("a\u0000b",)), "control"),
        (Details(suggestion_item_ids=(4,), suggestion_texts=("a", "b", "c")), "up to 3"),
        (Details(note="n" * 201), "at most 200"),
    ]
    for details, message in bad:
        with pytest.raises(InvalidResponse, match=message):
            ev.check_details(details, dietary_tag_ids=[9], active_item_ids=[4])


def test_resolve_maybes_turns_only_maybes_out() -> None:
    responses = [
        Response(1, Status.IN),
        Response(2, Status.MAYBE, guests=2, offers=frozenset({RoleOffer.COOK})),
        Response(3, Status.OUT),
    ]
    changed = ev.resolve_maybes(responses)
    assert [(r.member_id, r.status, r.offers, r.maybe_at_deadline, r.guests) for r in changed] == [
        (2, Status.OUT, frozenset(), True, 2)
    ]
    after = [responses[0], changed[0], responses[2]]
    assert ev.head_count(after) == 1
    assert ev.veto_participants(after) == {1, 2}  # the resolved maybe's vetoes still apply
    assert ev.veto_participants(responses) == {1, 2}


def test_head_count_counts_guests_of_members_who_are_in() -> None:
    assert ev.head_count([Response(1, Status.IN, guests=2), Response(2, Status.MAYBE, guests=5)]) == 3


def test_reminders_go_to_active_unanswered_unmuted_members() -> None:
    assert ev.needs_reminder(member(1), None)
    assert not ev.needs_reminder(member(1), Response(1, Status.MAYBE))
    assert not ev.needs_reminder(member(1, active=False), None)
    assert not ev.needs_reminder(replace(member(1), mute_dm_reminders=True), None)


def test_respond_only_while_collecting_and_active() -> None:
    ev.check_can_respond(event(), member(1))
    with pytest.raises(EventRuleViolation, match="closed"):
        ev.check_can_respond(event(EventState.VOTING), member(1))
    with pytest.raises(EventRuleViolation, match="active"):
        ev.check_can_respond(event(), member(1, active=False))


# Coverage (FR-17) -------------------------------------------------------------------------------


def test_coverage_counts_active_members_who_are_in_in_answer_order() -> None:
    members = {i: member(i) for i in range(1, 6)} | {6: member(6, active=False), 7: member(7, can_order=True)}
    later = NOW + timedelta(minutes=5)
    responses = [
        Response(2, Status.IN, offers=frozenset({RoleOffer.COOK, RoleOffer.ORDER}), responded_at=later),
        Response(1, Status.IN, offers=frozenset({RoleOffer.COOK, RoleOffer.PICKUP}), responded_at=NOW),
        Response(3, Status.MAYBE, offers=frozenset({RoleOffer.PICKUP})),
        Response(6, Status.IN, offers=frozenset({RoleOffer.PICKUP})),
        Response(7, Status.IN),
        Response(9, Status.IN, offers=frozenset({RoleOffer.COOK})),  # not a member any more
    ]
    covered = ev.coverage(responses, members)
    assert covered == Coverage(cooks=(1, 2), pickups=(1,), orderers=(2, 7))


def test_cooking_needs_a_volunteer_on_the_cook_list_when_there_is_one() -> None:
    covered = Coverage(cooks=(1, 2))
    assert covered.volunteers(Mode.COOK, frozenset()) == (1, 2)
    assert covered.volunteers(Mode.COOK, frozenset({2, 5})) == (2,)
    assert not covered.covers(Mode.COOK, frozenset({5}))
    assert not covered.covers(Mode.PICKUP, frozenset())
    assert Coverage(orderers=(4,)).covers(Mode.DELIVERY, frozenset())


# Decisions (FR-16) ------------------------------------------------------------------------------


def tally(proposal_id: int, rank: int, *voters: int, covered: bool = True) -> ProposalTally:
    return ProposalTally(proposal_id, rank, covered, frozenset(voters))


def test_most_votes_win() -> None:
    winner = ev.choose_winner([tally(1, 1, 5), tally(2, 2, 5, 6)], coordinator_id=None, admin_ids=frozenset())
    assert winner is not None and winner.id == 2


def test_the_coordinator_breaks_ties() -> None:
    tallies = [tally(1, 1, 5), tally(2, 2, 9)]
    assert ev.choose_winner(tallies, coordinator_id=9, admin_ids=frozenset({5})).id == 2  # type: ignore[union-attr]


def test_the_admin_breaks_ties_without_a_coordinator() -> None:
    tallies = [tally(1, 1, 6), tally(2, 2, 5)]
    assert ev.choose_winner(tallies, coordinator_id=None, admin_ids=frozenset({5})).id == 2  # type: ignore[union-attr]
    # With a coordinator who didn't vote either way, the admin's vote doesn't count extra.
    assert ev.choose_winner(tallies, coordinator_id=8, admin_ids=frozenset({5})).id == 1  # type: ignore[union-attr]


def test_ranking_order_breaks_remaining_ties_and_no_votes_means_top_ranked() -> None:
    assert ev.choose_winner([tally(1, 2), tally(2, 1)], coordinator_id=None, admin_ids=frozenset()).id == 2  # type: ignore[union-attr]


def test_uncovered_proposals_never_win() -> None:
    tallies = [tally(1, 1, 5, 6, covered=False), tally(2, 2)]
    assert ev.choose_winner(tallies, coordinator_id=None, admin_ids=frozenset()).id == 2  # type: ignore[union-attr]
    assert ev.choose_winner([tally(1, 1, covered=False)], coordinator_id=None, admin_ids=frozenset()) is None


def test_assign_roles() -> None:
    assert ev.assign_roles(Mode.COOK, (4, 5), 9) == {AssignmentRole.COOK: 4, AssignmentRole.COORDINATOR: 9}
    assert ev.assign_roles(Mode.DELIVERY, (4,), None) == {AssignmentRole.ORDER: 4}
    assert ev.assign_roles(Mode.PICKUP, (), None) == {}


# Meal time (FR-19) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 22, 18, 0, tzinfo=UTC), datetime(2026, 10, 22, 18, 0, tzinfo=UTC)),
        (datetime(2026, 10, 22, 18, 0, 1, tzinfo=UTC), datetime(2026, 10, 22, 18, 15, tzinfo=UTC)),
        (datetime(2026, 10, 22, 18, 44, tzinfo=UTC), datetime(2026, 10, 22, 18, 45, tzinfo=UTC)),
        (datetime(2026, 10, 22, 23, 50, tzinfo=UTC), datetime(2026, 10, 23, 0, 0, tzinfo=UTC)),
    ],
)
def test_round_up_to_quarter(moment: datetime, expected: datetime) -> None:
    assert ev.round_up_to_quarter(moment) == expected


def test_meal_time_is_the_latest_earliest_rounded_up() -> None:
    responses = [
        Response(1, Status.IN, eat_from=time(17, 40)),
        Response(2, Status.IN, eat_from=time(18, 5)),
        Response(3, Status.OUT, eat_from=time(21, 0)),  # out: doesn't count
        Response(4, Status.IN),
    ]
    decided = datetime(2026, 10, 22, 22, 15, tzinfo=UTC)  # 17:15 in Chicago
    got = ev.meal_time(responses, day=date(2026, 10, 22), zone=CHICAGO, default=time(18, 30), not_before=decided)
    assert got.astimezone(CHICAGO).time() == time(18, 15)


def test_meal_time_falls_back_to_the_slot_and_never_precedes_the_decision() -> None:
    day = date(2026, 10, 22)
    decided = datetime(2026, 10, 22, 22, 15, tzinfo=UTC)
    fallback = ev.meal_time([], day=day, zone=CHICAGO, default=time(18, 30), not_before=decided)
    assert fallback.astimezone(CHICAGO).time() == time(18, 30)
    late = datetime(2026, 10, 23, 0, 7, tzinfo=UTC)  # 19:07 in Chicago, after the usual time
    assert ev.meal_time([], day=day, zone=CHICAGO, default=time(18, 30), not_before=late) == datetime(
        2026, 10, 23, 0, 15, tzinfo=UTC
    )


# Permissions ----------------------------------------------------------------------------------


def test_coordinator_claim_and_handoff() -> None:
    ev.check_claim_coordinator(event(), member(1))
    ev.check_claim_coordinator(event(EventState.VOTING), member(1))
    with pytest.raises(EventRuleViolation, match="already"):
        ev.check_claim_coordinator(event(coordinator_id=2), member(1))
    with pytest.raises(EventRuleViolation, match="open"):
        ev.check_claim_coordinator(event(EventState.DECIDED), member(1))
    with pytest.raises(EventRuleViolation, match="active"):
        ev.check_claim_coordinator(event(), member(1, active=False))
    led = event(coordinator_id=1)
    ev.check_handoff(led, member(1), member(2))
    ev.check_handoff(replace(led, state=EventState.DECIDED), member(1), member(2))
    with pytest.raises(EventRuleViolation, match="Only the coordinator"):
        ev.check_handoff(led, member(2), member(3))
    with pytest.raises(EventRuleViolation, match="another"):
        ev.check_handoff(led, member(1), member(1))
    with pytest.raises(EventRuleViolation, match="another"):
        ev.check_handoff(led, member(1), member(2, active=False))
    with pytest.raises(EventRuleViolation, match="over"):
        ev.check_handoff(replace(led, state=EventState.DONE), member(1), member(2))


def test_voting_permissions() -> None:
    voting = event(EventState.VOTING, coordinator_id=9)
    ev.check_can_vote(voting, member(1), Response(1, Status.IN))
    ev.check_can_vote(voting, member(9), None)  # the coordinator votes even when not eating
    with pytest.raises(EventRuleViolation, match="Only people who are in"):
        ev.check_can_vote(voting, member(1), Response(1, Status.OUT, maybe_at_deadline=True))
    with pytest.raises(EventRuleViolation, match="Only people who are in"):
        ev.check_can_vote(voting, member(1, active=False), Response(1, Status.IN))
    with pytest.raises(EventRuleViolation, match="closed"):
        ev.check_can_vote(event(), member(1), Response(1, Status.IN))
    ev.check_can_propose(voting, member(1))
    with pytest.raises(EventRuleViolation, match="only while voting"):
        ev.check_can_propose(event(), member(1))
    with pytest.raises(EventRuleViolation, match="active"):
        ev.check_can_propose(voting, member(1, active=False))


def test_only_the_coordinator_or_an_admin_locks() -> None:
    voting = event(EventState.VOTING, coordinator_id=9)
    ev.check_can_lock(voting, member(9))
    ev.check_can_lock(voting, member(1, admin=True))
    with pytest.raises(EventRuleViolation, match="coordinator or an admin"):
        ev.check_can_lock(voting, member(1))
    with pytest.raises(EventRuleViolation, match="coordinator or an admin"):
        ev.check_can_lock(voting, member(9, active=False))
    with pytest.raises(EventRuleViolation, match="closed"):
        ev.check_can_lock(event(EventState.DECIDED, coordinator_id=9), member(9))


def test_managing_a_decision() -> None:
    decided = event(EventState.DECIDED, coordinator_id=9)
    ev.check_can_manage_decision(decided, member(9))
    ev.check_can_manage_decision(replace(decided, state=EventState.DONE), member(1, admin=True))
    with pytest.raises(EventRuleViolation, match="no decision"):
        ev.check_can_manage_decision(replace(decided, state=EventState.DONE), member(9))
    with pytest.raises(EventRuleViolation, match="coordinator or an admin"):
        ev.check_can_manage_decision(decided, member(1))
    with pytest.raises(EventRuleViolation, match="active"):
        ev.check_can_manage_decision(decided, member(9, active=False))


def test_marking_done() -> None:
    decided = event(EventState.DECIDED, coordinator_id=9, meal_time=NOW)
    ev.check_can_mark_done(decided, member(1), Response(1, Status.IN), now=NOW)
    ev.check_can_mark_done(decided, member(9), None, now=NOW)
    ev.check_can_mark_done(decided, member(2, admin=True), None, now=NOW)
    with pytest.raises(EventRuleViolation, match="someone at the meal"):
        ev.check_can_mark_done(decided, member(1), Response(1, Status.OUT), now=NOW)
    with pytest.raises(EventRuleViolation, match="decided"):
        ev.check_can_mark_done(event(EventState.VOTING), member(1), Response(1, Status.IN), now=NOW)


def test_members_mark_done_only_once_it_is_meal_time() -> None:
    """Regression: a tap right after the decision ended the call and logged the meal early."""
    decided = event(EventState.DECIDED, coordinator_id=9, meal_time=NOW)
    early = NOW - timedelta(seconds=1)
    for actor, response in ((member(1), Response(1, Status.IN)), (member(9), None)):
        with pytest.raises(EventRuleViolation, match="once it's meal time"):
            ev.check_can_mark_done(decided, actor, response, now=early)
    ev.check_can_mark_done(decided, member(2, admin=True), None, now=early)  # admins can any time


def test_ad_hoc_days_and_reruns() -> None:
    today = date(2026, 10, 22)
    ev.check_ad_hoc_day(today, today)
    ev.check_ad_hoc_day(today + timedelta(days=7), today)
    with pytest.raises(EventRuleViolation, match="today or a later"):
        ev.check_ad_hoc_day(today - timedelta(days=1), today)
    with pytest.raises(EventRuleViolation, match="7 days"):
        ev.check_ad_hoc_day(today + timedelta(days=8), today)
    late = NOW + timedelta(hours=4)
    for state in (EventState.VOTING, EventState.DECIDED, EventState.ABANDONED, EventState.CANCELLED):
        ev.check_rerun(event(state), today, now=NOW, too_late_after=late)
    for state in (EventState.SCHEDULED, EventState.COLLECTING, EventState.DONE):
        with pytest.raises(EventRuleViolation, match="re-run"):
            ev.check_rerun(event(state), today, now=NOW, too_late_after=late)
    with pytest.raises(EventRuleViolation, match="today's"):
        ev.check_rerun(event(EventState.ABANDONED), today + timedelta(days=1), now=NOW, too_late_after=late)
    with pytest.raises(EventRuleViolation, match="too late to re-run"):
        ev.check_rerun(event(EventState.ABANDONED), today, now=late, too_late_after=late)
    tomorrow = event(EventState.CANCELLED, day=today + timedelta(days=1))
    ev.check_rerun(tomorrow, today, now=late, too_late_after=late)  # a later day keeps its own timing


def test_window_changes() -> None:
    assert ev.shifted_deadline(NOW, 15, NOW, latest=FAR) == NOW + timedelta(minutes=15)
    assert ev.shifted_deadline(NOW + timedelta(minutes=10), -15, NOW, latest=FAR) == NOW  # never before now
    assert ev.shifted_deadline(NOW + timedelta(hours=1), None, NOW, latest=FAR) == NOW
    with pytest.raises(EventRuleViolation, match="240"):
        ev.shifted_deadline(NOW, 241, NOW, latest=FAR)
    collecting = event()
    assert ev.check_window_change(collecting, ev.Window.RESPOND) == collecting.respond_deadline
    voting = event(EventState.VOTING, vote_deadline=NOW)
    assert ev.check_window_change(voting, ev.Window.VOTE) == NOW
    for state_event, window in [(collecting, ev.Window.VOTE), (voting, ev.Window.RESPOND)]:
        with pytest.raises(EventRuleViolation, match="isn't open"):
            ev.check_window_change(state_event, window)


# Scheduler steps ------------------------------------------------------------------------------


FAR = NOW + timedelta(days=30)


def test_due_steps_walk_an_event_through_missed_deadlines() -> None:
    scheduled = event(EventState.SCHEDULED, opens_at=NOW + timedelta(minutes=1))
    assert ev.due_step(scheduled, NOW, stale_after=FAR) is None
    assert ev.due_step(scheduled, NOW + timedelta(minutes=1), stale_after=FAR) is Step.OPEN
    collecting = event()
    assert ev.due_step(collecting, NOW + timedelta(minutes=74), stale_after=FAR) is None
    assert ev.due_step(collecting, NOW + timedelta(minutes=75), stale_after=FAR) is Step.REMIND
    reminded = replace(collecting, reminder_sent_at=NOW)
    assert ev.due_step(reminded, NOW + timedelta(minutes=89), stale_after=FAR) is None
    assert ev.due_step(reminded, NOW + timedelta(minutes=90), stale_after=FAR) is Step.CLOSE_RESPONSES
    # After downtime past the deadline, the reminder is skipped, not sent late.
    assert ev.due_step(collecting, NOW + timedelta(hours=5), stale_after=FAR) is Step.CLOSE_RESPONSES
    voting = event(EventState.VOTING, vote_deadline=NOW)
    assert ev.due_step(voting, NOW - timedelta(seconds=1), stale_after=FAR) is None
    assert ev.due_step(voting, NOW, stale_after=FAR) is Step.CLOSE_VOTING
    decided = event(EventState.DECIDED, meal_time=NOW)
    assert ev.due_step(decided, NOW + timedelta(hours=2) - timedelta(seconds=1), stale_after=FAR) is None
    assert ev.due_step(decided, NOW + timedelta(hours=2), stale_after=FAR) is Step.MARK_DONE
    for state in (EventState.DONE, EventState.ABANDONED, EventState.CANCELLED):
        assert ev.due_step(event(state), NOW + timedelta(days=1), stale_after=NOW) is None


def test_stale_undecided_calls_expire_instead_of_catching_up() -> None:
    """Regression: after a long outage the tick decided old calls and sent messages days late."""
    stale = NOW + timedelta(hours=3)
    for state in (EventState.SCHEDULED, EventState.COLLECTING, EventState.VOTING):
        undecided = event(state, vote_deadline=NOW if state is EventState.VOTING else None)
        assert ev.due_step(undecided, stale, stale_after=stale) is Step.EXPIRE
    decided = event(EventState.DECIDED, meal_time=NOW)  # a decided meal still gets marked done
    assert ev.due_step(decided, stale, stale_after=stale) is Step.MARK_DONE


def test_a_scheduled_call_whose_window_passed_expires_without_opening() -> None:
    """Regression: it opened and was abandoned at once, a post FR-2 says not to make."""
    scheduled = event(EventState.SCHEDULED, opens_at=NOW)
    assert ev.due_step(scheduled, NOW + timedelta(minutes=89), stale_after=FAR) is Step.OPEN
    assert ev.due_step(scheduled, NOW + timedelta(minutes=90), stale_after=FAR) is Step.EXPIRE


def test_stale_after() -> None:
    day = date(2026, 10, 22)
    standing = event(day=day, opens_at=NOW, respond_deadline=NOW + timedelta(minutes=90))  # 15:00 Chicago
    # The usual 18:30 meal plus two hours is later than the call's own end (17:15 + 2 h).
    assert ev.stale_after(standing, usual_meal=time(18, 30), zone=CHICAGO) == datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    late_opens = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)  # an ad-hoc call at 21:00 Chicago
    late = event(day=day, opens_at=late_opens, respond_deadline=late_opens + timedelta(minutes=90))
    # Its own end, 23:15 + 2 h, is past midnight, so midnight is the cutoff.
    assert ev.stale_after(late, usual_meal=time(18, 30), zone=CHICAGO) == datetime(2026, 10, 23, 5, 0, tzinfo=UTC)
    voting = replace(late, vote_deadline=late_opens + timedelta(minutes=10))
    assert ev.stale_after(voting, usual_meal=time(18, 30), zone=CHICAGO) == datetime(2026, 10, 23, 4, 10, tzinfo=UTC)


def test_should_open_today() -> None:
    opens, deadline = NOW, NOW + timedelta(minutes=90)
    kwargs = {"opens_at": opens, "respond_deadline": deadline}
    assert ev.should_open_today(enabled=True, now=NOW, exists=False, **kwargs)
    assert not ev.should_open_today(enabled=True, now=NOW - timedelta(seconds=1), exists=False, **kwargs)
    assert not ev.should_open_today(enabled=True, now=deadline, exists=False, **kwargs)
    assert not ev.should_open_today(enabled=True, now=NOW, exists=True, **kwargs)
    assert not ev.should_open_today(enabled=False, now=NOW, exists=False, **kwargs)


def test_event_keys_and_paths() -> None:
    assert event().key == "event:7:run1"
    assert event(run=2).path == "/events/7"
    assert event().remind_at == NOW + timedelta(minutes=75)


# Schedule settings ----------------------------------------------------------------------------


def test_default_slots_match_the_settled_schedule() -> None:
    slots = sched.slot_schedules({})
    dinner, lunch = slots[Slot.DINNER], slots[Slot.LUNCH]
    assert (dinner.enabled, dinner.call_time, dinner.response_minutes, dinner.vote_minutes) == (
        True,
        time(15),
        90,
        45,
    )
    assert dinner.reminder_minutes == 15 and dinner.modes == {Mode.PICKUP, Mode.DELIVERY}
    assert (lunch.enabled, lunch.call_time, lunch.response_minutes, lunch.vote_minutes) == (
        False,
        time(10, 30),
        60,
        30,
    )
    assert lunch.reminder_minutes == 15 and lunch.modes == {Mode.PICKUP}
    assert dinner.opens_at(date(2026, 10, 22), CHICAGO) == datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    assert dinner.response_window == timedelta(minutes=90) and dinner.vote_window == timedelta(minutes=45)


def test_settings_round_trip_and_bad_values_fall_back() -> None:
    assert sched.slot_schedules(sched.default_settings()) == sched.DEFAULT_SLOTS
    assert sched.weights(sched.default_settings()) == sched.Weights()
    assert sched.proposal_count(sched.default_settings()) == 3
    assert sched.temperature(sched.default_settings()) == 1.0  # not stored until the admin saves
    garbage = {
        "slots": {
            "dinner": {
                "enabled": "yes",
                "call_time": "25:00",
                "response_minutes": True,
                "vote_minutes": 9999,
                "reminder_minutes": -1,
                "modes": ["teleport"],
                "meal_time": "1830",
            },
            "lunch": "nope",
        },
        "ranking_weights": {"novelty": "lots", "suggested": 11, "soft_preference": True, "cuisine_variety": 2},
        "proposal_count": 9,
        "ranking_temperature": 3.5,
    }
    assert sched.slot_schedules(garbage) == sched.DEFAULT_SLOTS
    assert sched.weights(garbage) == sched.Weights(cuisine_variety=2.0)
    assert sched.proposal_count(garbage) == 3
    assert sched.slot_schedules({"slots": []}) == sched.DEFAULT_SLOTS
    assert sched.slot_schedules({"slots": {"dinner": {"modes": "pickup"}}}) == sched.DEFAULT_SLOTS
    assert sched.weights({"ranking_weights": 3}) == sched.Weights()
    assert sched.temperature(garbage) == 1.0
    assert [sched.temperature({"ranking_temperature": v}) for v in (True, "0", -0.1, 0, 2.5)] == [1, 1, 1, 0, 2.5]
    custom = {"slots": {"lunch": {"enabled": True, "call_time": "11:05", "modes": ["cook"], "meal_time": "12:00"}}}
    lunch = sched.slot_schedule(custom, Slot.LUNCH)
    assert (lunch.enabled, lunch.call_time, lunch.modes, lunch.meal_time) == (True, time(11, 5), {Mode.COOK}, time(12))


def test_check_schedule_and_weights() -> None:
    dinner = sched.DEFAULT_SLOTS[Slot.DINNER]
    assert sched.check_schedule(dinner) is dinner
    for changes, message in [
        ({"response_minutes": 4}, "response window"),
        ({"vote_minutes": 361}, "vote window"),
        ({"reminder_minutes": 90}, "reminder"),
        ({"modes": frozenset()}, "default way"),
    ]:
        with pytest.raises(sched.InvalidSchedule, match=message):
            sched.check_schedule(replace(dinner, **changes))  # type: ignore[arg-type]
    assert sched.check_weights(sched.Weights(), 3) == (sched.Weights(), 3)
    with pytest.raises(sched.InvalidSchedule, match="Weights"):
        sched.check_weights(sched.Weights(novelty=10.5), 3)
    with pytest.raises(sched.InvalidSchedule, match="proposals"):
        sched.check_weights(sched.Weights(), 6)
    assert [sched.check_temperature(v) for v in (0.0, 3.0)] == [0.0, 3.0]
    for bad in (-0.5, 3.1):
        with pytest.raises(sched.InvalidSchedule, match="Randomness runs from 0 to 3"):
            sched.check_temperature(bad)
    assert sched.parse_hhmm(" 07:05 ", "call time") == time(7, 5)
    for bad in ("7:05", "ab:cd", "24:00", "12:60", "\uff11\uff12:\uff10\uff10"):
        with pytest.raises(sched.InvalidSchedule, match="HH:MM"):
            sched.parse_hhmm(bad, "call time")


def test_migration_writes_the_same_defaults_as_the_domain() -> None:
    """Migration 0003 carries its own copy of the defaults; it must match the domain's. Breakfast
    came later (#58), and a household without its schedule reads the default: no daily call."""
    path = Path(__file__).parents[1] / "src/dinnerbell/db/alembic/versions/0003_dinner_calls.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    defaults = sched.default_settings()
    assert defaults["slots"].pop("breakfast") == sched.slot_to_json(sched.DEFAULT_SLOTS[Slot.BREAKFAST])
    assert defaults == module.DEFAULT_SETTINGS
    assert sched.slot_schedules(module.DEFAULT_SETTINGS)[Slot.BREAKFAST].enabled is False


def test_windows_count_elapsed_time_across_a_daylight_saving_change() -> None:
    """Regression: wall-clock arithmetic gave a 01:30 call 30 real minutes on spring-forward day."""
    early = replace(sched.DEFAULT_SLOTS[Slot.DINNER], call_time=time(1, 30))
    opens = early.opens_at(date(2026, 3, 8), CHICAGO)  # US clocks jump from 02:00 to 03:00
    assert opens.tzinfo is UTC
    deadline = opens + early.response_window
    assert deadline - opens == timedelta(minutes=90)
    assert deadline.astimezone(CHICAGO).time() == time(4, 0)  # 01:30 CST + 90 elapsed minutes


def test_details_drop_guest_needs_without_guests() -> None:
    clean = ev.check_details(Details(guests=0, guest_needs=frozenset({9})), dietary_tag_ids=[9], active_item_ids=[])
    assert clean.guest_needs == frozenset()


def test_eat_windows_end_by_a_quarter_to_midnight() -> None:
    """Regression: an eat time of 23:46 rounded the meal to 00:00 and expired a normal call."""
    ok = ev.check_details(
        Details(eat_from=time(23, 45), eat_until=time(23, 45)), dietary_tag_ids=[], active_item_ids=[]
    )
    assert ok.eat_from == time(23, 45)
    for details in (Details(eat_from=time(23, 46)), Details(eat_until=time(23, 59))):
        with pytest.raises(InvalidResponse, match="11:45 PM"):
            ev.check_details(details, dietary_tag_ids=[], active_item_ids=[])


def test_schedules_stay_within_the_day() -> None:
    dinner = sched.DEFAULT_SLOTS[Slot.DINNER]
    late = replace(dinner, call_time=time(21, 45), response_minutes=90, vote_minutes=45, meal_time=time(23, 45))
    with pytest.raises(sched.InvalidSchedule, match="usual meal time"):  # voting ends at midnight
        sched.check_schedule(late)
    sched.check_schedule(replace(late, call_time=time(21, 0)))  # ends 23:15
    with pytest.raises(sched.InvalidSchedule, match="past midnight"):
        sched.check_schedule(replace(dinner, call_time=time(22, 0), response_minutes=90, vote_minutes=45))
    with pytest.raises(sched.InvalidSchedule, match="11:45 PM"):
        sched.check_schedule(replace(dinner, meal_time=time(23, 50)))


def test_deadlines_never_move_past_midnight() -> None:
    midnight = NOW + timedelta(hours=5)
    assert ev.shifted_deadline(NOW, 240, NOW, latest=midnight) == NOW + timedelta(hours=4)
    with pytest.raises(EventRuleViolation, match="past midnight"):
        ev.shifted_deadline(NOW + timedelta(hours=4), 90, NOW, latest=midnight)


# Calling it off and closing early (FR-56) -------------------------------------------------------

STARTER, COORDINATOR, ADMIN, OTHER = 2, 3, 1, 4
CANCEL_ACTORS = {
    "admin": member(ADMIN, admin=True),
    "coordinator": member(COORDINATOR),
    "starter": member(STARTER),
    "member": member(OTHER),
    "inactive admin": member(ADMIN, admin=True, active=False),
    "inactive coordinator": member(COORDINATOR, active=False),
    "inactive starter": member(STARTER, active=False),
}
CANCEL_AS = {
    # Who may call it off, by state: the capacity recorded, or None for a refusal.
    EventState.SCHEDULED: {"admin": "admin", "coordinator": "coordinator", "starter": "starter"},
    EventState.COLLECTING: {"admin": "admin", "coordinator": "coordinator", "starter": "starter"},
    EventState.VOTING: {"admin": "admin", "coordinator": "coordinator", "starter": "starter"},
    EventState.DECIDED: {"admin": "admin", "coordinator": "coordinator"},
    EventState.DONE: {},
    EventState.ABANDONED: {},
    EventState.CANCELLED: {},
}


@pytest.mark.parametrize(("state", "who"), list(itertools.product(EventState, CANCEL_ACTORS)))
def test_who_may_call_it_off(state: EventState, who: str) -> None:
    call = event(state, created_by=STARTER, coordinator_id=COORDINATOR)
    expected = CANCEL_AS[state].get(who)
    if expected is not None:
        assert ev.check_can_cancel(call, CANCEL_ACTORS[who]) is ev.ActingAs(expected)
        return
    with pytest.raises(EventRuleViolation) as refused:
        ev.check_can_cancel(call, CANCEL_ACTORS[who])
    message = str(refused.value)
    if who.startswith("inactive"):
        assert message == "Only active members can call off a call."
    elif not state.is_open:
        assert message == "This call already finished."
    elif who == "starter":
        assert message == "It's decided now, so only the coordinator or an admin can call it off."
    else:
        assert message == "Only the coordinator, whoever started this call, or an admin can call it off."


def test_call_off_records_the_narrowest_capacity() -> None:
    """An admin who coordinates or started the call is audited as that, not as an override.
    A scheduled daily call has no starter, so nobody can call it off as one."""
    admin = member(ADMIN, admin=True)
    assert ev.check_can_cancel(event(coordinator_id=ADMIN, created_by=ADMIN), admin) is ev.ActingAs.COORDINATOR
    assert ev.check_can_cancel(event(created_by=ADMIN), admin) is ev.ActingAs.STARTER
    assert ev.check_can_cancel(event(EventState.DECIDED, created_by=ADMIN), admin) is ev.ActingAs.ADMIN
    with pytest.raises(EventRuleViolation, match="whoever started"):
        ev.check_can_cancel(event(created_by=None), member(OTHER))


CLOSE_ACTORS = {
    "admin": member(ADMIN, admin=True),
    "coordinator": member(COORDINATOR),
    "starter": member(STARTER),
    "member": member(OTHER),
    "inactive admin": member(ADMIN, admin=True, active=False),
    "inactive coordinator": member(COORDINATOR, active=False),
}


@pytest.mark.parametrize(("state", "who"), list(itertools.product(EventState, CLOSE_ACTORS)))
def test_who_may_close_a_window_now(state: EventState, who: str) -> None:
    call = event(state, created_by=STARTER, coordinator_id=COORDINATOR, vote_deadline=NOW)
    allowed = who in {"admin", "coordinator"} and state in {EventState.COLLECTING, EventState.VOTING}
    if allowed:
        window = ev.Window.RESPOND if state is EventState.COLLECTING else ev.Window.VOTE
        assert ev.check_can_close_window(call, CLOSE_ACTORS[who]) is window
        return
    with pytest.raises(EventRuleViolation) as refused:
        ev.check_can_close_window(call, CLOSE_ACTORS[who])
    message = str(refused.value)
    if who.startswith("inactive"):
        assert message == "Only active members can close a window."
    elif not state.is_open:
        assert message == "This call already finished."
    elif state not in {EventState.COLLECTING, EventState.VOTING}:
        assert message == "There's no open window to close."
    else:
        assert message == "Only the coordinator or an admin can close it now."


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        (None, None),
        ("", None),
        ("  \n\t ", None),
        ("  Pizza   at\nGrandma's  ", "Pizza at Grandma's"),
        ("Snow\x00 day‮", "Snow day"),  # control and bidi-override characters are dropped
        ("Family 👨‍👩‍👧 night", "Family 👨‍👩‍👧 night"),  # emoji joiners stay
        ("x" * 141, "x" * 140),
        ("a" * 139 + " b", "a" * 139),  # cut, then trimmed
    ],
)
def test_clean_cancel_note(text: str | None, cleaned: str | None) -> None:
    assert ev.clean_cancel_note(text) == cleaned
    assert cleaned is None or len(cleaned) <= ev.CANCEL_NOTE_MAX


def test_an_illegal_transition_reads_as_a_finished_call() -> None:
    finished = ev.friendly(IllegalTransition(EventState.DONE, EventState.CANCELLED))
    assert isinstance(finished, EventRuleViolation) and str(finished) == "This call already finished."
    assert str(ev.friendly(IllegalTransition(EventState.COLLECTING, EventState.DONE))) == ev.STALE_CALL


def answered(*pairs: tuple[int, Status], at: datetime = NOW + timedelta(minutes=1)) -> list[Response]:
    """Answers given in this run (the test call opens at NOW), last changed ``at``."""
    return [Response(member_id=m, status=s, updated_at=at) for m, s in pairs]


PICKUP = Coverage(pickups=(1,))
QUIET = NOW + timedelta(minutes=1) + ev.EARLY_CLOSE_QUIET


def due(
    responses: list[Response], at: datetime, coverage: Coverage = PICKUP, active: tuple[int, ...] = (1, 2, 3)
) -> Step | None:
    return ev.due_step(event(), at, stale_after=FAR, responses=responses, active_member_ids=active, coverage=coverage)


def test_everyone_out_closes_as_soon_as_everyone_answered() -> None:
    """Nothing can come of waiting, and the call ends on-your-own."""
    soon = NOW + timedelta(minutes=2)
    assert due(answered((1, Status.OUT), (2, Status.OUT), (3, Status.OUT)), soon, Coverage()) is Step.CLOSE_RESPONSES


def test_a_lone_in_gets_the_quiet_spell_to_add_a_guest() -> None:
    lone = answered((1, Status.IN), (2, Status.OUT), (3, Status.OUT))
    assert due(lone, NOW + timedelta(minutes=2)) is None
    assert due(lone, QUIET, Coverage()) is Step.CLOSE_RESPONSES  # too few either way: no coverage needed
    with_a_guest = [replace(lone[0], guests=1), *lone[1:]]
    assert due(with_a_guest, QUIET, Coverage()) is None  # two eating: needs someone to cook, fetch, or order
    assert due(with_a_guest, QUIET) is Step.CLOSE_RESPONSES


def test_two_or_more_eating_close_early_only_after_a_quiet_spell_with_coverage() -> None:
    """Tapping In first and adding guests, an eat window, or an offer next is never cut short."""
    two_in = answered((1, Status.IN), (2, Status.IN), (3, Status.OUT))
    assert due(two_in, QUIET - timedelta(seconds=1)) is None
    assert due(two_in, QUIET) is Step.CLOSE_RESPONSES
    assert due(two_in, QUIET, Coverage()) is None  # nobody can cook, pick up, or order yet
    for coverage in (Coverage(cooks=(2,)), Coverage(orderers=(1,))):
        assert due(two_in, QUIET, coverage) is Step.CLOSE_RESPONSES
    # The latest change counts: one late edit restarts the quiet spell.
    edited = [*two_in[:2], replace(two_in[2], updated_at=QUIET - timedelta(minutes=1))]
    assert due(edited, QUIET) is None


def test_early_close_needs_every_active_member_to_answer_this_run() -> None:
    assert due(answered((1, Status.IN), (2, Status.IN)), QUIET) is None  # 3 hasn't answered
    assert due(answered((1, Status.IN), (2, Status.IN), (3, Status.MAYBE)), QUIET) is None  # a maybe might come
    # An inactive member's answer, maybe or not, doesn't count either way.
    extra = answered((1, Status.IN), (2, Status.IN), (3, Status.IN), (9, Status.MAYBE))
    assert due(extra, QUIET) is Step.CLOSE_RESPONSES
    # Answers kept from before a re-run or restart (changed before this run opened) don't count.
    kept = answered((1, Status.OUT), (2, Status.OUT), (3, Status.OUT), at=NOW - timedelta(minutes=1))
    assert due(kept, QUIET) is None
    partly = [*answered((1, Status.IN), (2, Status.IN)), *answered((3, Status.OUT), at=NOW - timedelta(minutes=1))]
    assert due(partly, QUIET) is None
    assert due([replace(r, updated_at=None) for r in extra], QUIET) is None
    assert due(extra, QUIET, active=()) is None  # no active members
    assert ev.due_step(event(), QUIET, stale_after=FAR) is None  # no responses given: wait for the deadline
    # Only a call that's collecting closes early.
    for state in (EventState.SCHEDULED, EventState.VOTING, EventState.DECIDED):
        call = event(state, opens_at=FAR, vote_deadline=FAR, meal_time=FAR)
        assert ev.due_step(call, QUIET, stale_after=FAR, responses=extra, active_member_ids=(1, 2, 3)) is None


def test_starting_a_called_off_call_again() -> None:
    today = date(2026, 10, 22)
    cancelled = event(EventState.CANCELLED)
    ev.check_restart(cancelled, today, now=NOW, too_late_after=FAR)
    with pytest.raises(EventRuleViolation, match="too late to start today's dinner call again"):
        ev.check_restart(cancelled, today, now=FAR, too_late_after=FAR)
    ev.check_restart(replace(cancelled, day=today + timedelta(days=1)), today, now=FAR, too_late_after=FAR)
    with pytest.raises(EventRuleViolation, match="already a dinner call"):
        ev.check_restart(event(EventState.ABANDONED), today, now=NOW, too_late_after=FAR)


def test_skip_day_reaches_as_far_as_an_ad_hoc_call() -> None:
    today = date(2026, 10, 22)
    ev.check_skip_day(today, today)
    ev.check_skip_day(today + timedelta(days=ev.AD_HOC_DAYS_AHEAD), today)
    with pytest.raises(EventRuleViolation, match="today's call or a later"):
        ev.check_skip_day(today - timedelta(days=1), today)
    with pytest.raises(EventRuleViolation, match="7 days ahead"):
        ev.check_skip_day(today + timedelta(days=ev.AD_HOC_DAYS_AHEAD + 1), today)


def test_an_action_from_an_earlier_run_is_stale() -> None:
    second = event(EventState.COLLECTING, run=2)
    ev.check_run(second, None)  # a surface that doesn't say which run it showed
    ev.check_run(second, 2)
    with pytest.raises(ev.StaleRun, match=r"^That can't happen anymore\.$"):
        ev.check_run(second, 1)
    assert issubclass(ev.StaleRun, EventRuleViolation)  # surfaces that don't know it still show it kindly


def test_muting_direct_messages_stops_the_feedback_reminder() -> None:
    asked = replace(member(1), feedback_reminder=True)
    assert wants_feedback_reminder(asked)
    assert not wants_feedback_reminder(replace(asked, mute_dm_reminders=True))
    assert not wants_feedback_reminder(replace(asked, active=False))
    assert not wants_feedback_reminder(member(1))
