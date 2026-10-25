# SPDX-License-Identifier: AGPL-3.0-or-later
"""Posting a later day's call now on the web (issue #59): the card says when a scheduled call
posts, Today lists it under Coming up, and **Post it now** and the start form's option post it
at once with that day's deadline."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import AuditRow, EventRow
from dinnerbell.domain.events import EventRuleViolation
from dinnerbell.services import calls
from tests.conftest import invite, page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, htmx, submit
from tests.test_auth_flow import rows
from tests.test_events_web import sign_in_as

STALE_SHOWN = "Reload the page to see what changed."


@pytest.fixture
def house(client: TestClient, provider: FakeProvider) -> TestClient:
    """An admin, and Sam, a member."""
    sign_in(client, provider)
    add_item(client, "Siam Garden", cuisines="Thai")
    assert invite(client, "sam@example.com", "Sam").status_code == 303
    return client


def start(client: TestClient, **fields: str) -> int:
    response = submit(client, "/events", {"slot": "lunch", "day": "2026-10-23"} | fields)
    assert response.status_code == 303, page(response)
    return int(response.headers["location"].rsplit("/", 1)[1])


def event_row(settings: Settings, event_id: int) -> EventRow:
    return next(e for e in rows(settings, EventRow) if e.id == event_id)


def test_a_scheduled_call_says_when_it_posts_and_posts_now(
    house: TestClient, provider: FakeProvider, migrated: Settings
) -> None:
    event_id = start(house)
    card = house.get(f"/events/{event_id}").text
    assert "Posts Fri Oct 23 at <strong>10:30 AM</strong>." in card
    assert f'action="/events/{event_id}/post-now"' in card

    sign_in_as(house, provider, "sam@example.com")  # any member may post it
    today = house.get("/").text
    assert "<h2>Coming up</h2>" in today and f'action="/events/{event_id}/post-now"' in today
    deadline = event_row(migrated, event_id).respond_deadline
    response = htmx(house, f"/events/{event_id}/post-now", {"run": "1", "from": "today"})
    assert response.status_code == 200
    assert "Answer by <strong>11:30 AM</strong> Fri Oct 23." in response.text
    assert "post-now" not in response.text
    row = event_row(migrated, event_id)
    assert (row.state, row.respond_deadline) == ("collecting", deadline)
    assert [a.action for a in rows(migrated, AuditRow) if a.action.startswith("event.")] == [
        "event.scheduled",
        "event.opened",
    ]


def test_the_start_form_can_post_a_later_days_call_now(house: TestClient, migrated: Settings) -> None:
    assert 'name="post_now"' in house.get("/").text
    event_id = start(house, post_now="1")
    assert event_row(migrated, event_id).state == "collecting"
    assert "Answer by <strong>11:30 AM</strong> Fri Oct 23." in house.get(f"/events/{event_id}").text


def test_post_now_needs_the_run_and_refuses_an_old_one(house: TestClient, migrated: Settings) -> None:
    event_id = start(house)
    assert submit(house, f"/events/{event_id}/post-now", {}).status_code == 400
    response = submit(house, f"/events/{event_id}/post-now", {"run": "2"})
    assert response.status_code == 409 and STALE_SHOWN in response.text
    assert event_row(migrated, event_id).state == "scheduled"
    assert submit(house, f"/events/{event_id}/post-now", {"run": "1"}).status_code == 303
    assert submit(house, f"/events/{event_id}/post-now", {"run": "1"}).status_code == 303  # nothing to do
    assert event_row(migrated, event_id).state == "collecting"


def test_the_start_form_posts_a_call_that_is_scheduled_already(house: TestClient, migrated: Settings) -> None:
    """As `/dinnerbell call ... now` does; without Post it now, or once it's posted, it's there already."""
    event_id = start(house)
    again = submit(house, "/events", {"slot": "lunch", "day": "2026-10-23"})
    assert again.status_code == 409 and "already a lunch call" in again.text
    assert start(house, post_now="1") == event_id
    assert event_row(migrated, event_id).state == "collecting"
    posted = submit(house, "/events", {"slot": "lunch", "day": "2026-10-23", "post_now": "1"})
    assert posted.status_code == 409 and "already a lunch call" in posted.text


def test_a_refused_post_now_after_starting_shows_on_the_new_calls_card(
    house: TestClient, migrated: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*args: object, **kwargs: object) -> None:
        raise EventRuleViolation("Not now.")

    monkeypatch.setattr(calls, "post_now", refuse)
    response = submit(house, "/events", {"slot": "lunch", "day": "2026-10-23", "post_now": "1"})
    assert response.status_code == 409 and "Not now." in response.text
    assert 'action="/events/1/post-now"' in response.text  # the call was started; it's scheduled
    assert event_row(migrated, 1).state == "scheduled"
