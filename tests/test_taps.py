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


def test_a_failed_save_says_why_until_it_saves(client: TestClient) -> None:
    script = client.get("/static/app.js").text
    assert "Not saved. Check your connection and try again." in script  # no network
    assert "Not saved. Something went wrong. Try again." in script  # a 5xx
    assert "Not saved. Reload the page." in script  # 401, 403: an expired session
    assert "the server took too long" in script
    for event in ("htmx:sendError", "htmx:responseError", "htmx:timeout", "htmx:afterRequest", "htmx:afterSettle"):
        assert event in script, event
    assert 'block: "nearest"' in script and "prefers-reduced-motion: reduce" in script
    # A failed preference row goes back to its saved level, and the banner stays until each card
    # or row that failed saves.
    assert "elt.reset()" in script and "form[hx-post='/preferences/stance']" in script
    assert "unsaved[targetOf(event)] = true" in script and "delete unsaved[targetOf(event)]" in script


def test_a_repeat_tap_is_dropped_and_polls_share_the_cards_sync(client: TestClient) -> None:
    """The card's hx-sync aborts a request only in the browser, so a double tap on one control
    would reach the server twice. A poll shares that sync, so a tap replaces it, and it never
    starts during a save or skips a card just because a tapped button kept focus."""
    script = client.get("/static/app.js").text
    assert '"htmx:confirm"' in script and 'elt.classList.contains("htmx-request")' in script
    assert "source: card, target: card" in script
    assert "input:not([type=hidden]):not([type=radio]):not([type=checkbox])" in script
    assert "card.contains(document.activeElement) && document.activeElement.matches(TYPING)" in script
    css = client.get("/static/app.css").text
    assert "form.htmx-request button, form.submitting button, form.htmx-request .segments {" in css
    assert "\n.htmx-request button" not in css  # a poll in flight doesn't dim the whole card
    assert ".event.htmx-request" not in css and "[data-poll]" not in css


# Tap and pending feedback (#153) ------------------------------------------------------------------


def motion_blocks(css: str, query: str) -> list[str]:
    """Every @media block for one prefers-reduced-motion value."""
    start = f"@media (prefers-reduced-motion: {query})"
    blocks = []
    at = css.find(start)
    while at != -1:
        depth, end = 0, at
        for end in range(at, len(css)):
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            if css[end] == "}" and depth == 0:
                break
        blocks.append(css[at : end + 1])
        at = css.find(start, end)
    return blocks


def test_a_tap_shows_at_once_and_moves_only_with_motion_allowed(client: TestClient) -> None:
    """Buttons, levels, and chips show a press the moment they're tapped, with a tint that keeps
    their text's contrast in both themes. Only with motion allowed do colors ease and a pressed
    control shrink."""
    css = client.get("/static/app.css").text
    tint = "box-shadow: inset 0 0 0 100vmax "
    assert (
        ":is(.button, button):not(:disabled):active,\n.segment:active span,\n.chip-toggle:active span {\n  " + tint
        in css
    )
    assert tint + "color-mix(in srgb, currentColor 10%, transparent);" in css  # toward the text on a light fill
    assert tint + "var(--press-filled);" in css  # away from the text on a filled choice
    assert "--press-filled: rgb(0 0 0 / 0.14);" in css and "--press-filled: rgb(255 255 255 / 0.18);" in css
    assert "a.chip:active { border-color: currentColor; }" in css
    moving = motion_blocks(css, "no-preference")
    assert any("scale(0.97)" in b and "transition: background-color 120ms ease" in b for b in moving)
    assert css.count("scale(0.97)") == 1
    still = css
    for block in moving:
        still = still.replace(block, "")
    assert "transition:" not in still  # no transition at all with reduced motion
    script = client.get("/static/app.js").text
    assert 'addEventListener("touchstart", function () {}, { passive: true })' in script  # :active on iOS


def test_a_pending_save_shows_after_150_ms(client: TestClient) -> None:
    """The form whose save is in flight dims after 150 ms, so a fast save doesn't flicker, and the
    card or row it replaces is aria-busy until the request ends. A refresh does neither."""
    css = client.get("/static/app.css").text
    pending = "form.htmx-request button, form.submitting button, form.htmx-request .segments"
    assert pending + " { animation: pending 0s 150ms forwards; }" in css
    assert "form.htmx-request .segment span { animation: pending-cursor 0s 150ms forwards; }" in css
    assert "@keyframes pending { to { opacity: 0.6; cursor: progress; } }" in css
    script = client.get("/static/app.js").text
    assert '"htmx:beforeSend"' in script and 'target.setAttribute("aria-busy", "true")' in script
    assert "if (isSave(event) && target instanceof Element)" in script  # not for a refresh
    assert 'target.removeAttribute("aria-busy")' in script


def test_a_full_page_form_posts_once(client: TestClient) -> None:
    """A full-page form (feedback, settings) is marked while it posts, a second submit is ignored,
    and Back clears the mark. One that HTMX sends or the size check stops is left alone."""
    script = client.get("/static/app.js").text
    assert "if (event.defaultPrevented || !isFullPagePost(form, event.submitter))" in script
    assert "form.classList.contains(SUBMITTING)" in script and "event.preventDefault();\n      return;" in script
    assert 'window.addEventListener("pageshow"' in script
    assert 'method.toLowerCase() === "post"' in script  # a GET search is safe to repeat
    # The size check runs first, in the same listener, so a file it stops never marks the form.
    assert script.index("fileFits(inputs[i])") < script.index("form.classList.add(SUBMITTING)")


def test_a_changed_choice_settles_and_saved_fades(admin: TestClient) -> None:  # noqa: F811
    """A swapped button starts from its old aria-current and aria-pressed, so a choice that changed
    eases into place and one that didn't stays still. "Saved" on a row fades after about 2 s, and
    with reduced motion hides without fading."""
    html = admin.get("/").text
    config = re.search(r"<meta name=\"htmx-config\" content='([^']*)'>", html)
    assert config is not None
    assert json.loads(config.group(1))["attributesToSettle"] == [
        "class",
        "style",
        "width",
        "height",
        "aria-current",
        "aria-pressed",
    ]
    row = re.search(r'<li class="pref" id="pref-(tag-\d+)">', admin.get("/preferences").text)
    assert row is not None
    saved = htmx(admin, "/preferences/stance", {"target": row.group(1).replace("-", ":"), "level": "1"}).text
    assert '<span class="saved small" role="status">Saved</span>' in saved
    css = admin.get("/static/app.css").text
    assert ".saved { animation: saved-fade 2.4s ease-in forwards; }" in css
    assert "@keyframes saved-fade { 0%, 75% { opacity: 1; } 100% { opacity: 0; visibility: hidden; } }" in css
    assert ".saved { animation: saved-hide 0s 2s forwards; }" in "".join(motion_blocks(css, "reduce"))
