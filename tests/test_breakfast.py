# SPDX-License-Identifier: AGPL-3.0-or-later
"""Breakfast calls (#58): never posted on their own unless someone turns the schedule on,
started on demand from Slack and the web, and run end to end like lunch. The migration is in
test_migrations.py, and the Home and modal layouts in the golden files."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from dinnerbell.channels.slack import ids
from dinnerbell.channels.slack.interactions import CALLED_OFF, parse_call_args, parse_cancel_args, which_call
from dinnerbell.config import Settings
from dinnerbell.db.events import EventRepository
from dinnerbell.domain.catalog import Meal, Mode
from dinnerbell.domain.events import Event, EventState, RoleOffer, Status, Step
from dinnerbell.domain.meals import CALL_ORDER, MealLog, MemberRef, Slot, by_week
from dinnerbell.domain.schedule import (
    DEFAULT_SLOTS,
    default_settings,
    slot_fingerprint,
    slot_schedules,
    slot_to_json,
)
from dinnerbell.services import calls
from dinnerbell.services import schedule as schedule_service
from dinnerbell.services.database import Database, open_database
from tests.conftest import page
from tests.support.calls import ZONE, House, RecordingChannel, furnish, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.slack import Slack, wire
from tests.support.web import submit
from tests.test_activity_logs import lines
from tests.test_events_web import admin  # noqa: F401 - a fixture

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 22)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    """A wall-clock time in the household's time zone, as UTC."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(ZONE)).astimezone(UTC)


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> House:
    """The usual household, plus two breakfast places: Sam's pancakes and a bagel shop that's
    open for lunch too."""
    house = await furnish(await make_house(database.sessionmaker, clock, migrated.admin_email))
    await house.add_item("Pancakes", Mode.COOK, cuisine="American", cooks=("Sam",), meals=frozenset({Meal.BREAKFAST}))
    await house.add_item("Bagel Barn", Mode.PICKUP, cuisine="Bakery", meals=frozenset({Meal.BREAKFAST, Meal.LUNCH}))
    return house


@pytest.fixture
async def slack(house: House) -> AsyncIterator[Slack]:
    wired = await wire(house)
    yield wired
    await wired.conn.stop()


async def call_for(house: House, slot: Slot, day: date = DAY) -> Event | None:
    async with house.sessionmaker() as db:
        return await EventRepository(db).call_for(house.household.id, slot, day)


async def breakfast(house: House, day: date = DAY) -> Event:
    event = await call_for(house, Slot.BREAKFAST, day)
    assert event is not None
    return event


# Defaults ----------------------------------------------------------------------------------------


def test_breakfast_has_no_daily_call_by_default_and_sensible_times() -> None:
    schedule = DEFAULT_SLOTS[Slot.BREAKFAST]
    assert not schedule.enabled
    assert (schedule.call_time.hour, schedule.meal_time.hour) == (7, 8)
    assert schedule.modes == {Mode.COOK, Mode.PICKUP}
    assert tuple(DEFAULT_SLOTS) == CALL_ORDER == (Slot.DINNER, Slot.LUNCH, Slot.BREAKFAST)
    assert default_settings()["slots"]["breakfast"]["enabled"] is False
    assert not slot_schedules({})[Slot.BREAKFAST].enabled  # a household saved before #58
    assert Slot.BREAKFAST.meal is Meal.BREAKFAST and list(Slot) == [Slot.BREAKFAST, Slot.LUNCH, Slot.DINNER]


@pytest.mark.parametrize("corrupt", ["true", 1, "on", None])
def test_a_corrupt_enabled_value_reads_as_off(corrupt: object) -> None:
    """Only a JSON ``true`` turns breakfast on. Anything else, such as a hand edit, falls back to
    the default, off, so a bad value can't start posting breakfast calls."""
    stored = {**slot_to_json(DEFAULT_SLOTS[Slot.BREAKFAST]), "enabled": corrupt}
    assert slot_schedules({"slots": {"breakfast": stored}})[Slot.BREAKFAST].enabled is False


