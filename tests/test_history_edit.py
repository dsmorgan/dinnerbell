# SPDX-License-Identifier: AGPL-3.0-or-later
"""History's meal edit and **Change the plan** agree on a call's meal (#224, spec 5.6, 5.7): only a
correction changes its place, a form from before someone else's change is refused, and a correction
can bring a call and meal that an earlier edit left apart back into line."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import date, time, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.exc import OperationalError

from dinnerbell.config import Settings
from dinnerbell.db.engine import BUSY_TIMEOUT_MS
from dinnerbell.db.events import AssignmentRepository
from dinnerbell.db.meals import MealLogRepository
from dinnerbell.db.models import AuditRow, EventRow, FeedbackRow, MealLogRow
from dinnerbell.domain.badges import BadgeKey, Tier
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.contributions import Kind
from dinnerbell.domain.events import AFTER_DECISION_RANK, AssignmentRole, EventRuleViolation
from dinnerbell.domain.feedback import FeedbackInput, Rating
from dinnerbell.domain.meals import (
    CALL_PLACE_FIXED,
    MEAL_CHANGED,
    InvalidMeal,
    MealInput,
    MealLog,
    MemberRef,
    Slot,
    StaleMeal,
)
from dinnerbell.services import calls, coordinator, event_views, meals, offers
from dinnerbell.services.coordinator import PLAN_CHANGED, SAME_PLACE, AdjustmentRefused
from dinnerbell.services.errors import NotFound
from dinnerbell.services.offers import OfferInput
from tests.conftest import csrf_from, invite, page
from tests.support.calls import House
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import submit
from tests.test_auth_flow import member, rows
from tests.test_change_place import tacos
from tests.test_contributions import DAY, LUNCH_AT, MORNING, awards, counts, dinner, held
from tests.test_coordinator import act, pick_ids, to_voting
from tests.test_coordinator import database as database  # the fixtures, re-exported for pytest
from tests.test_coordinator import house as house
from tests.test_correct_place import (
    credited,
    done_call,
    feedback_rows,
    give,
    item_credit_events,
    kims_dinner,
    meal_row,
    recounts,
    to_taqueria,
    web_done,
)
from tests.test_events_web import admin as admin
from tests.test_events_web import sign_in_as

pytestmark = pytest.mark.anyio

LIKED = FeedbackInput(rating=Rating.LIKED)


async def meal_of(house: House, event_id: int) -> MealLog:
    return await act_read(house, (await meal_row(house, event_id)).id)


async def act_read(house: House, meal_id: int) -> MealLog:
    async with house.sessionmaker() as db:
        return await meals.get_meal(db, house["Alex"], meal_id)


def same(meal: MealLog, **changes: Any) -> MealInput:
    """What a form posts for ``meal``, with ``changes``."""
    data = MealInput(meal.day, meal.slot, meal.item_id, frozenset(p.id for p in meal.participants))
    return replace(data, **changes)


async def save(house: House, who: str, meal: MealLog, data: MealInput | None = None, **kwargs: Any) -> MealLog:
    result: MealLog = await act(
        house, meals.update_meal, house[who], meal.id, data or same(meal), today=meal.day, **kwargs
    )
    return result


async def logged_by(house: House, meal_id: int, who: str) -> None:
    """Make ``who`` the meal's logger. Marking a call done logs its meal with nobody, so this is how
    a non-admin comes to be able to edit a call's meal."""
    async with house.sessionmaker() as db:
        row = await db.get(MealLogRow, meal_id)
        assert row is not None
        row.logged_by = house[who].id
        await db.commit()


async def left_behind(house: House, event_id: int, name: str) -> MealLog:
    """What a History edit before #224 left: the meal on ``name``, its feedback gone, and the call,
    its proposals, and its counts where they were."""
    async with house.sessionmaker() as db:
        row = await db.scalar(select(MealLogRow).where(MealLogRow.event_id == event_id))
        assert row is not None
        row.item_id = house.items[name].id
        await db.execute(delete(FeedbackRow).where(FeedbackRow.meal_id == row.id))
        await db.commit()
    return await meal_of(house, event_id)


async def correct_meal(house: House, event_id: int, **kwargs: Any) -> None:
    """The admin (Alex) corrects the place from a page opened now, which shows what the meal says."""
    meal = await meal_of(house, event_id)
    house.clock.advance(timedelta(seconds=1))
    await act(house, coordinator.correct_place, house["Alex"], event_id, opened_item_id=meal.item_id, **kwargs)


# What the form stamps -----------------------------------------------------------------------------


def test_the_stamp_follows_what_the_form_shows_and_nothing_else() -> None:
    sam, lee = MemberRef(1, "Sam"), MemberRef(2, "Lee")
    meal = MealLog(1, 1, date(2026, 11, 2), Slot.DINNER, 5, "Sam's Tacos", (), (sam, lee), None, 9)
    assert meal.form_stamp == replace(meal, participants=(lee, sam)).form_stamp  # the order says nothing
    assert meal.form_stamp == replace(meal, participants=(MemberRef(1, "Samantha"), lee)).form_stamp  # a new name
    assert meal.form_stamp == replace(meal, item_name="Tacos", logged_by=1).form_stamp
    for changed in (
        replace(meal, day=date(2026, 11, 1)),
        replace(meal, slot=Slot.LUNCH),
        replace(meal, item_id=6),
        replace(meal, participants=(sam,)),
    ):
        assert changed.form_stamp != meal.form_stamp
    assert len(meal.form_stamp) <= 40  # the form field's limit


