# SPDX-License-Identifier: AGPL-3.0-or-later
"""Taps that stick (#153): the last tap wins, focus stays on the control, and a failed save says
why. The browser side lives in app.js and HTMX; these check what the server renders for it."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import timedelta
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from dinnerbell.db.models import MemberRow
from tests.conftest import invite, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import htmx, submit
from tests.test_auth_flow import rows
from tests.test_events_web import admin, proposal_ids, sign_in_as, start, tick, to_voting  # noqa: F401  (fixture)
from tests.test_offers_web import make


class _Ids(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.buttons: dict[str, str] = {}
        self._button: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs).get("id")
        if found is not None:
            self.ids.append(found)
        if tag == "button" and found is not None:
            self._button = found
            self.buttons[found] = ""

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self.buttons[self._button] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self._button = None


def parsed(html: str) -> _Ids:
    ids = _Ids()
    ids.feed(html)
    return ids


def assert_unique_ids(html: str) -> None:
    repeated = [name for name, count in Counter(parsed(html).ids).items() if count > 1]
    assert repeated == [], repeated


def card_buttons(html: str) -> dict[str, str]:
    """Each card action button's id and label. Every one has an id, so HTMX can restore focus."""
    return {k: v.strip() for k, v in parsed(html).buttons.items() if k.startswith("event-")}


def test_htmx_gives_up_after_15_seconds(client: TestClient, provider: FakeProvider) -> None:
    html = client.get("/").text
    config = re.search(r"<meta name=\"htmx-config\" content='([^']*)'>", html)
    assert config is not None
    assert json.loads(config.group(1))["timeout"] == 15000


def test_the_last_tap_on_a_card_wins(admin: TestClient) -> None:  # noqa: F811
    """A second tap while the first is in flight replaces it (hx-sync), for every form on the card."""
    event_id = start(admin)
    card = htmx(admin, f"/events/{event_id}/respond", {"status": "maybe", "from": "today"}).text
    assert re.search(
        rf'<section class="card event [^"]*" id="event-{event_id}"[^>]* hx-sync="closest .event:replace"', card
    )
    assert 'hx-sync="closest .event:replace"' in admin.get("/").text


def test_card_buttons_keep_their_ids_across_a_swap(admin: TestClient, clock: FakeClock, migrated: Settings) -> None:  # noqa: F811
    event_id = start(admin)
    before = card_buttons(htmx(admin, f"/events/{event_id}/respond", {"status": "maybe"}).text)
    after = card_buttons(htmx(admin, f"/events/{event_id}/respond", {"status": "out"}).text)
    assert before == after
    assert before[f"event-{event_id}-respond-maybe"] == "Maybe"
    assert before[f"event-{event_id}-respond-out"] == "Out"
    assert before[f"event-{event_id}-offer-cook"] == "I can cook"
    on = card_buttons(htmx(admin, f"/events/{event_id}/offer", {"role": "cook", "offered": "1"}).text)
    assert on[f"event-{event_id}-offer-cook"] == "I can cook"  # the same id, on or off

    to_voting(admin, clock, event_id)
    first = proposal_ids(migrated)[0]
    vote = f"event-{event_id}-proposals-{first}-vote"
    assert card_buttons(admin.get(f"/events/{event_id}").text)[vote] == "Vote"
    assert card_buttons(htmx(admin, f"/events/{event_id}/proposals/{first}/vote", {}).text)[vote] == "Voted"


