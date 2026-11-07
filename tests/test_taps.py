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
    assert "elt.reset()" in script and "form[hx-post$='/preferences/stance']" in script
    assert "unsaved[targetOf(event)] = true" in script and "delete unsaved[targetOf(event)]" in script


def test_a_409_clears_the_failed_save_banner(admin: TestClient) -> None:  # noqa: F811
    """A 409 swaps in the card as the server has it, with the reason at the top, so an earlier
    failed tap on that card no longer shows the page's banner (#193). Before, only a 2xx cleared
    it, and a card with no refresh left kept it until a reload."""
    html = admin.get("/").text
    config = re.search(r"<meta name=\"htmx-config\" content='([^']*)'>", html)
    assert config is not None
    assert {"code": "409", "swap": True, "error": False} in json.loads(config.group(1))["responseHandling"]
    script = admin.get("/static/app.js").text
    assert "if (!xhr || ((xhr.status < 200 || xhr.status >= 300) && xhr.status !== 409)) {" in script
    cleared = script[script.index("xhr.status !== 409") :]
    assert cleared.index("delete unsaved[targetOf(event)]") < cleared.index("});")  # in that listener


def test_a_failing_refresh_backs_off_and_a_gone_card_stops(client: TestClient) -> None:
    """A card refresh that fails doesn't repeat every 45 seconds forever (#193). A 403 or 404
    stops that card's refresh. No network, a timeout, or another error doubles the wait for every
    card, once a round, up to 10 minutes. Any request that works, a refresh or a tap's save, and
    the browser coming back online, go back to 45 seconds. A 401 is left to its HX-Redirect, and
    a poll a tap aborted counts as neither."""
    script = re.sub(r"\s+", " ", client.get("/static/app.js").text)  # whitespace doesn't matter
    assert "window.setInterval" not in script  # each round sets the next, so the wait can change
    assert "var POLL_MS = 45000;" in script and "var POLL_MAX_MS = 600000;" in script
    assert "pollDelay = Math.min(pollDelay * 2, POLL_MAX_MS);" in script
    assert "pollStarted + pollDelay - Date.now()" in script  # timed from the round's start
    # Only a refresh counts: a GET whose target is a card with data-poll, never a save.
    assert 'return !isSave(event) && target instanceof Element && target.hasAttribute("data-poll");' in script
    # Once a round, and a request that worked in the round wins over a failure.
    assert "if (slowed || answered) { return; }" in script
    assert "slowed = false; answered = false;" in script
    # No answer: the error events count; an abort (a tap replaced the poll) doesn't.
    assert 'document.addEventListener("htmx:sendError", pollFailed);' in script
    assert 'document.addEventListener("htmx:timeout", pollFailed);' in script
    assert re.search(r'addEventListener\("htmx:sendAbort"', script) is None
    # Any 2xx resets the wait before the refresh-only checks, so a tap's save counts too.
    listener = script[script.index('addEventListener("htmx:afterRequest", function (event) { var status') :]
    listener = listener[: listener.index("});")]
    reset = "if (status >= 200 && status < 300) { answered = true; backToNormal(); return; }"
    assert reset in listener and listener.index(reset) < listener.index("if (!isPoll(event)) { return; }")
    assert 'if (status === 403 || status === 404) { event.detail.target.removeAttribute("data-poll"); }' in listener
    assert "else if (status >= 400 && status !== 401) { slowDown(); }" in listener
    assert 'window.addEventListener("online", backToNormal);' in script


def test_a_repeat_tap_is_dropped_and_polls_share_the_cards_sync(client: TestClient) -> None:
    """The card's hx-sync aborts a request only in the browser, so a double tap on one control
    would reach the server twice. A poll shares that sync, so a tap replaces it, and it never
    starts during a save or skips a card just because a tapped button kept focus. It does skip a
    card with a field changed and not saved in a form that's showing, such as a ticked box in an
    offer's Join form, which Safari doesn't focus (#193)."""
    script = client.get("/static/app.js").text
    assert '"htmx:confirm"' in script and 'elt.classList.contains("htmx-request")' in script
    assert "source: card, target: card" in script
    assert "input:not([type=hidden]):not([type=radio]):not([type=checkbox])" in script
    assert "card.contains(document.activeElement) && document.activeElement.matches(TYPING)" in script
    assert "|| unsavedIn(card)) {" in script
    assert 'querySelectorAll("textarea, select, input:not([type=hidden])")' in script
    assert '!fields[i].closest("details:not([open])")' in script  # a closed form's changes don't hold it
    assert "field.checked !== field.defaultChecked" in script
    assert "field.value !== field.defaultValue" in script
    assert "field.selectedIndex !== defaultIndex(field)" in script  # no option marked: the first one
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
    after = script[script.index('document.addEventListener("htmx:afterRequest", function (event) {\n    var target') :]
    assert after.index('target.removeAttribute("aria-busy")') < after.index("});")  # in that listener