async def test_no_breakfast_call_posts_on_its_own_until_someone_turns_it_on(house: House, clock: FakeClock) -> None:
    """A whole day of ticks posts dinner and nothing else. Once a member turns breakfast's
    schedule on, it posts at 7:00 AM from the next day it can."""
    for minutes in range(0, 24 * 60, 15):
        clock.now = at(0) + timedelta(minutes=minutes)
        await house.tick()
    assert await call_for(house, Slot.BREAKFAST) is None and await call_for(house, Slot.LUNCH) is None
    assert await call_for(house, Slot.DINNER) is not None

    clock.now = at(20)
    on = replace(DEFAULT_SLOTS[Slot.BREAKFAST], enabled=True)
    async with house.sessionmaker() as db:
        seen = slot_fingerprint(DEFAULT_SLOTS[Slot.BREAKFAST])
        assert await schedule_service.update_slot(db, house["Alex"], Slot.BREAKFAST, on, seen=seen, now=clock())
    tomorrow = DAY + timedelta(days=1)
    clock.now = at(6, 59, tomorrow)
    await house.tick()
    assert await call_for(house, Slot.BREAKFAST, tomorrow) is None
    clock.now = at(7, 0, tomorrow)
    await house.tick()
    assert (await breakfast(house, tomorrow)).state is EventState.COLLECTING


# End to end --------------------------------------------------------------------------------------


async def test_a_breakfast_call_runs_end_to_end_like_lunch(house: House, clock: FakeClock) -> None:
    """Kim starts breakfast at 6:30 AM. Ranking proposes the breakfast places first and tops up
    with one for other meals; the vote decides, the usual breakfast time is the meal time, and
    the meal is logged as breakfast."""
    channel = RecordingChannel()
    clock.now = at(6, 30)
    async with house.sessionmaker() as db:
        started = await calls.start_call(db, house["Kim"], Slot.BREAKFAST, DAY, now=clock())
    event = await breakfast(house)
    assert (event.id, event.state, event.created_by) == (started.id, EventState.COLLECTING, house["Kim"].id)
    assert (event.respond_deadline, event.vote_minutes, event.reminder_minutes) == (at(7), 15, 10)

    async with house.sessionmaker() as db:
        await calls.respond(db, house["Alex"], event.id, Status.IN, now=clock())
        await calls.offer_role(db, house["Alex"], event.id, RoleOffer.PICKUP, True, now=clock())
        await calls.respond(db, house["Sam"], event.id, Status.IN, now=clock())
        await calls.offer_role(db, house["Sam"], event.id, RoleOffer.COOK, True, now=clock())
    clock.now = at(6, 50)
    assert await house.tick() == [Step.REMIND]

    clock.now = at(7)
    assert await house.tick() == [Step.CLOSE_RESPONSES]
    ctx = await house.context(event.id)
    assert ctx.event.state is EventState.VOTING
    names = [ctx.items[p.item_id or 0].name for p in ctx.proposals]
    assert set(names[:2]) == {"Pancakes", "Bagel Barn"}  # breakfast places first
    assert names[2] in {"Siam Garden", "Taqueria", "Sam's Tacos"}  # and a top-up
    assert ctx.proposals[2].reasons[0] == "Usually for lunch and dinner."
    assert all(not p.reasons or not p.reasons[0].startswith("Usually") for p in ctx.proposals[:2])

    pancakes = next(p.id for p in ctx.proposals if p.item_id == house.items["Pancakes"].id)
    async with house.sessionmaker() as db:
        await calls.vote(db, house["Alex"], event.id, pancakes, now=clock())
        await calls.vote(db, house["Sam"], event.id, pancakes, now=clock())
    clock.now = at(7, 15)
    assert await house.tick() == [Step.CLOSE_VOTING]
    ctx = await house.context(event.id)
    assert ctx.event.state is EventState.DECIDED
    assert (ctx.event.decided_item_id, ctx.event.decided_mode) == (house.items["Pancakes"].id, Mode.COOK)
    assert ctx.event.meal_time == at(8)  # nobody gave a time: breakfast's usual meal time
    assert [(a.role.value, a.member_id) for a in ctx.assignments] == [("cook", house["Sam"].id)]

    clock.now = at(10)
    assert await house.tick() == [Step.MARK_DONE]
    (meal,) = await house.meals()
    assert (meal.event_id, meal.slot, meal.item_id) == (event.id, "breakfast", house.items["Pancakes"].id)
    assert [p["name"] for p in meal.participants] == ["Alex", "Sam"]

    await house.drain(channel)
    kinds = [s.kind for s in channel.sent if s.op == "deliver"]
    assert kinds[0] == "call_opened"
    assert {"proposals_ready", "decision_made", "assignment_made"} <= set(kinds)
    assert [a.action for a in await house.audits()] == ["event.opened", "event.voting", "event.decided", "event.done"]


