# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assistant's ``look_up_history`` tool (#264, spec 6.14, **Tools**): it answers from the
meal log, per catalog item, with the hint the catalog shows (FR-49) under the same thresholds,
and nothing about who ate, cooked, or rated. The privacy and injection attacks are in
``test_assistant_security``."""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.catalog import CatalogRepository
from dinnerbell.db.meals import household_settings
from dinnerbell.db.repositories import HouseholdRepository, MemberRepository
from dinnerbell.domain.catalog import Mode
from dinnerbell.domain.feedback import FeedbackInput, PlaceIssue, Rating
from dinnerbell.domain.meals import MealInput, Slot
from dinnerbell.domain.members import Member
from dinnerbell.domain.schedule import FEEDBACK_KEY, feedback_cap
from dinnerbell.services import feedback, meals
from dinnerbell.services.assistant import tools
from dinnerbell.services.assistant.tools import MAX_ITEMS, ToolResult
from dinnerbell.services.database import Database, open_database
from tests.conftest import page, sign_in
from tests.support.calls import House, make_house
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.test_events_web import sign_in_as
from tests.test_feedback_web import KIM, give, household, item_entry, meal_id  # noqa: F401 - a fixture
from tests.test_history import log

pytestmark = pytest.mark.anyio

DAY = date(2026, 10, 22)
"""Today in the household, at the fake clock's start."""
ANY = {"name": "", "tag": "", "kind": "", "since": "", "until": "", "order": "most"}
HISTORY = "look_up_history"


@pytest.fixture
async def database(migrated: Settings) -> AsyncIterator[Database]:
    database = open_database(migrated.async_database_url)
    yield database
    await database.close()


@pytest.fixture
async def house(database: Database, clock: FakeClock, migrated: Settings) -> AsyncIterator[House]:
    """Alex (the admin), Sam, and Kim; two pizza places, a Thai place, and a home meal."""
    house = await make_house(database.sessionmaker, clock, migrated.admin_email)
    await house.add_member("Sam")
    await house.add_member("Kim")
    await house.add_item("Slice Shop", Mode.PICKUP, cuisine="Pizza")
    await house.add_item("Crust Corner", Mode.DELIVERY, cuisine="Pizza", tags=("Vegetarian",))
    await house.add_item("Siam Garden", Mode.PICKUP, cuisine="Thai")
    await house.add_item("Pasta Night", Mode.COOK, cuisine="Italian")
    await house.add_item("Never Had", Mode.PICKUP, cuisine="Thai")
    yield house
    await house.close()


async def eat(house: House, item: str, day: date, *people: str, slot: Slot = Slot.DINNER) -> int:
    who = [house[p] for p in people or ("Sam",)]
    data = MealInput(day, slot, house.items[item].id, frozenset(m.id for m in who))
    async with house.sessionmaker() as db:
        meal = await meals.log_meal(db, who[0], data, today=DAY, now=house.clock())
    return meal.id


async def rate(house: House, who: str, meal: int, rating: Rating | None, *issues: PlaceIssue) -> None:
    async with house.sessionmaker() as db:
        await feedback.give(
            db, house[who], meal, FeedbackInput(rating=rating, place_issues=frozenset(issues)), now=house.clock()
        )


async def history(house: House, who: str = "Sam", **args: Any) -> dict[str, Any]:
    result = await call(house, ANY | args, who=who)
    assert not result.is_error, result.content
    found: dict[str, Any] = json.loads(result.content)
    return found


async def call(house: House, args: dict[str, Any], *, who: str = "Sam") -> ToolResult:
    async with house.sessionmaker() as db:
        return await tools.run_tool(HISTORY, args, db=db, member=house[who], private=False, now=house.clock())


def by_name(found: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in found["items"]}


async def test_it_answers_from_the_meal_log_per_item(house: House) -> None:
    for days_ago in (30, 20, 10):
        await eat(house, "Slice Shop", DAY - timedelta(days=days_ago))
    await eat(house, "Crust Corner", DAY - timedelta(days=3))
    await eat(house, "Siam Garden", DAY - timedelta(days=40))
    await eat(house, "Siam Garden", DAY - timedelta(days=5), slot=Slot.LUNCH)
    await eat(house, "Pasta Night", DAY - timedelta(days=1))

    found = await history(house)
    assert found["today"] == "2026-10-22" and found["more"] is False
    assert [i["name"] for i in found["items"]] == ["Slice Shop", "Siam Garden", "Pasta Night", "Crust Corner"]
    assert by_name(found)["Slice Shop"] == {
        "name": "Slice Shop",
        "kind": "restaurant",
        "cuisines": ["Pizza"],
        "tags": [],
        "in_catalog": True,
        "times_eaten": 3,
        "last_eaten": "2026-10-12",
        "hint": None,
    }
    assert by_name(found)["Crust Corner"]["tags"] == ["Vegetarian"]
    assert "Never Had" not in by_name(found)  # only what the household ate

    # "What's our most-ordered pizza place?" and "When did we last have pizza?"
    pizza = await history(house, tag="pizza")
    assert [i["name"] for i in pizza["items"]] == ["Slice Shop", "Crust Corner"]
    recent = await history(house, tag="PIZZA", order="recent")
    assert [i["name"] for i in recent["items"]] == ["Crust Corner", "Slice Shop"]

    assert [i["name"] for i in (await history(house, tag="vegetarian"))["items"]] == ["Crust Corner"]
    assert [i["name"] for i in (await history(house, name="siam"))["items"]] == ["Siam Garden"]
    assert [i["name"] for i in (await history(house, kind="home"))["items"]] == ["Pasta Night"]
    assert "Pasta Night" not in by_name(await history(house, kind="restaurant"))

    # A date range counts only the meals inside it, both ends included.
    window = await history(house, since="2026-09-12", until="2026-10-12")
    assert {i["name"]: i["times_eaten"] for i in window["items"]} == {"Slice Shop": 3, "Siam Garden": 1}
    assert by_name(window)["Slice Shop"]["last_eaten"] == "2026-10-12"
    month = await history(house, since="2026-09-23", until="today")
    assert by_name(month)["Slice Shop"]["times_eaten"] == 2
    siam = by_name(month)["Siam Garden"]
    assert (siam["times_eaten"], siam["last_eaten"]) == (1, "2026-10-17")