# A call's meal: only a correction changes its place --------------------------------------------------


@pytest.mark.parametrize("who", ["Alex", "Lee"])  # the admin, and a member who logged it
async def test_a_calls_meal_refuses_a_changed_item(house: House, who: str) -> None:
    event_id = await done_call(house)
    meal = await meal_of(house, event_id)
    await logged_by(house, meal.id, "Lee")
    await give(house, "Lee", meal.id, LIKED)
    taqueria = house.items["Taqueria"].id
    audits = len(await house.audits("meal."))

    with pytest.raises(InvalidMeal) as caught:
        await save(house, who, meal, same(meal, item_id=taqueria))
    assert str(caught.value) == CALL_PLACE_FIXED and "Change the plan" in CALL_PLACE_FIXED

    assert (await meal_of(house, event_id)).item_id == meal.item_id
    assert [r.item_id for r in await feedback_rows(house)] == [meal.item_id]
    assert len(await house.audits("meal.")) == audits


@pytest.mark.parametrize("who", ["Alex", "Lee"])
async def test_a_calls_meal_still_saves_with_its_item_unchanged(house: House, who: str) -> None:
    """Who ate and when keep working, feedback stays, and the call stays as it was."""
    event_id = await done_call(house)
    meal = await meal_of(house, event_id)
    await logged_by(house, meal.id, "Lee")
    await give(house, "Lee", meal.id, LIKED)
    decided = (await house.event(event_id)).decided_item_id
    saved = await save(house, who, meal, same(meal, slot=Slot.LUNCH), shown=meal.form_stamp)
    assert saved.slot is Slot.LUNCH and saved.item_id == meal.item_id
    assert len(await feedback_rows(house)) == 1
    assert (await house.event(event_id)).decided_item_id == decided


async def test_a_meal_logged_by_hand_keeps_its_item_edit_and_loses_its_feedback(house: House) -> None:
    item = house.items["Taqueria"].id
    logged = await act(
        house,
        meals.log_meal,
        house["Lee"],
        MealInput(DAY, Slot.DINNER, house.items["Siam Garden"].id, frozenset({house["Lee"].id})),
        today=DAY,
    )
    await give(house, "Lee", logged.id, LIKED)
    async with house.sessionmaker() as db:
        assert await meals.call_place(db, house["Lee"], logged) is None
    saved = await save(house, "Lee", logged, same(logged, item_id=item), shown=logged.form_stamp)
    assert saved.item_id == item and await feedback_rows(house) == []
    (*_, audit) = await house.audits("meal.updated")
    assert audit.payload["feedback_removed"] == 1


async def offers_meal(house: House) -> tuple[int, MealLog]:
    house.clock.now = MORNING
    data = OfferInput(slot=Slot.LUNCH, day=DAY, mode=Mode.COOK, at=time(12, 30), capacity=3, item_id=tacos(house))
    posted = await act(house, offers.post_offer, house["Sam"], data)
    house.clock.now = LUNCH_AT
    await house.tick()
    house.clock.now = LUNCH_AT + timedelta(hours=2)
    await house.tick()
    assert (await house.event(posted.event_id)).state.value == "done"
    return posted.event_id, await meal_of(house, posted.event_id)


async def test_an_offers_meal_keeps_its_item_edit_since_nothing_else_can_correct_it(house: House) -> None:
    event_id, meal = await offers_meal(house)
    async with house.sessionmaker() as db:
        assert await meals.call_place(db, house["Alex"], meal) is None
    item = house.items["Taqueria"].id
    saved = await save(house, "Alex", meal, same(meal, item_id=item), shown=meal.form_stamp)
    assert saved.item_id == item
    assert (await house.event(event_id)).decided_item_id == tacos(house)  # as it does today


async def test_what_the_form_says_about_a_calls_place_depends_on_who_is_looking(house: House) -> None:
    event_id = await done_call(house)
    meal = await meal_of(house, event_id)
    async with house.sessionmaker() as db:
        shown = await meals.call_place(db, house["Alex"], meal)
        assert shown == meals.CallPlace(event_id, can_correct=True)
        shown = await meals.call_place(db, house["Lee"], meal)
        assert shown == meals.CallPlace(event_id, can_correct=False)


# Stale saves --------------------------------------------------------------------------------------


async def a_manual_meal(house: House) -> MealLog:
    people = frozenset({house["Lee"].id, house["Alex"].id})
    data = MealInput(DAY, Slot.DINNER, house.items["Siam Garden"].id, people)
    logged: MealLog = await act(house, meals.log_meal, house["Lee"], data, today=DAY)
    return logged


