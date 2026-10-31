# SPDX-License-Identifier: AGPL-3.0-or-later
"""Feedback on the web (spec 5.9, 11, FR-45 to FR-49): History and the event page take
feedback, the catalog shows how many loved an item, the cook sees thank-yous and notes, and
nobody ever sees who gave which rating or what went wrong."""

from __future__ import annotations

import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import FeedbackRow, MealLogRow, ProposalRow
from tests.conftest import csrf_from, invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import add_item, htmx, submit
from tests.test_auth_flow import member, rows
from tests.test_catalog_web import item_row
from tests.test_events_web import sign_in_as, start, tick
from tests.test_history import log

SAM, KIM = "sam@example.com", "kim@example.com"


@pytest.fixture
def household(client: TestClient, provider: FakeProvider, migrated: Settings) -> dict[str, int]:
    """The admin, Sam, and Kim; Siam Garden, a restaurant; and Tacos, a home meal Sam cooks."""
    sign_in(client, provider)
    invite(client, SAM, "Sam", can_cook=True)
    invite(client, KIM, "Kim")
    sam = member(migrated, SAM).id
    add_item(client, "Siam Garden", cuisines="Thai")
    add_item(client, "Tacos", kind="home", cuisines="Mexican", cook_ids=[str(sam)])
    return {
        "admin": member(migrated, "admin@example.com").id,
        "sam": sam,
        "kim": member(migrated, KIM).id,
        "siam": item_row(migrated, "Siam Garden").id,
        "tacos": item_row(migrated, "Tacos").id,
    }


def meal_id(settings: Settings, item_id: int) -> int:
    return next(m.id for m in rows(settings, MealLogRow) if m.item_id == item_id)


def give(client: TestClient, meal: int, **answers: object) -> httpx.Response:
    return submit(client, f"/history/meals/{meal}/feedback", dict(answers))


def test_feedback_from_history_and_an_edit_replaces_it(
    client: TestClient, household: dict[str, int], migrated: Settings
) -> None:
    log(client, household["siam"], [household["admin"], household["kim"]])
    siam = meal_id(migrated, household["siam"])
    history = page(client.get("/history"))
    assert f'href="/history/meals/{siam}/feedback"' in history and "How was it?" in history

    form = page(client.get(f"/history/meals/{siam}/feedback"))
    assert 'name="rating"' in form and 'name="place_issues"' in form and 'name="issue_note"' in form
    assert 'name="cook_thanks"' not in form
    # app.css hides the box until Other is checked, unless it holds text (:placeholder-shown).
    assert '<fieldset class="field issues">' in form and 'class="field compact issue-note"' in form
    assert re.search(r'<input type="text" name="issue_note" maxlength="200" placeholder="[^"]+"', form)
    # The five ratings, lowest first, then "No answer", which starts checked.
    values = re.findall(r'name="rating" value="([a-z]*)"', form)
    assert values == ["hated", "disliked", "fine", "liked", "loved", ""]
    assert 'name="rating" value="" checked' in form
    saved = give(client, siam, rating="liked", place_issues=["cold", "late", "bogus"], issue_note=" No forks ")
    assert saved.status_code == 303 and saved.headers["location"] == f"/history?notice=feedback#meal-{siam}"
    (row,) = rows(migrated, FeedbackRow)
    # Text in "What went wrong?" checks Other, as a note to the cook checks the thank-you.
    assert (row.rating, row.place_issues, row.issue_note, row.mode) == (
        "liked",
        ["cold", "late", "other"],
        "No forks",
        None,
    )
    history = page(client.get("/history?notice=feedback"))
    assert "Thanks for the feedback!" in history
    assert "Your feedback: Liked it. Late, Cold, Other: “No forks”" in history
    assert "Change your feedback" in history
    form = page(client.get(f"/history/meals/{siam}/feedback"))
    assert 'value="liked" checked' in form and 'value="cold" checked' in form and 'value="other" checked' in form
    assert 'value="No forks"' in form

    assert give(client, siam, rating="fine", place_issues=["other"]).status_code == 303
    (row,) = rows(migrated, FeedbackRow)
    assert (row.rating, row.place_issues, row.issue_note) == ("fine", ["other"], None)
    assert "Your feedback: It was fine. Other." in page(client.get("/history"))
    taken_back = give(client, siam, rating="")
    assert taken_back.headers["location"] == f"/history?notice=no_feedback#meal-{siam}"
    assert rows(migrated, FeedbackRow) == []


