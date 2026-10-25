# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Schedule page (FR-44): any member changes a slot's standing schedule from the web,
through the browser's door: CSRF tokens, Origin checks, and refusals that change nothing."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, HouseholdRow, OutboxRow
from dinnerbell.domain.meals import Slot
from dinnerbell.domain.schedule import DEFAULT_SLOTS, slot_fingerprint
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.web import submit
from tests.test_auth_flow import member, rows
from tests.test_events_web import sign_in_as

DEFAULTS = {
    Slot.DINNER: {
        "call_time": "15:00",
        "meal_time": "18:30",
        "response": "90",
        "vote": "45",
        "modes": ["pickup", "delivery"],
    },
    Slot.LUNCH: {"call_time": "10:30", "meal_time": "12:30", "response": "60", "vote": "30", "modes": ["pickup"]},
}


def slot_form(slot: Slot, **changes: Any) -> dict[str, Any]:
    """The Schedule page's form for ``slot`` as it's first shown, drawn from the defaults."""
    p, d = slot.value, DEFAULTS[slot]
    form: dict[str, Any] = {
        f"{p}_seen": slot_fingerprint(DEFAULT_SLOTS[slot]),
        f"{p}_call_time": d["call_time"],
        f"{p}_meal_time": d["meal_time"],
        f"{p}_response_minutes": d["response"],
        f"{p}_vote_minutes": d["vote"],
        f"{p}_reminder_minutes": "15",
        f"{p}_modes": d["modes"],
    }
    if slot is Slot.DINNER:
        form["dinner_enabled"] = "on"
    return form | changes


def seen_on_page(client: TestClient, slot: Slot) -> str:
    """The fingerprint the Schedule page draws ``slot``'s form with now."""
    found = re.search(rf'name="{slot.value}_seen" value="(\w+)"', client.get("/schedule").text)
    assert found is not None
    return found.group(1)


@pytest.fixture
def kim(client: TestClient, provider: FakeProvider) -> TestClient:
    """Signed in as Kim, a member who isn't an admin."""
    sign_in(client, provider)
    assert invite(client, "kim@example.com", "Kim").status_code == 303
    sign_in_as(client, provider, "kim@example.com")
    return client


def schedule_rows(settings: Settings) -> tuple[list[AuditRow], list[OutboxRow]]:
    audits = [a for a in rows(settings, AuditRow) if a.action == "household.slot_schedule_updated"]
    return audits, [o for o in rows(settings, OutboxRow) if o.kind == "schedule_changed"]


def test_a_member_sees_the_schedule_in_the_nav_and_saves_a_change(kim: TestClient, migrated: Settings) -> None:
    today = kim.get("/").text
    assert 'href="/schedule"' in today and 'href="/admin/schedule"' not in today
    html = kim.get("/schedule").text
    assert 'action="/schedule/dinner"' in html and 'action="/schedule/lunch"' in html
    assert 'name="dinner_call_time" value="15:00"' in html and "Admin &gt; Ranking" not in html
    assert 'name="dinner_response_minutes" min="5" max="360"' in html  # the ranges the server accepts
    assert 'name="dinner_reminder_minutes" min="0" max="180"' in html
    seen = slot_fingerprint(DEFAULT_SLOTS[Slot.DINNER])
    assert f'name="dinner_seen" value="{seen}"' in html

    saved = submit(kim, "/schedule/dinner", slot_form(Slot.DINNER, dinner_call_time="14:00"))
    assert (saved.status_code, saved.headers["location"]) == (303, "/schedule?saved=dinner#dinner")
    assert "Dinner schedule saved and posted in the channel" in kim.get("/schedule?saved=dinner").text
    assert rows(migrated, HouseholdRow)[0].settings["slots"]["dinner"]["call_time"] == "14:00"
    (audit,), (post,) = schedule_rows(migrated)
    kim_id = member(migrated, "kim@example.com").id
    assert audit.actor_member_id == kim_id
    assert audit.payload["changes"] == {"call_time": {"from": "15:00", "to": "14:00"}}
    assert post.event_id is None and post.payload["changed_by"] == {"id": kim_id, "name": "Kim"}