@pytest.mark.parametrize("kind", ["manual", "call"])
async def test_a_stale_edit_is_refused_and_writes_nothing(house: House, kind: str) -> None:
    """Two admins opened the same form. The first save stands; the second says to look again."""
    await house.add_member("Pat", admin=True)
    meal = await a_manual_meal(house) if kind == "manual" else await meal_of(house, await done_call(house))
    opened = meal.form_stamp
    first = await save(house, "Alex", meal, same(meal, slot=Slot.LUNCH), shown=opened)
    with pytest.raises(StaleMeal) as caught:
        await save(house, "Pat", meal, same(meal, slot=Slot.BREAKFAST), shown=opened)
    assert str(caught.value) == MEAL_CHANGED
    assert (await act_read(house, meal.id)).slot is first.slot is Slot.LUNCH
    (audit, *others) = await house.audits("meal.updated")
    assert audit.actor_member_id == house["Alex"].id and not others


@pytest.mark.parametrize("kind", ["manual", "call"])
async def test_a_double_submit_saves_once(house: House, kind: str) -> None:
    meal = await a_manual_meal(house) if kind == "manual" else await meal_of(house, await done_call(house))
    posted = same(meal, slot=Slot.LUNCH)
    await save(house, "Alex", meal, posted, shown=meal.form_stamp)
    with pytest.raises(StaleMeal):
        await save(house, "Alex", meal, posted, shown=meal.form_stamp)
    assert len(await house.audits("meal.updated")) == 1


async def test_a_save_that_changed_nothing_leaves_other_open_forms_good(house: House) -> None:
    meal = await a_manual_meal(house)
    await save(house, "Alex", meal, shown=meal.form_stamp)
    await save(house, "Lee", meal, same(meal, slot=Slot.LUNCH), shown=meal.form_stamp)
    assert (await act_read(house, meal.id)).slot is Slot.LUNCH


@pytest.mark.parametrize("kind", ["manual", "call"])
async def test_a_stale_delete_is_refused(house: House, kind: str) -> None:
    meal = await a_manual_meal(house) if kind == "manual" else await meal_of(house, await done_call(house))
    await save(house, "Alex", meal, same(meal, slot=Slot.LUNCH), shown=meal.form_stamp)
    with pytest.raises(StaleMeal):
        await act(house, meals.delete_meal, house["Alex"], meal.id, shown=meal.form_stamp)
    assert (await act_read(house, meal.id)).slot is Slot.LUNCH
    assert not await house.audits("meal.deleted")
    current = await act_read(house, meal.id)
    await act(house, meals.delete_meal, house["Alex"], meal.id, shown=current.form_stamp)
    with pytest.raises(NotFound):  # a second click on the same delete
        await act(house, meals.delete_meal, house["Alex"], meal.id, shown=current.form_stamp)


async def test_an_old_page_without_the_stamp_still_works(house: House) -> None:
    manual = await a_manual_meal(house)
    other = same(manual, item_id=house.items["Taqueria"].id)
    assert (await save(house, "Lee", manual, other)).item_id == other.item_id
    await act(house, meals.delete_meal, house["Lee"], manual.id)
    called = await meal_of(house, await done_call(house))
    assert (await save(house, "Alex", called, same(called, slot=Slot.LUNCH))).slot is Slot.LUNCH
    await act(house, meals.delete_meal, house["Alex"], called.id)
    assert not [m for m in await house.meals() if m.id in {manual.id, called.id}]


async def test_a_form_opened_before_a_correction_cannot_undo_it(house: House) -> None:
    """The issue's scenario: the form shows Siam Garden, an admin corrects it to Taqueria and the
    feedback moves, and then the form is saved, with its stamp or from an old page without one."""
    event_id = await done_call(house)
    meal = await meal_of(house, event_id)
    await give(house, "Lee", meal.id, LIKED)
    opened = meal.form_stamp
    await to_taqueria(house, event_id)
    taqueria = house.items["Taqueria"].id

    with pytest.raises(StaleMeal):
        await save(house, "Alex", meal, shown=opened)  # the form's hidden item is the old place
    with pytest.raises(InvalidMeal) as caught:
        await save(house, "Alex", meal)  # an old page's menu
    assert str(caught.value) == CALL_PLACE_FIXED
    with pytest.raises(StaleMeal):
        await act(house, meals.delete_meal, house["Alex"], meal.id, shown=opened)

    assert (await meal_of(house, event_id)).item_id == taqueria
    assert (await house.event(event_id)).decided_item_id == taqueria
    (row,) = await feedback_rows(house)
    assert (row.item_id, row.item_name, row.rating) == (taqueria, "Taqueria", Rating.LIKED.value)