def test_the_form_follows_the_meal_and_the_rules(
    client: TestClient, provider: FakeProvider, household: dict[str, int], migrated: Settings, clock: FakeClock
) -> None:
    log(client, household["tacos"], [household["admin"], household["kim"]])
    tacos = meal_id(migrated, household["tacos"])
    form = page(client.get(f"/history/meals/{tacos}/feedback"))
    # A logged meal has no cook on record, so there's nobody to thank; no place for a home meal.
    assert 'name="rating"' in form and 'name="place_issues"' not in form and 'name="issue_note"' not in form
    assert 'name="cook_thanks"' not in form
    refused = give(client, tacos, rating="loved", issue_note="Cold")
    assert refused.status_code == 400 and "What went wrong is only for restaurants." in page(refused)
    assert 'value="loved" checked' in page(refused)
    too_long = give(client, tacos, issue_note="x" * 201)
    assert too_long.status_code == 400 and "200 characters" in page(too_long)
    assert rows(migrated, FeedbackRow) == []

    sign_in_as(client, provider, SAM)  # Sam didn't eat it
    assert "How was it?" not in page(client.get("/history"))
    denied = client.get(f"/history/meals/{tacos}/feedback")
    assert denied.status_code == 403 and "Only people who ate this meal" in page(denied)
    assert give(client, tacos, rating="hated").status_code == 403
    assert client.get("/history/meals/999/feedback").status_code == 404

    sign_in_as(client, provider, KIM)
    clock.advance(timedelta(days=8))
    closed = client.get(f"/history/meals/{tacos}/feedback")
    assert closed.status_code == 403 and "has closed" in page(closed)
    assert "How was it?" not in page(client.get("/history"))
    assert rows(migrated, FeedbackRow) == []


NEGATIVE_COUNT = re.compile(
    r"\b\d+\s*(hated|didn|disliked|fine|liked|late|cold|wrong order|other)\b|\u00d7", re.IGNORECASE
)
"""A count of anything but "Loved it", which could name the one person who gave it (FR-49)."""


def item_entry(html: str, item_id: int) -> str:
    match = re.search(rf'<li class="item[^"]*" id="item-{item_id}">(.*?)</li>', html, re.S)
    assert match, f"item {item_id} not listed"
    return match.group(1)


def test_only_loved_it_is_ever_counted_even_with_one_rater(
    client: TestClient, provider: FakeProvider, household: dict[str, int], migrated: Settings
) -> None:
    log(client, household["siam"], [household["admin"], household["kim"]])
    siam = meal_id(migrated, household["siam"])
    sign_in_as(client, provider, KIM)
    issues = ["late", "cold", "wrong_order", "other"]
    give(client, siam, rating="hated", place_issues=issues, issue_note="Soggy Qz8")
    assert "Your feedback: Hated it. Late, Wrong order, Cold, Other: “Soggy Qz8”" in page(client.get("/history"))

    # The admin, looking at an item only Kim rated, sees nothing that could be hers.
    client.cookies.clear()
    sign_in(client, provider)
    pages = {
        "list": item_entry(page(client.get("/catalog")), household["siam"]),
        "item": page(client.get(f"/catalog/items/{household['siam']}")),
        "history": page(client.get("/history")),
    }
    for name, html in pages.items():
        assert "item-feedback" not in html and "hated" not in html.lower() and "Qz8" not in html, name
        assert NEGATIVE_COUNT.search(html) is None, name

    sign_in_as(client, provider, KIM)
    give(client, siam, rating="loved", place_issues=["cold"])
    client.cookies.clear()
    sign_in(client, provider)
    give(client, siam, rating="liked")  # the admin ate it too; "Liked it" isn't counted
    entry = item_entry(page(client.get("/catalog")), household["siam"])
    assert "1 loved it" in entry and NEGATIVE_COUNT.search(entry) is None
    item = page(client.get(f"/catalog/items/{household['siam']}"))
    assert "1 loved it" in item and NEGATIVE_COUNT.search(item) is None