# Slack -------------------------------------------------------------------------------------------


def test_parsing_knows_breakfast() -> None:
    assert parse_call_args(["breakfast"], DAY) == (Slot.BREAKFAST, DAY, False)
    assert parse_call_args(["tomorrow", "breakfast", "now"], DAY) == (Slot.BREAKFAST, DAY + timedelta(days=1), True)
    assert parse_cancel_args(["Breakfast", "tomorrow", "Out", "of", "eggs"], DAY) == (
        Slot.BREAKFAST,
        DAY + timedelta(days=1),
        "Out of eggs",
    )
    assert parse_cancel_args(["dinner", "breakfast", "ran", "late"], DAY) == (Slot.DINNER, None, "breakfast ran late")


def test_which_call_names_every_open_call() -> None:
    assert which_call(list(Slot), when="today", command="/dinnerbell", day="") == (
        "Breakfast, lunch, and dinner are all open today. Which one? Try `/dinnerbell cancel breakfast`, "
        "`/dinnerbell cancel lunch`, or `/dinnerbell cancel dinner`, and add a reason after it if you like."
    )
    assert which_call([Slot.BREAKFAST, Slot.DINNER], when="on Friday, Oct 23", command="/db", day=" 2026-10-23") == (
        "Breakfast and dinner are both open on Friday, Oct 23. Which one? Try `/db cancel breakfast 2026-10-23` "
        "or `/db cancel dinner 2026-10-23`, and add a reason after it if you like."
    )


async def test_slash_call_breakfast_starts_one_and_cancel_asks_which_of_three(
    slack: Slack, house: House, caplog: pytest.LogCaptureFixture
) -> None:
    house.clock.now = at(6, 30)
    await slack.command("Kim", "call breakfast")
    event = await breakfast(house)
    assert event.state is EventState.COLLECTING and event.created_by == house["Kim"].id
    assert "breakfast" in slack.ephemerals("Kim")[-1].lower()

    await slack.command("Kim", "call lunch")
    await slack.command("Kim", "call")
    await slack.command("Kim", "cancel")
    assert slack.ephemerals("Kim")[-1] == which_call(list(Slot), when="today", command="/dinnerbell", day="")
    await slack.command("Kim", "cancel breakfast Out of eggs")
    called_off = await breakfast(house)
    assert (called_off.state, called_off.end_note) == (EventState.CANCELLED, "Out of eggs")
    assert slack.ephemerals("Kim")[-1] == CALLED_OFF.format(slot="breakfast", day="Thursday, Oct 22")
    assert (await call_for(house, Slot.LUNCH)).state is EventState.COLLECTING  # type: ignore[union-attr]
    logged = [(line["msg"], line["slot"]) for line in lines(caplog) if line.get("event_id") == event.id]
    assert logged == [("start", "breakfast"), ("call_off", "breakfast")]


