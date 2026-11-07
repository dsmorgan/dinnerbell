# SPDX-License-Identifier: AGPL-3.0-or-later
"""An admin corrects the place after **Mark done** (#220, spec 5.7): who may, every stored copy of
the place, the feedback that moves with it, the counts and badges that follow, and the web."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from dinnerbell.channels.slack.messages import render
from dinnerbell.config import Settings
from dinnerbell.db.contributions import ContributionRepository
from dinnerbell.db.feedback import FeedbackRepository
from dinnerbell.db.models import AuditRow, CatalogItemRow, ContributionRow, EventRow, FeedbackRow, MealLogRow
from dinnerbell.domain.badges import Announcement, BadgeKey, Tier
from dinnerbell.domain.catalog import ItemKind, Mode
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import AFTER_DECISION_RANK, AssignmentRole, EventRuleViolation, StaleRun
from dinnerbell.domain.feedback import FeedbackInput, PlaceIssue, Rating, answers_for
from dinnerbell.domain.fun import DONE_CAPTION
from dinnerbell.domain.guests import REFUSED, GuestAction
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.preferences import Stance, TargetType
from dinnerbell.services import (
    activity,
    announcements,
    calls,
    contributions,
    coordinator,
    event_views,
    feedback,
    fun,
    meals,
    offers,
    stats,
    variety,
)
from dinnerbell.services import call_context as cc
from dinnerbell.services.coordinator import PLAN_CHANGED, AdjustmentRefused
from dinnerbell.services.offers import OfferInput
from dinnerbell.services.preferences import set_my_preference
from tests.conftest import invite, page, sign_in
from tests.support.calls import House
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import htmx, submit
from tests.test_auth_flow import rows
from tests.test_change_place import guest_coordinating, tacos, web_decided
from tests.test_contributions import DAY, LUNCH_AT, MORNING, awards, counts, dinner, held
from tests.test_coordinator import CTX, act, at_deadline, open_dinner, pick_ids, sent, to_voting, vote
from tests.test_coordinator import database as database  # the fixtures, re-exported for pytest
from tests.test_coordinator import house as house
from tests.test_events_web import admin as admin
from tests.test_events_web import proposal_ids, sign_in_as
from tests.test_events_web import start as web_start
from tests.test_events_web import tick as web_tick
from tests.test_slack_adjust import decided

pytestmark = pytest.mark.anyio


async def done_call(house: House) -> int:
    """``decided``, marked done: Alex picks up Siam Garden, Lee orders, and Sam coordinates."""
    event_id = await decided(house)
    await act(house, calls.mark_done, house["Alex"], event_id)
    return event_id


async def correct(house: House, event_id: int, **kwargs: Any) -> None:
    """The admin (Alex) corrects the place, from what the call has now unless told otherwise."""
    who = kwargs.pop("who", "Alex")
    shown = kwargs.pop("opened_item_id", (await house.event(event_id)).decided_item_id)
    house.clock.advance(timedelta(seconds=1))
    await act(house, coordinator.correct_place, house[who], event_id, opened_item_id=shown, **kwargs)


async def to_tacos(house: House, event_id: int) -> None:
    await correct(house, event_id, item_id=tacos(house), mode=Mode.COOK)


async def to_siam(house: House, event_id: int) -> None:
    await correct(house, event_id, item_id=house.items["Siam Garden"].id, mode=Mode.PICKUP)


async def to_taqueria(house: House, event_id: int) -> None:
    await correct(house, event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)


async def meal_row(house: House, event_id: int) -> MealLogRow:
    (row,) = [m for m in await house.meals() if m.event_id == event_id]
    return row


async def credited(house: House, event_id: int, kind: Kind) -> set[int]:
    async with house.sessionmaker() as db:
        return await ContributionRepository(db).credited(house.household.id, event_id, kind)


async def give(house: House, name: str, meal_id: int, answers: FeedbackInput) -> None:
    async with house.sessionmaker() as db:
        await feedback.give(db, house[name], meal_id, answers, now=house.clock())


async def feedback_rows(house: House) -> list[FeedbackRow]:
    async with house.sessionmaker() as db:
        return list(await db.scalars(select(FeedbackRow).order_by(FeedbackRow.id)))


async def hints(house: House) -> dict[int, str]:
    async with house.sessionmaker() as db:
        return await feedback.hints_on(db, house.household.id, DAY)


async def mine(house: House, name: str, event_id: int) -> FeedbackInput | None:
    async with house.sessionmaker() as db:
        found = await feedback.event_meal(db, house[name], event_id, now=house.clock())
    assert found is not None
    return found[1].mine


async def thanks(house: House, name: str, event_id: int) -> Any:
    async with house.sessionmaker() as db:
        found = await feedback.event_meal(db, house[name], event_id, now=house.clock())
    assert found is not None
    return found[1].thanks


async def recounts(house: House) -> list[AuditRow]:
    return await house.audits("event.contributions_recounted")


async def kim_pitches(house: House, event_id: int) -> None:
    """Kim adds Pho House, a place and a cuisine nobody has had, as a free-text pick."""
    tag = house.tags.get("Vietnamese") or await house.tag("Vietnamese")
    await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, free_text="Pho House", cuisine_id=tag.id)


async def kims_dinner(house: House, n: int = 0) -> int:
    """A done dinner on Kim's Pho House, which she adds to the catalog by winning with it."""
    return await dinner(
        house, n=n, ins=("Alex", "Sam", "Kim"), while_voting=lambda e: kim_pitches(house, e), vote_for="Kim"
    )