def test_feedback_posts_check_csrf_and_origin(
    client: TestClient, household: dict[str, int], migrated: Settings
) -> None:
    log(client, household["siam"], [household["admin"]])
    siam = meal_id(migrated, household["siam"])
    path = f"/history/meals/{siam}/feedback"
    assert client.post(path, data={"rating": "loved"}).status_code == 403
    assert client.post(path, data={"rating": "loved", "csrf_token": "0" * 64}).status_code == 403
    token = csrf_from(client)
    data = {"rating": "loved", "csrf_token": token}
    foreign = client.post(path, data=data, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403
    assert rows(migrated, FeedbackRow) == []


def done_tacos(client: TestClient, provider: FakeProvider, clock: FakeClock, settings: Settings) -> int:
    """Today's dinner, decided on Tacos with Sam cooking, eaten by the admin, Sam, and Kim, and
    then done. Leaves the admin signed in."""
    event_id = start(client)
    submit(client, f"/events/{event_id}/respond", {"status": "in"})
    for email in (SAM, KIM):
        sign_in_as(client, provider, email)
        submit(client, f"/events/{event_id}/respond", {"status": "in"})
    sign_in_as(client, provider, SAM)
    submit(client, f"/events/{event_id}/offer", {"role": "cook", "offered": "1"})
    clock.advance(timedelta(minutes=90))
    tick(client, clock)
    client.cookies.clear()
    sign_in(client, provider)
    tacos = item_row(settings, "Tacos").id
    htmx(client, f"/events/{event_id}/proposals", {"mode": "cook", "item_id": str(tacos)})  # unless ranked already
    (proposal,) = [p.id for p in rows(settings, ProposalRow) if p.item_id == tacos and p.mode == "cook"]
    assert htmx(client, f"/events/{event_id}/proposals/{proposal}/lock", {}).status_code == 200
    clock.advance(timedelta(hours=8))
    tick(client, clock)
    assert "Done" in client.get(f"/events/{event_id}").text
    return event_id


def test_the_event_page_takes_feedback_and_only_the_cook_sees_notes(
    client: TestClient, provider: FakeProvider, household: dict[str, int], migrated: Settings, clock: FakeClock
) -> None:
    event_id = done_tacos(client, provider, clock, migrated)
    tacos = meal_id(migrated, household["tacos"])

    sign_in_as(client, provider, KIM)
    event = page(client.get(f"/events/{event_id}"))
    assert f'href="/history/meals/{tacos}/feedback?from=event"' in event
    form = page(client.get(f"/history/meals/{tacos}/feedback?from=event"))
    assert "Thanks, Sam!" in form and "only Sam sees it" in form and 'href="/events/' in form
    saved = give(client, tacos, rating="hated", cook_thanks="1", cook_note="Thanks for trying!", **{"from": "event"})
    assert saved.headers["location"] == f"/events/{event_id}#feedback"
    event = page(client.get(f"/events/{event_id}"))
    assert "Your feedback: Hated it. You said thanks: “Thanks for trying!”" in event

    # Sam, the cook, sees who thanked him and the note, but never anyone's rating.
    sign_in_as(client, provider, SAM)
    for path in (f"/events/{event_id}", "/history"):
        seen = page(client.get(path))
        assert "Thanks to you from Kim." in seen and "<q>Thanks for trying!</q>" in seen, path
        assert "hated" not in seen.lower(), path
    assert "How was it?" in page(client.get("/history"))  # Sam ate too, and can rate the meal

    # The admin sees only the thank-you count, and nothing about Kim's answers.
    client.cookies.clear()
    sign_in(client, provider)
    for path in (f"/events/{event_id}", "/history"):
        seen = page(client.get(path))
        assert "1 thank-you for Sam." in seen, path
        assert "Thanks for trying" not in seen and "hated" not in seen.lower(), path
    # Kim hated it, which is never counted for display; nobody loved it, so no line.
    for path in ("/catalog", f"/catalog/items/{household['tacos']}"):
        assert "item-feedback" not in page(client.get(path)), path
    # The catalog file never carries feedback.
    exported = client.get("/catalog/export.yaml").text
    assert "hated" not in exported and "Thanks for trying" not in exported and "feedback" not in exported


def test_the_preferences_page_turns_the_reminder_on(
    client: TestClient, household: dict[str, int], migrated: Settings
) -> None:
    prefs = page(client.get("/preferences"))
    assert "send me one direct message asking how it was. Muting direct messages also stops this." in prefs
    submit(client, "/preferences/settings", {"feedback_reminder": "on"})
    assert member(migrated, "admin@example.com").feedback_reminder
