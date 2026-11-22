# SPDX-License-Identifier: AGPL-3.0-or-later
"""Button roles (#317, spec 11 "Buttons"): primary, secondary, danger, and admin each mean one
thing. The admin style marks an admin's actions, never shows to a plain member, and covers every
action on the pages only admins see. Each card has at most one primary."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc.jwk import RSAKey

from dinnerbell.config import SLACK_ISSUER, Settings
from tests.conftest import invite, page, sign_in
from tests.support.clock import FakeClock
from tests.support.fake_oidc import FakeProvider
from tests.support.web import htmx, submit
from tests.test_auth_flow import member
from tests.test_events_web import admin as admin
from tests.test_events_web import sign_in_as, start, to_voting
from tests.test_guests_web import SLACK_ADMIN, TEAM, add_guest, as_guest
from tests.test_history import log
from tests.test_offers_web import offer_form
from tests.test_web_app import switch

KEY = '<svg width="16" height="16" viewBox="0 0 24 24"'
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Buttons(HTMLParser):
    """Each button on a page, and each card. A button records its classes, attributes, label, and
    the card and expandable section (``<details>``) it sits in, by the order they opened."""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[tuple[str, list[str], int]] = []
        self.buttons: list[Button] = []
        self.cards: set[int] = set()
        self.count = 0
        self.label: Button | None = None
        self.label_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if "button" in classes and tag in {"a", "button"}:
            self.label = Button(tag, classes, values, self.innermost("card"), self.innermost(None, tag="details"))
            self.buttons.append(self.label)
            self.label_depth = len(self.open)
        if tag in VOID:
            return
        self.count += 1
        self.open.append((tag, classes, self.count))
        if "card" in classes:
            self.cards.add(self.count)

    def handle_data(self, data: str) -> None:
        if self.label is not None:
            self.label.text += data

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                del self.open[i:]
                break
        if self.label is not None and len(self.open) <= self.label_depth:
            self.label.text = " ".join(self.label.text.split())
            self.label = None

    def innermost(self, cls: str | None, tag: str | None = None) -> int | None:
        for name, classes, key in reversed(self.open):
            if (cls is None or cls in classes) and (tag is None or name == tag):
                return key
        return None


@dataclass
class Button:
    tag: str
    classes: list[str]
    attrs: dict[str, str | None]
    card: int | None
    section: int | None
    text: str = ""

    @property
    def primary(self) -> bool:
        """A primary action: toggles (``.choice``) aren't actions."""
        return "primary" in self.classes and "choice" not in self.classes


def parse(html: str) -> Buttons:
    parser = Buttons()
    parser.feed(html)
    parser.close()
    return parser


def primaries_per_card(html: str) -> Counter[tuple[int, int | None]]:
    """How many primaries each card has, counting each expandable section (a ``<details>``, such
    as Invite a guest on a call) as a card of its own."""
    return Counter((b.card, b.section) for b in parse(html).buttons if b.card is not None and b.primary)


def assert_one_primary_per_card(html: str) -> None:
    crowded = {key: n for key, n in primaries_per_card(html).items() if n > 1}
    assert not crowded, f"cards with more than one primary: {crowded}"


OUTSIDE_CARDS: dict[tuple[str, str], str] = {}
"""Primaries allowed outside any card, by page and label, each with the reason."""


def check_page(where: str, html: str, *, min_cards: int = 1) -> list[Button]:
    """The button rules on one page or fragment: one primary per card or section, no primary
    outside a card unless OUTSIDE_CARDS says why, and every toggle says whether it's pressed.
    Returns the page's primaries."""
    parsed = parse(html)
    assert len(parsed.cards) >= min_cards, f"{where}: {len(parsed.cards)} cards, expected {min_cards} or more"
    assert_one_primary_per_card(html)
    for b in parsed.buttons:
        if "choice" in b.classes:
            assert b.attrs.get("aria-pressed") in {"true", "false"}, f"{where}: a toggle without aria-pressed: {b}"
        if b.primary and b.card is None:
            assert (where, b.text) in OUTSIDE_CARDS, f"{where}: a primary outside any card: {b.text!r}"
    return [b for b in parsed.buttons if b.primary]


def buttons_in(html: str, start: str, end: str) -> list[str]:
    """The opening tag and label of each button between ``start`` and the next ``end``."""
    section = html[html.index(start) :]
    section = section[: section.index(end)]
    return re.findall(r"<button\b[^>]*>.*?</button>", section, re.S)


