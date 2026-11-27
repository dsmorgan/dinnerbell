# SPDX-License-Identifier: AGPL-3.0-or-later
"""A card's action that works says what changed, and the 45-second refresh leaves a card alone
while someone is using it (#336, spec 11 "Page structure"). The server marks the card it swaps
in with ``data-announce``; app.js puts the words in the page's status line. The browser side
was checked in jsdom; these check what the server renders and what app.js asks for."""

from __future__ import annotations

import re
from datetime import date, timedelta
from html import unescape

import httpx
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.web.routes import events
from tests.conftest import invite, page
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import htmx, submit
from tests.test_auth_flow import member
from tests.test_events_web import admin, proposal_ids, sign_in_as, start, tick
from tests.test_offers_web import make

__all__ = ["admin"]  # a fixture, imported for pytest


def said(response: httpx.Response) -> str | None:
    """The words the swapped-in card carries for a screen reader, or None."""
    found = re.findall(r'<section class="card event[^>]*?data-announce="([^"]*)"', response.text)
    assert len(found) <= 1, found
    assert response.text.count("data-announce=") == len(found)  # only ever on the card
    return unescape(found[0]) if found else None


def act(client: TestClient, event_id: int, path: str, data: dict[str, str] | None = None) -> str | None:
    response = htmx(client, f"/events/{event_id}/{path}", {"from": "today"} | (data or {}))
    assert response.status_code == 200, page(response)
    return said(response)


def guest_id(html: str, event_id: int, name: str) -> int:
    match = re.search(rf"<strong>{name}</strong>.*?/events/{event_id}/guests/(\d+)/answer", html, re.S)
    assert match is not None, name
    return int(match.group(1))


def test_answers_offers_details_and_away_say_what_changed(admin: TestClient) -> None:
    event_id = start(admin)
    assert act(admin, event_id, "respond", {"status": "in"}) == "You're in."
    assert act(admin, event_id, "respond", {"status": "maybe"}) == "You're a maybe."
    assert act(admin, event_id, "respond", {"status": "out"}) == "You're out."
    assert act(admin, event_id, "offer", {"role": "cook", "offered": "1"}) == "You can cook."
    assert act(admin, event_id, "offer", {"role": "pickup", "offered": "1"}) == "You can pick up."
    assert act(admin, event_id, "offer", {"role": "cook", "offered": "0"}) == "You no longer offer to cook."
    assert act(admin, event_id, "details", {"note": "Running late"}) == "Details saved."
    assert act(admin, event_id, "coordinate") == "You're coordinating."
    back = re.search(r'name="back" min="([\d-]+)"', admin.get(f"/events/{event_id}").text)
    assert back is not None
    day = date.fromisoformat(back.group(1))
    assert act(admin, event_id, "away", {"back": day.isoformat()}) == f"You're away until {day:%a}, {day:%b} {day.day}."
    assert act(admin, event_id, "back") == "Away days cleared."


def test_guests_deadlines_and_coordinators_say_what_changed(admin: TestClient, migrated: Settings) -> None:
    event_id = start(admin)
    assert act(admin, event_id, "guests", {"name": "Jo", "run": "1"}) == "Guest invited."
    assert act(admin, event_id, "guests", {"name": "Al", "run": "1"}) == "Guest invited."
    html = admin.get(f"/events/{event_id}").text
    jo, al = guest_id(html, event_id, "Jo"), guest_id(html, event_id, "Al")
    assert act(admin, event_id, f"guests/{jo}/answer", {"status": "in", "run": "1"}) == "The guest is in."
    assert act(admin, event_id, f"guests/{jo}/answer", {"status": "out", "run": "1"}) == "The guest is out."
    answer = {"role": "cook", "offered": "1", "run": "1"}
    assert act(admin, event_id, f"guests/{jo}/answer", answer) == "The guest can cook."
    answer["offered"] = "0"
    assert act(admin, event_id, f"guests/{jo}/answer", answer) == "The guest no longer offers to cook."
    assert act(admin, event_id, f"guests/{al}/remove") == "Guest taken off the call."
    moved = act(admin, event_id, "admin/deadline", {"window": "respond", "shift": "plus15"})
    assert moved == "Answers close 15 minutes later."
    someone = str(member(migrated, "admin@example.com").id)
    assert act(admin, event_id, "admin/coordinator", {"member_id": someone}) == "Coordinator assigned."
    assert act(admin, event_id, "admin/coordinator", {"member_id": ""}) == "Coordinator cleared."
    assert act(admin, event_id, "coordinate") == "You're coordinating."
    invite(admin, "sam@example.com", "Sam")
    sam = str(member(migrated, "sam@example.com").id)
    assert act(admin, event_id, "hand-off", {"member_id": sam}) == "Coordinating handed off."


