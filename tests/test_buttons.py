# SPDX-License-Identifier: AGPL-3.0-or-later
"""Button roles (#317, spec 11 "Buttons"): primary, secondary, danger, and admin each mean one
thing. The admin style marks an admin's actions on pages members also see, and never shows to a
plain member, and each card has at most one primary."""

from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import Settings
from tests.conftest import invite, page
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.test_auth_flow import member
from tests.test_events_web import admin as admin
from tests.test_events_web import sign_in_as, start, to_voting
from tests.test_web_app import switch

KEY = '<svg width="16" height="16" viewBox="0 0 24 24"'
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Buttons(HTMLParser):
    """Each button on a page: its classes, and which card and expandable section it sits in."""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[tuple[str, list[str], int]] = []
        self.buttons: list[tuple[list[str], int | None, int | None]] = []
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if "button" in classes and tag in {"a", "button"}:
            self.buttons.append((classes, self.innermost("card"), self.innermost(None, tag="details")))
        if tag not in VOID:
            self.count += 1
            self.open.append((tag, classes, self.count))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                del self.open[i:]
                return

    def innermost(self, cls: str | None, tag: str | None = None) -> int | None:
        for name, classes, key in reversed(self.open):
            if (cls is None or cls in classes) and (tag is None or name == tag):
                return key
        return None


def primaries_per_card(html: str) -> Counter[tuple[int, int | None]]:
    """How many primaries each card has, counting each expandable section (a ``<details>``, such
    as Invite a guest on a call) as a card of its own. Toggles (``.choice``) aren't actions."""
    parser = Buttons()
    parser.feed(html)
    return Counter(
        (card, section)
        for classes, card, section in parser.buttons
        if card is not None and "primary" in classes and "choice" not in classes
    )


def assert_one_primary_per_card(html: str) -> None:
    crowded = {key: n for key, n in primaries_per_card(html).items() if n > 1}
    assert not crowded, f"cards with more than one primary: {crowded}"


def buttons_in(html: str, start: str, end: str) -> list[str]:
    """The opening tag and label of each button between ``start`` and the next ``end``."""
    section = html[html.index(start) :]
    section = section[: section.index(end)]
    return re.findall(r"<button\b[^>]*>.*?</button>", section, re.S)


# The check itself ----------------------------------------------------------------------------


def test_the_check_finds_a_card_with_two_primaries() -> None:
    two = '<section class="card"><a class="button primary">Add</a><button class="button primary">Save</button>'
    two += "</section>"
    with pytest.raises(AssertionError, match="more than one primary"):
        assert_one_primary_per_card(two)


def test_the_check_counts_each_section_and_skips_toggles() -> None:
    html = (
        '<section class="card"><button class="button primary">Count me in</button>'
        '<button class="button small primary choice" aria-pressed="false">Vote</button>'
        '<details class="details-form"><summary>Invite</summary><form><input name="x">'
        '<button class="button primary">Invite</button></form></details></section>'
        '<form class="card"><button class="button primary">Save</button></form>'
    )
    assert_one_primary_per_card(html)
    assert sorted(primaries_per_card(html).values()) == [1, 1, 1]


# Admin ---------------------------------------------------------------------------------------


@pytest.fixture
def voting(admin: TestClient, provider: FakeProvider, clock: FakeClock, migrated: Settings) -> int:
    """A dinner call in voting, coordinated by the admin, with Sam, a plain member, invited and
    the web app on. The admin is signed in."""
    invite(admin, "sam@example.com", "Sam")
    switch(admin, on=True)
    event_id = start(admin)
    to_voting(admin, clock, event_id)
    return event_id


def test_a_calls_admin_section_uses_the_admin_style(admin: TestClient, voting: int) -> None:
    for html in (page(admin.get("/")), page(admin.get(f"/events/{voting}"))):
        start = '<details class="details-form admin-controls">'
        assert re.search(re.escape(start) + r'\s*<summary class="small">' + re.escape(KEY), html)  # the key leads
        buttons = buttons_in(html, start, "</details>")
        assert len(buttons) >= 3  # at least one deadline move, Assign, and nothing else unstyled
        for button in buttons:
            assert re.search(r'class="button small admin"', button), button
            assert KEY in button and button.index(KEY) < button.index("</svg>") < button.index("</button>")
        assert re.search(r'<svg [^>]*aria-hidden="true"', buttons[0])
        assert any(b.endswith("</svg>Assign</button>") for b in buttons)
        # Outside it, the coordinator's own actions are plain secondary buttons.
        assert re.search(r'class="button small"[^>]*>Lock this<', html)
        assert html.count(KEY) == len(buttons) + 1


def test_an_admin_editing_someones_preferences_saves_with_the_admin_style(
    admin: TestClient, voting: int, migrated: Settings
) -> None:
    sam = member(migrated, "sam@example.com").id
    html = page(admin.get(f"/admin/members/{sam}/preferences"))
    base = f'action="/admin/members/{sam}/preferences'
    forms = re.findall(r"<form\b[^>]*>.*?</form>", html, re.S)
    theirs = [f for f in forms if base in f.split(">", 1)[0]]
    assert len(theirs) >= 6  # needs, settings, away, notifications, fun mode, light or dark, and each row
    for form in theirs:
        for button in re.findall(r"<button\b[^>]*>.*?</button>", form, re.S):
            assert 'class="button admin' in button or 'class="button small admin"' in button, button
            assert KEY in button
    assert "button primary" not in html


def test_a_plain_member_never_sees_the_admin_style(
    admin: TestClient, provider: FakeProvider, voting: int, migrated: Settings
) -> None:
    own = page(admin.get("/preferences"))  # the admin's own preferences are their own
    assert KEY not in own and not re.search(r'class="button[^"]*\badmin\b', own)
    sign_in_as(admin, provider, "sam@example.com")
    for path in ("/", f"/events/{voting}", "/preferences", "/catalog", "/catalog/tags", "/schedule"):
        html = page(admin.get(path))
        if path == "/":
            assert f'id="event-{voting}"' in html  # Sam sees the call, without its Admin section
        assert KEY not in html, path
        assert "admin-controls" not in html and not re.search(r'class="button[^"]*\badmin\b', html), path


# One primary per card ------------------------------------------------------------------------


def test_each_card_has_at_most_one_primary(admin: TestClient, voting: int, migrated: Settings) -> None:
    sam = member(migrated, "sam@example.com").id
    paths = (
        "/",
        f"/events/{voting}",
        "/preferences",
        f"/admin/members/{sam}/preferences",
        "/admin/members",
        f"/admin/members/{sam}",
        "/catalog",
        "/catalog/tags",
        "/history",
        "/schedule",
        "/admin/schedule",
        "/admin/badges",
    )
    for path in paths:
        html = page(admin.get(path))
        assert "button" in html, path
        assert_one_primary_per_card(html)