@pytest.fixture
def provider(signing_key: RSAKey, clock: FakeClock, settings: Settings) -> FakeProvider:
    """The configured issuer: the fake one, or Sign in with Slack for the guest's page."""
    return FakeProvider(key=signing_key, clock=clock, issuer=settings.oidc_issuer)


# The check itself ----------------------------------------------------------------------------


def test_the_check_finds_a_card_with_two_primaries() -> None:
    two = '<section class="card"><a class="button primary">Add</a><button class="button primary">Save</button>'
    two += "</section>"
    with pytest.raises(AssertionError, match="more than one primary"):
        check_page("two", two)


def test_the_check_counts_each_section_and_skips_toggles() -> None:
    html = (
        '<section class="card"><button class="button primary">Count me in</button>'
        '<button class="button small primary choice" aria-pressed="false">Vote</button>'
        '<details class="details-form"><summary>Invite</summary><form><input name="x">'
        '<button class="button primary"><svg><path d="M0 0"/></svg>Invite</button></form></details></section>'
        '<form class="card"><button class="button primary">Save</button></form>'
    )
    primaries = check_page("sections", html, min_cards=2)
    assert [b.text for b in primaries] == ["Count me in", "Invite", "Save"]
    assert sorted(primaries_per_card(html).values()) == [1, 1, 1]


def test_the_check_finds_a_primary_outside_a_card_and_a_toggle_without_its_state() -> None:
    with pytest.raises(AssertionError, match="outside any card"):
        check_page("outside", '<div class="card"></div><a class="button primary">Add restaurant</a>')
    with pytest.raises(AssertionError, match="without aria-pressed"):
        check_page("toggle", '<div class="card"><button class="button small choice">I\'ll coordinate</button></div>')
    with pytest.raises(AssertionError, match="0 cards"):
        check_page("empty", "<p>Nothing here</p>")


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
    """Its actions are admin buttons with the key, except Re-run: it clears the proposals, votes,
    and any decision, so danger wins."""
    for html in (page(admin.get("/")), page(admin.get(f"/events/{voting}"))):
        start = '<details class="details-form admin-controls">'
        assert re.search(re.escape(start) + r'\s*<summary class="small">' + re.escape(KEY), html)  # the key leads
        buttons = buttons_in(html, start, "</details>")
        keyed = [b for b in buttons if 'class="button small admin"' in b]
        assert len(keyed) >= 4  # three deadline moves and Assign
        for button in keyed:
            assert KEY in button and button.index(KEY) < button.index("</svg>") < button.index("</button>")
        (rerun,) = [b for b in buttons if b not in keyed]
        assert 'class="button small danger"' in rerun and rerun.endswith(">Re-run</button>") and KEY not in rerun
        assert re.search(r'<svg [^>]*aria-hidden="true"', keyed[0])
        assert any(b.endswith("</svg>Assign</button>") for b in keyed)
        # Outside it, the coordinator's own actions are plain secondary buttons.
        assert re.search(r'class="button small"[^>]*>Lock this<', html)
        assert html.count(KEY) == len(keyed) + 1


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


ADMIN_MAIN = '<main class="page admin-page">'


def assert_admin_only(where: str, html: str) -> int:
    """Every action on a page only admins see is admin or danger, each with the key, and none is
    primary. Returns how many actions it checked."""
    actions = [b for b in parse(html).buttons if "choice" not in b.classes]
    for b in actions:
        assert ("admin" in b.classes) != ("danger" in b.classes), f"{where}: {b.text!r} is {b.classes}"
        assert "primary" not in b.classes, f"{where}: {b.text!r} is primary"
    tags = re.findall(r'<(?:a|button)\b[^>]*class="button\b[^"]*"[^>]*>.*?</(?:a|button)>', html, re.S)
    assert len(tags) == len(actions), where
    for tag in tags:
        assert tag.split(">", 1)[1].startswith(KEY), f"{where}: no key on {tag}"
    return len(actions)


TEMPLATES = Path(__file__).parents[1] / "src" / "dinnerbell" / "web" / "templates"
ADMIN_ONLY = ("admin_*.html", "event_discard.html")