def test_votes_the_decision_and_after_say_what_changed(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:
    event_id = start(admin)
    submit(admin, f"/events/{event_id}/respond", {"status": "in"})
    submit(admin, f"/events/{event_id}/offer", {"role": "pickup", "offered": "1"})
    submit(admin, f"/events/{event_id}/guests", {"name": "Jo", "run": "1"})
    submit(admin, f"/events/{event_id}/coordinate")
    clock.advance(timedelta(minutes=90))
    tick(admin, clock)
    first, second = proposal_ids(migrated)
    assert act(admin, event_id, f"proposals/{first}/vote") == "Voted."
    assert act(admin, event_id, f"proposals/{first}/unvote") == "Vote removed."
    later = act(admin, event_id, "decision-time", {"shift": "plus30", "run": "1"})
    assert later == "Voting closes 30 minutes later."
    added = {"mode": "pickup", "free_text": "Pho Place", "cuisine_id": ""}
    refused = htmx(admin, f"/events/{event_id}/proposals", added | {"from": "today"})
    assert refused.status_code == 409 and said(refused) is None  # its alert says why instead
    assert act(admin, event_id, f"proposals/{second}/lock") == "Locked in."
    assert act(admin, event_id, "meal-time", {"at": "19:15"}) == "Meal time saved."
    assert act(admin, event_id, "assignments", {"role": "pickup", "member_id": ""}) == "Role cleared."
    someone = str(member(migrated, "admin@example.com").id)
    assert act(admin, event_id, "assignments", {"role": "pickup", "member_id": someone}) == "Role assigned."
    assert act(admin, event_id, "done") == "Marked done."


def test_closing_calling_off_and_starting_again_say_what_changed(admin: TestClient) -> None:
    event_id = start(admin)
    assert act(admin, event_id, "close", {"run": "1", "window": "respond"}) == "Answers closed."
    again = start_other(admin)
    assert act(admin, again, "cancel", {"run": "1"}) == "Called off."
    assert act(admin, again, "restart", {"run": "1"}) == "Call started again."
    run = re.search(r'name="run" value="(\d+)"', admin.get(f"/events/{again}").text)
    assert run is not None
    assert act(admin, again, "cancel", {"run": run.group(1)}) == "Called off."
    assert act(admin, again, "admin/rerun") == "Call started over."


def test_posting_now_and_offers_say_what_changed(admin: TestClient, provider: FakeProvider) -> None:
    later = start_other(admin, "2026-10-23")
    assert act(admin, later, "post-now", {"run": "1"}) == "Call posted."
    offer_id = make(admin)
    invite(admin, "kim@example.com", "Kim")
    sign_in_as(admin, provider, "kim@example.com")
    assert act(admin, offer_id, "join", {"guests": "1", "run": "1"}) == "You're in, with 1 guest."
    assert act(admin, offer_id, "join", {"guests": "0", "run": "1"}) == "You're in."
    assert act(admin, offer_id, "leave", {"run": "1"}) == "You're out."


def start_other(client: TestClient, day: str = "2026-10-22") -> int:
    response = submit(client, "/events", {"slot": "lunch", "day": day})
    assert response.status_code == 303, page(response)
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_only_a_swap_after_success_carries_words(admin: TestClient) -> None:
    """A refusal has its own alert, a refresh and a page say nothing, and a post without HTMX
    loads a new page, which a screen reader reads as a page."""
    event_id = start(admin)
    refused = htmx(admin, f"/events/{event_id}/respond", {"status": "sideways", "from": "today"})
    assert refused.status_code == 409 and 'role="alert"' in refused.text and said(refused) is None
    stale = htmx(admin, f"/events/{event_id}/cancel", {"run": "7", "from": "today"})
    assert stale.status_code == 409 and said(stale) is None
    malformed = htmx(admin, f"/events/{event_id}/cancel", {"from": "today"})
    assert malformed.status_code == 400 and said(malformed) is None
    act(admin, event_id, "respond", {"status": "in"})
    assert said(admin.get(f"/events/{event_id}/card?from=today", headers={"HX-Request": "true"})) is None
    assert "data-announce" not in admin.get("/").text and "data-announce" not in admin.get(f"/events/{event_id}").text
    plain = submit(admin, f"/events/{event_id}/respond", {"status": "in", "from": "today"})
    assert plain.status_code == 303


def test_words_are_escaped_in_the_attribute(admin: TestClient) -> None:
    event_id = start(admin)
    response = htmx(admin, f"/events/{event_id}/respond", {"status": "in", "from": "today"})
    assert 'data-announce="You&#39;re in."' in response.text


def test_the_words_for_each_shape_of_form() -> None:
    """The fallbacks never reach anyone (the action refuses first), but they never say something
    false either."""
    assert events._offered("you", "order", True) == "You can order."
    assert events._offered("guest", "pickup", False) == "The guest no longer offers to pick up."
    assert events._offered("you", "juggle", True) == events.SAVED
    assert events._away_said("2026-11-12") == "You're away until Thu, Nov 12."
    assert events._away_said("soon") == events.SAVED
    assert events._joined("0") == events._joined("") == events._joined("x") == "You're in."
    assert events._joined("1") == "You're in, with 1 guest."
    assert events._joined(" 3 ") == "You're in, with 3 guests."
    assert events._moved("vote", "minus15") == "Voting closes 15 minutes earlier."
    assert events._moved("respond", "close") == "Answers closed."
    assert events._moved("vote", "close") == "Voting closed."
    assert events._moved("respond", "sideways") == events._moved("later", "plus15") == events.SAVED
    assert events._moved("later", "close") == events.SAVED


def test_app_js_speaks_once_in_the_status_line_and_spares_a_card_in_use(client: TestClient) -> None:
    script = re.sub(r"\s+", " ", client.get("/static/app.js").text)
    # The words go in the status line that's on the page already, after a save only, once.
    assert 'document.getElementById("saved-status")' in script
    assert 'card.removeAttribute("data-announce"); // once' in script
    assert "if (isSave(event) && text) { announce(text); }" in script
    assert 'line.textContent = ""; statusTimer = window.setTimeout(' in script  # same words twice are heard twice
    assert ".focus(" not in script.split("function announce")[1].split("});")[0]  # focus stays put
    # The refresh skips a card in use, and refreshes one it skipped once focus leaves it.
    assert "if (inUse(cards[i], true)) { due[cards[i].id] = true; } else { refresh(cards[i]); }" in script
    assert "focused.matches(TYPING) || !focusByPointer" in script
    assert "var pressed = pressing instanceof Node && card.contains(pressing);" in script
    assert "var recent = sinceUse && Date.now() - (usedAt[card.id] || 0) < POLL_MS;" in script
    assert 'document.addEventListener("click", noteUse, true);' in script
    assert 'document.addEventListener("focusout", function (event) {' in script
    assert "!inUse(card, false)" in script
