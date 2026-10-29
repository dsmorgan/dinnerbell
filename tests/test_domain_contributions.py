# SPDX-License-Identifier: AGPL-3.0-or-later
"""The contribution and badge rules, with no database (spec 12, FR-38, FR-39, FR-43)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from dinnerbell.domain.badges import (
    BADGES,
    BY_KEY,
    DEFAULT_THRESHOLDS,
    SETTINGS_KEY,
    TIERS,
    BadgeKey,
    InvalidThresholds,
    Tier,
    check_thresholds,
    longest_streak,
    progress,
    thresholds_from,
    tiers_reached,
)
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.contributions import (
    GAMBLER_MIN_PICKS,
    ON_TIME,
    Credit,
    DoneFacts,
    Kind,
    Newness,
    credits_for_done,
    newness_by_meal,
)
from dinnerbell.domain.events import (
    AssignmentRole,
    Event,
    EventKind,
    EventState,
    Proposal,
    ProposalSource,
    Response,
    Status,
)
from dinnerbell.domain.meals import MealLog, MemberRef, Slot, TagRef

OPENED = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
DAY = date(2026, 10, 22)
ALEX, SAM, KIM, GUEST = 1, 2, 3, 9


def event(kind: EventKind = EventKind.CALL) -> Event:
    return Event(
        7, 1, kind, Slot.DINNER, DAY, EventState.DONE, 1, OPENED, OPENED + timedelta(hours=2), 15, 45,
        decided_item_id=5, decided_mode=Mode.COOK,
    )  # fmt: skip


def answer(member: int, minutes: float = 1, status: Status = Status.IN, **changes: object) -> Response:
    return Response(member, status, responded_at=OPENED + timedelta(minutes=minutes), **changes)  # type: ignore[arg-type]


def facts(**changes: object) -> DoneFacts:
    base = DoneFacts(event(), (), (), frozenset({GUEST}))
    return replace(base, **changes)  # type: ignore[arg-type]


def kinds(found: list[Credit]) -> set[tuple[int, Kind]]:
    return {(c.member_id, c.kind) for c in found}


# Credits ---------------------------------------------------------------------------------------


def test_each_assignment_credits_its_role() -> None:
    assigned = (
        (ALEX, AssignmentRole.COOK),
        (SAM, AssignmentRole.PICKUP),
        (KIM, AssignmentRole.ORDER),
        (ALEX, AssignmentRole.COORDINATOR),
    )
    assert kinds(credits_for_done(facts(assignments=assigned))) == {
        (ALEX, Kind.COOKED),
        (SAM, Kind.PICKED_UP),
        (KIM, Kind.ORDERED),
        (ALEX, Kind.COORDINATED),
    }


def test_being_in_counts_as_participating_and_out_or_maybe_does_not() -> None:
    responses = (answer(ALEX), answer(SAM, status=Status.OUT), answer(KIM, status=Status.MAYBE))
    found = kinds(credits_for_done(facts(responses=responses)))
    assert (ALEX, Kind.PARTICIPATED) in found
    assert not {(SAM, Kind.PARTICIPATED), (KIM, Kind.PARTICIPATED)} & found


@pytest.mark.parametrize(
    ("minutes", "on_time"),
    [(0, True), (14.9, True), (ON_TIME.total_seconds() / 60, True), (15.1, False), (60, False)],
)
def test_an_answer_within_15_minutes_of_the_call_opening_is_on_time(minutes: float, on_time: bool) -> None:
    found = kinds(credits_for_done(facts(responses=(answer(ALEX, minutes),))))
    assert ((ALEX, Kind.RESPONDED_ON_TIME) in found) is on_time


def test_any_quick_answer_is_on_time_even_out_but_a_late_joiner_and_an_offer_never_are() -> None:
    assert (SAM, Kind.RESPONDED_ON_TIME) in kinds(
        credits_for_done(facts(responses=(answer(SAM, 2, status=Status.OUT),)))
    )
    late = answer(KIM, 2, late=True)
    assert (KIM, Kind.RESPONDED_ON_TIME) not in kinds(credits_for_done(facts(responses=(late,))))
    assert (KIM, Kind.PARTICIPATED) in kinds(credits_for_done(facts(responses=(late,))))  # late joiners take part
    offer = replace(facts(), event=event(EventKind.OFFER), responses=(answer(ALEX, 1),))
    assert (ALEX, Kind.RESPONDED_ON_TIME) not in kinds(credits_for_done(offer))


def test_answers_from_before_a_rerun_opened_the_call_again_are_not_quick() -> None:
    """A re-run moves the opening to now and keeps the old answers (``responded_at`` before it)."""
    rerun = replace(event(), opens_at=OPENED + timedelta(hours=3))
    old = answer(ALEX, 1)  # an hour into the first run
    fresh = Response(SAM, Status.IN, responded_at=OPENED + timedelta(hours=3, minutes=2))
    found = kinds(credits_for_done(replace(facts(), event=rerun, responses=(old, fresh))))
    assert (ALEX, Kind.RESPONDED_ON_TIME) not in found and (ALEX, Kind.PARTICIPATED) in found
    assert (SAM, Kind.RESPONDED_ON_TIME) in found


def test_a_late_joiner_the_coordinator_switched_off_earns_nothing() -> None:
    off = answer(KIM, 2, late=True, status=Status.OUT)
    assert kinds(credits_for_done(facts(responses=(off,)))) == set()


def test_bringing_a_guest_counts_as_hosting_by_head_count_or_by_invitation() -> None:
    by_count = facts(responses=(answer(ALEX, 30, guests=2), answer(SAM, 30)))
    assert (ALEX, Kind.HOSTED) in kinds(credits_for_done(by_count))
    assert (SAM, Kind.HOSTED) not in kinds(credits_for_done(by_count))
    invited = facts(responses=(answer(SAM, 30), answer(GUEST, 30)), invitations={GUEST: SAM})
    assert (SAM, Kind.HOSTED) in kinds(credits_for_done(invited))
    gone = facts(responses=(answer(SAM, 30), answer(GUEST, 30, status=Status.OUT)), invitations={GUEST: SAM})
    assert (SAM, Kind.HOSTED) not in kinds(credits_for_done(gone))


def test_a_guest_earns_only_what_they_pitched_in() -> None:
    """A guest who cooks cooked and coordinated; being at the table, answering fast, and
    suggesting are for members."""
    found = kinds(
        credits_for_done(
            facts(
                assignments=((GUEST, AssignmentRole.COOK), (GUEST, AssignmentRole.COORDINATOR)),
                responses=(answer(GUEST, 1),),
                invitations={GUEST: SAM},
            )
        )
    )
    assert found == {(GUEST, Kind.COOKED), (GUEST, Kind.COORDINATED)}


def proposal(by: int | None, source: ProposalSource = ProposalSource.MEMBER) -> Proposal:
    return Proposal(1, 7, Mode.COOK, 1, source, item_id=5, proposed_by=by)


def test_a_winning_suggestion_credits_who_made_it_and_a_new_one_credits_exploring() -> None:
    fresh = Newness(item_new=True, cuisine_new=False, first_use=False)
    stale = Newness(item_new=False, cuisine_new=False, first_use=False)
    assert kinds(credits_for_done(facts(winner=proposal(KIM), newness=fresh))) == {
        (KIM, Kind.SUGGESTED),
        (KIM, Kind.EXPLORED),
    }
    assert kinds(credits_for_done(facts(winner=proposal(KIM), newness=stale))) == {(KIM, Kind.SUGGESTED)}
    coordinator = proposal(KIM, ProposalSource.COORDINATOR)
    assert (KIM, Kind.SUGGESTED) in kinds(credits_for_done(facts(winner=coordinator)))


def test_a_pick_the_system_made_or_a_guest_credits_no_idea() -> None:
    assert kinds(credits_for_done(facts(winner=proposal(None, ProposalSource.SYSTEM)))) == set()
    assert kinds(credits_for_done(facts(winner=proposal(KIM, ProposalSource.SYSTEM)))) == set()
    assert kinds(credits_for_done(facts(winner=proposal(GUEST)))) == set()


def test_the_creator_of_an_item_is_credited_when_it_is_first_chosen() -> None:
    first = Newness(item_new=False, cuisine_new=False, first_use=True)
    again = Newness(item_new=False, cuisine_new=False, first_use=False)
    found = credits_for_done(facts(item_creator=KIM, newness=first))
    assert found == [Credit(KIM, Kind.ADDED_ITEM, item_id=5)]
    assert credits_for_done(facts(item_creator=KIM, newness=again)) == []
    assert credits_for_done(facts(item_creator=None, newness=first)) == []
    assert credits_for_done(facts(item_creator=GUEST, newness=first)) == []


def test_a_member_has_each_kind_once() -> None:
    both = ((ALEX, AssignmentRole.COOK), (ALEX, AssignmentRole.COOK))
    assert len(credits_for_done(facts(assignments=both))) == 1


# Newness ---------------------------------------------------------------------------------------


PICKS = frozenset({101, 102, 103})


def gamble(
    votes: dict[int, set[int]], *answers: Response, picks: frozenset[int] = PICKS, **changes: object
) -> set[int]:
    """The members who earn ``gambled`` for a call with ``picks`` and these final ``votes``."""
    found = credits_for_done(
        facts(
            picks=picks,
            votes={m: frozenset(v) for m, v in votes.items()},
            responses=answers,
            **changes,  # type: ignore[arg-type]
        )
    )
    return {c.member_id for c in found if c.kind is Kind.GAMBLED}


def test_voting_for_every_pick_or_for_none_leaves_the_choice_to_others() -> None:
    everyone = (answer(ALEX), answer(SAM), answer(KIM))
    votes = {ALEX: {101, 102, 103}, SAM: {102}}  # Kim voted for none; Sam made a choice
    assert gamble(votes, *everyone) == {ALEX, KIM}
    assert gamble({ALEX: {101, 102}}, answer(ALEX)) == set()  # some is a choice


def test_a_call_needs_three_picks_for_all_or_none_to_be_a_gamble() -> None:
    assert GAMBLER_MIN_PICKS == 3
    for picks in (frozenset(), frozenset({101}), frozenset({101, 102})):
        assert gamble({ALEX: set(picks)}, answer(ALEX), picks=picks) == set()
    four = frozenset({101, 102, 103, 104})
    assert gamble({SAM: {101}}, answer(ALEX), picks=four) == {ALEX}


def test_only_a_member_who_was_in_and_not_a_late_joiner_gambles() -> None:
    sam = {SAM: {101}}
    assert gamble(sam, answer(ALEX, status=Status.OUT), answer(KIM, status=Status.MAYBE)) == set()
    assert gamble(sam, answer(ALEX, late=True)) == set()  # a late joiner's votes don't count
    assert gamble({KIM: {101, 102, 103}}, answer(GUEST), answer(KIM), guest_ids=frozenset({GUEST})) == {KIM}
    assert gamble(sam, answer(ALEX), event=event(EventKind.OFFER)) == set()


def test_voting_for_none_counts_without_any_other_votes() -> None:
    assert gamble({}, answer(ALEX), answer(SAM)) == {ALEX, SAM}  # nobody voted (a lock, an automatic decision)
    assert gamble({}, answer(ALEX)) == {ALEX}  # the sole member, no votes at all
    assert gamble({ALEX: {101, 102, 103}}, answer(ALEX)) == {ALEX}  # all needs no one else
    assert gamble({SAM: {101}}, answer(ALEX), answer(SAM)) == {ALEX}  # someone else decided
    assert gamble({ALEX: {101}}, answer(ALEX)) == set()  # their own vote is a choice
    assert gamble({GUEST: {101}}, answer(ALEX), guest_ids=frozenset({GUEST})) == {ALEX}  # guest votes are ignored
    assert gamble({SAM: {999}}, answer(ALEX)) == {ALEX}  # not a vote for any pick


def test_voting_for_none_still_needs_the_other_conditions() -> None:
    two = frozenset({101, 102})
    assert gamble({}, answer(ALEX), picks=two) == set()  # fewer than three picks
    assert gamble({}, answer(ALEX, late=True)) == set()  # a late joiner
    assert gamble({}, answer(ALEX, status=Status.OUT), answer(KIM, status=Status.MAYBE)) == set()  # not in
    assert gamble({}, answer(GUEST), guest_ids=frozenset({GUEST})) == set()  # a guest
    assert gamble({}, answer(ALEX), event=event(EventKind.OFFER)) == set()  # an offer


def test_a_vote_for_a_proposal_that_is_not_a_pick_counts_for_nothing() -> None:
    assert gamble({ALEX: {999}, SAM: {101}}, answer(ALEX), answer(SAM)) == {ALEX}  # none of the real picks


def meal(
    meal_id: int, day: date, item: int, *cuisines: int, slot: Slot = Slot.DINNER, event_id: int | None = 1
) -> MealLog:
    return MealLog(
        meal_id, 1, day, slot, item, f"item {item}", tuple(TagRef(c, f"c{c}") for c in cuisines),
        (MemberRef(1, "A"),), None, event_id,
    )  # fmt: skip


def test_newness_looks_only_at_meals_before_it_whatever_order_they_are_given_in() -> None:
    mon, tue, wed = date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)
    meals = [meal(1, mon, 10, 1), meal(2, tue, 10, 1), meal(3, wed, 11, 1), meal(4, wed, 12, 2, slot=Slot.LUNCH)]
    found = newness_by_meal(meals)
    assert found == newness_by_meal(meals[::-1])
    assert found[1] == Newness(item_new=True, cuisine_new=True, first_use=True)
    assert found[2] == Newness(item_new=False, cuisine_new=False, first_use=False)
    assert found[3] == Newness(item_new=True, cuisine_new=False, first_use=True)
    # Lunch comes before dinner on the same day, so a lunch on Wednesday is before Wednesday's dinner.
    assert found[4].cuisine_new
    later_dinner = meal(5, wed, 12, 2)
    assert not newness_by_meal([*meals, later_dinner])[5].item_new


def test_a_meal_logged_by_hand_makes_an_item_old_but_not_first_used() -> None:
    mon, tue = date(2026, 10, 19), date(2026, 10, 20)
    by_hand = meal(1, mon, 10, 1, event_id=None)
    chosen = meal(2, tue, 10, 1)
    found = newness_by_meal([by_hand, chosen])[2]
    assert (found.item_new, found.first_use) == (False, True)


# Thresholds and tiers --------------------------------------------------------------------------


def test_the_ladder_has_five_tiers_and_thirteen_badges() -> None:
    assert [t.value for t in TIERS] == ["first", "bronze", "silver", "gold", "diamond"]
    assert DEFAULT_THRESHOLDS == (1, 10, 50, 100, 200)
    assert len(BADGES) == 13 and len(BY_KEY) == 13 and {b.key for b in BADGES} == set(BadgeKey)
    assert [b.kind for b in BADGES if b.kind is None] == [None]  # only On a Roll counts weeks


@pytest.mark.parametrize(
    ("count", "reached"),
    [
        (0, 0), (1, 1), (9, 1), (10, 2), (49, 2), (50, 3), (99, 3), (100, 4), (199, 4), (200, 5), (5000, 5),
    ],
)  # fmt: skip
def test_each_tier_is_reached_exactly_at_its_threshold(count: int, reached: int) -> None:
    assert tiers_reached(count, DEFAULT_THRESHOLDS) == list(TIERS[:reached])


def test_thresholds_come_from_settings_and_fall_back_to_the_default() -> None:
    assert thresholds_from({}) == DEFAULT_THRESHOLDS
    assert thresholds_from({SETTINGS_KEY: [2, 4, 6, 8, 10]}) == (2, 4, 6, 8, 10)
    for broken in ([1, 2, 3], "1,10,50", [5, 5, 6, 7, 8], [0, 1, 2, 3, 4], None, [1, 2, 3, 4, "x"]):
        assert thresholds_from({SETTINGS_KEY: broken}) == DEFAULT_THRESHOLDS


@pytest.mark.parametrize(
    "bad",
    [[1, 2, 3, 4], [1, 2, 3, 4, 5, 6], [1, 1, 2, 3, 4], [3, 2, 1, 0, -1], [0, 1, 2, 3, 4], [1, 2, 3, 4, 10_001],
     [1, 2, 3, 4, True], [1, 2, 3, 4, 5.5], [1, 2, 3, 4, "5"]],
)  # fmt: skip
def test_a_ladder_needs_five_whole_rising_counts(bad: list[object]) -> None:
    with pytest.raises(InvalidThresholds):
        check_thresholds(bad)


def test_progress_names_the_next_tier_and_how_far() -> None:
    chef = BY_KEY[BadgeKey.CHEF]
    none = progress(chef, 0, DEFAULT_THRESHOLDS)
    assert (none.tier, none.next_tier, none.next_at, none.remaining) == (None, Tier.FIRST, 1, 1)
    some = progress(chef, 12, DEFAULT_THRESHOLDS)
    assert (some.tier, some.next_tier, some.next_at, some.remaining) == (Tier.BRONZE, Tier.SILVER, 50, 38)
    top = progress(chef, 250, DEFAULT_THRESHOLDS)
    assert (top.tier, top.next_tier, top.remaining) == (Tier.DIAMOND, None, None)


def test_a_tier_earned_before_the_admin_raised_a_threshold_still_counts_as_reached() -> None:
    chef = BY_KEY[BadgeKey.CHEF]
    raised = (5, 20, 60, 120, 240)
    kept = progress(chef, 3, raised, earned=[Tier.FIRST, Tier.BRONZE])
    assert (kept.tier, kept.next_tier, kept.next_at) == (Tier.BRONZE, Tier.SILVER, 60)


# On a Roll -------------------------------------------------------------------------------------


def test_a_streak_is_calendar_weeks_in_a_row() -> None:
    mon = date(2026, 10, 5)
    assert longest_streak([]) == 0
    assert longest_streak([mon]) == 1
    assert longest_streak([mon, mon + timedelta(days=3)]) == 1  # one week
    assert longest_streak([mon, mon + timedelta(days=7), mon + timedelta(days=14)]) == 3
    assert longest_streak([mon, mon + timedelta(days=6), mon + timedelta(days=8)]) == 2  # Mon, Sun | Tue next week
    gap = [mon, mon + timedelta(days=7), mon + timedelta(days=21), mon + timedelta(days=28), mon + timedelta(days=35)]
    assert longest_streak(gap) == 3
    assert longest_streak(reversed(gap)) == 3