async def test_a_place_off_the_catalog_still_answers_and_any_member_asks_alike(house: House) -> None:
    await eat(house, "Siam Garden", DAY - timedelta(days=2))
    async with house.sessionmaker() as db:
        await CatalogRepository(db).set_active(house.household.id, house.items["Siam Garden"].id, False, house.clock())
        await db.commit()
    found = await history(house)
    assert by_name(found)["Siam Garden"]["in_catalog"] is False
    assert await history(house, who="Kim") == found == await history(house, who="Alex")


async def test_at_most_max_items_come_back(house: House) -> None:
    for n in range(MAX_ITEMS + 2):
        await house.add_item(f"Place {n:02}", Mode.PICKUP, cuisine="Burgers")
        await eat(house, f"Place {n:02}", DAY - timedelta(days=n + 1))
    found = await history(house, tag="burgers", order="recent")
    assert len(found["items"]) == MAX_ITEMS and found["more"] is True
    assert found["items"][0]["name"] == "Place 00"
    assert (await history(house, name="place 0"))["more"] is False


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"name": ""}, "Send exactly these fields"),
        (ANY | {"extra": ""}, "Send exactly these fields"),
        (ANY | {"name": "x" * 81}, "name is at most 80 characters"),
        (ANY | {"tag": 3}, "tag must be a string"),
        (ANY | {"kind": "takeout"}, "kind is one of"),
        (ANY | {"order": "best"}, "order is one of"),
        (ANY | {"since": "last week"}, "since is today, tomorrow, or a date"),
        (ANY | {"until": "2026-02-30"}, "until is today, tomorrow, or a date"),
        (ANY | {"since": "2026-10-20", "until": "2026-10-01"}, "since is on or before until"),
    ],
)
async def test_bad_input_is_an_error_for_the_model(house: House, args: dict[str, Any], error: str) -> None:
    result = await call(house, args)
    assert result.is_error and error in result.content, result.content


# Hints: the catalog's, under the same thresholds ------------------------------------------------


async def test_the_hint_is_the_catalogs_and_needs_two_recent_raters(house: House, clock: FakeClock) -> None:
    """Two people's fresh ratings give a verdict; one person's rating gives none, and nor do two
    people's ratings once they've faded (ADR 0008). Each is what the catalog shows."""
    slice_meal = await eat(house, "Slice Shop", DAY - timedelta(days=1), "Sam", "Kim")
    await rate(house, "Sam", slice_meal, Rating.LOVED)
    await rate(house, "Kim", slice_meal, Rating.HATED, PlaceIssue.LATE)
    crust_meal = await eat(house, "Crust Corner", DAY - timedelta(days=1), "Sam", "Kim")
    await rate(house, "Sam", crust_meal, Rating.LOVED)  # one person: no verdict
    siam_meal = await eat(house, "Siam Garden", DAY - timedelta(days=1), "Sam", "Kim")
    await rate(house, "Sam", siam_meal, Rating.LIKED)
    await rate(house, "Kim", siam_meal, Rating.LOVED)
    pasta_meal = await eat(house, "Pasta Night", DAY - timedelta(days=1), "Sam", "Kim")
    await rate(house, "Kim", pasta_meal, Rating.DISLIKED)

    found = by_name(await history(house))
    assert found["Slice Shop"]["hint"] == "Mixed reviews · recently late"
    assert found["Siam Garden"]["hint"] == "Well liked"
    assert found["Crust Corner"]["hint"] is None
    assert found["Pasta Night"]["hint"] is None
    async with house.sessionmaker() as db:
        shown = await feedback.item_hints(db, house["Sam"], today=DAY)
    assert {name: item["hint"] for name, item in found.items()} == {
        name: shown.get(house.items[name].id) for name in found
    }

    # Four months on, both verdicts have faded below the threshold, as on the catalog.
    clock.now += timedelta(days=120)
    later = by_name(await history(house))
    assert later["Siam Garden"]["hint"] is None and later["Slice Shop"]["hint"] is None
    async with house.sessionmaker() as db:
        assert await feedback.item_hints(db, house["Sam"], today=DAY + timedelta(days=120)) == {}


