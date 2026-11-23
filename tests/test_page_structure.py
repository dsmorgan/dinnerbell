# SPDX-License-Identifier: AGPL-3.0-or-later
"""Page structure and field edges (#336, spec 11 "Page structure"): every page has one ``<h1>``,
a skip link past the top bar comes first, form fields have edges people can see, and the top bar
lets go on a short screen."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from dinnerbell.config import SLACK_ISSUER, Settings
from tests.conftest import page, sign_in
from tests.support.fake_oidc import FakeProvider
from tests.support.web import htmx, submit
from tests.test_buttons import ASSISTANT_ON, provider, voting, walk_every_page
from tests.test_events_web import admin as admin
from tests.test_guests_web import SLACK_ADMIN, TEAM, add_guest, as_guest
from tests.test_theme import CSS, block, rules, tokens

__all__ = ["admin", "provider", "voting"]  # fixtures, imported for pytest

SKIP = '<a class="skip-link" href="#content">Skip to content</a>'
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Outline(HTMLParser):
    """The headings by level, the first element a keyboard reaches, and the ``<main>`` tag."""

    def __init__(self) -> None:
        super().__init__()
        self.h1 = 0
        self.first_focusable: tuple[str, dict[str, str | None]] | None = None
        self.main: dict[str, str | None] | None = None
        self.hidden_depth = 0  # inside <template> or a hidden element
        self.open: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        hidden = "hidden" in values or tag == "template"
        if tag == "h1":
            self.h1 += 1
        if tag == "main":
            self.main = values
        if self.first_focusable is None and not self.hidden_depth and not hidden and focusable(tag, values):
            self.first_focusable = (tag, values)
        if tag in VOID:
            return
        self.open.append(hidden)
        self.hidden_depth += hidden

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID and self.open:
            self.hidden_depth -= self.open.pop()


def focusable(tag: str, attrs: dict[str, str | None]) -> bool:
    if "disabled" in attrs or attrs.get("tabindex") == "-1":
        return False
    if attrs.get("tabindex") is not None:
        return True
    if tag == "a":
        return "href" in attrs
    if tag == "input":
        return attrs.get("type") != "hidden"
    return tag in {"button", "select", "textarea", "summary"}


def outline(html: str) -> Outline:
    parser = Outline()
    parser.feed(html)
    parser.close()
    return parser


def check_structure(where: str, html: str) -> None:
    """A full page has one h1, the skip link first, and a ``<main>`` it can land on."""
    parsed = outline(html)
    assert parsed.h1 == 1, f"{where}: {parsed.h1} h1 headings"
    assert SKIP in html, f"{where}: no skip link"
    assert parsed.first_focusable is not None
    tag, attrs = parsed.first_focusable
    assert (tag, attrs.get("class"), attrs.get("href")) == ("a", "skip-link", "#content"), where
    assert parsed.main is not None and parsed.main.get("id") == "content", where
    assert parsed.main.get("tabindex") == "-1", where


# The check itself ----------------------------------------------------------------------------


def test_the_check_finds_a_page_without_an_h1_or_with_two() -> None:
    body = f'<body>{SKIP}<header><a href="/">Home</a></header><main id="content" tabindex="-1">'
    with pytest.raises(AssertionError, match="0 h1"):
        check_structure("none", body + "<h2>Dinner</h2></main></body>")
    with pytest.raises(AssertionError, match="2 h1"):
        check_structure("two", body + "<h1>Hi</h1><h1>Hi</h1></main></body>")
    check_structure("one", body + "<h1>Hi</h1></main></body>")


def test_the_check_finds_a_link_before_the_skip_link() -> None:
    html = f'<body><a href="/">Home</a>{SKIP}<main id="content" tabindex="-1"><h1>Hi</h1></main></body>'
    with pytest.raises(AssertionError, match="a link first"):
        check_structure("a link first", html)
    hidden = f'<body><div hidden><a href="/">Home</a></div>{SKIP}<main id="content" tabindex="-1"><h1>Hi</h1>'
    check_structure("hidden first", hidden + "</main></body>")  # a hidden link isn't reached


# Every page ----------------------------------------------------------------------------------


@pytest.mark.parametrize("settings_overrides", [ASSISTANT_ON])
def test_every_page_has_one_h1_and_the_skip_link_first(
    admin: TestClient, provider: FakeProvider, voting: int, migrated: Settings
) -> None:
    """Every page the button check walks, as an admin, a plain member, and signed out, and an
    error page."""
    full = [(where, html) for where, html in walk_every_page(admin, provider, voting, migrated) if "<html" in html]
    assert len(full) > 30
    for where, html in full:
        check_structure(where, html)
    missing = admin.get("/events/999999")
    assert missing.status_code == 404
    check_structure("a call that isn't there", missing.text)


@pytest.mark.parametrize("settings_overrides", [{"slack_team_id": TEAM, "oidc_issuer": SLACK_ISSUER}])
def test_a_guests_pages_have_one_h1_and_the_skip_link_first(client: TestClient, provider: FakeProvider) -> None:
    assert sign_in(client, provider, claims=SLACK_ADMIN).status_code == 303
    add_guest(client, "Guest 1", slack="U07PAT")
    guest = as_guest(client, provider)
    for path in ("/", "/me", "/stats"):
        response = guest.get(path)
        assert response.status_code == 200, path
        check_structure(f"guest: {path}", response.text)


def test_a_calls_page_titles_its_card_with_the_h1_and_today_keeps_h2(admin: TestClient) -> None:
    """The card's title is the page's h1 on the call's page only, and looks the same as on Today.
    A card swapped in on the call's page keeps it."""
    started = submit(admin, "/events", {"slot": "dinner", "day": "2026-10-22", "post_now": "1"})
    event_id = int(started.headers["location"].rsplit("/")[-1])
    title = f'id="event-{event_id}-title"'
    assert re.search(rf"<h1 {title} class=\"event-title\">", page(admin.get(f"/events/{event_id}")))
    assert re.search(rf"<h2 {title} class=\"event-title\">", page(admin.get("/")))
    swapped = htmx(admin, f"/events/{event_id}/respond", {"status": "in", "from": "event"})
    assert swapped.status_code == 200 and "<html" not in swapped.text
    assert f"<h1 {title}" in swapped.text
    on_today = htmx(admin, f"/events/{event_id}/respond", {"status": "out", "from": "today"})
    assert f"<h2 {title}" in on_today.text and "<h1" not in on_today.text
    # The h1 takes the h2's size and spacing, so the card looks as it does on Today.
    head = block(".event-head .event-title")
    assert "font-size: 1.15rem" in head and "margin: 0" in head