def test_saving_without_a_change_posts_nothing(kim: TestClient, migrated: Settings) -> None:
    response = submit(kim, "/schedule/lunch", slot_form(Slot.LUNCH))
    assert response.status_code == 200 and "Nothing changed, so nothing was posted." in page(response)
    assert schedule_rows(migrated) == ([], [])


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dinner_call_time": "3pm"}, "HH:MM"),
        ({"dinner_vote_minutes": "1"}, "vote window"),
        ({"dinner_reminder_minutes": ""}, "Enter the reminder time"),
        ({"dinner_response_minutes": "ninety"}, "whole number"),
        ({"dinner_modes": []}, "default way"),
        ({"dinner_modes": ["teleport"]}, "cook, pickup, or delivery"),
        ({"dinner_meal_time": "17:00"}, "Voting would close at 5:15 PM, but the usual meal time is 5:00 PM."),
        ({"dinner_call_time": "22:30"}, "past midnight"),
    ],
)
def test_bad_values_are_refused_and_kept(
    kim: TestClient, migrated: Settings, changes: dict[str, Any], message: str
) -> None:
    response = submit(kim, "/schedule/dinner", slot_form(Slot.DINNER, **changes))
    assert response.status_code == 400 and message in page(response)
    for name, value in changes.items():  # what the member typed is still there
        if isinstance(value, str) and value:
            assert f'value="{value}"' in response.text, name
    assert schedule_rows(migrated) == ([], [])


def test_a_save_over_someone_elses_change_is_refused(kim: TestClient, migrated: Settings) -> None:
    stale = slot_fingerprint(DEFAULT_SLOTS[Slot.DINNER])
    first = slot_form(Slot.DINNER, dinner_call_time="14:00", dinner_seen=stale)
    assert submit(kim, "/schedule/dinner", first).status_code == 303
    second = slot_form(Slot.DINNER, dinner_vote_minutes="30", dinner_seen=stale)
    refused = submit(kim, "/schedule/dinner", second)
    assert refused.status_code == 409 and "Someone changed the dinner schedule" in page(refused)
    assert 'name="dinner_call_time" value="14:00"' in refused.text  # what's stored now, not what was typed
    assert len(schedule_rows(migrated)[0]) == 1
    again = submit(kim, "/schedule/dinner", first)  # the first save again, from the same old form
    assert again.status_code == 200 and "Nothing changed" in page(again)


def test_a_form_without_its_fingerprint_is_refused(kim: TestClient, migrated: Settings) -> None:
    form = slot_form(Slot.DINNER, dinner_call_time="14:00")
    del form["dinner_seen"]
    refused = submit(kim, "/schedule/dinner", form)
    assert refused.status_code == 400 and "This form is out of date." in page(refused)
    assert 'name="dinner_call_time" value="15:00"' in refused.text
    assert schedule_rows(migrated) == ([], [])


def test_an_unknown_meal_is_not_found(kim: TestClient, migrated: Settings) -> None:
    assert submit(kim, "/schedule/brunch", slot_form(Slot.DINNER)).status_code == 404
    assert schedule_rows(migrated) == ([], [])


def test_saves_need_a_csrf_token_and_our_origin(kim: TestClient, migrated: Settings) -> None:
    form = slot_form(Slot.DINNER, dinner_call_time="14:00")
    assert kim.post("/schedule/dinner", data=form).status_code == 403
    assert kim.post("/schedule/dinner", data=form | {"csrf_token": "0" * 64}).status_code == 403
    foreign = kim.post(
        "/schedule/dinner", data=form | {"csrf_token": csrf_from(kim)}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert schedule_rows(migrated) == ([], [])


def test_signed_out_people_see_nothing(client: TestClient, migrated: Settings) -> None:
    for response in (client.get("/schedule"), client.post("/schedule/dinner", data=slot_form(Slot.DINNER))):
        assert (response.status_code, response.headers["location"]) == (303, "/")  # the sign-in page
    assert schedule_rows(migrated) == ([], [])


def test_admins_reach_the_schedule_and_their_own_settings(client: TestClient, provider: FakeProvider) -> None:
    sign_in(client, provider)
    html = client.get("/schedule").text
    assert 'action="/schedule/dinner"' in html and 'href="/admin/schedule"' in html
    assert re.search(r'<a class="button" href="/schedule">Schedule</a>', client.get("/").text)
    tabs = client.get("/admin/schedule").text
    assert ">Ranking</a>" in tabs and 'action="/admin/schedule/skip"' in tabs