# Who may, and when --------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["Sam", "Lee", "Kim"])
async def test_only_an_admin_may_so_not_the_coordinator_or_a_member(house: House, who: str) -> None:
    event_id = await done_call(house)  # Sam coordinates
    with pytest.raises(EventRuleViolation) as caught:
        await correct(house, event_id, who=who, item_id=tacos(house), mode=Mode.COOK)
    assert caught.value.code == "not_admin"
    assert (await house.event(event_id)).decided_item_id == house.items["Siam Garden"].id
    assert (await meal_row(house, event_id)).item_id == house.items["Siam Garden"].id


async def test_a_guest_coordinator_is_refused(house: House) -> None:
    event_id, pat = await guest_coordinating(house)
    meal_time = (await house.event(event_id)).meal_time
    assert meal_time is not None
    house.clock.now = meal_time
    await act(house, calls.mark_done, pat, event_id)
    with pytest.raises(EventRuleViolation) as caught:
        await act(
            house,
            coordinator.correct_place,
            pat,
            event_id,
            item_id=tacos(house),
            mode=Mode.COOK,
            opened_item_id=house.items["Siam Garden"].id,
        )
    assert caught.value.code == "guests_dont" and str(caught.value) == REFUSED[GuestAction.CHANGE_PLACE]


async def test_a_call_that_is_not_done_is_refused(house: House) -> None:
    event_id = await decided(house)
    with pytest.raises(EventRuleViolation) as caught:
        await correct(house, event_id, item_id=tacos(house), mode=Mode.COOK)
    assert caught.value.code == "not_done"


async def test_a_called_off_call_is_refused(house: House) -> None:
    event_id = await decided(house)
    await act(house, calls.cancel_call, house["Alex"], event_id)
    with pytest.raises(EventRuleViolation) as caught:
        await correct(house, event_id, item_id=tacos(house), mode=Mode.COOK, opened_item_id=None)
    assert caught.value.code == "not_done"


async def test_an_abandoned_call_is_refused(house: House) -> None:
    event_id = await open_dinner(house)
    house.clock.now = (await house.event(event_id)).respond_deadline + timedelta(minutes=1)
    await house.tick()
    assert (await house.event(event_id)).state.value == "abandoned"
    with pytest.raises(EventRuleViolation) as caught:
        await correct(house, event_id, item_id=tacos(house), mode=Mode.COOK, opened_item_id=None)
    assert caught.value.code == "not_done"