def test_every_button_in_the_admin_only_templates_is_admin_or_danger_with_the_key() -> None:
    """The templates themselves, so a button that only shows in a state the render test doesn't
    reach, such as Link anyway or Back to automatic, can't slip back to primary or secondary.
    Every ``<button`` and every link styled as one must match the strict form below, so none can
    escape the check. A ``{{ }}`` in the class must be ``'admin' if ... else 'danger'`` or the
    reverse; ``{% %}``
    isn't allowed, since a condition could leave the role out. ``_code_value.html``, admin only on
    Admin > Members, is checked by the render test below."""
    files = [*sorted({f for pattern in ADMIN_ONLY for f in TEMPLATES.glob(pattern)}), TEMPLATES / "_member_row.html"]
    assert len(files) >= 9
    strict = re.compile(r'<(?:a|button)\b[^>]*?(?<![\w-])class="(button\b[^"]*)"[^>]*>(\{\{ key_icon\(\) \}\})?', re.I)
    choice = re.compile(r"\s*'(admin|danger)'\s+if\s+[^']+?\s+else\s+'(admin|danger)'\s*")
    found = 0
    for f in files:
        source = f.read_text()
        if f.name != "_member_row.html":
            assert "{% set admin_page = true %}" in source, f.name
        everything = re.findall(r"<button\b|<a\b[^>]*(?<![\w-])class=[\"'][^\"']*\bbutton\b", source, re.I)
        tags = strict.findall(source)
        assert len(tags) == len(everything), f"{f.name}: {len(everything) - len(tags)} buttons in an unchecked form"
        for roles, key in tags:
            assert "{%" not in roles and "primary" not in roles.split(), f"{f.name}: {roles}"
            chosen = re.findall(r"\{\{(.*?)\}\}", roles)
            if chosen:
                assert all(choice.fullmatch(c) for c in chosen), f"{f.name}: {roles}"
            else:
                assert re.search(r"\b(admin|danger)\b", roles), f"{f.name}: {roles}"
            assert key, f"{f.name}: no key on {roles}"
        found += len(tags)
    assert found >= 32


@pytest.mark.parametrize("settings_overrides", [{"anthropic_api_key": "sk-ant-test-not-a-real-key"}])
def test_the_pages_only_admins_see_use_the_admin_style(admin: TestClient, voting: int, migrated: Settings) -> None:
    """Under Admin, cards and rows sit in the admin outline (the page's ``admin-page`` class), and
    every action is admin or danger with the key: Save, Invite, Edit, Copy, Remove, Deactivate,
    and the rest. An admin's view of a member's Preferences keeps its own look."""
    add_guest(admin, "Pat")
    sam = member(migrated, "sam@example.com").id
    checked = 0
    for path in (
        "/admin/members",
        f"/admin/members/{sam}",
        "/admin/channel",
        "/admin/schedule",
        "/admin/badges",
        "/admin/assistant",
        "/admin/web-app",
    ):
        html = page(admin.get(path))
        assert ADMIN_MAIN in html, path
        checked += assert_admin_only(path, html)
    assert checked >= 25

    members = page(admin.get("/admin/members"))
    for label in ("Invite", "Add guest", "Edit", "Sign-in code", "Sign out everywhere", "Remove", "Deactivate"):
        assert re.search(r'class="button[^"]*"[^>]*>' + re.escape(KEY) + r".*?</svg>" + label + "<", members), label
    assert re.search(r'class="button small danger"[^>]*>' + re.escape(KEY) + r".*?</svg>Remove<", members)

    # A sign-in code for Sam: Copy is an admin button too.
    issued = page(submit(admin, f"/admin/members/{sam}/sign-in-code"))
    assert ADMIN_MAIN in issued
    assert re.search(r'class="button small admin" data-copy="issued-code-value" hidden>' + re.escape(KEY), issued)
    assert_admin_only("a code issued", issued)

    # Deactivate swaps in Sam's row, then Reactivate: the fragment keeps the roles and keys.
    row = htmx(admin, f"/admin/members/{sam}/deactivate", {})
    assert row.status_code == 200 and "<html" not in row.text
    assert assert_admin_only("Sam's row", page(row)) >= 3
    assert re.search(r'class="button small admin">' + re.escape(KEY) + r".*?</svg>Reactivate<", page(row), re.S)

    # Pages members also see keep the plain roles, apart from their Admin parts.
    for path in (f"/admin/members/{sam}/preferences", "/preferences", "/", "/schedule"):
        assert ADMIN_MAIN not in page(admin.get(path)), path
    assert '<main class="page">' in page(admin.get("/preferences"))


# One primary per card ------------------------------------------------------------------------

SETTINGS_ON = {"enabled": "on", "cap_dollars": "5", "conversation_hours": "24"}
EXPECTED = {
    "Post it now",
    "Count me in",
    "Save details",
    "I'm in (late)",
    "Start dinner again",
    "Invite",
    "Add tag",
    "Add to catalog",
    "Log it",
    "Save needs",
    "Save away days",
    "Send",
}
"""Primaries the pages below must show, so the check can't pass on pages that lost them."""