async def test_home_starts_breakfast_and_starts_it_again(slack: Slack, house: House) -> None:
    house.clock.now = at(6, 30)
    await slack.open_home("Lee")
    starts = next(b for b in slack.last_home("Lee")["blocks"] if b.get("block_id") == "start")["elements"]
    assert [e["text"]["text"] for e in starts] == [
        "Make an offer",
        "Start a dinner call",
        "Start a lunch call",
        "Start a breakfast call",
    ]
    await slack.click("Lee", ids.ACTION_FOR_START[Slot.BREAKFAST], "breakfast", home=True)
    event = await breakfast(house)
    assert event.state is EventState.COLLECTING and event.created_by == house["Lee"].id
    home = json.dumps(slack.last_home("Lee"))
    assert "start_call_breakfast" not in home and "*Breakfast*" in home

    await slack.command("Lee", "cancel breakfast")
    await slack.open_home("Sam")
    starts = next(b for b in slack.last_home("Sam")["blocks"] if b.get("block_id") == "start")["elements"]
    assert "Start breakfast again" in [e["text"]["text"] for e in starts]
    await slack.click("Sam", ids.ACTION_FOR_START[Slot.BREAKFAST], "breakfast", home=True)
    again = await breakfast(house)
    assert (again.id, again.state, again.run, again.created_by) == (
        event.id,
        EventState.COLLECTING,
        2,
        house["Sam"].id,
    )


# The web -----------------------------------------------------------------------------------------


def test_the_today_page_starts_a_breakfast_call(admin: TestClient) -> None:  # noqa: F811 - the fixture
    today = page(admin.get("/"))
    assert '<option value="breakfast">Breakfast</option>' in today
    started = submit(admin, "/events", {"slot": "breakfast", "day": "2026-10-23"})
    assert started.status_code == 303, page(started)
    shown = page(admin.get(started.headers["location"]))
    assert "<title>Breakfast" in shown and "Scheduled" in shown
    brunch = submit(admin, "/events", {"slot": "brunch", "day": "2026-10-23"})
    assert "Pick breakfast, lunch, or dinner." in page(brunch)


def test_the_schedule_page_shows_breakfast_off_and_skipping_needs_it_on(
    admin: TestClient,  # noqa: F811 - the fixture
    migrated: Settings,
) -> None:
    html = page(admin.get("/schedule"))
    assert html.index('id="dinner"') < html.index('id="lunch"') < html.index('id="breakfast"')
    assert 'name="breakfast_enabled">' in html and 'name="breakfast_call_time" value="07:00"' in html
    assert 'name="breakfast_meal_time" value="08:00"' in html
    assert '<option value="breakfast"' not in page(admin.get("/admin/schedule"))
    refused = submit(admin, "/admin/schedule/skip", {"slot": "breakfast", "day": "2026-10-23"})
    assert refused.status_code == 409 and "Breakfast is off in the schedule." in page(refused)

    form = {
        "breakfast_seen": slot_fingerprint(DEFAULT_SLOTS[Slot.BREAKFAST]),
        "breakfast_enabled": "on",
        "breakfast_call_time": "08:00",
        "breakfast_meal_time": "09:30",
        "breakfast_response_minutes": "30",
        "breakfast_vote_minutes": "15",
        "breakfast_reminder_minutes": "10",
        "breakfast_modes": ["cook", "pickup"],
    }
    saved = submit(admin, "/schedule/breakfast", form)
    assert saved.headers["location"] == "/schedule?saved=breakfast#breakfast"
    assert '<option value="breakfast"' in page(admin.get("/admin/schedule"))
    skipped = submit(admin, "/admin/schedule/skip", {"slot": "breakfast", "day": "2026-10-23"})
    assert skipped.status_code == 303


def test_history_logs_breakfast_and_orders_a_day_from_dinner_back(admin: TestClient, provider: FakeProvider) -> None:  # noqa: F811
    assert '<input type="radio" name="slot" value="breakfast">' in page(admin.get("/history"))
    who = (MemberRef(1, "Alex"),)
    meals = [MealLog(n, 1, DAY, slot, 1, "X", (), who, None) for n, slot in enumerate(Slot, start=1)]
    ((_, week),) = by_week(meals)
    assert [m.slot for m in week] == [Slot.DINNER, Slot.LUNCH, Slot.BREAKFAST]