# Field edges ---------------------------------------------------------------------------------


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def themes() -> dict[str, dict[str, str]]:
    light = tokens(CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))])
    return {"light": light, "dark": tokens(block(':root[data-theme="dark"]'))}


def test_the_contrast_helper_matches_wcag() -> None:
    assert round(contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(contrast("#767676", "#ffffff"), 2) == 4.54


def test_form_fields_have_edges_with_3_to_1_on_cards_and_the_page() -> None:
    """WCAG 1.4.11: an input, a menu, or a text box shows where it is, in light and dark."""
    for selector in ('input:not([type="checkbox"]), select', "textarea"):
        assert "border: 1px solid var(--button-line);" in block(selector), selector
    for theme, colors in themes().items():
        for background in ("--surface", "--bg"):
            ratio = contrast(colors["--button-line"], colors[background])
            assert ratio >= 3, f"{theme}: --button-line on {background} is {ratio:.2f}:1"


# The top bar on a short screen ---------------------------------------------------------------


def test_the_top_bar_lets_go_on_a_short_screen() -> None:
    """A landscape phone, or one with the keyboard up, gets its screen back: under 30rem tall, the
    top bar scrolls away with the page, and an anchor no longer stops below it. A taller screen,
    portrait phones included, keeps it."""
    assert "position: sticky" in block(".topbar")
    short = CSS[CSS.index("@media (max-height: 30rem) {") :]
    short = short[: short.index("\n}\n")]
    inner = dict(rules(short.split("{", 1)[1]))
    assert inner[".topbar"] == "position: static;"
    assert inner["html"] == "scroll-padding-top: 0;"


# The skip link -------------------------------------------------------------------------------


def test_the_skip_link_hides_until_focused_without_inline_style() -> None:
    hidden = block(".skip-link")
    assert "position: fixed" in hidden and "transform: translateY(" in hidden
    assert "transform: none" in block(".skip-link:focus")
    assert block("#content:focus") == "outline: none;"