def made(response: httpx.Response) -> int:
    assert response.status_code == 303, page(response)
    return int(response.headers["location"].rsplit("/", 1)[1])


@pytest.mark.parametrize("settings_overrides", [{"anthropic_api_key": "sk-ant-test-not-a-real-key"}])
def test_each_card_has_at_most_one_primary(
    admin: TestClient, provider: FakeProvider, voting: int, migrated: Settings
) -> None:
    """Every page an admin and a plain member see, with calls in each state: scheduled (Post it
    now), collecting (Save details), voting without Sam (I'm in (late)), called off (Start dinner
    again), and an offer (Count me in), plus an HTMX answer's card."""
    submit(admin, "/catalog/tags", {"kind": "dietary", "name": "Vegetarian"})  # for Save needs
    scheduled = made(submit(admin, "/events", {"slot": "lunch", "day": "2026-10-23"}))
    collecting = made(submit(admin, "/events", {"slot": "lunch", "day": "2026-10-24", "post_now": "1"}))
    called_off = made(submit(admin, "/events", {"slot": "dinner", "day": "2026-10-26", "post_now": "1"}))
    assert submit(admin, f"/events/{called_off}/cancel", {"run": "1"}).status_code == 303
    offer = made(submit(admin, "/offers", offer_form(day="2026-10-25")))
    assert submit(admin, "/admin/assistant", SETTINGS_ON).status_code == 303
    sam = member(migrated, "sam@example.com").id
    me = member(migrated, "admin@example.com").id
    assert log(admin, 1, [me, sam], day=date(2026, 10, 21)).status_code == 303
    meal = int(re.findall(r'id="meal-(\d+)"', admin.get("/history").text)[0])

    seen: set[str] = set()
    admin_pages = (
        "/",
        f"/events/{voting}",
        f"/events/{offer}",
        "/preferences",
        f"/admin/members/{sam}/preferences",
        "/admin/members",
        f"/admin/members/{sam}",
        "/admin/channel",
        "/admin/schedule",
        "/admin/badges",
        "/admin/assistant",
        "/admin/web-app",
        "/catalog",
        "/catalog/new",
        "/catalog/items/1",
        "/catalog/import",
        "/catalog/tags",
        "/history",
        f"/history/meals/{meal}",
        f"/history/meals/{meal}/feedback",
        "/schedule",
        "/assistant",
        "/app",
    )
    for path in admin_pages:
        response = admin.get(path)
        assert response.status_code == 200, path
        # Admin > Channel lists people from Slack, as rows, not cards; without Slack it has none.
        seen |= {b.text for b in check_page(path, response.text, min_cards=0 if path == "/admin/channel" else 1)}

    sign_in_as(admin, provider, "sam@example.com")
    member_pages = ("/", *(f"/events/{e}" for e in (voting, scheduled, collecting, called_off, offer)))
    for path in (*member_pages, "/preferences", "/history", "/catalog", "/schedule"):
        response = admin.get(path)
        assert response.status_code == 200, path
        seen |= {b.text for b in check_page(f"Sam: {path}", response.text)}
    answered = htmx(admin, f"/events/{collecting}/respond", {"status": "in", "from": "today"})
    assert answered.status_code == 200 and "<html" not in answered.text
    seen |= {b.text for b in check_page("Sam answers In", answered.text)}

    assert seen >= EXPECTED, f"primaries no page showed: {sorted(EXPECTED - seen)}"
    # Signed out: the sign-in page and, with the web app on, code sign-in.
    out = TestClient(admin.app, base_url=str(admin.base_url), headers={"Origin": str(admin.base_url).rstrip("/")})
    for path in ("/", "/auth/code"):
        response = out.get(path)
        assert response.status_code == 200, path
        assert check_page(f"signed out: {path}", response.text), path  # each has its one primary


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": TEAM, "oidc_issuer": SLACK_ISSUER}])
def test_a_guests_own_page_has_one_primary_per_card(client: TestClient, provider: FakeProvider) -> None:
    assert sign_in(client, provider, claims=SLACK_ADMIN).status_code == 303
    submit(client, "/catalog/tags", {"kind": "dietary", "name": "Vegetarian"})
    add_guest(client, "Pat", slack="U07PAT")
    guest = as_guest(client, provider)
    primaries = check_page("/me", guest.get("/me").text, min_cards=3)
    assert [b.text for b in primaries] == ["Save", "Save", "Save"]  # needs, fun mode, light or dark