def test_a_full_page_form_posts_once(client: TestClient) -> None:
    """A full-page form (feedback, settings) is marked while it posts, a second submit is ignored,
    and Back or 10 seconds clears the mark. One that HTMX sends, the size check stops, or a
    modifier key may open elsewhere is left alone."""
    script = client.get("/static/app.js").text
    assert "if (event.defaultPrevented || !isFullPagePost(form, event.submitter))" in script
    assert "form.classList.contains(SUBMITTING)" in script and "event.preventDefault();\n      return;" in script
    assert 'window.addEventListener("pageshow"' in script
    assert 'method.toLowerCase() === "post"' in script  # a GET search is safe to repeat
    # The size check runs first, in the same listener, so a file it stops never marks the form.
    assert script.index("fileFits(inputs[i])") < script.index("form.classList.add(SUBMITTING)")
    # A stopped load never leaves the page: the mark lifts after 10 s, so the form works again.
    assert "var RELEASE_MS = 10000;" in script and "unmark(form);\n      }, RELEASE_MS)" in script
    # A Ctrl-, Cmd-, or Shift-click, or Enter, may open the result elsewhere: no mark.
    assert "elsewhere = event.ctrlKey || event.metaKey || event.shiftKey;" in script
    assert '"click",' in script and 'event.target.closest("button, input[type=submit], input[type=image]")' in script
    assert 'if (event.key === "Enter")' in script and "if (elsewhere) {\n      return;" in script


def test_a_changed_choice_settles_and_saved_fades(admin: TestClient) -> None:  # noqa: F811
    """A swapped button starts from its old aria-current and aria-pressed, so a choice that changed
    eases into place and one that didn't stays still. "Saved" on a row fades after about 2 s, and
    with reduced motion goes without fading. Either way it stays readable to a screen reader."""
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
    assert "@keyframes saved-fade { 0%, 75% { opacity: 1; } 100% { opacity: 0; } }" in css
    assert "@keyframes saved-hide { to { opacity: 0; } }" in css
    rules = css[css.index(".saved { animation") : css.index("@keyframes saved-hide")]
    assert "visibility" not in rules  # faded, a screen reader still reads it
    assert ".saved { animation: saved-hide 0s 2s forwards; }" in "".join(motion_blocks(css, "reduce"))


# Expandable menus (#215) --------------------------------------------------------------------------


def test_every_expandable_menu_has_the_text_color_a_marker_and_its_tap_height(client: TestClient) -> None:
    css = re.sub(r"/\*.*?\*/", "", client.get("/static/app.css").text, flags=re.DOTALL)
    summary = "details > summary { list-style: none; cursor: pointer; display: flex; align-items: center; "
    assert summary + "min-height: 44px; color: var(--text); }" in css
    # A summary with no heading has its own marker, so one with a heading never shows two.
    assert 'details > summary h2::after { content: " +"; color: var(--muted); }' in css
    assert 'details[open] > summary h2::after { content: " \u2212"; }' in css
    assert 'details > summary:not(:has(h2))::after { content: "+"; margin-left: 0.3em; color: var(--muted); }' in css
    assert 'details[open] > summary:not(:has(h2))::after { content: "\u2212"; }' in css
    # Nothing mutes a summary's own text again. Call it off stays red, marker too.
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    summaries = [(selector, body) for selector, body in rules if re.search(r"(?<![.\w-])summary", selector)]
    assert [selector for selector, body in summaries if "::after" not in selector and "--muted" in body] == []
    assert ".calloff > summary, .calloff > summary::after { color: var(--danger); }" in css
