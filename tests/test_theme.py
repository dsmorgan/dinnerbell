# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Hearth theme (#230): one look for a choice, one radius scale, and dark colors a page can
force (#231) as well as take from the device."""

from __future__ import annotations

import re
from importlib.resources import files

CSS = files("dinnerbell").joinpath("web", "static", "app.css").read_text()
BASE = files("dinnerbell").joinpath("web", "templates", "base.html").read_text()
FUN_CSS = files("dinnerbell").joinpath("web", "static", "fun.css").read_text()
FILL = "background: var(--accent); border-color: var(--accent); color: var(--accent-text);"


def rules(css: str) -> list[tuple[str, str]]:
    """Every innermost rule, as (selector, body), with comments dropped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(selector.strip(), body.strip()) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def tokens(body: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


def block(start: str) -> str:
    """The body of the first rule whose selector is exactly `start`."""
    return next(body for selector, body in rules(CSS) if selector == start)


def backgrounds() -> dict[str, str]:
    """The page background, ``--bg``, of the light tokens on ``:root`` and the forced dark ones."""
    light = tokens(CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))])
    return {"light": light["--bg"], "dark": tokens(block(':root[data-theme="dark"]'))["--bg"]}


def test_every_chosen_control_but_voted_is_the_accent_fill() -> None:
    """Voted is the one exception: a done vote is the soft tint, so Vote stays the loudest button."""
    chosen = [
        '.segmented .answer-option[aria-current="true"]',
        ".chip-toggle input:checked + span",
        '.button.choice[aria-pressed="true"]',
        ".segment input:checked + span",
    ]
    for selector in chosen:
        assert FILL in block(selector), selector
    quiet = block('.button.quiet.choice[aria-pressed="true"]')
    assert "background: var(--accent);" in quiet and "color: var(--accent-text);" in quiet
    voted = block('.button.primary.choice[aria-pressed="true"]')
    assert "background: var(--accent-soft);" in voted and "color: var(--accent-soft-text);" in voted
    # Nothing chosen is filled with the text color any more.
    assert [s for s, body in rules(CSS) if "background: var(--text)" in body] == []


def test_the_current_page_is_a_tint_not_a_fill() -> None:
    for selector in ('.nav a[aria-current="page"]', '.tabs a[aria-current="page"]'):
        body = block(selector)
        assert "background: var(--accent-soft);" in body and "color: var(--accent-soft-text);" in body, selector


def test_preference_levels_keep_their_meaning_colors() -> None:
    assert "background: var(--danger); border-color: var(--danger); color: var(--surface);" in block(
        ".level-veto input:checked + span"
    )
    assert "background: var(--danger-soft);" in block(".level-neg input:checked + span")
    assert "background: var(--line);" in block(".level-zero input:checked + span")
    assert "background: var(--ok-soft);" in block(".level-pos input:checked + span")


def test_a_chosen_segment_comes_to_the_front_and_shows_all_four_edges() -> None:
    # Each segment overlaps the one before by a pixel; the chosen one must sit above its neighbor.
    assert "margin-left: -1px;" in block(".segment span")
    assert "position: relative; z-index: 1;" in block(".segment input:checked + span")
    assert "position: relative; z-index: 1;" in block('.segmented .answer-option[aria-current="true"]')


def test_corners_come_from_the_four_radii() -> None:
    scale = next(tokens(body) for selector, body in rules(CSS) if selector == ":root" and "--radius-card" in body)
    assert {k: scale[k] for k in ("--radius-card", "--radius-control", "--radius-input", "--radius-pill")} == {
        "--radius-card": "14px",
        "--radius-control": "12px",
        "--radius-input": "10px",
        "--radius-pill": "999px",
    }
    corners = re.findall(r"border-radius:\s*([^;]+);", re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL))
    for value in corners:
        parts = value.split()
        assert all(p == "0" or re.fullmatch(r"var\(--radius-(card|control|input|pill)\)", p) for p in parts), value


def test_dark_colors_follow_the_device_or_a_forced_theme() -> None:
    light = tokens(CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))])
    media = CSS[CSS.index("@media (prefers-color-scheme: dark) {") :]
    assert media.startswith('@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {')
    device_dark = tokens(media[: media.index("}")])
    forced_dark = tokens(block(':root[data-theme="dark"]'))
    assert device_dark == forced_dark  # the same dark colors either way
    assert set(device_dark) <= set(light)  # every dark color has a light one
    assert "color-scheme: light;" in CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))]
    assert "color-scheme: dark;" in media[: media.index("}")] and "color-scheme: dark;" in block(
        ':root[data-theme="dark"]'
    )
    assert CSS.count("@media (prefers-color-scheme") == 1


def test_the_browser_bar_matches_the_page_background() -> None:
    bg = backgrounds()
    assert f'<meta name="theme-color" content="{bg["light"]}" media="(prefers-color-scheme: light)">' in BASE
    assert f'<meta name="theme-color" content="{bg["dark"]}" media="(prefers-color-scheme: dark)">' in BASE
    # A forced Light or Dark (#231) gets one theme-color, with no media query.
    assert f'<meta name="theme-color" content="{bg["light"]}">' in BASE
    assert f'<meta name="theme-color" content="{bg["dark"]}">' in BASE


def test_the_den_follows_a_forced_theme_too() -> None:
    """fun.css's dark den colors take the same two routes as app.css's: the device's dark setting
    unless the page is forced light, and a forced dark whatever the device says (#231)."""
    dark = [(s, body) for s, body in rules(FUN_CSS) if "--den-floor:" in body and s != "body[data-fun]"]
    assert sorted(s for s, _ in dark) == [
        ':root:not([data-theme="light"]) body[data-fun]',
        ':root[data-theme="dark"] body[data-fun]',
    ]
    assert dark[0][1] == dark[1][1]  # the same dark colors either way
    media = FUN_CSS[FUN_CSS.index("@media (prefers-color-scheme: dark) {") :]
    assert media.startswith('@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) body[data-fun] {')
    assert FUN_CSS.count("@media (prefers-color-scheme") == 1


def test_a_lifted_part_stays_inside_its_joined_control() -> None:
    """A chosen level is z-index 1, like the sticky top bar. Without isolation it painted over the
    bar when Preferences scrolled, and took taps meant for the menu (#233)."""
    assert ".segments, .segmented, .way-pair { isolation: isolate; }" in CSS
    lifted = [s for s, body in rules(CSS) if "z-index" in body and re.search(r"\.segment\b|\.segmented|\.way-pair", s)]
    assert lifted  # the rules this guards
    topbar = block(".topbar")
    assert "position: sticky;" in topbar and "z-index: 1;" in topbar


def test_dimmed_text_is_muted_not_faded() -> None:
    """Fading text with opacity drops muted text under 4.5:1; dimmed rows take --muted instead.
    Opacity stays for disabled inputs, the locked badge art, hidden inputs, and animations."""
    for selector in (".member.inactive .member-main", ".item.inactive > :not(.item-actions)", ".roster .status-out"):
        assert block(selector) == "color: var(--muted);", selector
    allowed = {"input:disabled", ".badge-svg-locked"}
    faded = [
        s
        for s, body in rules(CSS)
        if re.search(r"opacity:\s*0?\.\d", body) and s not in allowed and not re.fullmatch(r"(to|from|[\d%, ]+)", s)
    ]
    assert faded == []