async def test_the_hint_covers_every_rating_whatever_the_dates_asked(house: House) -> None:
    """The dates narrow the counts, never the hint: it's the household's, from all its ratings,
    as on the catalog, even in a window that leaves out the rated meal."""
    await eat(house, "Siam Garden", DAY - timedelta(days=20))
    rated = await eat(house, "Siam Garden", DAY - timedelta(days=5), "Sam", "Kim")
    await rate(house, "Sam", rated, Rating.LIKED)
    await rate(house, "Kim", rated, Rating.LOVED)
    await eat(house, "Siam Garden", DAY - timedelta(days=1))
    everything = by_name(await history(house))["Siam Garden"]
    assert (everything["times_eaten"], everything["hint"]) == (3, "Well liked")
    windows = {
        "only the latest meal, after the rated one": ({"since": "2026-10-21"}, 1, "2026-10-21"),
        "only the oldest meal": ({"until": "2026-10-10"}, 1, "2026-10-02"),
        "only the rated meal": ({"since": "2026-10-17", "until": "2026-10-17"}, 1, "2026-10-17"),
    }
    for name, (dates, times, last) in windows.items():
        item = by_name(await history(house, **dates))["Siam Garden"]
        assert (item["times_eaten"], item["last_eaten"], item["hint"]) == (times, last, everything["hint"]), name


async def set_feedback(house: House, cap: int) -> None:
    async with house.sessionmaker() as db:
        stored = await household_settings(db, house.household.id)
        await HouseholdRepository(db).update_settings(house.household.id, stored | {FEEDBACK_KEY: cap})
        await db.commit()


async def test_hints_show_with_feedback_off_as_on_the_catalog(house: House) -> None:
    """Admin's Feedback setting stops ratings moving scores (ADR 0008), not the hint everyone
    sees, so the tool gives the hint at Feedback 0 too."""
    meal = await eat(house, "Siam Garden", DAY - timedelta(days=1), "Sam", "Kim")
    await rate(house, "Sam", meal, Rating.LIKED)
    await rate(house, "Kim", meal, Rating.LOVED)
    await set_feedback(house, 0)
    async with house.sessionmaker() as db:
        assert feedback_cap(await household_settings(db, house.household.id)) == 0
        shown = await feedback.item_hints(db, house["Sam"], today=DAY)
    assert shown == {house.items["Siam Garden"].id: "Well liked"}
    assert by_name(await history(house))["Siam Garden"]["hint"] == "Well liked"


def on_the_database(settings: Settings, work: Any) -> Any:
    async def go() -> Any:
        database = open_database(settings.async_database_url)
        try:
            async with database.sessionmaker() as db:
                [admin] = [m for m in await MemberRepository(db).list(1) if m.email == settings.admin_email]
                return await work(db, admin)
        finally:
            await database.close()

    return asyncio.run(go())


def catalog_hint(client: TestClient, item_id: int) -> str | None:
    entry = item_entry(page(client.get("/catalog")), item_id)
    match = re.search(r'<p class="small item-feedback">(.*?)</p>', entry, re.S)
    return html.unescape(match.group(1).strip()) if match else None


@pytest.mark.parametrize("cap", [2, 0])
def test_hints_match_what_the_catalog_page_shows(
    client: TestClient,
    provider: FakeProvider,
    household: dict[str, int],  # noqa: F811 - the fixture imported above
    migrated: Settings,
    clock: FakeClock,
    cap: int,
) -> None:
    """The same data, through the web forms: the tool's hint for each item is the catalog's, at
    Admin's Feedback 2 (the default) and 0 (off)."""
    log(client, household["siam"], [household["admin"], household["kim"]])
    log(client, household["tacos"], [household["admin"], household["kim"]])
    siam, tacos = meal_id(migrated, household["siam"]), meal_id(migrated, household["tacos"])
    give(client, siam, rating="liked")
    give(client, tacos, rating="loved")
    sign_in_as(client, provider, KIM)
    give(client, siam, rating="hated", place_issues=["late"])
    client.cookies.clear()
    sign_in(client, provider)

    async def feedback_at(db: Any, admin: Member) -> None:
        stored = await household_settings(db, admin.household_id)
        await HouseholdRepository(db).update_settings(admin.household_id, stored | {FEEDBACK_KEY: cap})
        await db.commit()

    on_the_database(migrated, feedback_at)

    async def look(db: Any, admin: Member) -> dict[str, Any]:
        result = await tools.run_tool(HISTORY, ANY, db=db, member=admin, private=False, now=clock())
        assert not result.is_error, result.content
        found: dict[str, Any] = json.loads(result.content)
        return found

    found = by_name(on_the_database(migrated, look))
    assert catalog_hint(client, household["siam"]) == "Mixed reviews · recently late"
    assert found["Siam Garden"]["hint"] == catalog_hint(client, household["siam"])
    assert catalog_hint(client, household["tacos"]) is None  # one rater
    assert found["Tacos"]["hint"] is None
