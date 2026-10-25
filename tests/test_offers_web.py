# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offers on the web (spec 11, FR-4): the Make an offer form on Today, joining with guests,
dropping out, calling an offer off, and refusals that show on the page."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import EventRow, ResponseRow
from tests.conftest import invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, htmx, submit
from tests.test_auth_flow import rows
from tests.test_events_web import sign_in_as


@pytest.fixture
def admin(client: TestClient, provider: FakeProvider) -> TestClient:
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai")
    return client


def offer_form(**changes: Any) -> dict[str, Any]:
    return {
        "slot": "dinner",
        "day": "2026-10-22",
        "mode": "pickup",
        "at": "18:00",
        "capacity": "2",
        "item_id": "1",
    } | changes


def make(client: TestClient, **changes: Any) -> int:
    response = submit(client, "/offers", offer_form(**changes))
    assert response.status_code == 303, page(response)
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_today_has_the_offer_form_with_the_slots_defaults(admin: TestClient) -> None:
    html = page(admin.get("/"))
    assert "Make an offer" in html and 'action="/offers"' in html
    # It's 1 PM in Chicago, past lunch's usual time, so the form starts on dinner at 6:30 PM.
    assert re.search(r'<option value="dinner" selected>', html)
    assert 'name="at" value="18:30"' in html


def test_post_join_and_leave_an_offer(admin: TestClient, provider: FakeProvider, migrated: Settings) -> None:
    offer_id = make(admin)
    html = page(admin.get(f"/events/{offer_id}"))
    assert "Dinner offer" in html and "is picking up" in html and "2 servings spare" in html
    invite(admin, "kim@example.com", "Kim")
    sign_in_as(admin, provider, "kim@example.com")
    assert "Count me in" in page(admin.get(f"/events/{offer_id}"))
    card = htmx(admin, f"/events/{offer_id}/join", {"guests": "1", "run": "1", "from": "event"})
    assert card.status_code == 200 and "It's full." in page(card)
    kim = next(r for r in rows(migrated, ResponseRow) if r.member_id != 1)
    assert (kim.status, kim.guests) == ("in", 1)
    too_many = submit(admin, f"/events/{offer_id}/join", {"guests": "3", "run": "1"})
    assert too_many.status_code == 409 and "full" in page(too_many)
    refused = submit(admin, f"/events/{offer_id}/cancel", {"run": "1"})
    assert refused.status_code == 409 and "whoever made the offer" in page(refused)
    left = submit(admin, f"/events/{offer_id}/leave", {"run": "1"})
    assert left.status_code == 303
    kim = next(r for r in rows(migrated, ResponseRow) if r.member_id != 1)
    assert (kim.status, kim.guests) == ("out", 0)
    # A card left open across a restart, or a post missing its run, changes nothing.
    assert submit(admin, f"/events/{offer_id}/join", {"guests": "0"}).status_code == 400
    assert submit(admin, f"/events/{offer_id}/join", {"guests": "0", "run": "2"}).status_code == 409


def test_an_offer_refuses_call_actions_on_the_web(admin: TestClient, migrated: Settings) -> None:
    offer_id = make(admin)
    for path, data in [
        ("respond", {"status": "in"}),
        ("offer", {"role": "cook", "offered": "1"}),
        ("details", {"guests": "4"}),
        ("coordinate", {}),
        ("close", {"run": "1", "window": "respond"}),
        ("admin/rerun", {}),
        ("admin/deadline", {"window": "respond", "shift": "plus15"}),
    ]:
        response = submit(admin, f"/events/{offer_id}/{path}", data)
        assert response.status_code == 409, path
    (event,) = rows(migrated, EventRow)
    assert event.state == "collecting"
    assert len(rows(migrated, ResponseRow)) == 1  # only the offerer's seat


def test_a_bad_offer_shows_on_today(admin: TestClient, migrated: Settings) -> None:
    for changes, message in [
        ({"capacity": "0"}, "Spare servings"),
        ({"capacity": "x"}, "whole number"),
        ({"at": "11:00"}, "still ahead"),
        ({"at": "noon"}, "HH:MM"),
        ({"slot": "brunch"}, "Pick the meal"),
        ({"item_id": "", "free_text": ""}, "not both"),
        ({"item_id": "", "free_text": "Pho Real"}, "cuisine"),
    ]:
        response = submit(admin, "/offers", offer_form(**changes))
        assert response.status_code == 409 and message in page(response), changes
    assert rows(migrated, EventRow) == []


def test_an_offer_for_an_open_call_goes_to_the_call(admin: TestClient, migrated: Settings) -> None:
    call = submit(admin, "/events", {"slot": "dinner", "day": "2026-10-22"})
    call_id = int(call.headers["location"].rsplit("/", 1)[1])
    response = submit(admin, "/offers", offer_form())
    assert response.headers["location"] == f"/events/{call_id}"
    assert "Siam Garden" in page(admin.get(f"/events/{call_id}"))
    assert [e.kind for e in rows(migrated, EventRow)] == ["call"]