def test_ids_are_unique_on_every_kind_of_card(
    admin: TestClient,  # noqa: F811
    provider: FakeProvider,
    clock: FakeClock,
    migrated: Settings,
) -> None:
    """HTMX restores focus by id, so a repeated id would send it to the wrong control."""
    invite(admin, "kim@example.com", "Kim")
    event_id = start(admin)
    assert htmx(admin, f"/events/{event_id}/guests", {"name": "Jo", "run": "1", "from": "event"}).status_code == 200
    jo = next(m for m in rows(migrated, MemberRow) if m.display_name == "Jo")
    submit(admin, f"/events/{event_id}/guests/{jo.id}/answer", {"status": "in", "run": "1"})
    submit(admin, f"/events/{event_id}/coordinate")
    seen = [admin.get("/").text, admin.get(f"/events/{event_id}").text]
    collecting = card_buttons(seen[1])
    assert f"event-{event_id}-guests-{jo.id}-answer-in" in collecting
    assert f"event-{event_id}-guests-{jo.id}-answer-cook" in collecting
    assert f"event-{event_id}-admin-deadline-respond-plus15" in collecting

    to_voting(admin, clock, event_id)
    sign_in_as(admin, provider, "kim@example.com")
    seen.append(admin.get(f"/events/{event_id}").text)  # Join late and vote
    first = proposal_ids(migrated)[0]
    assert submit(admin, f"/events/{event_id}/proposals/{first}/vote", {"join_late": "1"}).status_code == 303
    seen.append(admin.get(f"/events/{event_id}").text)  # her late role offers
    admin.cookies.clear()
    sign_in(admin, provider)  # the admin again
    voting = admin.get(f"/events/{event_id}").text
    seen.append(voting)
    kim = next(m for m in rows(migrated, MemberRow) if m.display_name == "Kim")
    assert f"event-{event_id}-late-{kim.id}-part" in card_buttons(voting)

    assert submit(admin, f"/events/{event_id}/proposals/{first}/lock").status_code == 303
    seen.append(admin.get(f"/events/{event_id}").text)
    clock.advance(timedelta(hours=6))  # meal time: Mark done
    seen += [admin.get("/").text, admin.get(f"/events/{event_id}").text]
    assert f"event-{event_id}-done" in card_buttons(seen[-1])

    seen.append(admin.get("/preferences").text)
    for html in seen:
        assert_unique_ids(html)


def test_ids_are_unique_on_an_offer(admin: TestClient, clock: FakeClock) -> None:  # noqa: F811
    offer_id = make(admin)
    seen = [admin.get("/").text, admin.get(f"/events/{offer_id}").text]
    clock.advance(timedelta(hours=5, minutes=30))  # 6:30 PM in Chicago: on, and time to mark done
    tick(admin, clock)
    seen += [admin.get("/").text, admin.get(f"/events/{offer_id}").text]
    assert "It's on!" in seen[-1]
    assert f"event-{offer_id}-done" in card_buttons(seen[-1])
    for html in seen:
        assert_unique_ids(html)


def test_preference_rows_let_the_last_tap_win_and_keep_focus(admin: TestClient) -> None:  # noqa: F811
    html = admin.get("/preferences").text
    forms = re.findall(r'<form method="post" action="/preferences/stance"[^>]*>', html)
    assert forms and all('hx-sync="this:replace"' in f for f in forms)
    assert_unique_ids(html)
    row = re.search(r'<li class="pref" id="(pref-tag-\d+)">', html)
    assert row is not None
    target = row.group(1).removeprefix("pref-").replace("-", ":")
    saved = htmx(admin, "/preferences/stance", {"target": target, "level": "-2"}).text
    radios = re.findall(r'<input type="radio" id="([^"]+)" name="level" value="([^"]+)"', saved)
    assert radios == [
        (f"{row.group(1)}-veto", "veto"),
        (f"{row.group(1)}-minus2", "-2"),
        (f"{row.group(1)}-minus1", "-1"),
        (f"{row.group(1)}-0", "0"),
        (f"{row.group(1)}-1", "1"),
        (f"{row.group(1)}-2", "2"),
    ]
    assert 'hx-sync="this:replace"' in saved


def test_a_failed_save_says_why_and_clears_after_the_next(client: TestClient) -> None:
    script = client.get("/static/app.js").text
    assert "Not saved. Check your connection and try again." in script  # no network
    assert "Not saved. Something went wrong. Try again." in script  # a 5xx
    assert "Not saved. Reload the page." in script  # 401, 403: an expired session
    assert "the server took too long" in script
    for event in ("htmx:sendError", "htmx:responseError", "htmx:timeout", "htmx:afterRequest", "htmx:afterSettle"):
        assert event in script, event
    assert 'block: "nearest"' in script and "prefers-reduced-motion: reduce" in script