async def test_an_offer_is_refused(house: House) -> None:
    house.clock.now = MORNING
    data = OfferInput(slot=Slot.LUNCH, day=DAY, mode=Mode.COOK, at=time(12, 30), capacity=3, item_id=tacos(house))
    posted = await act(house, offers.post_offer, house["Sam"], data)
    house.clock.now = LUNCH_AT
    await house.tick()
    house.clock.now = LUNCH_AT + timedelta(hours=2)
    await house.tick()
    assert (await house.event(posted.event_id)).state.value == "done"
    with pytest.raises(EventRuleViolation) as caught:
        await correct(house, posted.event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    assert caught.value.code == "is_offer"


async def test_a_stale_page_a_stale_run_and_a_way_the_place_does_not_allow_are_refused(house: House) -> None:
    event_id = await done_call(house)
    siam, taqueria = house.items["Siam Garden"].id, house.items["Taqueria"].id
    with pytest.raises(AdjustmentRefused) as stale:
        await correct(house, event_id, item_id=tacos(house), mode=Mode.COOK, opened_item_id=taqueria)
    assert str(stale.value) == PLAN_CHANGED
    with pytest.raises(StaleRun):
        await correct(house, event_id, item_id=tacos(house), mode=Mode.COOK, run=2)
    with pytest.raises(AdjustmentRefused) as way:
        await correct(house, event_id, item_id=taqueria, mode=Mode.DELIVERY)
    assert way.value.part == "place_mode"
    with pytest.raises(AdjustmentRefused) as no_way:
        await correct(house, event_id, name="Noodle Bar")
    assert no_way.value.part == "place_mode"
    with pytest.raises(EventRuleViolation) as same:
        await correct(house, event_id, item_id=siam, mode=Mode.DELIVERY)
    assert same.value.code == "same_place"
    assert (await house.event(event_id)).decided_item_id == siam
    assert not [a for a in await house.audits() if a.action == "event.place_changed"]


async def test_a_meal_deleted_from_history_has_nothing_to_correct(house: House) -> None:
    event_id = await done_call(house)
    await act(house, meals.delete_meal, house["Alex"], (await meal_row(house, event_id)).id)
    with pytest.raises(EventRuleViolation) as caught:
        await to_tacos(house, event_id)
    assert caught.value.code == "no_meal"
    assert (await house.event(event_id)).decided_item_id == house.items["Siam Garden"].id


async def test_a_place_out_of_the_catalog_is_refused_and_filters_do_not_block_one_in_it(house: House) -> None:
    event_id = await done_call(house)
    item = house.items["Sam's Tacos"]
    async with house.sessionmaker() as db:
        await set_my_preference(db, house["Lee"], TargetType.ITEM, item.id, Stance(veto=True), now=house.clock())
    await to_tacos(house, event_id)  # vetoed, nobody offered to cook, and Mexican isn't for the variety window to say
    assert (await house.event(event_id)).decided_item_id == item.id

    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE catalog_item SET active = 0 WHERE id = :id"), {"id": item.id})
        await db.commit()
    await to_siam(house, event_id)
    with pytest.raises(AdjustmentRefused) as caught:
        await correct(house, event_id, item_id=item.id, mode=Mode.COOK)
    assert caught.value.part == "place"


# Every stored copy --------------------------------------------------------------------------------


async def test_the_call_the_meal_and_their_cuisines_all_say_the_corrected_place(house: House) -> None:
    event_id = await done_call(house)
    before = await meal_row(house, event_id)
    assert [c["name"] for c in before.cuisine_tags] == ["Thai"]
    done_at = (await house.event(event_id)).done_at
    await to_taqueria(house, event_id)

    event, ctx = await house.event(event_id), await house.context(event_id)
    assert (event.decided_item_id, event.decided_mode) == (house.items["Taqueria"].id, Mode.PICKUP)
    assert event.state.value == "done" and event.done_at == done_at and event.run == 1
    row = await meal_row(house, event_id)
    assert row.id == before.id and row.item_id == house.items["Taqueria"].id
    assert [c["name"] for c in row.cuisine_tags] == ["Mexican"]
    assert (row.date, row.slot, row.participants) == (before.date, before.slot, before.participants)
    assert ctx.winner is not None and ctx.winner.item_id == house.items["Taqueria"].id
    decision = cc.decision_info(ctx)
    assert decision is not None and decision.proposal.item.name == "Taqueria"


async def test_history_stats_the_variety_window_and_ranking_read_the_corrected_place(house: House) -> None:
    event_id = await done_call(house)
    siam, taqueria = house.items["Siam Garden"].id, house.items["Taqueria"].id
    meal_id = (await meal_row(house, event_id)).id
    await give(house, "Lee", meal_id, FeedbackInput(rating=Rating.LOVED))
    await give(house, "Alex", meal_id, FeedbackInput(rating=Rating.LOVED))
    assert (await hints(house)) == {siam: "Well liked"}
    await to_taqueria(house, event_id)

    day = (await house.event(event_id)).day
    async with house.sessionmaker() as db:
        window = await variety.variety_window(db, house.household.id, day)
        eaten = await variety.last_eaten(db, house.household.id)
        page_of = await meals.history(db, house["Alex"])
        totals = await stats.household_stats(db, house["Alex"])
        group = await FeedbackRepository(db).opinions(house.household.id, member_ids=[house["Lee"].id])
    assert {i.item_id for i in window.items} == {taqueria}
    assert {c.name for c in window.cuisines} == {"Mexican"}
    assert eaten == {taqueria: day}  # the date stays, so date windows read the corrected meal on its day
    assert [m.item_name for _, week in page_of.weeks for m in week] == ["Taqueria"]
    assert totals.meals == 1
    assert [o.item_id for o in group] == [taqueria]
    assert (await hints(house)) == {taqueria: "Well liked"}  # and off Siam Garden


async def test_a_new_name_joins_the_catalog_and_earns_nobody_idea_credit(house: House) -> None:
    event_id = await done_call(house)
    thai = house.tags["Thai"].id
    await correct(house, event_id, name="Noodle Bar", mode=Mode.DELIVERY, cuisine_id=thai)
    ctx = await house.context(event_id)
    (item,) = [i for i in ctx.items.values() if i.name == "Noodle Bar"]
    assert item.kind is ItemKind.RESTAURANT and item.created_by is None and item.modes == frozenset({Mode.DELIVERY})
    assert (await house.event(event_id)).decided_item_id == item.id
    row = await meal_row(house, event_id)
    assert (row.item_id, [c["name"] for c in row.cuisine_tags]) == (item.id, ["Thai"])
    for kind in (Kind.SUGGESTED, Kind.EXPLORED, Kind.ADDED_ITEM):
        assert await credited(house, event_id, kind) == set()
    (audit,) = [a for a in await house.audits() if a.action == "event.place_changed"]
    assert audit.payload["new_item"] is True and audit.payload["to_item_id"] == item.id
    assert (await house.audits("catalog.item_created"))[-1].payload["name"] == "Noodle Bar"


async def test_a_name_the_catalog_has_is_that_place(house: House) -> None:
    event_id = await done_call(house)
    await correct(house, event_id, name="taqueria", mode=Mode.PICKUP)
    assert (await house.event(event_id)).decided_item_id == house.items["Taqueria"].id


async def test_the_row_outside_the_picks_is_added_replaced_and_gone_when_a_pick_is_reused(house: House) -> None:
    event_id = await done_call(house)
    siam = (await pick_ids(house, event_id))["Siam Garden"]
    picks_before = [p.id for p in (await house.context(event_id)).picks]
    count = len((await house.context(event_id)).proposals)

    await to_tacos(house, event_id)  # added
    ctx = await house.context(event_id)
    assert ctx.winner is not None and ctx.winner.rank == AFTER_DECISION_RANK
    assert len(ctx.proposals) == count + 1 and [p.id for p in ctx.picks] == picks_before

    await correct(house, event_id, name="Noodle Bar", mode=Mode.PICKUP)  # replaced
    ctx = await house.context(event_id)
    assert ctx.winner is not None and ctx.winner.rank == AFTER_DECISION_RANK
    assert len(ctx.proposals) == count + 1 and [p.id for p in ctx.picks] == picks_before

    await to_siam(house, event_id)  # a pick reused, keeping its votes
    ctx = await house.context(event_id)
    assert ctx.winner is not None and ctx.winner.id == siam and len(ctx.proposals) == count
    assert ctx.votes[siam] == [house["Alex"].id]


async def test_how_the_old_place_won_stops_showing(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await act(house, calls.lock, house["Sam"], event_id, (await pick_ids(house, event_id))["Taqueria"])
    await act(house, calls.mark_done, house["Alex"], event_id)
    await to_tacos(house, event_id)
    event = await house.event(event_id)
    assert (event.locked_by, event.tie_how, event.tie_by) == (None, None, None)
    (*_, updated) = await sent(house, "decision_updated")
    assert updated.locked_by is None


async def test_it_is_audited_as_a_place_change_after_done_and_logged(
    house: House, caplog: pytest.LogCaptureFixture
) -> None:
    event_id = await done_call(house)
    meal_id = (await meal_row(house, event_id)).id
    await give(house, "Lee", meal_id, FeedbackInput(rating=Rating.FINE))
    with caplog.at_level(logging.INFO, logger=activity.log.name):
        await to_taqueria(house, event_id)
    (audit,) = [a for a in await house.audits() if a.action == "event.place_changed"]
    assert audit.actor_member_id == house["Alex"].id and audit.target == f"event:{event_id}"
    payload = audit.payload
    assert payload["after_done"] is True and payload["as"] == "admin" and payload["feedback_moved"] == 1
    assert payload["from_item_id"] == house.items["Siam Garden"].id and payload["from_mode"] == "pickup"
    assert payload["to_item_id"] == house.items["Taqueria"].id and payload["to_mode"] == "pickup"
    assert payload["assignments"] == {"order": house["Lee"].id, "pickup": house["Alex"].id}
    (line,) = [r for r in caplog.records if r.getMessage() == "change_place"]
    assert line.__dict__["after_done"] is True and line.__dict__["state_after"] == "done"


# Nobody is messaged -------------------------------------------------------------------------------


async def test_it_messages_nobody_and_the_posted_messages_update(house: House) -> None:
    """Before the meal time too: marking it done early doesn't turn the correction into news."""
    event_id = await done_call(house)
    assert (await house.event(event_id)).meal_time > house.clock()
    updates = {"call_updated", "proposals_updated", "decision_updated"}
    before = Counter(e.kind for e in await house.outbox() if e.kind not in updates)
    await to_tacos(house, event_id)
    # Updates replace the ones still waiting, so the outbox holds no more than the new state.
    assert Counter(e.kind for e in await house.outbox() if e.kind not in updates) == before
    (*_, last) = await sent(house, "decision_updated")
    assert last.decision.proposal.item.name == "Sam's Tacos"
    assert "Sam's Tacos" in json.dumps(render(last, CTX).payload())
    (*_, picks) = await sent(house, "proposals_updated")
    assert "Sam's Tacos" not in json.dumps(render(picks, CTX).payload())


async def test_the_den_still_says_done_and_never_flips_for_a_badge_taken_back(house: House) -> None:
    event_id = await done_call(house)
    assert ("Alex", BadgeKey.ROAD_RUNNER, Tier.FIRST) in held(await awards(house))
    await to_tacos(house, event_id)  # nobody picks up a home meal
    async with house.sessionmaker() as db:
        alex = await fun.den(db, house["Alex"], house.household, now=house.clock(), with_badge=True)
    assert alex.caption == DONE_CAPTION
    assert alex.badge is None or "Road Runner" not in alex.badge.caption


# Feedback -----------------------------------------------------------------------------------------


async def test_feedback_moves_with_its_answers_and_when_it_was_saved(house: House) -> None:
    event_id = await done_call(house)
    meal_id = (await meal_row(house, event_id)).id
    await give(house, "Lee", meal_id, FeedbackInput(rating=Rating.LIKED, place_issues=frozenset({PlaceIssue.LATE})))
    (before,) = await feedback_rows(house)
    await correct(house, event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    (row,) = await feedback_rows(house)
    assert (row.id, row.meal_id, row.member_id) == (before.id, meal_id, house["Lee"].id)
    assert (row.item_id, row.item_name, row.item_kind, row.mode) == (
        house.items["Taqueria"].id,
        "Taqueria",
        "restaurant",
        "pickup",
    )
    assert (row.rating, row.place_issues) == ("liked", ["late"])
    assert (row.rating_saved_at, row.updated_at) == (before.rating_saved_at, before.updated_at)
    assert await hints(house) == {house.items["Taqueria"].id: "Recently late"}


async def test_correcting_many_times_keeps_one_row_for_each_person(house: House) -> None:
    event_id = await done_call(house)
    meal_id = (await meal_row(house, event_id)).id
    await give(house, "Lee", meal_id, FeedbackInput(rating=Rating.LIKED))
    await give(house, "Alex", meal_id, FeedbackInput(rating=Rating.FINE))
    await to_taqueria(house, event_id)
    await to_tacos(house, event_id)
    await to_siam(house, event_id)
    rows_now = await feedback_rows(house)
    assert [(r.member_id, r.item_id, r.item_kind) for r in rows_now] == [
        (house["Lee"].id, house.items["Siam Garden"].id, "restaurant"),
        (house["Alex"].id, house.items["Siam Garden"].id, "restaurant"),
    ]


async def test_what_went_wrong_sleeps_on_a_home_meal_and_comes_back_with_the_restaurant(house: House) -> None:
    event_id = await done_call(house)
    meal_id = (await meal_row(house, event_id)).id
    answers = FeedbackInput(rating=Rating.DISLIKED, place_issues=frozenset({PlaceIssue.COLD}), issue_note="Qz8")
    await give(house, "Lee", meal_id, answers)
    siam = house.items["Siam Garden"].id
    assert "Recently cold" in (await hints(house))[siam]

    await to_tacos(house, event_id)
    assert await hints(house) == {}  # one rating gives no verdict, and a home meal has no issues
    shown = await mine(house, "Lee", event_id)
    assert shown is not None and shown.rating is Rating.DISLIKED and not shown.place_issues and not shown.issue_note
    (row,) = await feedback_rows(house)
    assert row.place_issues == ["cold", "other"] and row.issue_note == "Qz8"  # kept, not shown

    await to_siam(house, event_id)
    shown = await mine(house, "Lee", event_id)
    assert shown is not None and shown.place_issues == frozenset({PlaceIssue.COLD, PlaceIssue.OTHER})
    assert shown.issue_note == "Qz8" and "Recently cold" in (await hints(house))[siam]


async def test_feedback_with_nothing_that_applies_now_shows_as_none_and_still_counts_as_given(house: House) -> None:
    event_id = await done_call(house)
    meal_id = (await meal_row(house, event_id)).id
    await give(house, "Lee", meal_id, FeedbackInput(place_issues=frozenset({PlaceIssue.LATE})))
    await to_tacos(house, event_id)
    assert await mine(house, "Lee", event_id) is None
    async with house.sessionmaker() as db:
        form = await feedback.form(db, house["Lee"], meal_id, now=house.clock())
        waiting = await feedback.awaiting(db, house["Lee"], now=house.clock())
    assert form.mine is None and not form.facts.has_place and not waiting
    assert (await counts(house, "Lee"))[Kind.GAVE_FEEDBACK] == 1


async def home_dinner(house: House) -> int:
    """A done dinner of Sam's Tacos that Sam cooks and Lee thanks Sam for."""
    event_id = await to_voting(house, "Sam", "Lee", coordinator_name="Sam")
    await act(house, calls.mark_done, house["Alex"], await _decide_on_tacos(house, event_id))
    meal_id = (await meal_row(house, event_id)).id
    await give(house, "Lee", meal_id, FeedbackInput(rating=Rating.LOVED, cook_note="Zq7x"))
    return event_id


async def _decide_on_tacos(house: House, event_id: int) -> int:
    await at_deadline(house, event_id)
    ctx = await house.context(event_id)
    await act(
        house,
        coordinator.change_place,
        house["Sam"],
        event_id,
        item_id=tacos(house),
        mode=Mode.COOK,
        member_id=house["Sam"].id,
        opened_item_id=ctx.event.decided_item_id,
    )
    return event_id


async def test_a_thank_you_stops_showing_on_a_restaurant_and_comes_back_with_the_home_meal(house: House) -> None:
    event_id = await home_dinner(house)
    before = await thanks(house, "Sam", event_id)
    assert before.count == 1 and before.notes == (("Lee", "Zq7x"),)

    await correct(house, event_id, item_id=house.items["Taqueria"].id, mode=Mode.PICKUP)
    for name in ("Sam", "Lee", "Alex"):
        shown = await thanks(house, name, event_id)
        assert shown.count == 0 and shown.notes == () and shown.cook is None
    assert not (await mine(house, "Lee", event_id) or FeedbackInput()).cook_thanks
    (row,) = await feedback_rows(house)
    assert row.cook_thanks and row.cook_note == "Zq7x" and row.cook_member_id == house["Sam"].id

    await correct(house, event_id, item_id=tacos(house), mode=Mode.COOK)
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.COOK, house["Sam"].id)
    assert await thanks(house, "Sam", event_id) == before


def test_answers_for_keeps_what_applies_to_the_kind() -> None:
    answers = FeedbackInput(
        rating=Rating.FINE,
        place_issues=frozenset({PlaceIssue.OTHER}),
        issue_note="Qz8",
        cook_thanks=True,
        cook_note="Zq7x",
    )
    assert answers_for(answers, ItemKind.HOME) == FeedbackInput(Rating.FINE, cook_thanks=True, cook_note="Zq7x")
    assert answers_for(answers, ItemKind.RESTAURANT) == FeedbackInput(Rating.FINE, frozenset({PlaceIssue.OTHER}), "Qz8")


# The counts and badges ----------------------------------------------------------------------------


async def test_a_way_without_a_role_drops_its_count_and_its_badges(house: House) -> None:
    event_id = await done_call(house)
    before = held(await awards(house))
    assert await credited(house, event_id, Kind.PICKED_UP) == {house["Alex"].id}
    assert await credited(house, event_id, Kind.ORDERED) == {house["Lee"].id}
    await to_tacos(house, event_id)

    assert await credited(house, event_id, Kind.PICKED_UP) == set()
    assert await credited(house, event_id, Kind.ORDERED) == set()
    assert await credited(house, event_id, Kind.COOKED) == set()  # nobody offered, so nobody's named
    assert await credited(house, event_id, Kind.COORDINATED) == {house["Sam"].id}
    assert (await counts(house, "Alex"))[Kind.PARTICIPATED] == 1
    after = held(await awards(house))
    assert before - after >= {
        ("Alex", BadgeKey.ROAD_RUNNER, Tier.FIRST),
        ("Lee", BadgeKey.ORDER_UP, Tier.FIRST),
    }
    (audit,) = await recounts(house)
    assert audit.payload["for"] == "place" and audit.actor_member_id == house["Alex"].id
    assert audit.payload["removed"] == {"picked_up": [house["Alex"].id], "ordered": [house["Lee"].id]}
    assert audit.payload["added"] == {}
    revoked = {(b["member_id"], b["badge"]) for b in audit.payload["badges_revoked"]}
    assert (house["Alex"].id, "road_runner") in revoked and (house["Lee"].id, "order_up") in revoked


async def test_delivery_drops_the_pickup_count_and_keeps_the_orderer(house: House) -> None:
    event_id = await done_call(house)
    await house.add_item("Noodle Bar", Mode.DELIVERY)
    await correct(house, event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.DELIVERY)
    assert await credited(house, event_id, Kind.PICKED_UP) == set()
    assert await credited(house, event_id, Kind.ORDERED) == {house["Lee"].id}


async def test_a_new_cook_who_offered_earns_the_count_pending(house: House) -> None:
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), cooks=("Sam",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    await act(house, calls.mark_done, house["Alex"], event_id)
    await to_tacos(house, event_id)
    assert await credited(house, event_id, Kind.COOKED) == {house["Sam"].id}
    assert ("Sam", BadgeKey.CHEF, Tier.FIRST, Announcement.PENDING) in await awards(house)


async def test_nothing_that_changes_no_count_is_recounted_or_audited_as_a_recount(house: House) -> None:
    event_id = await done_call(house)
    before = await awards(house)
    await to_taqueria(house, event_id)  # the same way and people, and the system's pick, which credits nobody
    assert await recounts(house) == []
    assert await awards(house) == before


async def test_correcting_away_from_a_winning_find_takes_every_idea_count_and_correcting_back_restores(
    house: House,
) -> None:
    event_id = await kims_dinner(house)
    pho = (await house.event(event_id)).decided_item_id
    kim = await counts(house, "Kim")
    assert (kim[Kind.SUGGESTED], kim[Kind.EXPLORED], kim[Kind.ADDED_ITEM]) == (1, 1, 1)
    ideas = {(BadgeKey.BRIGHT_IDEA, Tier.FIRST), (BadgeKey.EXPLORER, Tier.FIRST), (BadgeKey.SCOUT, Tier.FIRST)}
    assert ideas <= {(b, t) for _, b, t, _ in await awards(house, "Kim")}

    await to_taqueria(house, event_id)  # a system pick: it names nobody
    kim = await counts(house, "Kim")
    assert not {Kind.SUGGESTED, Kind.EXPLORED, Kind.ADDED_ITEM} & set(kim)
    assert not ideas & {(b, t) for _, b, t, _ in await awards(house, "Kim")}
    (away,) = await recounts(house)
    assert away.payload["removed"] == {
        "suggested": [house["Kim"].id],
        "explored": [house["Kim"].id],
        "added_item": [house["Kim"].id],
    }

    await correct(house, event_id, item_id=pho, mode=Mode.PICKUP)
    kim = await counts(house, "Kim")
    assert (kim[Kind.SUGGESTED], kim[Kind.EXPLORED], kim[Kind.ADDED_ITEM]) == (1, 1, 1)
    restored = {(b, t, a) for _, b, t, a in await awards(house, "Kim") if (b, t) in ideas}
    assert {(b, t) for b, t, _ in restored} == ideas
    assert {a for _, _, a in restored} == {Announcement.PENDING}
    assert len(await recounts(house)) == 2


async def test_the_new_place_credits_whoever_added_it_and_whoever_proposed_it(house: House) -> None:
    """Lee adds Noodle Bar to the catalog and to the call. It loses, and it's where they ate."""
    event_id = await to_voting(house, "Sam", "Lee", "Kim", pickups=("Alex",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await house.add_item("Noodle Bar", Mode.PICKUP, cuisine="Korean")
    async with house.sessionmaker() as db:
        await db.execute(
            text("UPDATE catalog_item SET created_by = :who WHERE id = :id"),
            {"who": house["Kim"].id, "id": house.items["Noodle Bar"].id},
        )
        await db.commit()
    await act(house, calls.add_proposal, house["Lee"], event_id, Mode.PICKUP, item_id=house.items["Noodle Bar"].id)
    await vote(house, "Alex", event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await at_deadline(house, event_id)
    await act(house, calls.mark_done, house["Alex"], event_id)
    assert (await credited(house, event_id, Kind.SUGGESTED)) == set()

    await correct(house, event_id, item_id=house.items["Noodle Bar"].id, mode=Mode.PICKUP)
    assert await credited(house, event_id, Kind.SUGGESTED) == {house["Lee"].id}
    assert await credited(house, event_id, Kind.EXPLORED) == {house["Lee"].id}
    assert await credited(house, event_id, Kind.ADDED_ITEM) == {house["Kim"].id}
    found = await awards(house)
    assert ("Lee", BadgeKey.BRIGHT_IDEA, Tier.FIRST, Announcement.PENDING) in found
    assert ("Lee", BadgeKey.EXPLORER, Tier.FIRST, Announcement.PENDING) in found
    assert ("Kim", BadgeKey.SCOUT, Tier.FIRST, Announcement.PENDING) in found
    (audit,) = await recounts(house)
    assert audit.payload["added"]["added_item"] == [house["Kim"].id]
    assert {b["badge"] for b in audit.payload["badges_awarded"]} >= {"bright_idea", "explorer", "scout"}


async def item_credit_events(house: House, item_id: int) -> list[int | None]:
    async with house.sessionmaker() as db:
        found = await db.scalars(
            select(ContributionRow.event_id).where(
                ContributionRow.kind == Kind.ADDED_ITEM.value, ContributionRow.item_id == item_id
            )
        )
        return list(found)


async def test_a_second_call_of_the_old_place_takes_over_the_added_item_count(house: House) -> None:
    """Kim's Pho House won twice. The first correction takes the place off the call the count was
    on, so it moves to the other; only taking the place off both ends it."""
    first = await kims_dinner(house, n=0)
    pho = (await house.event(first)).decided_item_id
    assert pho is not None

    async def add_it_again(event_id: int) -> None:
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, item_id=pho)

    second = await dinner(house, n=1, ins=("Alex", "Sam", "Kim"), while_voting=add_it_again, vote_for="Kim")
    assert (await counts(house, "Kim"))[Kind.ADDED_ITEM] == 1
    assert await item_credit_events(house, pho) == [first]

    await to_taqueria(house, first)
    assert (await counts(house, "Kim"))[Kind.ADDED_ITEM] == 1
    assert await item_credit_events(house, pho) == [second]
    assert ("Kim", BadgeKey.SCOUT, Tier.FIRST) in held(await awards(house))
    (audit,) = await recounts(house)
    assert audit.payload["item_credit_moved"] == [pho]
    async with house.sessionmaker() as db:
        row = await db.scalar(select(ContributionRow).where(ContributionRow.item_id == pho))
    assert row is not None and row.day == (await house.event(second)).day

    await correct(house, first, item_id=pho, mode=Mode.PICKUP)  # back: first again, and the count stays put
    assert (await counts(house, "Kim"))[Kind.ADDED_ITEM] == 1
    assert await item_credit_events(house, pho) == [second]

    await to_taqueria(house, second)
    assert await item_credit_events(house, pho) == [first]
    await to_taqueria(house, first)
    assert await item_credit_events(house, pho) == []
    assert Kind.ADDED_ITEM not in await counts(house, "Kim")
    assert ("Kim", BadgeKey.SCOUT, Tier.FIRST) not in held(await awards(house))


async def test_other_meals_keep_the_counts_they_have(house: House) -> None:
    """Correcting the first of two dinners on Pho House makes the second the first time anyone had
    it. Its explorer count says what was new when it was counted, so it doesn't change."""
    first = await kims_dinner(house, n=0)
    pho = (await house.event(first)).decided_item_id

    async def add_it_again(event_id: int) -> None:
        await act(house, calls.add_proposal, house["Sam"], event_id, Mode.PICKUP, item_id=pho)

    second = await dinner(house, n=1, ins=("Alex", "Sam", "Kim"), while_voting=add_it_again, vote_for="Sam")
    assert await credited(house, second, Kind.SUGGESTED) == {house["Sam"].id}
    assert await credited(house, second, Kind.EXPLORED) == set()
    await to_taqueria(house, first)
    assert await credited(house, second, Kind.EXPLORED) == set()
    assert await credited(house, second, Kind.SUGGESTED) == {house["Sam"].id}


async def test_gambler_and_the_other_counts_that_do_not_read_the_place_stay(house: House) -> None:
    event_id = await kims_dinner(house)
    kinds = (Kind.GAMBLED, Kind.PARTICIPATED, Kind.RESPONDED_ON_TIME, Kind.COORDINATED, Kind.HOSTED)
    before = {kind: await credited(house, event_id, kind) for kind in kinds}
    assert before[Kind.GAMBLED]  # three picks, and two of the three who are in left it to the others
    await to_tacos(house, event_id)
    await correct(house, event_id, name="Noodle Bar", mode=Mode.PICKUP)
    assert {kind: await credited(house, event_id, kind) for kind in kinds} == before


async def test_running_it_again_changes_nothing(house: House) -> None:
    event_id = await kims_dinner(house)
    await to_tacos(house, event_id)
    held_now, rows_now = await awards(house), len(await recounts(house))
    event = await house.event(event_id)
    for _ in range(2):
        async with house.sessionmaker() as db:
            await contributions.recount_place(db, house["Alex"], event, now=house.clock())
            await db.commit()
    assert await awards(house) == held_now and len(await recounts(house)) == rows_now


async def test_to_a_home_meal_and_back_restores_the_counts_and_the_badges(house: House) -> None:
    event_id = await done_call(house)
    people = ("Alex", "Lee", "Sam")
    counted = {name: await counts(house, name) for name in people}
    badges = held(await awards(house))
    await to_tacos(house, event_id)
    assert held(await awards(house)) != badges
    await to_taqueria(house, event_id)
    await to_tacos(house, event_id)
    await to_siam(house, event_id)
    assert {name: await counts(house, name) for name in people} == counted
    assert held(await awards(house)) == badges
    assert (await house.event(event_id)).decided_mode is Mode.PICKUP
    assert (await meal_row(house, event_id)).item_id == house.items["Siam Garden"].id


async def test_a_badge_already_queued_is_taken_out_of_its_post(house: House) -> None:
    event_id = await done_call(house)
    async with house.sessionmaker() as db:
        assert await announcements.announce_household(db, house.household.id, now=house.clock()) == 1

    await to_tacos(house, event_id)
    (post,) = await sent(house, "badges_earned")
    named = {(n.person.id, b.name) for n in post.news for b in (*n.badges, *n.rest)}
    assert (house["Alex"].id, "Road Runner") not in named and (house["Lee"].id, "Order Up") not in named
    assert (house["Alex"].id, "Full Table") in named
    assert not [a for a in await awards(house, "Alex") if a[1] is BadgeKey.ROAD_RUNNER]


async def test_a_call_never_counted_is_left_to_the_next_start(house: House) -> None:
    event_id = await done_call(house)
    async with house.sessionmaker() as db:
        await db.execute(text("DELETE FROM contribution"))
        await db.commit()
    await to_tacos(house, event_id)
    assert await recounts(house) == []
    async with house.sessionmaker() as db:
        assert await ContributionRepository(db).members(house.household.id) == []
        await contributions.ensure_backfilled(db, house.household.id, now=house.clock())
    assert await credited(house, event_id, Kind.PICKED_UP) == set()
    assert await credited(house, event_id, Kind.COORDINATED) == {house["Sam"].id}


# The page -----------------------------------------------------------------------------------------


async def test_the_view_offers_it_to_an_admin_on_a_done_call_only(house: House) -> None:
    event_id = await decided(house)
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    assert view.can.change_place and not view.can.correct_place
    await act(house, calls.mark_done, house["Alex"], event_id)
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    assert view.can.correct_place and not view.can.change_place and view.can.manage
    view = await act(house, event_views.event_view, house["Sam"], event_id)
    assert not view.can.correct_place and not view.can.change_place


def web_done(admin: TestClient, clock: FakeClock, migrated: Settings) -> tuple[int, int, int]:
    event_id, shown, home = web_decided(admin, clock, migrated)
    assert submit(admin, f"/events/{event_id}/done", {}).status_code == 303
    return event_id, shown, home


def test_web_corrects_the_place_from_change_the_plan(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, home = web_done(admin, clock, migrated)
    url = f"/events/{event_id}/admin/place"
    html = page(admin.get(f"/events/{event_id}"))
    assert "Change the plan" in html and "Correct the place" in html and f'action="{url}"' in html
    assert f'action="/events/{event_id}/place"' not in html and f'action="/events/{event_id}/meal-time"' not in html
    assert f'action="/events/{event_id}/assignments"' in html  # beside the other correction
    assert f'name="opened_item" value="{shown}"' in html and 'name="run" value="1"' in html
    place_menu = html.split(f'action="{url}"')[1].split('name="item_id"')[1].split("</select>")[0]
    assert f'value="{shown}"' not in place_menu and f'value="{home}"' in place_menu

    form_ = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    assert submit(admin, url, form_).status_code == 303
    assert "<h3>Sam's Tacos <span" in page(admin.get(f"/events/{event_id}"))
    assert "Sam's Tacos" in page(admin.get("/history"))
    (event_row,) = rows(migrated, EventRow)
    assert (event_row.state, event_row.decided_item_id, event_row.decided_mode) == ("done", home, "cook")
    (meal_log,) = rows(migrated, MealLogRow)
    assert meal_log.item_id == home
    (audit,) = [a for a in rows(migrated, AuditRow) if a.action == "event.place_changed"]
    assert audit.payload["after_done"] is True


def test_web_saves_a_new_name_over_htmx(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, _ = web_done(admin, clock, migrated)
    form_ = {"new_place": " Noodle Bar ", "mode": "delivery", "opened_item": str(shown), "run": "1"}
    response = htmx(admin, f"/events/{event_id}/admin/place", form_)
    assert response.status_code == 200 and "<h3>Noodle Bar <span" in page(response)
    assert "Noodle Bar" in {i.name for i in rows(migrated, CatalogItemRow)}


def test_web_refuses_a_stale_page_and_a_way_the_place_does_not_allow(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    event_id, shown, home = web_done(admin, clock, migrated)
    url = f"/events/{event_id}/admin/place"
    way = submit(admin, url, {"item_id": str(home), "mode": "pickup", "opened_item": str(shown), "run": "1"})
    assert way.status_code == 409
    stale = submit(admin, url, {"new_place": "Noodle Bar", "mode": "pickup", "opened_item": str(home), "run": "1"})
    assert stale.status_code == 409 and PLAN_CHANGED in page(stale)
    old_run = submit(admin, url, {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "9"})
    assert old_run.status_code == 409
    (event_row,) = rows(migrated, EventRow)
    assert event_row.decided_item_id == shown


@pytest.mark.parametrize(
    "bad",
    [
        {"item_id": "1"},
        {"item_id": "1", "opened_item": "1"},
        {"item_id": "x", "opened_item": "1", "run": "1"},
        {"cuisine_id": "x", "opened_item": "1", "run": "1"},
    ],
)
def test_web_a_malformed_form_is_a_400(
    admin: TestClient, clock: FakeClock, migrated: Settings, bad: dict[str, str]
) -> None:
    event_id, _, _ = web_done(admin, clock, migrated)
    assert submit(admin, f"/events/{event_id}/admin/place", bad).status_code == 400


def test_web_a_decided_call_refuses_this_path(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, home = web_decided(admin, clock, migrated)
    form_ = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    assert f'action="/events/{event_id}/admin/place"' not in page(admin.get(f"/events/{event_id}"))
    assert submit(admin, f"/events/{event_id}/admin/place", form_).status_code == 409


def test_web_needs_csrf(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, home = web_done(admin, clock, migrated)
    form_ = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    response = admin.post(f"/events/{event_id}/admin/place", data=form_, follow_redirects=False)
    assert response.status_code == 403
    (event_row,) = rows(migrated, EventRow)
    assert event_row.decided_item_id == shown


def test_web_refuses_the_coordinator_and_a_member(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """Kim coordinates and isn't an admin; Lee is only a member."""
    invite(admin, "kim@example.com", "Kim")
    invite(admin, "lee@example.com", "Lee")
    event_id = web_start(admin)
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"}).status_code == 303
    sign_in_as(admin, provider, "kim@example.com")
    assert submit(admin, f"/events/{event_id}/respond", {"status": "in"}).status_code == 303
    assert submit(admin, f"/events/{event_id}/coordinate", {}).status_code == 303
    admin.cookies.clear()
    sign_in(admin, provider)
    clock.advance(timedelta(minutes=90))
    web_tick(admin, clock)
    first = proposal_ids(migrated)[0]
    assert submit(admin, f"/events/{event_id}/proposals/{first}/lock", {}).status_code == 303
    assert submit(admin, f"/events/{event_id}/done", {}).status_code == 303
    (event_row,) = rows(migrated, EventRow)
    shown = event_row.decided_item_id
    form_ = {"new_place": "Noodle Bar", "mode": "pickup", "opened_item": str(shown), "run": "1"}

    for email in ("kim@example.com", "lee@example.com"):
        sign_in_as(admin, provider, email)
        assert f'action="/events/{event_id}/admin/place"' not in page(admin.get(f"/events/{event_id}"))
        assert submit(admin, f"/events/{event_id}/admin/place", form_).status_code == 403
    assert "Noodle Bar" not in {i.name for i in rows(migrated, CatalogItemRow)}
    (event_row,) = rows(migrated, EventRow)
    assert event_row.decided_item_id == shown
