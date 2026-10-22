# SPDX-License-Identifier: AGPL-3.0-or-later
"""The meal log and History page (FR-30 to FR-32, spec 5.6)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, MealLogRow, TagRow
from dinnerbell.services import variety
from dinnerbell.services.bootstrap import bootstrap
from dinnerbell.services.database import open_database
from tests.conftest import invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, submit
from tests.test_admin import sign_in_as
from tests.test_auth_flow import member, rows
from tests.test_catalog_web import item_row

# The fake clock reads 2026-10-22 18:00 UTC: 13:00 in America/Chicago, a Thursday.
TODAY = date(2026, 10, 22)


def log(client: TestClient, item_id: int, people: list[int], day: date = TODAY, slot: str = "dinner") -> httpx.Response:
    data: dict[str, Any] = {"day": day.isoformat(), "slot": slot, "item_id": str(item_id)}
    data["participant_ids"] = [str(p) for p in people]
    return submit(client, "/history/meals", data)


def set_window(settings: Settings, days: int) -> None:
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE household SET settings = :s"), {"s": json.dumps({"variety_window_days": days})}
            )
    finally:
        engine.dispose()


@pytest.fixture
def household(client: TestClient, provider: FakeProvider, migrated: Settings) -> dict[str, int]:
    """The admin and Sam, a Thai restaurant, and a Mexican home meal."""
    sign_in(client, provider)
    invite(client, "sam@example.com", "Sam")
    add_item(client, "Siam Garden", cuisines="Thai, Lao", new_tags="spicy")
    add_item(client, "Tacos", kind="home", cuisines="Mexican")
    return {
        "admin": member(migrated, "admin@example.com").id,
        "sam": member(migrated, "sam@example.com").id,
        "siam": item_row(migrated, "Siam Garden").id,
        "tacos": item_row(migrated, "Tacos").id,
    }


def test_empty_history(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    text = page(client.get("/history"))
    assert "No meals logged yet." in text
    assert "Nothing logged since Oct 16." in text
    assert "Add something to the catalog first." in re.sub(r"<[^>]+>", "", text)


def test_log_form_defaults(client: TestClient, household: dict[str, int]) -> None:
    response = client.get("/history")
    assert 'name="day" required max="2026-10-22" value="2026-10-22"' in response.text
    assert 'value="dinner" checked' in response.text
    assert f'name="participant_ids" value="{household["admin"]}" checked' in response.text
    assert f'name="participant_ids" value="{household["sam"]}">' in response.text


def test_log_a_meal_snapshots_cuisines_and_people(
    client: TestClient, household: dict[str, int], migrated: Settings
) -> None:
    response = log(client, household["siam"], [household["admin"], household["sam"]], day=TODAY - timedelta(days=1))
    assert response.status_code == 303
    [meal] = rows(migrated, MealLogRow)
    assert (meal.date, meal.slot, meal.item_id, meal.event_id) == (
        date(2026, 10, 21),
        "dinner",
        household["siam"],
        None,
    )
    assert sorted(t["name"] for t in meal.cuisine_tags) == ["Lao", "Thai"]  # cuisines only, not "spicy"
    assert [p["name"] for p in meal.participants] == ["admin", "Sam"]
    assert meal.logged_by == household["admin"]
    [audit] = [a for a in rows(migrated, AuditRow) if a.action == "meal.logged"]
    assert audit.target == f"meal:{meal.id}"
    text = page(client.get(response.headers["location"]))
    assert "Meal logged." in text
    assert "Week of Oct 19" in text and "Wed Oct 21 · Dinner" in text
    assert "admin, Sam · logged by admin" in text
    assert "Lao · yesterday" in text and "Thai · yesterday" in text


def test_snapshots_survive_catalog_changes(client: TestClient, household: dict[str, int], migrated: Settings) -> None:
    log(client, household["siam"], [household["admin"]])
    thai = next(t.id for t in rows(migrated, TagRow) if t.name == "Thai")
    submit(client, f"/catalog/tags/{thai}/rename", {"name": "Thai food"})
    submit(
        client,
        f"/catalog/items/{household['siam']}",
        {"name": "Siam Garden", "new_cuisines": "Greek", "modes": ["pickup"]},
    )
    text = page(client.get("/history"))
    week = text.split("Week of", 1)[1]
    assert ">Thai<" in week and "Greek" not in week  # the meal shows what it was when logged
    assert "Thai food · today" in text  # the window names the cuisine by its current name


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"day": "2026-10-23"}, "not the future"),
        ({"day": "yesterday"}, "Enter the date as YYYY-MM-DD."),
        ({"slot": "brunch"}, "Pick lunch or dinner."),
        ({"item_id": ""}, "Pick something from the catalog."),
        ({"item_id": "999"}, "Pick something from the catalog."),
        ({"participant_ids": []}, "Pick at least one person who ate."),
        ({"participant_ids": ["999"]}, "isn't an active member"),
    ],
)
def test_log_validation(
    client: TestClient, household: dict[str, int], migrated: Settings, fields: dict[str, Any], message: str
) -> None:
    data: dict[str, Any] = {
        "day": TODAY.isoformat(),
        "slot": "lunch",
        "item_id": str(household["tacos"]),
        "participant_ids": [str(household["admin"])],
    }
    response = submit(client, "/history/meals", data | fields)
    assert response.status_code == 400
    text = page(response)
    assert message in text
    if "slot" not in fields:
        assert 'value="lunch" checked' in response.text  # the form keeps what was posted
    assert rows(migrated, MealLogRow) == []


def test_today_is_the_household_date(
    client: TestClient, household: dict[str, int], clock: FakeClock, migrated: Settings
) -> None:
    clock.advance(timedelta(hours=7))  # 01:00 UTC on Oct 23, still Oct 22 in Chicago
    assert log(client, household["tacos"], [household["admin"]], day=date(2026, 10, 23)).status_code == 400
    assert log(client, household["tacos"], [household["admin"]], day=TODAY).status_code == 303


def test_members_change_only_their_own_meals(
    client: TestClient, provider: FakeProvider, household: dict[str, int], migrated: Settings
) -> None:
    log(client, household["siam"], [household["admin"]])
    admin_meal = rows(migrated, MealLogRow)[0].id
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    log(client, household["tacos"], [household["sam"]])
    sam_meal = next(m.id for m in rows(migrated, MealLogRow) if m.id != admin_meal)
    listing = client.get("/history").text
    assert f'href="/history/meals/{sam_meal}"' in listing
    assert f'href="/history/meals/{admin_meal}"' not in listing
    assert client.get(f"/history/meals/{admin_meal}").status_code == 403
    edit = {
        "day": TODAY.isoformat(),
        "slot": "lunch",
        "item_id": str(household["tacos"]),
        "participant_ids": [str(household["sam"])],
    }
    assert submit(client, f"/history/meals/{admin_meal}", edit).status_code == 403
    assert submit(client, f"/history/meals/{admin_meal}/delete").status_code == 403
    assert len(rows(migrated, MealLogRow)) == 2

    assert "Edit meal" in page(client.get(f"/history/meals/{sam_meal}"))
    response = submit(client, f"/history/meals/{sam_meal}", edit | {"item_id": str(household["siam"])})
    assert response.status_code == 303
    assert "Meal updated." in page(client.get(response.headers["location"]))
    updated = next(m for m in rows(migrated, MealLogRow) if m.id == sam_meal)
    assert (updated.slot, updated.item_id) == ("lunch", household["siam"])
    assert sorted(t["name"] for t in updated.cuisine_tags) == ["Lao", "Thai"]  # a fresh snapshot
    assert submit(client, f"/history/meals/{sam_meal}/delete").status_code == 303
    assert [m.id for m in rows(migrated, MealLogRow)] == [admin_meal]


def test_admin_changes_any_meal(
    client: TestClient, provider: FakeProvider, household: dict[str, int], migrated: Settings
) -> None:
    sign_in_as(client, provider, "subject-sam", "sam@example.com")
    log(client, household["tacos"], [household["sam"]])
    [meal] = rows(migrated, MealLogRow)
    sign_in_as(client, provider, "subject-admin", "admin@example.com")
    assert client.get(f"/history/meals/{meal.id}").status_code == 200
    edit = {
        "day": "2026-10-20",
        "slot": "dinner",
        "item_id": str(household["tacos"]),
        "participant_ids": [str(household["sam"])],
    }
    assert submit(client, f"/history/meals/{meal.id}", edit).status_code == 303
    bad = submit(client, f"/history/meals/{meal.id}", edit | {"day": "2027-01-01"})
    assert bad.status_code == 400 and "not the future" in page(bad)
    response = submit(client, f"/history/meals/{meal.id}/delete")
    assert response.status_code == 303
    assert "Meal deleted." in page(client.get(response.headers["location"]))
    assert rows(migrated, MealLogRow) == []
    audits = [a for a in rows(migrated, AuditRow) if a.action.startswith("meal.")]
    assert [a.action for a in audits] == ["meal.logged", "meal.updated", "meal.deleted"]
    assert audits[-1].actor_member_id == household["admin"]
    assert audits[-1].payload["logged_by"] == household["sam"]


def test_unknown_meal_is_404(client: TestClient, household: dict[str, int]) -> None:
    assert client.get("/history/meals/999").status_code == 404
    assert submit(client, "/history/meals/999/delete").status_code == 404


def test_inactive_items_stay_editable_on_old_meals(
    client: TestClient, household: dict[str, int], migrated: Settings
) -> None:
    log(client, household["tacos"], [household["admin"]])
    submit(client, f"/catalog/items/{household['tacos']}/deactivate")
    [meal] = rows(migrated, MealLogRow)
    assert f'value="{household["tacos"]}" selected' in client.get(f"/history/meals/{meal.id}").text
    assert f'<option value="{household["tacos"]}"' not in client.get("/history").text


def test_variety_window_uses_the_household_setting(
    client: TestClient, household: dict[str, int], migrated: Settings
) -> None:
    log(client, household["siam"], [household["admin"]], day=TODAY - timedelta(days=4))
    log(client, household["tacos"], [household["admin"]], day=TODAY - timedelta(days=1))
    text = page(client.get("/history"))
    assert "Cuisines in the last 7 days" in text
    assert "Thai · 4 days ago" in text and "Mexican · yesterday" in text
    set_window(migrated, 3)
    text = page(client.get("/history"))
    assert "Cuisines in the last 3 days" in text
    assert "Mexican · yesterday" in text and "Thai ·" not in text


def test_history_pages_by_eight_weeks(client: TestClient, household: dict[str, int]) -> None:
    log(client, household["tacos"], [household["admin"]], day=date(2026, 8, 10))  # 10 weeks back
    first = page(client.get("/history"))
    assert "No meals logged in these weeks." not in first and "Week of Aug" not in first
    assert 'href="/history?page=1">Older' in first
    older = page(client.get("/history?page=1"))
    assert "Week of Aug 10" in older and 'href="/history?page=0">Newer' in older
    assert ">Older</a>" not in older
    assert "No meals logged in these weeks." in page(client.get("/history?page=5"))


@pytest.fixture
async def db(migrated: Settings, clock: FakeClock) -> AsyncIterator[AsyncSession]:
    database = open_database(migrated.async_database_url)
    async with database.sessionmaker() as session:
        await bootstrap(session, household_name="Home", timezone="UTC", admin_email=migrated.admin_email, now=clock())
        yield session
    await database.close()


@pytest.mark.anyio
async def test_variety_service_for_ranking(db: AsyncSession, migrated: Settings) -> None:
    """M2's ranking reuses this query; check it directly."""
    window = await variety.variety_window(db, 1, TODAY)
    assert (window.days, window.cuisines, window.items) == (7, (), ())
    assert await variety.last_eaten(db, 1) == {}
    set_window(migrated, 14)
    assert await variety.window_days(db, 1) == 14