@pytest.mark.parametrize("use_case", ["update", "delete"])
async def test_the_check_and_the_write_hold_one_lock(
    house: House, monkeypatch: pytest.MonkeyPatch, use_case: str
) -> None:
    """A correction can't commit between the check and the write: it needs this lock first. Another
    connection, told not to wait, finds it taken right after the check."""
    meal = await meal_of(house, await done_call(house))
    tried: list[bool] = []

    class Tried(MealLogRepository):
        """The write, after the check, with another connection trying for the lock first."""

        async def update(self, *args: Any, **kwargs: Any) -> None:
            await try_the_lock()
            await super().update(*args, **kwargs)

        async def delete(self, *args: Any, **kwargs: Any) -> None:
            await try_the_lock()
            await super().delete(*args, **kwargs)

    async def try_the_lock() -> None:
        async with house.sessionmaker() as other:
            await other.execute(text("PRAGMA busy_timeout=0"))
            with pytest.raises(OperationalError, match="locked"):
                await other.execute(text("BEGIN IMMEDIATE"))
            await other.execute(text(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}"))
        tried.append(True)

    monkeypatch.setattr(meals, "MealLogRepository", Tried)
    if use_case == "update":
        await save(house, "Alex", meal, shown=meal.form_stamp)
    else:
        await act(house, meals.delete_meal, house["Alex"], meal.id, shown=meal.form_stamp)
    assert tried


async def test_a_save_and_a_correction_at_once_end_on_the_corrected_place(house: House) -> None:
    """Whichever goes first, the correction stands and the feedback is on the corrected place."""
    event_id = await done_call(house)
    meal = await meal_of(house, event_id)
    await give(house, "Lee", meal.id, LIKED)
    saved, corrected = await asyncio.gather(
        save(house, "Alex", meal, same(meal, slot=Slot.LUNCH), shown=meal.form_stamp),
        to_taqueria(house, event_id),
        return_exceptions=True,
    )
    assert isinstance(saved, MealLog | StaleMeal) and corrected is None
    (row,) = await feedback_rows(house)
    assert row.item_name == "Taqueria" and (await meal_of(house, event_id)).item_name == "Taqueria"


# Calls already out of step -------------------------------------------------------------------------


async def test_correcting_to_the_place_the_call_still_has_moves_only_the_meal(house: House) -> None:
    """The meal says Taqueria and the call says Siam Garden. Correcting to Siam Garden isn't "the
    same place", and leaves the call, its roles, how it won, and its counts as they were."""
    event_id = await to_voting(house, "Sam", "Lee", pickups=("Alex",), coordinator_name="Sam")
    await house.settle_ways(event_id)
    await act(house, calls.lock, house["Sam"], event_id, (await pick_ids(house, event_id))["Siam Garden"])
    await act(house, calls.mark_done, house["Alex"], event_id)
    before = await house.event(event_id)
    meal = await left_behind(house, event_id, "Taqueria")
    await give(house, "Lee", meal.id, LIKED)
    picked_up = await credited(house, event_id, Kind.PICKED_UP)
    siam = house.items["Siam Garden"].id

    await correct_meal(house, event_id, item_id=siam)

    event = await house.event(event_id)
    assert (event.decided_item_id, event.decided_mode, event.locked_by) == (siam, before.decided_mode, before.locked_by)
    assert (await meal_of(house, event_id)).item_id == siam
    assert (await meal_of(house, event_id)).cuisines[0].name == "Thai"
    (row,) = await feedback_rows(house)
    assert (row.item_id, row.mode) == (siam, before.decided_mode.value if before.decided_mode else None)
    assert await credited(house, event_id, Kind.PICKED_UP) == picked_up
    assert not await recounts(house)
    (*_, audit) = [a for a in await house.audits() if a.action == "event.place_changed"]
    assert audit.payload["from_meal_item_id"] == house.items["Taqueria"].id
    assert audit.payload["from_item_id"] == audit.payload["to_item_id"] == siam
    assert audit.payload["dropped"] == [] and audit.payload["new_item"] is False


async def test_correcting_to_the_place_the_call_has_keeps_a_role_an_admin_cleared(house: House) -> None:
    """Nothing about the call changes, so a role nobody has doesn't go to its first volunteer."""
    event_id = await done_call(house)
    await left_behind(house, event_id, "Taqueria")
    await act(house, calls.change_assignment, house["Alex"], event_id, AssignmentRole.PICKUP, None)
    await correct_meal(house, event_id, item_id=house.items["Siam Garden"].id)
    async with house.sessionmaker() as db:
        roles = {a.role for a in await AssignmentRepository(db).for_event(house.household.id, event_id)}
    assert AssignmentRole.PICKUP not in roles


async def test_correcting_to_the_place_the_call_has_in_another_way(house: House) -> None:
    """The call's row outside the picks stays one row, in the new way, with the meal back on it."""
    event_id = await done_call(house)
    await to_taqueria(house, event_id)  # a rank 0 row, which the next correction replaces
    await house.add_item("Pho House", Mode.PICKUP, Mode.DELIVERY, cuisine="Vietnamese")
    pho = house.items["Pho House"].id
    await correct_meal(house, event_id, item_id=pho, mode=Mode.PICKUP)
    await left_behind(house, event_id, "Siam Garden")

    await correct_meal(house, event_id, item_id=pho, mode=Mode.DELIVERY)

    event = await house.event(event_id)
    assert (event.decided_item_id, event.decided_mode) == (pho, Mode.DELIVERY)
    assert (await meal_of(house, event_id)).item_id == pho
    async with house.sessionmaker() as db:
        found = await db.execute(
            text("SELECT item_id, mode FROM proposal WHERE event_id = :e AND rank = :r"),
            {"e": event_id, "r": AFTER_DECISION_RANK},
        )
        assert [tuple(r) for r in found] == [(pho, "delivery")]


@pytest.mark.parametrize(
    "ask",
    [
        lambda taqueria: {},  # Keep, on the page
        lambda taqueria: {"item_id": taqueria},
        lambda taqueria: {"name": "Taqueria", "mode": Mode.PICKUP},
    ],
    ids=["keep", "item", "name"],
)
async def test_correcting_to_the_place_the_meal_has_brings_the_call_into_line(house: House, ask: Any) -> None:
    event_id = await done_call(house)
    meal = await left_behind(house, event_id, "Taqueria")
    await give(house, "Lee", meal.id, LIKED)
    taqueria = house.items["Taqueria"].id

    await correct_meal(house, event_id, **ask(taqueria))

    event = await house.event(event_id)
    assert (event.decided_item_id, event.decided_mode) == (taqueria, Mode.PICKUP)
    assert (await meal_of(house, event_id)).item_id == taqueria
    (row,) = await feedback_rows(house)
    assert row.item_id == taqueria
    with pytest.raises(EventRuleViolation) as caught:  # in step now, so it's the same place
        await correct_meal(house, event_id)
    assert caught.value.code == "same_place" and str(caught.value) == SAME_PLACE


async def test_bringing_the_call_into_line_moves_the_roles_and_counts_with_it(house: House) -> None:
    """Sam's Tacos has one way, which it takes without being asked. The orderer's and the pickup
    person's counts go, as with any correction to a home meal."""
    event_id = await done_call(house)
    assert await credited(house, event_id, Kind.PICKED_UP) == {house["Alex"].id}
    await left_behind(house, event_id, "Sam's Tacos")
    await correct_meal(house, event_id)
    event = await house.event(event_id)
    assert (event.decided_item_id, event.decided_mode) == (tacos(house), Mode.COOK)
    assert await credited(house, event_id, Kind.PICKED_UP) == set()
    assert await credited(house, event_id, Kind.ORDERED) == set()
    assert len(await recounts(house)) == 1


async def test_bringing_the_call_into_line_needs_a_way_the_place_allows(house: House) -> None:
    event_id = await done_call(house)
    await left_behind(house, event_id, "Taqueria")
    with pytest.raises(AdjustmentRefused) as caught:
        await correct_meal(house, event_id, mode=Mode.COOK)
    assert caught.value.part == "place_mode"
    assert (await house.event(event_id)).decided_item_id == house.items["Siam Garden"].id


async def test_bringing_the_call_into_line_works_for_a_place_out_of_the_catalog(house: House) -> None:
    """The meal already says it. Moving anything else to it is still refused."""
    event_id = await done_call(house)
    await left_behind(house, event_id, "Taqueria")
    taqueria = house.items["Taqueria"].id
    async with house.sessionmaker() as db:
        await db.execute(text("UPDATE catalog_item SET active = 0 WHERE id = :i"), {"i": taqueria})
        await db.commit()
    await correct_meal(house, event_id)
    assert (await house.event(event_id)).decided_item_id == taqueria


async def test_a_page_that_showed_the_calls_place_is_stale_when_the_meal_says_another(house: House) -> None:
    """A page from before #224 shows the call's place. Whatever it asks for is refused, and the page
    it opens next shows what the meal says."""
    event_id = await done_call(house)
    await left_behind(house, event_id, "Taqueria")
    siam, taqueria = house.items["Siam Garden"].id, house.items["Taqueria"].id
    for asked in ({"item_id": taqueria}, {"name": "Noodle Bar", "mode": Mode.PICKUP}, {"item_id": siam}, {}):
        with pytest.raises(AdjustmentRefused) as caught:
            await act(house, coordinator.correct_place, house["Alex"], event_id, opened_item_id=siam, **asked)
        assert PLAN_CHANGED in str(caught.value)
    assert (await house.event(event_id)).decided_item_id == siam
    assert (await meal_of(house, event_id)).item_id == taqueria


async def test_the_added_item_count_moves_to_another_call_when_the_call_comes_into_line(house: House) -> None:
    """Kim's Pho House won twice, and an old edit moved the first meal. Bringing that call into line
    is the correction that takes Pho House off it, so the count moves to the second call."""
    first = await kims_dinner(house, n=0)
    pho = (await house.event(first)).decided_item_id
    assert pho is not None

    async def add_it_again(event_id: int) -> None:
        await act(house, calls.add_proposal, house["Kim"], event_id, Mode.PICKUP, item_id=pho)

    second = await dinner(house, n=1, ins=("Alex", "Sam", "Kim"), while_voting=add_it_again, vote_for="Kim")
    assert await item_credit_events(house, pho) == [first]
    await left_behind(house, first, "Taqueria")
    await correct_meal(house, first)
    assert await item_credit_events(house, pho) == [second]
    assert (await counts(house, "Kim"))[Kind.ADDED_ITEM] == 1


async def test_the_added_item_count_goes_when_no_other_call_says_the_place_and_comes_back(house: House) -> None:
    first = await kims_dinner(house, n=0)
    pho = (await house.event(first)).decided_item_id
    assert pho is not None
    await left_behind(house, first, "Taqueria")

    await correct_meal(house, first, item_id=pho)  # back to the place the call has: nothing to recount
    assert await item_credit_events(house, pho) == [first] and not await recounts(house)

    await left_behind(house, first, "Taqueria")
    await correct_meal(house, first)  # into line with the meal
    assert await item_credit_events(house, pho) == [] and Kind.ADDED_ITEM not in await counts(house, "Kim")

    await correct_meal(house, first, item_id=pho, mode=Mode.PICKUP)
    assert await item_credit_events(house, pho) == [first]
    assert (await counts(house, "Kim"))[Kind.ADDED_ITEM] == 1
    assert ("Kim", BadgeKey.SCOUT, Tier.FIRST) in held(await awards(house))


# A deleted meal ------------------------------------------------------------------------------------


async def test_a_deleted_meal_stops_offering_the_correction(house: House) -> None:
    event_id = await done_call(house)
    meal = await meal_of(house, event_id)
    assert (await act(house, event_views.event_view, house["Alex"], event_id)).can.correct_place
    await act(house, meals.delete_meal, house["Alex"], meal.id, shown=meal.form_stamp)
    view = await act(house, event_views.event_view, house["Alex"], event_id)
    assert not view.can.correct_place and view.can.manage and view.meal is None
    siam = house.items["Siam Garden"].id
    with pytest.raises(EventRuleViolation) as caught:
        await act(house, coordinator.correct_place, house["Alex"], event_id, item_id=tacos(house), opened_item_id=siam)
    assert caught.value.code == "no_meal"


# The web ------------------------------------------------------------------------------------------


def stamp_of(html: str) -> str:
    found = re.search(r'name="shown_meal" value="([0-9a-f]+)"', html)
    assert found is not None
    return found.group(1)


def set_meal(settings: Settings, meal_id: int, **values: Any) -> None:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(update(MealLogRow).where(MealLogRow.id == meal_id).values(**values))
    finally:
        engine.dispose()


def the_meal(settings: Settings) -> MealLogRow:
    (row,) = rows(settings, MealLogRow)
    return row


def posted(row: MealLogRow, **changes: Any) -> dict[str, Any]:
    form = {
        "day": row.date.isoformat(),
        "slot": row.slot,
        "item_id": str(row.item_id),
        "participant_ids": [str(p["id"]) for p in row.participants],
    }
    return form | changes


def test_web_a_calls_meal_shows_its_place_read_only_with_a_link_for_an_admin(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    event_id, shown, _ = web_done(admin, clock, migrated)
    row = the_meal(migrated)
    html = page(admin.get(f"/history/meals/{row.id}"))
    assert "<strong>Siam Garden</strong>" in html
    assert '<select name="item_id"' not in html
    assert f'<input type="hidden" name="item_id" value="{shown}">' in html
    assert f'href="/events/{event_id}#event-{event_id}-plan"' in html and "Change the plan" in html
    assert f'id="event-{event_id}-plan"' in page(admin.get(f"/events/{event_id}"))  # the link lands


def test_web_the_read_only_form_saves_what_it_shows(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    web_done(admin, clock, migrated)
    row = the_meal(migrated)
    html = page(admin.get(f"/history/meals/{row.id}"))
    saved = submit(admin, f"/history/meals/{row.id}", posted(row, slot="lunch", shown_meal=stamp_of(html)))
    assert saved.status_code == 303 and "notice=updated" in saved.headers["location"]
    after = the_meal(migrated)
    assert (after.slot, after.item_id) == ("lunch", row.item_id)


def test_web_a_changed_item_on_a_calls_meal_is_refused_with_where_to_correct_it(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    event_id, shown, home = web_done(admin, clock, migrated)
    row = the_meal(migrated)
    refused = submit(admin, f"/history/meals/{row.id}", posted(row, item_id=str(home), slot="lunch"))
    assert refused.status_code == 400
    assert "This meal came from a call, so its place can&#39;t change here." in refused.text
    assert f'<input type="hidden" name="item_id" value="{shown}">' in refused.text  # its own place to save again
    assert the_meal(migrated).item_id == shown and the_meal(migrated).slot == row.slot
    (event_row,) = rows(migrated, EventRow)
    assert event_row.decided_item_id == shown and event_row.id == event_id


def test_web_a_member_who_logged_a_calls_meal_sees_no_misleading_link(
    admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings
) -> None:
    """A member can't correct, so the form doesn't send them to a form they don't have."""
    invite(admin, "sam@example.com", "Sam")
    event_id, shown, home = web_done(admin, clock, migrated)
    row = the_meal(migrated)
    sam = member(migrated, "sam@example.com").id
    set_meal(migrated, row.id, logged_by=sam)
    sign_in_as(admin, provider, "sam@example.com")

    html = page(admin.get(f"/history/meals/{row.id}"))
    assert '<select name="item_id"' not in html and f'name="item_id" value="{shown}"' in html
    assert "An admin can correct it." in html
    assert f"/events/{event_id}" not in html and "Change the plan" not in html

    refused = submit(admin, f"/history/meals/{row.id}", posted(row, item_id=str(home)))
    assert refused.status_code == 400 and the_meal(migrated).item_id == shown
    saved = submit(admin, f"/history/meals/{row.id}", posted(row, slot="lunch", shown_meal=stamp_of(html)))
    assert saved.status_code == 303 and the_meal(migrated).slot == "lunch"


def test_web_an_offers_meal_keeps_its_menu(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, home = web_done(admin, clock, migrated)
    engine = create_engine(migrated.sync_database_url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE event SET kind = 'offer', capacity = 3 WHERE id = :i"), {"i": event_id})
    engine.dispose()
    row = the_meal(migrated)
    html = page(admin.get(f"/history/meals/{row.id}"))
    assert '<select name="item_id"' in html and "came from a call" not in html and "Change the plan" not in html
    form = posted(row, item_id=str(home), shown_meal=stamp_of(html))
    assert submit(admin, f"/history/meals/{row.id}", form).status_code == 303
    assert the_meal(migrated).item_id == home
    (event_row,) = rows(migrated, EventRow)
    assert event_row.decided_item_id == shown


def web_manual(admin: TestClient, migrated: Settings) -> MealLogRow:
    """A meal the admin logged, of the first catalog item they added."""
    me = member(migrated, "admin@example.com").id
    html = page(admin.get("/history"))
    found = re.search(r'<option value="(\d+)">Siam Garden', html)
    assert found is not None
    form = {"day": "2026-10-22", "slot": "dinner", "item_id": found.group(1), "participant_ids": [str(me)]}
    assert submit(admin, "/history/meals", form).status_code == 303
    (row,) = [r for r in rows(migrated, MealLogRow) if r.event_id is None]
    return row


def test_web_a_manual_meal_keeps_its_menu_and_a_stale_save_is_refused(admin: TestClient, migrated: Settings) -> None:
    row = web_manual(admin, migrated)
    path = f"/history/meals/{row.id}"
    html = page(admin.get(path))
    assert '<select name="item_id"' in html and "came from a call" not in html
    stamp = stamp_of(html)
    assert html.count(f'name="shown_meal" value="{stamp}"') == 2  # the save and the delete
    assert f'name="shown_meal" value="{stamp}"' in page(admin.get("/history"))

    other = str(rows(migrated, MealLogRow)[0].item_id)
    assert submit(admin, path, posted(row, slot="lunch", shown_meal=stamp)).status_code == 303
    stale = submit(admin, path, posted(row, slot="breakfast", item_id=other, shown_meal=stamp))
    assert stale.status_code == 409
    assert "This meal changed since you opened this page" in stale.text
    assert re.search(r'value="lunch" checked', stale.text)  # the meal as it is now, not what was posted
    assert stamp_of(stale.text) != stamp  # so saving from here is a fresh look
    assert the_meal(migrated).slot == "lunch"
    assert submit(admin, path, posted(row, slot="breakfast", shown_meal=stamp_of(stale.text))).status_code == 303
    assert the_meal(migrated).slot == "breakfast"


def test_web_an_error_redisplaying_the_form_keeps_the_stamp_it_showed(admin: TestClient, migrated: Settings) -> None:
    """A rejected save doesn't refresh the stamp, or the next save could write over a change the form
    never showed."""
    row = web_manual(admin, migrated)
    path = f"/history/meals/{row.id}"
    stamp = stamp_of(page(admin.get(path)))
    bad = submit(admin, path, posted(row, day="not a date", shown_meal=stamp))
    assert bad.status_code == 400 and stamp_of(bad.text) == stamp
    assert submit(admin, path, posted(row, slot="lunch", shown_meal=stamp)).status_code == 303
    malformed = submit(admin, path, posted(row, day="not a date", shown_meal=stamp))
    assert malformed.status_code == 400 and stamp_of(malformed.text) == stamp
    assert submit(admin, path, posted(row, slot="dinner", shown_meal=stamp_of(malformed.text))).status_code == 409
    assert the_meal(migrated).slot == "lunch"


@pytest.mark.parametrize("kind", ["manual", "call"])
def test_web_a_stale_delete_lands_on_the_meal_with_why(
    admin: TestClient, clock: FakeClock, migrated: Settings, kind: str
) -> None:
    if kind == "manual":
        row = web_manual(admin, migrated)
    else:
        web_done(admin, clock, migrated)
        row = the_meal(migrated)
    path = f"/history/meals/{row.id}"
    stamp = stamp_of(page(admin.get(path)))
    assert submit(admin, path, posted(row, slot="lunch", shown_meal=stamp)).status_code == 303

    boosted = {"HX-Request": "true", "HX-Boosted": "true"}
    refused = admin.post(
        f"{path}/delete",
        data={"shown_meal": stamp, "csrf_token": csrf_from(admin)},
        headers=boosted,
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == f"/history?notice=changed&meal={row.id}#meal-{row.id}"
    html = page(admin.get(refused.headers["location"]))
    assert 'class="alert error landed-notice" role="alert"' in html
    assert "This meal changed since you opened this page" in html
    assert the_meal(migrated).id == row.id

    fresh = stamp_of(page(admin.get(path)))
    assert submit(admin, f"{path}/delete", {"shown_meal": fresh}).status_code == 303
    assert not rows(migrated, MealLogRow)
    assert submit(admin, f"{path}/delete", {"shown_meal": fresh}).status_code == 404  # the second click


def test_web_a_top_of_page_refusal_shows_as_an_error(admin: TestClient, migrated: Settings) -> None:
    """The notice has its meal's entry only on the page that lists it."""
    html = page(admin.get("/history?notice=changed"))
    assert 'class="alert error" role="alert"' in html and "This meal changed" in html
    assert 'class="alert ok" role="status"' in page(admin.get("/history?notice=deleted"))


def test_web_an_old_page_without_the_stamp_still_works(admin: TestClient, migrated: Settings) -> None:
    row = web_manual(admin, migrated)
    path = f"/history/meals/{row.id}"
    assert submit(admin, path, posted(row, slot="lunch")).status_code == 303
    assert the_meal(migrated).slot == "lunch"
    assert submit(admin, f"{path}/delete").status_code == 303
    assert not rows(migrated, MealLogRow)


def test_web_a_form_opened_before_a_correction_leaves_the_place_and_feedback_alone(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    event_id, shown, home = web_done(admin, clock, migrated)
    row = the_meal(migrated)
    path = f"/history/meals/{row.id}"
    feedback_form = {"rating": Rating.LIKED.value}
    assert submit(admin, f"{path}/feedback", feedback_form).status_code == 303
    stamp = stamp_of(page(admin.get(path)))

    place = {"item_id": str(home), "mode": "cook", "opened_item": str(shown), "run": "1"}
    assert submit(admin, f"/events/{event_id}/admin/place", place).status_code == 303
    (moved,) = rows(migrated, FeedbackRow)
    assert moved.item_id == home

    stale = submit(admin, path, posted(row, shown_meal=stamp))
    assert stale.status_code == 409 and "This meal changed" in stale.text and "Sam&#39;s Tacos" in stale.text
    old_page = submit(admin, path, posted(row))
    assert old_page.status_code == 400 and "came from a call" in old_page.text
    assert submit(admin, f"{path}/delete", {"shown_meal": stamp}).status_code == 303  # refused: lands on the meal

    assert the_meal(migrated).item_id == home
    (still,) = rows(migrated, FeedbackRow)
    assert (still.item_id, still.rating) == (home, Rating.LIKED.value)
    assert not [a for a in rows(migrated, AuditRow) if a.action in {"meal.updated", "meal.deleted"}]


def test_web_change_the_plan_works_from_what_the_meal_says(
    admin: TestClient, clock: FakeClock, migrated: Settings
) -> None:
    event_id, siam, home = web_done(admin, clock, migrated)
    row = the_meal(migrated)
    set_meal(migrated, row.id, item_id=home)
    url = f"/events/{event_id}/admin/place"

    html = page(admin.get(f"/events/{event_id}"))
    assert f'name="opened_item" value="{home}"' in html
    place_menu = html.split(f'action="{url}"')[1].split('name="item_id"')[1].split("</select>")[0]
    assert "Keep Sam's Tacos, or type a new one" in place_menu
    assert f'value="{siam}"' in place_menu and f'value="{home}"' not in place_menu
    assert "this call still says" in html

    # Back to the place the call still names: the page's own choice.
    back = {"item_id": str(siam), "opened_item": str(home), "run": "1"}
    assert submit(admin, url, back).status_code == 303
    assert the_meal(migrated).item_id == siam
    (event_row,) = rows(migrated, EventRow)
    assert event_row.decided_item_id == siam
    html = page(admin.get(f"/events/{event_id}"))
    assert f'name="opened_item" value="{siam}"' in html and "this call still says" not in html
    same_again = submit(admin, url, {"opened_item": str(siam), "run": "1"})
    assert same_again.status_code == 409 and "the place already" in same_again.text

    # Left apart again, and kept: the call comes to the meal.
    set_meal(migrated, row.id, item_id=home)
    assert submit(admin, url, {"opened_item": str(home), "run": "1"}).status_code == 303
    (event_row,) = rows(migrated, EventRow)
    assert (event_row.decided_item_id, event_row.decided_mode) == (home, "cook")
    assert the_meal(migrated).item_id == home


def test_web_a_deleted_meal_has_no_place_to_correct(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id, shown, _ = web_done(admin, clock, migrated)
    url = f"/events/{event_id}/admin/place"
    row = the_meal(migrated)
    assert f'action="{url}"' in page(admin.get(f"/events/{event_id}"))
    assert submit(admin, f"/history/meals/{row.id}/delete").status_code == 303

    html = page(admin.get(f"/events/{event_id}"))
    assert f'action="{url}"' not in html and "Correct the place" not in html
    assert f'action="/events/{event_id}/assignments"' in html  # who did what can still be corrected
    refused = submit(admin, url, {"new_place": "Noodle Bar", "mode": "pickup", "opened_item": str(shown), "run": "1"})
    assert refused.status_code == 409 and "no longer in History" in refused.text