def test_editing_keeps_deactivated_participants(
    client: TestClient, household: dict[str, int], migrated: Settings
) -> None:
    log(client, household["siam"], [household["admin"], household["sam"]])
    [meal] = rows(migrated, MealLogRow)
    assert submit(client, f"/admin/members/{household['sam']}/deactivate").status_code == 303
    form = client.get(f"/history/meals/{meal.id}").text
    assert f'name="participant_ids" value="{household["sam"]}" checked' in form  # still offered, still ticked
    assert "Sam" in form
    edit = {
        "day": "2026-10-21",
        "slot": "dinner",
        "item_id": str(household["siam"]),
        "participant_ids": [str(household["admin"]), str(household["sam"])],
    }
    assert submit(client, f"/history/meals/{meal.id}", edit).status_code == 303
    [meal] = rows(migrated, MealLogRow)
    assert meal.participants == [{"id": household["admin"], "name": "admin"}, {"id": household["sam"], "name": "Sam"}]
    # A new meal still can't name someone who is deactivated.
    new = log(client, household["siam"], [household["sam"]])
    assert new.status_code == 400 and "isn't an active member" in page(new)
    assert f'name="participant_ids" value="{household["sam"]}"' not in client.get("/history").text


def test_delete_asks_for_confirmation(client: TestClient, household: dict[str, int], migrated: Settings) -> None:
    log(client, household["siam"], [household["admin"]])
    [meal] = rows(migrated, MealLogRow)
    for path in ("/history", f"/history/meals/{meal.id}"):
        html = client.get(path).text
        form = html.split(f'action="/history/meals/{meal.id}/delete"', 1)[1].split(">", 1)[0]
        assert 'hx-boost="true"' in form and 'hx-confirm="Delete this meal' in form, path


def test_old_meals_stay_editable(client: TestClient, household: dict[str, int], migrated: Settings) -> None:
    log(client, household["siam"], [household["admin"]])
    engine = create_engine(migrated.sync_database_url)
    try:
        with engine.begin() as connection:  # logged before the two-year window, say by an import
            connection.execute(text("UPDATE meal_log SET date = '2023-05-01'"))
    finally:
        engine.dispose()
    [meal] = rows(migrated, MealLogRow)
    edit = {
        "day": "2023-05-01",
        "slot": "lunch",
        "item_id": str(household["tacos"]),
        "participant_ids": [str(household["admin"])],
    }
    assert submit(client, f"/history/meals/{meal.id}", edit).status_code == 303
    [meal] = rows(migrated, MealLogRow)
    assert (meal.date, meal.slot, meal.item_id) == (date(2023, 5, 1), "lunch", household["tacos"])
    moved = submit(client, f"/history/meals/{meal.id}", edit | {"day": "2023-05-02"})
    assert moved.status_code == 400 and "last two years" in page(moved)
